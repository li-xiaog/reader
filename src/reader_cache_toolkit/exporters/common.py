"""导出器共享的命名、范围说明及文本处理函数。"""

import re
from html import escape
from xml.etree import ElementTree

from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.models import BookDocument, Chapter


def safe_name(value: str) -> str:
    """规范化文件名。参数：value为真实名称。返回：安全文件名；无有效名称则报错。"""
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:100].rstrip(" .")
    if not result:
        raise ReaderCacheError("真实书名或章节名称无法构成有效文件名")
    if result.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *{f"COM{i}" for i in range(1, 10)},
        *{f"LPT{i}" for i in range(1, 10)},
    }:
        result = "_" + result
    return result


def file_stem(book: BookDocument) -> str:
    """生成标识缓存范围的书籍文件名。参数：book为书籍。返回：不含扩展名的名称。"""
    return f"{safe_name(book.title)}_{book.book_id}_已缓存{len(book.chapters)}项"


def scope_note(book: BookDocument) -> str:
    """说明真实导出范围。参数：book为已解析书籍。返回：完整性说明，未知总数明确标注。"""
    if book.expected_count is None:
        return f"导出已缓存的 {len(book.chapters)} 个目录项；缺少可靠的完整目录，不能确认全书完整。"
    return (
        f"导出已缓存的 {len(book.chapters)} 个目录项；备份目录共 {book.expected_count} 项，"
        f"未缓存 {book.expected_count - len(book.chapters)} 项。目录项可能包含版权页及作者说明。"
    )


def body_without_heading(chapter: Chapter) -> str:
    """去除与索引完全相同的首行标题以免重复。参数：chapter为章节。返回：保留正文的文本。"""
    text = chapter.text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[0].strip() == chapter.title.strip():
        return "\n".join(lines[1:]).lstrip("\n")
    return text


def chapter_xhtml(chapter: Chapter) -> bytes:
    """生成可移植的文字章节页。参数：chapter为真实章节。返回：已通过XML检查的UTF-8页面。"""
    paragraphs = "\n".join(
        f"<p>{escape(line) if line else '&#160;'}</p>" for line in body_without_heading(chapter).splitlines()
    )
    page = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="zh-CN" xml:lang="zh-CN">'
        f"<head><title>{escape(chapter.title)}</title><style>"
        "body{font-family:serif;font-size:1em;line-height:1.8;margin:1em}"
        "p{white-space:pre-wrap}h1{font-size:1.5em}"
        f"</style></head><body><h1>{escape(chapter.title)}</h1>{paragraphs}</body></html>"
    )
    ElementTree.fromstring(page)
    return page.encode("utf-8")
