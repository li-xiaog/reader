"""番茄缓存的严格二进制解析与正文转换，不执行文件输出。"""

from __future__ import annotations

import base64
import gzip
import json
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from reader_cache_toolkit.errors import ReaderCacheError

STREAM_MAGIC = b"\xac\xed\x00\x05"
KEY_PATTERN = re.compile(rb"key_(\d+)\W{0,16}([0-9A-Fa-f]{32})")
ARRAY_LIST_CLASS = b"java.util.ArrayList"
CHAPTER_ITEM_CLASS = b"com.dragon.reader.lib.datalevel.model.ChapterItem"
CATALOG_CLASS = b"com.dragon.read.reader.depend.data.CatalogCache"
BLOCK_TAGS = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "li", "blockquote", "pre", "tr", "blk"}
)


def stream_position(data: bytes) -> int:
    """定位缓存中的 Java 序列化流。参数：data 原始文件字节。返回：流标记之后的位置。"""
    position = data.find(STREAM_MAGIC)
    if position < 0 or position + len(STREAM_MAGIC) >= len(data):
        raise ReaderCacheError("缓存缺少完整 Java 序列化流标记")
    return position + len(STREAM_MAGIC)


def read_serialized_json(data: bytes) -> dict[str, Any]:
    """严格读取字符串形式的章节 JSON。参数：data 缓存字节。返回：章节元数据对象。"""
    position = stream_position(data)
    tag = data[position]
    length_bytes = {0x74: 2, 0x7C: 8}.get(tag)
    if length_bytes is None:
        raise ReaderCacheError(f"不支持的章节序列化标记：0x{tag:02x}")
    position += 1
    if position + length_bytes > len(data):
        raise ReaderCacheError("章节字符串长度被截断")
    size = int.from_bytes(data[position : position + length_bytes], "big")
    position += length_bytes
    if size <= 0 or position + size != len(data):
        raise ReaderCacheError("章节字符串为空、被截断或含未识别尾部内容")
    try:
        result = json.loads(data[position : position + size].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReaderCacheError("章节 JSON 或 UTF-8 数据损坏") from exc
    if not isinstance(result, dict):
        raise ReaderCacheError("章节 JSON 不是对象")
    return result


def inspect_catalog_record(data: bytes) -> tuple[str, int | None] | None:
    """识别已验证的目录对象及实际目录计数。参数：data 原始缓存。返回：对象名和计数，章节字符串返回 None。"""
    position = stream_position(data)
    if data[position] in (0x74, 0x7C):
        return None
    if data[position : position + 2] != b"\x73\x72" or position + 4 > len(data):
        raise ReaderCacheError("遇到未支持的非字符串缓存对象")
    name_length = int.from_bytes(data[position + 2 : position + 4], "big")
    name_start = position + 4
    name_end = name_start + name_length
    class_name = data[name_start:name_end]
    if name_end + 11 > len(data):
        raise ReaderCacheError("目录对象类描述被截断")
    if class_name == CATALOG_CLASS:
        if b"catalogList" not in data[name_end : name_end + 300] or not data.endswith(b"\x78"):
            raise ReaderCacheError("CatalogCache 目录对象结构损坏")
        return "CatalogCache", None
    if class_name != ARRAY_LIST_CLASS:
        raise ReaderCacheError("遇到未识别的 Java 缓存对象类型")
    # 1. 核对已实测的 ArrayList 类描述、字段及序列化版本。
    descriptor = bytes.fromhex("7881d21d99c7619d03000149000473697a657870")
    if data[name_end : name_end + len(descriptor)] != descriptor:
        raise ReaderCacheError("ArrayList 目录类描述不兼容")
    position = name_end + len(descriptor)
    if position + 10 > len(data):
        raise ReaderCacheError("ArrayList 目录计数被截断")
    # 2. 目录大小在对象字段与 writeObject 块中各保存一次，必须一致。
    count = int.from_bytes(data[position : position + 4], "big", signed=True)
    repeated_count = int.from_bytes(data[position + 6 : position + 10], "big", signed=True)
    if data[position + 4 : position + 6] != b"\x77\x04" or count < 1 or count != repeated_count:
        raise ReaderCacheError("ArrayList 目录计数无效或不一致")
    # 3. 只把 ChapterItem 列表视为章节目录，拒绝其他 ArrayList。
    item_header = b"\x73\x72" + len(CHAPTER_ITEM_CLASS).to_bytes(2, "big") + CHAPTER_ITEM_CLASS
    if not data[position + 10 :].startswith(item_header) or not data.endswith(b"\x78"):
        raise ReaderCacheError("ArrayList 不是完整的 ChapterItem 目录")
    # 4. 计数只表达备份目录记录总量，不推断作品当前更新状态。
    return "ChapterItem 目录", count


def load_keys(directory: Path) -> dict[str, bytes]:
    """读取备份中的真实版本密钥。参数：directory MMKV 目录。返回：版本到密钥的映射，不写出密钥。"""
    keys: dict[str, bytes] = {}
    for path in sorted(directory.glob("prefix_public_crypt_key_kv_*")):
        if not path.is_file() or path.suffix == ".crc":
            continue
        for match in KEY_PATTERN.finditer(path.read_bytes()):
            version = match.group(1).decode("ascii")
            key = bytes.fromhex(match.group(2).decode("ascii"))
            if version in keys and keys[version] != key:
                raise ReaderCacheError(f"备份中密钥版本 {version} 存在冲突")
            keys[version] = key
    if not keys:
        raise ReaderCacheError("MMKV 备份中没有章节密钥，请使用与缓存配套的完整备份")
    return keys


def decrypt_xhtml(content: str, key: bytes) -> bytes:
    """严格解开章节负载。参数：content Base64 章节字段，key 真实密钥。返回：原始 XHTML 字节。"""
    # 1. 校验 Base64 及 AES-CBC 的 IV 和密文边界。
    try:
        encrypted = base64.b64decode(content, validate=True)
    except ValueError as exc:
        raise ReaderCacheError("章节 content 不是合法 Base64") from exc
    if len(encrypted) < 32 or (len(encrypted) - 16) % 16:
        raise ReaderCacheError("章节 AES-CBC 负载长度无效")
    # 2. 使用缓存自带 IV 和备份中的密钥解码，严格检查 PKCS7。
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(encrypted[:16])).decryptor()
        padded = decryptor.update(encrypted[16:]) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        payload = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise ReaderCacheError("章节密钥不匹配、负载损坏或 PKCS7 校验失败") from exc
    # 3. 只对具有真实 gzip 标记的内容解压，损坏时明确报错。
    if payload.startswith(b"\x1f\x8b\x08"):
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise ReaderCacheError("章节 gzip 内容损坏") from exc
    # 4. 空负载不得作为可用正文进入领域模型。
    if not payload:
        raise ReaderCacheError("章节解码结果为空")
    return payload


