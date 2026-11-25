"""TXT、Markdown 与原始 XHTML/资源导出策略。"""

from pathlib import Path, PurePosixPath

from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.exporters.common import (
    body_without_heading,
    chapter_xhtml,
    file_stem,
    safe_name,
    scope_note,
)
from reader_cache_toolkit.models import BookDocument


class TextExporter:
    """合并及分章TXT策略；参数：无；返回：导出策略实例。"""

    def export(self, book: BookDocument, directory: Path) -> tuple[Path, ...]:
        """写入UTF-8 BOM文本。参数：book为真实书籍；directory为工作目录。返回：所有文本文件。"""
        chapter_dir = directory / "chapters_txt"
        image_only = [chapter.title for chapter in book.chapters if not body_without_heading(chapter).strip()]
        if image_only:
            raise ReaderCacheError(
                f"{len(image_only)} 个目录项只有原生图片页面或标题，TXT无法表达其正文；请只选择EPUB或XHTML。"
            )
        chapter_dir.mkdir()
        parts = [book.title, f"作者：{book.author}", scope_note(book)]
        paths = []
        for index, chapter in enumerate(book.chapters, 1):
            content = chapter.title + "\n\n" + body_without_heading(chapter)
            path = chapter_dir / f"{index:04d}_{safe_name(chapter.title)}.txt"
            path.write_text(content, encoding="utf-8-sig", newline="\n")
            paths.append(path)
            parts.append(content)
        merged = directory / (file_stem(book) + ".txt")
        merged.write_text("\n\n".join(parts), encoding="utf-8-sig", newline="\n")
        return (merged, *paths)


class MarkdownExporter:
    """Markdown策略；参数：无；返回：导出策略实例。"""

    def export(self, book: BookDocument, directory: Path) -> tuple[Path, ...]:
        """写入带章节标题的Markdown。参数：book书籍、directory目录。返回：Markdown文件。"""
        if any(not body_without_heading(chapter).strip() for chapter in book.chapters):
            raise ReaderCacheError(
                "存在只有原生图片页面或标题的目录项，纯文本Markdown不能表达其正文；请只选择EPUB或XHTML。"
            )
        parts = [f"# {book.title}", f"作者：{book.author}", scope_note(book)]
        for chapter in book.chapters:
            parts.append(f"## {chapter.title}\n\n{body_without_heading(chapter)}")
        path = directory / (file_stem(book) + ".md")
        path.write_text("\n\n".join(parts), encoding="utf-8-sig", newline="\n")
        return (path,)


class XhtmlExporter:
    """XHTML与关联资源策略；参数：无；返回：导出策略实例。"""

    def export(self, book: BookDocument, directory: Path) -> tuple[Path, ...]:
        """保留已有XHTML资源，无原始页面时由正文生成。参数：书籍与目录。返回：资源文件。"""
        asset_dir = directory / "xhtml"
        asset_dir.mkdir()
        assets = (
            book.assets
            if book.assets
            else {
                f"chapter-{index:04d}.xhtml": chapter_xhtml(chapter)
                for index, chapter in enumerate(book.chapters, 1)
            }
        )
        files = []
        normalized_names = set()
        for name, data in assets.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise ReaderCacheError(f"资源包含非法相对路径：{name}")
            if any(part != part.rstrip(" .") or safe_name(part) != part for part in relative.parts):
                raise ReaderCacheError(f"资源名称无法在Windows中无损保存：{name}")
            normalized = name.casefold()
            if normalized in normalized_names:
                raise ReaderCacheError(f"资源名称在Windows中产生大小写碰撞：{name}")
            normalized_names.add(normalized)
            path = asset_dir.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(data)
            files.append(path)
        return tuple(files)
