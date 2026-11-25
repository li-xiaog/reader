"""EPUB 3 策略：保留原生资源或生成文字页，并验证清单与阅读顺序。"""

import mimetypes
import posixpath
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from html import escape
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.exporters.common import chapter_xhtml, file_stem, scope_note
from reader_cache_toolkit.models import BookDocument

OPF = "http://www.idpf.org/2007/opf"
XHTML = "http://www.w3.org/1999/xhtml"
NCX = "http://www.daisy.org/z3986/2005/ncx/"


def media_type(path: str) -> str:
    """识别资源类型。参数：path为资源相对路径。返回：媒体类型；未知格式明确报错。"""
    overrides = {
        ".xhtml": "application/xhtml+xml",
        ".html": "application/xhtml+xml",
        ".htm": "application/xhtml+xml",
        ".ncx": "application/x-dtbncx+xml",
        ".opf": "application/oebps-package+xml",
    }
    result = overrides.get(PurePosixPath(path).suffix.lower()) or mimetypes.guess_type(path)[0]
    if not result:
        raise ReaderCacheError(f"EPUB资源媒体类型尚未支持：{path}")
    return result


def resource_id(path: str) -> str:
    """生成资源清单ID。参数：path为资源路径。返回：由路径哈希构成的稳定XML ID。"""
    return "r-" + sha256(path.encode("utf-8")).hexdigest()


def prepare_assets(book: BookDocument) -> tuple[dict[str, bytes], list[str], list[str]]:
    """准备正文资源、唯一阅读顺序及章节目录目标。

    参数：book为带原始资产的书籍。
    返回：资源字典、spine路径和每个章节的首个导航路径。
    """
    assets = dict(book.epub_assets or book.assets) if book.metadata.get("epub_native_assets") else {}
    spine = []
    navigation = []
    for index, chapter in enumerate(book.chapters, 1):
        if book.metadata.get("epub_native_assets"):
            paths = chapter.asset_paths
            if not paths:
                raise ReaderCacheError(f"原生EPUB章节没有资源引用：{chapter.title}")
        else:
            path = f"text/chapter-{index:04d}.xhtml"
            assets[path] = chapter_xhtml(chapter)
            paths = (path,)
        navigation.append(paths[0])
        for path in paths:
            if path not in assets:
                raise ReaderCacheError(f"缺少原始章节资源：{path}")
            if path not in spine:
                spine.append(path)
    for name in assets:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ReaderCacheError(f"EPUB资源路径非法：{name}")
        if name in {"toolkit-nav.xhtml", "toolkit-toc.ncx", "package.opf"}:
            raise ReaderCacheError(f"原始资源与生成文件重名：{name}")
    return assets, spine, navigation


def build_package(book: BookDocument, assets: dict[str, bytes], spine: list[str]) -> bytes:
    """生成EPUB包清单。参数：book书籍、assets资源、spine阅读顺序。返回：UTF-8 OPF字节。"""
    items = [
        '<item id="nav" href="toolkit-nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toolkit-toc.ncx" media-type="application/x-dtbncx+xml"/>',
    ]
    for path in assets:
        items.append(
            f'<item id="{resource_id(path)}" href="{escape(path, quote=True)}" media-type="{media_type(path)}"/>'
        )
    references = "".join(f'<itemref idref="{resource_id(path)}"/>' for path in spine)
    identifier = f"urn:{book.channel}:{book.book_id}"
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><package xmlns="{OPF}" version="3.0" unique-identifier="book-id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="book-id">{escape(identifier)}</dc:identifier>'
        f"<dc:title>{escape(book.title)}</dc:title><dc:creator>{escape(book.author)}</dc:creator>"
        f"<dc:language>zh-CN</dc:language><dc:description>{escape(scope_note(book))}</dc:description>"
        f'<meta property="dcterms:modified">{modified}</meta></metadata>'
        f'<manifest>{"".join(items)}</manifest><spine toc="ncx">{references}</spine></package>'
    ).encode()


def build_navigation(book: BookDocument, navigation: list[str]) -> tuple[bytes, bytes]:
    """生成现代和兼容目录。参数：book书籍、navigation每章目标。返回：XHTML导航与NCX字节。"""
    items = []
    points = []
    for index, (chapter, path) in enumerate(zip(book.chapters, navigation, strict=True), 1):
        target, title = escape(path, quote=True), escape(chapter.title)
        items.append(f'<li><a href="{target}">{title}</a></li>')
        points.append(
            f'<navPoint id="chapter-{index}" playOrder="{index}"><navLabel><text>{title}</text>'
            f'</navLabel><content src="{target}"/></navPoint>'
        )
    nav = (
        f'<?xml version="1.0" encoding="UTF-8"?><html xmlns="{XHTML}" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN">'
        f"<head><title>目录</title></head><body><h1>{escape(book.title)}</h1><p>{escape(scope_note(book))}</p>"
        f'<nav epub:type="toc" id="toc"><ol>{"".join(items)}</ol></nav></body></html>'
    )
    ncx = (
        f'<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="{NCX}" version="2005-1">'
        f'<head><meta name="dtb:uid" content="urn:{book.channel}:{book.book_id}"/>'
        '<meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/>'
        '<meta name="dtb:maxPageNumber" content="0"/></head>'
        f"<docTitle><text>{escape(book.title)}</text></docTitle><navMap>{''.join(points)}</navMap></ncx>"
    )
    return nav.encode("utf-8"), ncx.encode("utf-8")