def plain_text(xhtml: bytes) -> str:
    """从合法 XHTML 保留文字与段落。参数：xhtml 原始正文字节。返回：真实文本，纯图片正文返回空文本。"""
    try:
        document = xhtml.decode("utf-8")
        root = ElementTree.fromstring(document)
    except (UnicodeError, ElementTree.ParseError) as exc:
        raise ReaderCacheError("章节 XHTML 或 UTF-8 数据损坏") from exc
    body = next((node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "body"), None)
    if body is None:
        raise ReaderCacheError("章节 XHTML 缺少 body")
    pieces: list[str] = []
    collect_text(body, pieces)
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(pieces)).strip()
    if "\ufffd" in text:
        raise ReaderCacheError("章节正文为空或存在乱码替换字符")
    if not text and not any(node.tag.rsplit("}", 1)[-1].lower() == "img" for node in body.iter()):
        raise ReaderCacheError("章节正文为空且没有实际图片页面")
    return text


def collect_text(node: ElementTree.Element, pieces: list[str]) -> None:
    """递归保留正文节点和段落边界。参数：node XML 节点、pieces 文本片段。返回：无，追加真实文本。"""
    tag = node.tag.rsplit("}", 1)[-1].lower()
    if tag in {"script", "style", "head"}:
        return
    if tag in BLOCK_TAGS and pieces:
        pieces.append("\n\n")
    if tag == "br":
        pieces.append("\n")
    if node.text:
        pieces.append(node.text)
    for child in node:
        collect_text(child, pieces)
        if child.tail:
            pieces.append(child.tail)
    if tag in BLOCK_TAGS:
        pieces.append("\n\n")


def public_metadata(value: Any) -> Any:
    """删除正文密文及密钥字段并保留其他原始元信息。参数：value JSON 值。返回：可安全写入报告的同型值。"""
    if isinstance(value, dict):
        return {
            name: public_metadata(item)
            for name, item in value.items()
            if name.lower() != "content" and not ("key" in name.lower() and name.lower() != "key_version")
        }
    if isinstance(value, list):
        return [public_metadata(item) for item in value]
    return value
