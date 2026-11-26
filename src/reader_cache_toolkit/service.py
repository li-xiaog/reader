"""应用服务：统一执行输入校验、渠道解析、策略导出与结果发布。"""

import csv
import json
import shutil
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.factory import create_adapter, create_exporter
from reader_cache_toolkit.models import (
    BookDocument,
    ConversionRequest,
    ConversionResult,
    ExportFormat,
    ProgressCallback,
)


def quiet_progress(message: str) -> None:
    """供无界面调用者忽略进度。参数：message为真实进度消息。返回：无。"""


def validate_book(book: BookDocument, require_complete: bool) -> None:
    """统一校验解析结果。参数：book书籍、require_complete完整性要求。返回：无，错误时抛异常。"""
    if not book.title.strip() or not book.author.strip() or not book.chapters:
        raise ReaderCacheError("书籍缺少真实书名、作者或有效章节")
    ids = [chapter.chapter_id for chapter in book.chapters]
    if len(set(ids)) != len(ids):
        raise ReaderCacheError("解析结果含重复章节编号")
    for chapter in book.chapters:
        if not chapter.title.strip() or "\ufffd" in chapter.title + chapter.text:
            raise ReaderCacheError(f"章节标题为空或含乱码替换字符：{chapter.chapter_id}")
        if not chapter.text.strip() and not chapter.asset_paths:
            raise ReaderCacheError(f"章节既无正文也无原生页面：{chapter.chapter_id}")
    if book.expected_count is not None and book.expected_count < len(book.chapters):
        raise ReaderCacheError("缓存章节数超过真实目录总数，无法确认顺序")
    if require_complete and (
        book.expected_count is None or book.expected_count != len(book.chapters) or book.missing_chapters
    ):
        raise ReaderCacheError("无法确认本地目录全部缓存，已按完整性要求终止")


def write_reports(book: BookDocument, directory: Path, outputs: tuple[Path, ...]) -> Path:
    """写入真实章节索引与不含密钥的核验报告。

    参数：book为解析结果；directory为暂存目录；outputs为实际生成文件。
    返回：JSON核验报告路径。
    """
    chapter_meta = directory / "chapters_meta"
    chapter_meta.mkdir()
    index_path = directory / "index.csv"
    with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["order", "chapter_id", "title", "text_chars", "source_sha256"]
        )
        writer.writeheader()
        for index, chapter in enumerate(book.chapters, 1):
            entry = {
                "order": index,
                "chapter_id": chapter.chapter_id,
                "title": chapter.title,
                "text_chars": len(chapter.text),
                "source_sha256": chapter.source_sha256,
            }
            writer.writerow(entry)
            (chapter_meta / f"{index:04d}.json").write_text(
                json.dumps({**entry, "details": chapter.metadata}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    report = {
        "channel": book.channel,
        "book_id": book.book_id,
        "title": book.title,
        "author": book.author,
        "generated_at": datetime.now(UTC).isoformat(),
        "decoded_count": len(book.chapters),
        "expected_count": book.expected_count,
        "missing_count": None if book.expected_count is None else book.expected_count - len(book.chapters),
        "missing_chapters": book.missing_chapters,
        "diagnostics": book.diagnostics,
        "metadata": book.metadata,
        "replacement_characters": 0,
        "files": [
            {
                "path": path.relative_to(directory).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
            for path in outputs
        ],
    }
    path = directory / "conversion_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class ConversionService:
    """统一转换用例；参数：无；返回：不依赖任何UI的应用服务实例。"""

    def convert(
        self, request: ConversionRequest, progress: ProgressCallback = quiet_progress
    ) -> ConversionResult:
        """完整执行一次本地转换，失败不发布半成品。

        参数：request为不可变请求；progress为真实阶段进度回调。
        返回：转换结果；输入错误、缺章限制或导出失败时抛出真实异常。
        """
        # 1. 校验输入和输出边界，所有格式必须显式选择，已有目录不得覆盖。
        source, destination = request.source.resolve(), request.output_dir.resolve()
        if not source.is_dir():
            raise ReaderCacheError(f"缓存目录不存在：{source}")
        if not request.book_id.isdecimal() or not request.formats:
            raise ReaderCacheError("请输入真实数字书籍编号并至少选择一个格式")
        if len(set(request.formats)) != len(request.formats):
            raise ReaderCacheError("输出格式重复")
        if destination.exists():
            raise FileExistsError(f"输出目录已存在，请选择新目录：{destination}")
        if destination == source or destination in source.parents or destination.is_relative_to(source):
            raise ReaderCacheError("输出目录必须独立于缓存目录，不能是其父目录或子目录")

        # 2. 适配器先完整解析；只有校验通过的数据才进入导出阶段。
        progress(f"读取 {request.channel} 本地缓存")
        book = create_adapter(request.channel).load(request, progress)
        for diagnostic in book.diagnostics:
            progress(f"提示：{diagnostic}")
        return self._publish(book, destination, request.formats, request.require_complete, progress)

    def export_book(
        self,
        book: BookDocument,
        output_dir: Path,
        formats: tuple[ExportFormat, ...],
        require_complete: bool = False,
        progress: ProgressCallback = quiet_progress,
    ) -> ConversionResult:
        """把已解析书籍导出为真实文件，供手动目录导出等入口复用。

        参数：book为解析结果；output_dir为尚不存在的新目录；formats为格式列表；
        require_complete为完整性要求；progress为阶段进度回调。
        返回：转换结果；输出冲突或校验失败时抛出真实异常。
        """
        # 1. 输出必须指向尚未创建的独立目录，格式必须显式且不重复。
        destination = Path(output_dir).expanduser().resolve()
        if destination.exists():
            raise FileExistsError(f"输出目录已存在，请选择新目录：{destination}")
        if not formats:
            raise ReaderCacheError("请至少选择一个导出格式")
        if len(set(formats)) != len(formats):
            raise ReaderCacheError("输出格式重复")
        return self._publish(book, destination, tuple(formats), require_complete, progress)

    def _publish(
        self,
        book: BookDocument,
        destination: Path,
        formats: tuple[ExportFormat, ...],
        require_complete: bool,
        progress: ProgressCallback,
    ) -> ConversionResult:
        """统一校验、暂存导出、写入报告并原子发布。参数：book书籍、destination新目录、formats格式、require_complete完整性要求、progress进度回调。返回：转换结果。"""
        validate_book(book, require_complete)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".reader-export-", dir=destination.parent))
        try:
            # 3. 格式策略在专用暂存目录生成文件，写入核验报告后再发布。
            outputs = []
            for format_name in formats:
                progress(f"正在生成并校验 {format_name.upper()}")
                outputs.extend(create_exporter(format_name).export(book, stage))
            report = write_reports(book, stage, tuple(outputs))
            relative_files = tuple(path.relative_to(stage) for path in outputs)
            report_name = report.name
            if destination.exists():
                raise FileExistsError(f"输出目录在转换过程中被创建：{destination}")
            stage.rename(destination)
        except BaseException:
            # 4. 失败只清理本次由mkdtemp创建的目录，不触碰输入和既有结果。
            if (
                stage.is_dir()
                and stage.parent == destination.parent
                and stage.name.startswith(".reader-export-")
            ):
                shutil.rmtree(stage)
            raise
        progress(f"转换完成：{len(book.chapters)} 个目录项")
        return ConversionResult(
            destination,
            tuple(destination / path for path in relative_files),
            len(book.chapters),
            destination / report_name,
        )
