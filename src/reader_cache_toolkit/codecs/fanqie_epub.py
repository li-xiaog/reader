"""将已验证的番茄原始页面与图片缓存转换为独立的 EPUB 资源副本。"""

import posixpath
from hashlib import sha256
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.models import Chapter

XHTML = "http://www.w3.org/1999/xhtml"
TAGS = {"html", "head", "body", "title", "h1", "blk", "p", "div", "img", "span", "br"}
PRIVATE_ATTRIBUTES = {"idx", "p_idx", "e_idx", "e_order", "group-id", "source", "media-idx"}
STANDARD_ATTRIBUTES = {"class", "id", "src", "alt", "width", "height", "lang"}


def java_string_hash(value: str) -> str:
    """计算缓存采用的 Java 字符串哈希。参数：value 为未解码的 URL 路径文件名。返回：32 位有符号十进制哈希。"""
    encoded = value.encode("utf-16-be")
    result = 0
    for offset in range(0, len(encoded), 2):
        result = (31 * result + int.from_bytes(encoded[offset : offset + 2], "big")) & 0xFFFFFFFF
    return str(result if result < 0x80000000 else result - 0x100000000)


def image_extension(data: bytes) -> str:
    """根据真实魔数识别可打包的图片。参数：data 为缓存原始字节。返回：扩展名；未知类型明确报错。"""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    raise ReaderCacheError("番茄图片缓存不是已支持的 JPEG、PNG 或 GIF 格式")


def normalize_page(
    name: str,
    raw: bytes,
    title: str,
    original_assets: dict[str, bytes],
    output_assets: dict[str, bytes],
    image_sources: dict[str, str],
) -> tuple[bytes, list[dict[str, str]]]:
    """规范化单个页面并保留真实图片位置。

    参数：name 页面相对路径；raw 原页面；title 真实标题；original_assets 原资源；
    output_assets 规范化资源；image_sources 用于检查图片哈希碰撞的文件名记录。
    返回：规范化 XHTML 字节和不含签名 URL 的图片来源审计。
    """
    # 1. 只接受已经验证过的页面结构，不使用宽松 HTML 修复未知节点。
    root = ET.fromstring(raw)
    if root.tag != "html" or len(root.findall("head")) != 1 or len(root.findall("body")) != 1:
        raise ReaderCacheError(f"番茄页面结构无法规范化：{name}")
    head = root.find("head")
    assert head is not None
    if head.find("title") is None:
        ET.SubElement(head, "title").text = title
    mappings = []

    # 2. 保持原节点及文本顺序，将应用私有块与属性转换为 XHTML 表达。
    for node in root.iter():
        tag = node.tag
        if tag not in TAGS:
            raise ReaderCacheError(f"番茄页面包含未支持的标签：{tag}")
        attributes = {}
        for key, value in node.attrib.items():
            if key in {"img-width", "img-height"}:
                if not value.isascii() or not value.isdecimal() or int(value) <= 0:
                    raise ReaderCacheError(f"番茄图片尺寸无效：{name}")
                normalized_key = key.removeprefix("img-")
            elif key in PRIVATE_ATTRIBUTES:
                normalized_key = "data-" + key.replace("_", "-")
            elif key in STANDARD_ATTRIBUTES or key.startswith("data-"):
                normalized_key = key
            else:
                raise ReaderCacheError(f"番茄页面包含未支持的属性：{key}")
            if normalized_key in attributes and attributes[normalized_key] != value:
                raise ReaderCacheError(f"番茄页面规范化属性发生冲突：{name}")
            attributes[normalized_key] = value

        # 3. 根据实际确认的 basename 哈希定位缓存；缺图和碰撞不能按远程图兜底。
        if tag == "img":
            source = attributes.get("src", "")
            url = urlsplit(source)
            basename = url.path.rsplit("/", 1)[-1]
            if url.scheme not in {"http", "https"} or not url.netloc or not basename:
                raise ReaderCacheError(f"番茄图片引用不符合已验证的缓存结构：{name}")
            cache_id = java_string_hash(basename)
            if cache_id in image_sources and image_sources[cache_id] != basename:
                raise ReaderCacheError("番茄图片文件名哈希碰撞，无法唯一确认图片")
            image_sources[cache_id] = basename
            cache_path = f"xhtml/image/{cache_id}"
            if cache_path not in original_assets:
                raise ReaderCacheError(f"番茄图片缓存缺失：{cache_id}")
            data = original_assets[cache_path]
            target = cache_path + image_extension(data)
            output_assets[target] = data
            attributes["src"] = posixpath.relpath(target, posixpath.dirname(name))
            attributes.setdefault("alt", "")
            mappings.append(
                {"source_cache": cache_path, "epub_path": target, "sha256": sha256(data).hexdigest()}
            )
        node.attrib.clear()
        node.attrib.update(attributes)
        node.tag = f"{{{XHTML}}}{'div' if tag == 'blk' else tag}"

    # 4. 输出规范化副本并重新解析，原始页面和图片字节始终保留原样。
    normalized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(normalized)
    return normalized, mappings


def prepare_epub_assets(
    chapters: tuple[Chapter, ...], assets: dict[str, bytes]
) -> tuple[dict[str, bytes], dict[str, dict[str, object]]]:
    """建立完整番茄 EPUB 资源副本并记录审计。

    参数：chapters 为真实章节顺序；assets 为原始 XHTML 与图片缓存。
    返回：规范化资源及逐页哈希、图片映射；任意缺失或未知结构立即抛出异常。
    """
    output: dict[str, bytes] = {}
    audit: dict[str, dict[str, object]] = {}
    image_sources: dict[str, str] = {}
    for chapter in chapters:
        for name in chapter.asset_paths:
            raw = assets[name]
            normalized, mappings = normalize_page(name, raw, chapter.title, assets, output, image_sources)
            output[name] = normalized
            audit[name] = {
                "source_sha256": sha256(raw).hexdigest(),
                "epub_sha256": sha256(normalized).hexdigest(),
                "images": mappings,
            }
    return output, audit