def verify_epub(path: Path, expected_spine: list[str], expected_navigation: list[str]) -> None:
    """核查最终EPUB的容器、XML、清单、目录与图片资源引用。

    参数：path为生成文件；expected_spine和expected_navigation为真实解析得到的阅读顺序。
    返回：无；校验失败抛出错误，禁止当作成功结果发布。
    """
    # 1. 检查容器内容与所有XML，不忽略CRC或XML错误。
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not infos or infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED:
            raise ReaderCacheError("EPUB首项mimetype缺失或被压缩")
        if archive.read("mimetype") != b"application/epub+zip" or archive.testzip() is not None:
            raise ReaderCacheError("EPUB类型或ZIP完整性校验失败")
        names = set(archive.namelist())
        if len(names) != len(infos):
            raise ReaderCacheError("EPUB存在重复资源名")
        xml = {
            name: ET.fromstring(archive.read(name))
            for name in names
            if PurePosixPath(name).suffix.lower()
            in {".xml", ".xhtml", ".html", ".htm", ".opf", ".ncx", ".svg"}
        }

        # 2. 清单必须包含全部正文目标，阅读顺序与真实缓存一致。
        package = xml["OEBPS/package.opf"]
        manifest = {
            node.attrib["id"]: node.attrib["href"]
            for node in package.findall(f"{{{OPF}}}manifest/{{{OPF}}}item")
        }
        if any("OEBPS/" + name not in names for name in manifest.values()):
            raise ReaderCacheError("EPUB清单包含缺失的资源")
        spine = [
            manifest[node.attrib["idref"]] for node in package.findall(f"{{{OPF}}}spine/{{{OPF}}}itemref")
        ]
        if spine != expected_spine:
            raise ReaderCacheError("EPUB正文阅读顺序与真实缓存不一致")

        # 3. 两套目录必须逐一指向正确章节，允许单章引用多个原始页面。
        nav = xml["OEBPS/toolkit-nav.xhtml"]
        targets = [node.attrib["href"] for node in nav.findall(f".//{{{XHTML}}}a")]
        ncx = xml["OEBPS/toolkit-toc.ncx"]
        old_targets = [node.attrib["src"] for node in ncx.findall(f".//{{{NCX}}}content")]
        if targets != expected_navigation or old_targets != expected_navigation:
            raise ReaderCacheError("EPUB章节目录与原始章节不一致")

        # 4. 验证原生XHTML引用的本地图片、样式和页面，外链保持原样不请求网络。
        for name, document in xml.items():
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            for node in document.iter():
                for attribute in ("src", "href"):
                    target = node.get(attribute)
                    if not target:
                        continue
                    parsed = urlsplit(target)
                    if parsed.scheme or parsed.netloc or not parsed.path:
                        continue
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), unquote(parsed.path))
                    )
                    if resolved not in names:
                        raise ReaderCacheError(f"EPUB存在缺失的内部资源：{name} -> {target}")


class EpubExporter:
    """EPUB导出策略；参数：无；返回：策略实例。"""

    def export(self, book: BookDocument, directory: Path) -> tuple[Path, ...]:
        """生成并验证电子书。参数：book为真实书籍、directory为临时目录。返回：EPUB文件。"""
        # 1. 准备真实资源及两个目录。
        if error := book.metadata.get("epub_error"):
            raise ReaderCacheError(f"无法完整生成EPUB：{error}")
        if book.metadata.get("missing_assets"):
            raise ReaderCacheError(
                "原生EPUB缺少本地引用资源，无法生成完整电子书；可单独选择TXT或Markdown导出已校验正文。"
            )
        assets, spine, navigation = prepare_assets(book)
        nav, ncx = build_navigation(book, navigation)
        package = build_package(book, assets, spine)
        # 2. 按EPUB规范写入容器和未压缩的mimetype。
        output = directory / (file_stem(book) + ".epub")
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
            # 3. 写入正文、原生资源和统一的导航元数据。
            archive.writestr("OEBPS/package.opf", package)
            archive.writestr("OEBPS/toolkit-nav.xhtml", nav)
            archive.writestr("OEBPS/toolkit-toc.ncx", ncx)
            for name, data in assets.items():
                archive.writestr("OEBPS/" + name, data)
        # 4. 结构与引用全部通过后才交回应用服务。
        verify_epub(output, spine, navigation)
        return (output,)
