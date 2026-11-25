"""微信读书真实 RES 容器解析；只在内存中解码，不修改原始缓存。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup, NavigableString, Tag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from lxml import etree

REMAP_TABLE = bytes.fromhex(
    "34ca55401db693c63130293532a7b811c2b516fa8bb124a4109004e908f83b8a"
    "9c8c44f9bc5c69e2a1dad2d37589f71e2d5056d77253bf22fb200f012e45876e"
    "6648f2e0cdfe67a943f49451cea54aee13268eccaa33145d0e39bbcf912b814d"
    "ea99ec1a2c85c5d936744b18e1f13d9d419fb4170dd64cbedcaf972877f062ff"
    "71c1c8278f6c68a89be6591c1b1209984e3f063700ba1f0a192fc9d5d057496f"
    "fd25e4610c42cb96645fdbad60238d9a6dc3c45e3eb9926abd5b077f7695ed4f"
    "ab847a80e778c7e5eb73836bfc38467d4765b352633a05d1efa3a6de9e3c02ae"
    "b27ba0f6f32ac0ac86035a540bf582d47ee3dfb0d8dd21e87c88a2795870b715"
)


BUNDLE_RES_NAME_PATTERN = re.compile(r"^(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)\.res$")


SINGLE_RES_NAME_PATTERN = re.compile(r"^(\d+)\.res$")


BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "caption",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}


@dataclass(frozen=True)
class ChapterData:
    """保存一个已解析章节的标题、正文和校验信息。

    参数:
        uid: 章节 UID。
        index: 章节顺序。
        title: 章节标题。
        text: 纯文本正文。
        source_file: 章节在 EPUB 中的文件名。
    返回:
        ChapterData: 不可变的章节数据对象。
    """

    uid: int
    index: int
    title: str
    text: str
    source_file: str


@dataclass(frozen=True)
class ParsedBundle:
    """保存章节包中的正文记录和 EPUB 资源。

    参数:
        chapters: 包内章节记录。
        assets: 规范化路径到原始资源字节的映射。
        normalized_assets: 对原始无效 XHTML 所作确定性去重的哈希审计。
    返回:
        ParsedBundle: 不可变的章节包解析结果。
    """

    chapters: tuple[ChapterData, ...]
    assets: dict[str, bytes]
    normalized_assets: dict[str, dict[str, str | int]] = field(default_factory=dict)


def derive_header_key_iv(vid: str) -> bytes:
    """按客户端算法生成章节包头密钥和 IV。

    参数:
        vid: 当前登录账号 VID。
    返回:
        bytes: 前 16 字节为 AES 密钥，后 16 字节为 CBC IV。
    """

    vid_bytes = vid.encode("utf-8")
    if not vid_bytes:
        raise ValueError("账号 VID 不能为空")
    repeated = (vid_bytes * ((32 + len(vid_bytes) - 1) // len(vid_bytes)))[:32]
    return bytes(REMAP_TABLE[value] for value in repeated)


def decrypt_bundle_password(encrypted_key: bytes, vid: str) -> bytes:
    """解密章节包 ZIP 密码。

    参数:
        encrypted_key: `.res` 文件头中的加密密码。
        vid: 当前登录账号 VID。
    返回:
        bytes: 可传给 ZIP 解压器的原始密码字节。
    """

    if not encrypted_key or len(encrypted_key) % 16 != 0:
        raise ValueError("章节包加密密码长度不合法")
    key_iv = derive_header_key_iv(vid)
    decryptor = Cipher(algorithms.AES(key_iv[:16]), modes.CBC(key_iv[16:])).decryptor()
    padded = decryptor.update(encrypted_key) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def normalize_whitespace(text: str) -> str:
    """整理 XHTML 文本中的空白，同时保留有意义的段落换行。

    参数:
        text: 待整理的文本。
    返回:
        str: 空白规范化后的文本。
    """

    text = text.replace("\u00a0", " ").replace("\u3000", "　")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def node_to_text(node: Tag | NavigableString) -> str:
    """递归转换一个 XHTML 节点并保留段落、表格和脚注结构。

    参数:
        node: BeautifulSoup 标签或文本节点。
    返回:
        str: 节点对应的纯文本。
    """

    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in {"script", "style", "noscript", "svg"}:
        return ""
    if name == "br":
        return "\n"
    if name == "img":
        alt = normalize_whitespace(node.get("alt", ""))
        if not alt:
            return ""
        raw_classes = node.get("class", [])
        classes = set(raw_classes.split() if isinstance(raw_classes, str) else raw_classes)
        return f"〔注：{alt}〕" if "qqreader-footnote" in classes else f"〔图片：{alt}〕"
    content = "".join(node_to_text(child) for child in node.children)
    if name == "li":
        content = f"- {content.strip()}"
    if name in {"td", "th"}:
        return content.strip() + "\t"
    if name in BLOCK_TAGS:
        return "\n" + content.strip() + "\n"
    return content


def xhtml_to_text(data: bytes) -> str:
    """将 UTF-8 XHTML 正文转换成适合 TXT 阅读的文本。

    参数:
        data: XHTML 原始字节。
    返回:
        str: 已保留标题、段落、表格和脚注的纯文本。
    """

    document = data.decode("utf-8-sig")
    if "\ufffd" in document:
        raise ValueError("XHTML 包含乱码替换字符")
    etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, recover=False))
    soup = BeautifulSoup(document, "lxml-xml")
    body = soup.find("body")
    if body is None:
        raise RuntimeError("XHTML 中找不到 body 标签")
    return normalize_whitespace(node_to_text(body))


def normalize_xhtml(data: bytes, asset_path: str) -> tuple[bytes, dict[str, str | int] | None]:
    """仅修复值完全相同的重复 data-* 属性，并严格验证修复后的 XML。

    参数:
        data: 原始 XHTML 字节，不会写回原始备份。
        asset_path: 用于错误说明和审计的资源路径。
    返回:
        tuple: 输出字节和可选哈希审计；不同值重复属性或其他损坏直接报错。
    """

    # 1. 优先验证原始字节；合法 XHTML 原样保留。
    document = data.decode("utf-8-sig")
    if "\ufffd" in document:
        raise ValueError(f"XHTML 含乱码替换字符：{asset_path}")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        etree.fromstring(data, parser)
        return data, None
    except etree.XMLSyntaxError:
        if not parser.error_log or any(
            error.type_name != "ERR_ATTRIBUTE_REDEFINED" for error in parser.error_log
        ):
            raise ValueError(f"原始 XHTML 结构损坏：{asset_path}") from None

    # 2. 逐标签检查重复属性，只移除相同字面值的重复 data-* 项。
    tag_pattern = re.compile(
        r"<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>|"
        r"""<[A-Za-z_][\w:.-]*(?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|'[^']*'))*\s*/?>"""
    )
    attribute_pattern = re.compile(r"""\s+([\w:.-]+)\s*=\s*(["'])(.*?)\2""", re.DOTALL)
    replacements: list[tuple[int, int, str]] = []
    removed = 0
    for tag_match in tag_pattern.finditer(document):
        tag = tag_match.group()
        if tag.startswith(("<!--", "<![CDATA[", "<?")):
            continue
        seen: dict[str, str] = {}
        deletions: list[tuple[int, int]] = []
        for attribute in attribute_pattern.finditer(tag):
            name, value = attribute.group(1), attribute.group(3)
            if name in seen:
                if not name.startswith("data-") or seen[name] != value:
                    raise ValueError(f"XHTML 有冲突或不受支持的重复属性 {name}：{asset_path}")
                deletions.append(attribute.span())
            else:
                seen[name] = value
        for start, end in reversed(deletions):
            tag = tag[:start] + tag[end:]
        if deletions:
            replacements.append((tag_match.start(), tag_match.end(), tag))
            removed += len(deletions)

    # 3. 保留其余源文本和可选 BOM，修复后再次严格解析。
    if removed == 0:
        raise ValueError(f"无法确定性修复重复属性：{asset_path}")
    for start, end, tag in reversed(replacements):
        document = document[:start] + tag + document[end:]
    output = document.encode("utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8")
    etree.fromstring(output, etree.XMLParser(resolve_entities=False, no_network=True, recover=False))

    # 4. 返回可追溯审计，不嵌入正文或私有账号信息。
    return output, {
        "original_sha256": hashlib.sha256(data).hexdigest(),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "removed_attribute_count": removed,
    }


