"""手动选择目录的智能扫描与加载：识别桌面备份目录和原始起点缓存布局。

支持三种目录形态，规则与 Android App 完全一致：
1. 标准备份目录：含 qidian_book_metadata_{ID}.json、book_index、book_cache、database/databases；
2. 原始缓存目录：从模拟器手动复制的 QDReader/book 布局（{用户目录}/{书籍ID}.qd 索引 + 同名子目录章节缓存）；
3. 上述目录的任意上级目录（一次扫描可发现多本书）。

QDConfig 设备标识数据库是解密必需文件，可位于所选目录树任意位置，
也可在界面中单独指定。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reader_cache_toolkit.codecs import qidian
from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.models import BookDocument, Channel, Chapter, ProgressCallback
from reader_cache_toolkit.service import quiet_progress

METADATA_NAME = re.compile(r"qidian_book_metadata_(\d+)\.json")
INDEX_NAME = re.compile(r"(\d+)\.qd\Z")
NUMERIC_NAME = re.compile(r"\d+\Z")
QDCONFIG_NAME = "QDConfig"
SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
MAX_DEPTH = 4
RAW_PLACEHOLDER_TITLE = "书籍 {book_id}"
RAW_PLACEHOLDER_AUTHOR = "未知作者"
READY_NOTE = "结构完整 · 可导出"
QDCONFIG_HINT = (
    "缺少 QDConfig 设备标识数据库（解密必需）：请把 QDConfig 文件放进所选目录的任意位置，"
    "或在界面中手动选择该文件。桌面工具备份目录自带 database/databases/QDConfig。"
)


@dataclass(frozen=True, slots=True)
class LocalBook:
    """智能扫描发现的本地书籍。参数：身份、布局类型、实际路径与结构检查结果。返回：不可变记录。"""

    book_id: str
    title: str
    author: str
    user_dir: str
    layout: str
    index_path: Path | None
    cache_dir: Path | None
    qdconfig_path: Path | None
    ready: bool
    note: str

    def label(self) -> str:
        """生成书籍列表展示文本。参数：无。返回：含书名、编号与状态的文本。"""
        return f"{self.title}（{self.book_id}）· {self.note}"


def _read_metadata_lenient(metadata_path: Path, book_id: str) -> tuple[str, str, str, str | None]:
    """宽容读取备份元数据供扫描展示。参数：metadata_path 元数据文件、book_id 书籍编号。返回：书名、作者、用户目录及错误说明。"""
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        return "", "", "", f"元数据读取失败（{error}）"
    if not isinstance(metadata, dict):
        return "", "", "", "元数据不是 JSON 对象"
    title = metadata.get("title") if isinstance(metadata.get("title"), str) else ""
    author = metadata.get("author") if isinstance(metadata.get("author"), str) else ""
    user_dir = metadata.get("qdUserDir") if isinstance(metadata.get("qdUserDir"), str) else ""
    stored_id = metadata.get("qdBookId")
    if not title.strip() or not author.strip():
        return title, author, user_dir, "元数据缺少书名或作者"
    if stored_id != book_id:
        return title, author, user_dir, "元数据书籍编号与文件名不一致"
    if not user_dir.isdecimal():
        return title, author, user_dir, "元数据用户目录编号无效"
    return title, author, user_dir, None


def _sqlite_family(directory: Path, main_name: str) -> Path | None:
    """确认主数据库文件存在。参数：directory 所在目录、main_name 主文件名。返回：主文件路径；缺失时 None。"""
    main = directory / main_name
    return main if main.is_file() else None


def _cache_has_files(cache_dir: Path) -> bool:
    """判断章节缓存目录中是否有 .qd 文件。参数：cache_dir 缓存目录。返回：存在至少一个缓存文件时为真。"""
    return any(path.is_file() for path in cache_dir.glob("*.qd"))


def _assign_qdconfig(candidate: Path | None, backup_root: Path, qdconfigs: list[Path]) -> Path | None:
    """为一本书确定 QDConfig 数据库。参数：candidate 扫描期预置值、backup_root 书籍所在根、qdconfigs 全部候选。返回：命中的数据库路径。"""
    if candidate is not None:
        return candidate
    text = str(backup_root)
    for path in qdconfigs:
        if str(path).startswith(text):
            return path
    return qdconfigs[0] if len(qdconfigs) == 1 else None


def scan_local_source(root: Path, progress: ProgressCallback = quiet_progress) -> tuple[LocalBook, ...]:
    """扫描用户手动选择的任意目录并发现起点书籍。参数：root 所选目录、progress 进度回调。返回：书籍列表；目录无效时抛出异常。"""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ReaderCacheError(f"所选目录不存在或不是文件夹：{root}")
    qdconfigs: list[Path] = []
    pending: list[tuple[Path, str, dict[str, Any]]] = []
    seen_caches: set[str] = set()

    progress(f"正在扫描目录：{root}")
    for current, dirnames, filenames in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        ordered = sorted(filenames)
        # 1. 收集树中全部 QDConfig 设备标识数据库。
        for name in ordered:
            if name == QDCONFIG_NAME and (Path(current) / name).is_file():
                qdconfigs.append(Path(current) / name)
        # 2. 备份布局：元数据文件所在目录即备份根。
        for name in ordered:
            match = METADATA_NAME.fullmatch(name)
            if match:
                pending.append((Path(current), match.group(1), {"metadata": Path(current) / name}))
        # 3. 原始布局：{书籍ID}.qd 索引与同名缓存子目录并排出现。
        for name in ordered:
            match = INDEX_NAME.fullmatch(name)
            if not match:
                continue
            book_id = match.group(1)
            cache_dir = Path(current) / book_id
            if not cache_dir.is_dir() or not _cache_has_files(cache_dir):
                continue
            key = str(cache_dir)
            if key in seen_caches:
                continue
            seen_caches.add(key)
            pending.append((Path(current), book_id, {"raw_index": Path(current) / name, "raw_cache": cache_dir}))

    books: list[LocalBook] = []
    seen_books: set[tuple[str, str]] = set()
    for backup_root, book_id, extra in pending:
        if (book_id, str(extra.get("raw_cache", backup_root))) in seen_books:
            continue
        seen_books.add((book_id, str(extra.get("raw_cache", backup_root))))
        books.append(_build_book(backup_root, book_id, extra, qdconfigs))
    books.sort(key=lambda item: (item.title, item.book_id))
    if not books:
        progress(
            "提示：没有发现 qidian_book_metadata_*.json 或“数字.qd”索引文件。"
            "请确认选择的是备份目录本身，或从模拟器复制的 QDReader/book 目录。"
        )
    progress(f"扫描完成，发现 {len(books)} 本起点书籍")
    return tuple(books)


def _build_book(backup_root: Path, book_id: str, extra: dict[str, Any], qdconfigs: list[Path]) -> LocalBook:
    """把扫描到的候选整理成书籍记录并核对结构。参数：backup_root 所在目录、book_id 编号、extra 布局证据、qdconfigs 候选库。返回：书籍记录。"""
    raw_index: Path | None = extra.get("raw_index")
    if raw_index is not None:
        cache_dir: Path | None = extra["raw_cache"]
        index_path = raw_index if raw_index.is_file() else None
        user_dir = backup_root.name if NUMERIC_NAME.fullmatch(backup_root.name) else ""
        title, author = RAW_PLACEHOLDER_TITLE.format(book_id=book_id), RAW_PLACEHOLDER_AUTHOR
        layout = "raw"
        metadata_error = None
    else:
        metadata_path: Path = extra["metadata"]
        index_path = _sqlite_family(backup_root / "book_index", f"{book_id}.qd")
        cache_candidate = backup_root / "book_cache" / book_id
        cache_dir = cache_candidate if cache_candidate.is_dir() else None
        title, author, user_dir, metadata_error = _read_metadata_lenient(metadata_path, book_id)
        if not title.strip():
            title = RAW_PLACEHOLDER_TITLE.format(book_id=book_id)
        if not author.strip():
            author = RAW_PLACEHOLDER_AUTHOR
        layout = "backup"

    notes: list[str] = []
    if metadata_error is not None:
        notes.append(metadata_error)
    if index_path is None:
        notes.append(f"缺少章节索引 {book_id}.qd")
    if cache_dir is None or not _cache_has_files(cache_dir):
        notes.append(f"缺少 book_cache/{book_id} 章节缓存")
    if user_dir and not user_dir.isdecimal():
        notes.append("用户目录编号无效")
    qdconfig = _assign_qdconfig(None, backup_root, qdconfigs)
    if qdconfig is None:
        notes.append(QDCONFIG_HINT)
    if not user_dir:
        notes.append("无法识别原用户目录编号（原始布局要求索引所在目录名为数字）")
    ready = index_path is not None and cache_dir is not None and bool(user_dir) and qdconfig is not None
    return LocalBook(
        book_id=book_id,
        title=title,
        author=author,
        user_dir=user_dir,
        layout=layout,
        index_path=index_path,
        cache_dir=cache_dir,
        qdconfig_path=qdconfig,
        ready=ready,
        note="；".join(notes) if notes else READY_NOTE,
    )


def resolve_qdconfig(book: LocalBook, override: Path | None) -> Path:
    """确定导出使用的 QDConfig 数据库。参数：book 扫描书籍、override 用户手动指定的文件。返回：存在的数据库路径。"""
    path = override if override is not None else book.qdconfig_path
    if path is None:
        raise ReaderCacheError(QDCONFIG_HINT)
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ReaderCacheError(f"QDConfig 数据库不存在或不是文件：{path}")
    return path


def load_local_book(book: LocalBook, qdconfig: Path, progress: ProgressCallback = quiet_progress) -> BookDocument:
    """把扫描到的本地书籍解码为统一书籍模型。参数：book 扫描记录、qdconfig 设备标识库、progress 进度回调。返回：完整书籍模型。"""
    # 1. 结构必须在扫描后仍然完整。
    if book.index_path is None or book.cache_dir is None or not book.user_dir.isdecimal():
        raise ReaderCacheError(f"备份结构不完整：{book.note}")
    progress(f"读取章节索引：{book.index_path.name}")
    entries = qidian.read_index(book.index_path)
    files = {path.stem: path for path in book.cache_dir.glob("*.qd") if path.is_file()}
    orphan_ids = set(files) - {entry["chapter_id"] for entry in entries}
    if orphan_ids:
        raise ReaderCacheError(f"起点缓存无法对应章节索引：{sorted(orphan_ids)}")
    if not files:
        raise ReaderCacheError("起点备份中没有章节缓存")

    # 2. 设备标识来自真实 QDConfig 数据库。
    candidates = qidian.read_device_candidates(qdconfig)
    chapters: list[Chapter] = []
    missing: list[dict[str, Any]] = []
    copyright_title: str | None = None
    copyright_author: str | None = None

    # 3. 逐章严格解码；缺失只记录，损坏或未知结构直接报错。
    for entry in entries:
        path = files.get(entry["chapter_id"])
        if path is None:
            missing.append(entry)
            continue
        item = qidian.decode_cached_chapter(path, entry, book.user_dir, candidates)
        if item["chapter_id"] == "-10000" and isinstance(item.get("copyright_metadata"), dict):
            name = item["copyright_metadata"].get("BookName")
            writer = item["copyright_metadata"].get("Author")
            if isinstance(name, str) and name.strip():
                copyright_title = name.strip()
            if isinstance(writer, str) and writer.strip():
                copyright_author = writer.strip()
        chapters.append(
            Chapter(
                chapter_id=item["chapter_id"],
                order=len(chapters) + 1,
                title=item["title"],
                text=item["text"],
                source_file=str(path),
                source_sha256=item["sha256"],
                metadata={
                    key: value
                    for key, value in item.items()
                    if key not in {"text", "source_file", "sha256", "device_source"}
                },
            )
        )
        if len(chapters) == 1 or len(chapters) % 25 == 0:
            progress(f"起点：已校验 {len(chapters)}/{len(files)} 个缓存项")

    # 4. 原始布局没有元数据文件，用版权页的真实书名与作者补齐。
    title, author = book.title, book.author
    if book.layout == "raw":
        if copyright_title:
            title = copyright_title
        if copyright_author:
            author = copyright_author
        if copyright_title is None:
            progress("提示：缓存中没有版权页，书籍标题以编号代替")
    progress(f"起点：{len(chapters)} 个缓存项已校验，目录缺失 {len(missing)} 项")
    return BookDocument(
        channel=Channel.QIDIAN,
        book_id=book.book_id,
        title=title,
        author=author,
        chapters=tuple(chapters),
        expected_count=len(entries),
        missing_chapters=tuple(missing),
        metadata={
            "verified_app_version": "7.9.460",
            "cache_layout": "length-prefixed-v1",
            "source_layout": book.layout,
        },
        diagnostics=("目录项数包含版权页与作者说明；仅代表备份时的目录范围。",),
    )
