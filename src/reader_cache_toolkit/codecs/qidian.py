"""从已验证的本地脚本迁移的起点缓存解析器，不包含设备操作与导出职责。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import PKCS7

from reader_cache_toolkit.infrastructure.database import open_readonly_snapshot

CONTENT_SALT = "2EEE1433A152E84B3756301D8FA3E69A"


@contextmanager
def open_readonly_database(path: Path) -> Iterator[sqlite3.Connection]:
    """在临时副本上以只读方式检查和查询 SQLite，保护原库及日志文件。

    参数：path 为现有主数据库路径，配套 WAL、SHM 和 journal 仅复制不修改。
    返回：供 with 使用的只读连接上下文；退出时关闭连接并清理本次临时副本。
    """
    with open_readonly_snapshot(path) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError(f"数据库完整性检查失败：{path.name}")
        yield connection


def read_metadata(root: Path, book_id: str) -> dict:
    """读取本次备份已有的书籍信息，不推测缺失信息。

    参数：root 为备份根目录；book_id 为十进制书籍编号。
    返回：包含真实书名、作者、书籍编号和缓存用户目录编号的字典。
    """
    if not book_id.isdecimal():
        raise ValueError("book-id 必须是十进制书籍编号")
    path = root / f"qidian_book_metadata_{book_id}.json"
    metadata = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("title", "author", "qdBookId", "qdUserDir"):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f"备份元数据缺少有效字段：{key}")
    if metadata["qdBookId"] != book_id or not metadata["qdUserDir"].isdecimal():
        raise ValueError("书籍编号或备份用户目录编号不匹配")
    return metadata


def read_index(path: Path) -> list[dict]:
    """读取按分卷和展示顺序排列的章节索引。

    参数：path 为 book_index 下的 SQLite 索引文件。
    返回：含章节编号、标题、VIP 标志和排序字段的列表；重复编号报错。
    """
    with open_readonly_database(path) as connection:
        rows = connection.execute(
            "SELECT ChapterId, ChapterName, IsVip, VolumeCode, ShowOrder "
            "FROM chapter ORDER BY CAST(VolumeCode AS INTEGER), CAST(ShowOrder AS INTEGER)"
        ).fetchall()
    chapters = []
    seen = set()
    for chapter_id, title, is_vip, volume, order in rows:
        identifier = str(chapter_id)
        if not identifier.lstrip("-").isdecimal() or identifier in seen:
            raise ValueError(f"章节编号无效或重复：{identifier}")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"章节标题为空：{identifier}")
        seen.add(identifier)
        chapters.append(
            {"chapter_id": identifier, "title": title, "is_vip": is_vip, "volume": volume, "order": order}
        )
    if not chapters:
        raise ValueError("章节索引为空")
    return chapters


def read_device_candidates(path: Path) -> list[tuple[str, str]]:
    """读取备份中真实保存的当前和历史设备标识。

    参数：path 为 QDConfig 数据库路径。
    返回：去重后的 (设备标识, 来源字段) 列表，不生成或打印设备标识。
    """
    with open_readonly_database(path) as connection:
        rows = connection.execute(
            "SELECT Key, Value FROM setting WHERE Key IN "
            "('SettingIMEIList', 'imei_release', 'android_release', 'UUID') "
            "ORDER BY CASE Key WHEN 'SettingIMEIList' THEN 0 ELSE 1 END, Key"
        ).fetchall()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ImeiTable" in tables:
            rows.extend(("ImeiTable", row[0]) for row in connection.execute("SELECT Imei FROM ImeiTable"))
    candidates = []
    seen = set()
    for source, value in rows:
        if not isinstance(value, str) or not value:
            continue
        values = value.split("|") if source == "SettingIMEIList" else [value]
        for device in values:
            if device and device not in seen:
                candidates.append((device, source))
                seen.add(device)
    if not candidates:
        raise ValueError("QDConfig 未保存可用于校验的设备标识")
    return candidates


def read_cache_fields(raw: bytes) -> tuple[list[bytes], int]:
    """按应用读取顺序解析五个带长度的二进制字段。

    参数：raw 为完整缓存文件字节。
    返回：五个字段及文件预分配尾部字节数；截断或未支持格式直接报错。
    """
    position = 0
    fields = []
    for _ in range(5):
        if position + 4 > len(raw):
            raise ValueError("缓存字段长度被截断")
        size = struct.unpack_from("<I", raw, position)[0]
        position += 4
        if position + size > len(raw):
            raise ValueError("缓存字段内容被截断")
        fields.append(raw[position : position + size])
        position += size
    if fields[0]:
        raise ValueError("当前脚本尚未验证含头信息的起点缓存格式")
    if not fields[1] or len(fields[1]) % 8:
        raise ValueError("章节负载为空或块长度无效")
    if fields[2] or fields[4] or fields[3] not in (b"", b"null"):
        raise ValueError("当前缓存含尚未支持的独立作者说明、资源或内容块")
    return fields, len(raw) - position


def decrypt_layer(payload: bytes, key: str) -> bytes:
    """解开应用格式中的一层章节数据并严格验证填充。

    参数：payload 为密文字节；key 为派生出的 24 字符密钥。
    返回：去除合法 PKCS7 填充的字节；密钥不匹配或数据损坏时抛出异常。
    """
    decryptor = Cipher(TripleDES(key.encode("utf-8")), modes.CBC(bytes(8))).decryptor()
    padded = decryptor.update(payload) + decryptor.finalize()
    unpadder = PKCS7(64).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def decode_payload(payload: bytes, user_id: str, chapter_id: str, device: str) -> dict:
    """复现已核对的 APK 本地读取逻辑。

    参数：payload 为章节负载；user_id、chapter_id、device 均来自真实备份。
    返回：经过两层填充、UTF-8 和 JSON 校验的章节字典。
    """
    message = user_id + device + chapter_id + CONTENT_SALT
    key1 = base64.b64encode(
        hmac.new(device.encode("utf-8"), message.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")[:24]
    key2 = base64.b64encode(
        hmac.new(user_id.encode("utf-8"), (key1 + device).encode("utf-8"), hashlib.md5).digest()
    ).decode("ascii")
    decoded = decrypt_layer(decrypt_layer(payload, key2), key1).decode("utf-8")
    document = json.loads(decoded)
    if not isinstance(document, dict) or "Content" not in document:
        raise ValueError("章节 JSON 缺少 Content 字段")
    return document


def render_copyright_page(content: dict) -> str:
    """将缓存中结构化的版权页转换为可读文字，保留原有声明和简介。

    参数：content 为编号 -10000 的真实 Content 对象。
    返回：使用中文标签组织的版权页文本；字段类型异常时直接报错。
    """
    labels = {
        "BookName": "书名",
        "Author": "作者",
        "BookStatus": "状态",
        "CategoryName": "分类",
        "SubCategoryName": "子分类",
        "PunishTime": "发行日期",
        "PunishInfo": "发行信息",
        "Copyright": "版权声明",
        "AuthorWords": "作者寄语",
        "WelcomeSpeech": "欢迎语",
        "Description": "简介",
    }
    sections = []
    for key, label in labels.items():
        value = content.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"版权页字段类型异常：{key}")
        sections.append(f"{label}：{value}")
    if not content.get("BookName") or not content.get("Copyright"):
        raise ValueError("版权页缺少书名或版权声明")
    return "\n\n".join(sections)


def decode_cached_chapter(path: Path, entry: dict, user_id: str, candidates: list[tuple[str, str]]) -> dict:
    """用真实历史设备标识逐一校验单章，保留原始正文。

    参数：path 为缓存路径；entry 为索引；user_id 为原用户目录；candidates 为实际配置。
    返回：含正文、源文件哈希和匹配字段名的记录；所有候选失败则明确报错。
    """
    raw = path.read_bytes()
    fields, trailing = read_cache_fields(raw)
    for device, source in candidates:
        try:
            document = decode_payload(fields[1], user_id, entry["chapter_id"], device)
        except (ValueError, UnicodeError):
            continue
        content = document["Content"]
        extra = {}
        if entry["chapter_id"] == "-10000" and isinstance(content, dict):
            text = render_copyright_page(content)
            extra["copyright_metadata"] = content
        elif isinstance(content, str):
            text = content
        else:
            raise ValueError(f"章节 Content 字段类型尚未支持：{entry['chapter_id']}")
        if not text.strip() or "\ufffd" in text:
            raise ValueError(f"章节正文为空或含乱码替换字符：{entry['chapter_id']}")
        if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
            raise ValueError(f"章节含不可显示控制字符：{entry['chapter_id']}")
        for key in ("AuthorComments", "Resources", "Blocks"):
            if document.get(key) not in (None, "", [], {}):
                raise ValueError(f"章节含尚未支持的非空 {key}：{entry['chapter_id']}")
        return {
            **entry,
            "text": text.replace("\r\n", "\n").replace("\r", "\n"),
            "source_file": str(path.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "device_source": source,
            "trailing_bytes": trailing,
            **extra,
        }
    raise ValueError(f"章节 {entry['chapter_id']} 没有匹配的设备标识，或缓存损坏")