def normalize_archive_path(name: str) -> str:
    """规范化 ZIP 条目路径并阻止目录穿越。

    参数:
        name: ZIP 中记录的原始路径。
    返回:
        str: 使用正斜杠的安全相对路径。
    """

    normalized = name.replace("\\", "/").removeprefix("./")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not normalized or ":" in path.parts[0]:
        raise RuntimeError(f"章节包包含不安全路径: {name}")
    return path.as_posix()


def parse_bundle_uid_spec(spec: str) -> tuple[int, ...]:
    """展开章节包文件名中的章节编号表达式。

    参数:
        spec: 不含扩展名的编号表达式，例如 ``1,68-71``。
    返回:
        tuple[int, ...]: 按文件名声明顺序展开的章节 UID。
    """

    uids: list[int] = []
    for segment in spec.split(","):
        if "-" not in segment:
            uids.append(int(segment))
            continue
        start_text, end_text = segment.split("-", 1)
        start = int(start_text)
        end = int(end_text)
        if end < start:
            raise RuntimeError(f"章节包编号范围无效: {spec}")
        uids.extend(range(start, end + 1))
    return tuple(uids)


def read_bundle(bundle_path: Path, account_id: str) -> tuple[list[int], zipfile.ZipFile, bytes]:
    """严格读取 RES 文件头、章节 UID、密码和 ZIP 数据。

    参数:
        bundle_path: 已存在的本地章节包路径。
        account_id: 用户显式提供或备份明确记录的账号 VID。
    返回:
        tuple: 章节 UID 列表、须由调用者关闭的 ZIP 对象与解包密码。
    """

    # 1. 校验头部边界与章节数量，防止损坏长度越界。
    data = bundle_path.read_bytes()
    if len(data) < 24:
        raise ValueError(f"章节包过短：{bundle_path.name}")
    magic, count = struct.unpack_from(">II", data)
    if magic not in (0xA344FE40, 0x4412F671) or count == 0:
        raise ValueError(f"章节包标识或章节数不合法：{bundle_path.name}")
    offset = 8 + count * 4
    if offset + 12 > len(data):
        raise ValueError(f"章节包 UID 目录越界：{bundle_path.name}")
    chapter_uids = list(struct.unpack_from(f">{count}I", data, 8))
    if len(set(chapter_uids)) != len(chapter_uids):
        raise ValueError(f"章节包头包含重复 UID：{bundle_path.name}")

    # 2. 严格读取加密密码，不接受不完整字段。
    key_length = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    if offset + key_length + 8 > len(data):
        raise ValueError(f"章节包密码字段越界：{bundle_path.name}")
    encrypted_key = data[offset : offset + key_length]
    offset += key_length
    body_length = struct.unpack_from(">Q", data, offset)[0]
    offset += 8

    # 3. 核对 ZIP 长度，不静默忽略文件尾部的未知内容。
    body = data[offset:]
    if len(body) != body_length or not body.startswith(b"PK\x03\x04"):
        raise ValueError(f"章节包 ZIP 长度或标识错误：{bundle_path.name}")

    # 4. 使用真实账号解密；错误账号和损坏数据直接报告失败。
    try:
        password = decrypt_bundle_password(encrypted_key, account_id)
    except ValueError as exc:
        raise ValueError(f"章节包密码解密失败，请核对账号 VID 和缓存完整性：{bundle_path.name}") from exc
    return chapter_uids, zipfile.ZipFile(io.BytesIO(body)), password


def parse_bundle(bundle_path: Path, account_id: str, book_id: str) -> ParsedBundle:
    """完整解析章节包并保留原始 XHTML、图片、CSS 和脚注资产。

    参数:
        bundle_path: 真实 RES 文件路径。
        account_id: 已核实的账号 VID，不尝试猜测其他账号。
        book_id: 请求的书籍标识，用于核对包内显式身份。
    返回:
        ParsedBundle: 按文件头顺序排列的章节和未经重写的原始资产。
    """

    # 1. 解开容器并核对真实目录结构和可选书籍身份。
    expected_uids, archive, password = read_bundle(bundle_path, account_id)
    with archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"章节包含重复 ZIP 条目：{bundle_path.name}")
        info = json.loads(archive.read("info.txt", pwd=password).decode("utf-8-sig"))
        if not isinstance(info, dict):
            raise ValueError("章节包 info.txt 必须是对象")
        if "bookId" in info and str(info["bookId"]) != book_id:
            raise ValueError("章节包书籍 ID 与转换请求冲突")
        if "chapters" in info and "data" in info and info["chapters"] != info["data"]:
            raise ValueError("章节包包含冲突的两份章节目录")
        records = info.get("chapters", info.get("data"))
        if not isinstance(records, list) or not records:
            raise ValueError("章节包缺少真实章节目录")

        # 2. 读取全部原始资产，由 ZIP 读取过程验证每项 CRC 和密码。
        assets: dict[str, bytes] = {}
        normalized_assets: dict[str, dict[str, str | int]] = {}
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            asset_path = normalize_archive_path(entry.filename)
            if asset_path in {"info.txt", "mimetype"} or asset_path.startswith("META-INF/"):
                continue
            if asset_path in assets:
                raise ValueError(f"章节包规范化后的资源路径重复：{asset_path}")
            data = archive.read(entry, pwd=password)
            if PurePosixPath(asset_path).suffix.lower() in {".xhtml", ".html", ".htm"}:
                data, audit = normalize_xhtml(data, asset_path)
                if audit is not None:
                    normalized_assets[asset_path] = audit
            assets[asset_path] = data

        # 3. 提取真实标题和纯文本，同时保存章节的原生阅读资源顺序。
        chapters: list[ChapterData] = []
        for record in records:
            uid = int(record["chapterUid"])
            index = int(record["chapterIdx"])
            title = record["title"]
            files = record["files"]
            if not isinstance(title, str) or not title.strip() or "\ufffd" in title:
                raise ValueError(f"章节 {uid} 缺少有效的真实标题")
            if not isinstance(files, list) or not files or not all(isinstance(name, str) for name in files):
                raise ValueError(f"章节 {uid} 缺少合法 XHTML 资源列表")
            normalized = [normalize_archive_path(name) for name in files]
            if len(normalized) != len(set(normalized)):
                raise ValueError(f"章节 {uid} 重复引用正文资源")
            missing = [name for name in normalized if name not in assets]
            if missing:
                raise ValueError(f"章节 {uid} 缺少资源：{missing}")
            parts = [xhtml_to_text(assets[name]) for name in normalized]
            chapters.append(
                ChapterData(
                    uid, index, title, "\n\n".join(part for part in parts if part), "|".join(normalized)
                )
            )

    # 4. 文件头、文件名和目录必须一致，任何矛盾阻止继续导出。
    actual_uids = [chapter.uid for chapter in chapters]
    if actual_uids != expected_uids:
        raise ValueError(f"章节包目录与文件头 UID 不一致：{bundle_path.name}")
    match = BUNDLE_RES_NAME_PATTERN.fullmatch(bundle_path.name)
    if match is None or parse_bundle_uid_spec(match.group(1)) != tuple(expected_uids):
        raise ValueError(f"章节包文件名与内部 UID 不一致：{bundle_path.name}")
    return ParsedBundle(tuple(chapters), assets, normalized_assets)


def discover_bundles(cache_dir: Path) -> list[Path]:
    """查找真实 RES 文件，仅去除能够逐字节证明同源的单章别名副本。

    参数:
        cache_dir: 单本书的原始缓存目录。
    返回:
        list[Path]: 按 UID 排序的独立包；无法证实同源的普通文件保留并严格校验。
    """

    # 1. 校验所有候选名称与符号链接的真实目标。
    bundles: list[Path] = []
    for path in cache_dir.glob("*.res"):
        if BUNDLE_RES_NAME_PATTERN.fullmatch(path.name) is None:
            raise ValueError(f"不支持的 RES 文件名：{path.name}")
        if path.is_symlink():
            target = path.resolve(strict=True)
            if target.parent != cache_dir.resolve() or not target.is_file():
                raise ValueError(f"章节包链接指向缓存目录之外：{path.name}")
            target_match = BUNDLE_RES_NAME_PATTERN.fullmatch(target.name)
            if target_match is None or not set(parse_bundle_uid_spec(path.stem)).issubset(
                parse_bundle_uid_spec(target.stem)
            ):
                raise ValueError(f"章节包链接名称与目标 UID 不匹配：{path.name}")
            continue
        if not path.is_file():
            raise ValueError(f"章节包不是普通文件：{path.name}")
        bundles.append(path)
    if not bundles:
        raise ValueError(f"缓存目录没有可读取的 RES 文件：{cache_dir}")

    # 2. 记录现存复合包的真实字节和文件名 UID，不能仅按名称排除单章文件。
    composite = [
        (path, parse_bundle_uid_spec(path.stem), path.read_bytes())
        for path in bundles
        if "," in path.stem or "-" in path.stem
    ]

    # 3. ADB 可能把原符号链接拉取为普通文件，只在全部字节相同且 UID 匹配时去重。
    independent: list[Path] = []
    for path in bundles:
        if SINGLE_RES_NAME_PATTERN.fullmatch(path.name):
            uid = int(path.stem)
            relevant = [(uids, data) for _, uids, data in composite if uid in uids]
            if relevant:
                data = path.read_bytes()
                if any(data == content for _, content in relevant):
                    continue
        independent.append(path)

    # 4. 保留复合包的后续文件名、文件头及 info 三方校验，并按真实 UID 排序。
    independent.sort(key=lambda path: min(parse_bundle_uid_spec(path.stem)))
    return independent
