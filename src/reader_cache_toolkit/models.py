"""渠道适配器、应用服务与导出策略之间的统一领域模型。"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class Channel(StrEnum):
    """支持的真实数据渠道；参数：枚举值；返回：稳定渠道标识。"""

    FANQIE = "fanqie"
    QIDIAN = "qidian"
    WEREAD = "weread"


class ExportFormat(StrEnum):
    """支持的输出格式；参数：枚举值；返回：稳定格式标识。"""

    TXT = "txt"
    MARKDOWN = "md"
    EPUB = "epub"
    XHTML = "xhtml"


@dataclass(frozen=True, slots=True)
class ConversionRequest:
    """转换参数；参数：渠道、输入、书籍ID、输出及可选账号和数据库；返回：不可变请求。"""

    channel: Channel
    source: Path
    book_id: str
    output_dir: Path
    formats: tuple[ExportFormat, ...] = (ExportFormat.TXT, ExportFormat.EPUB)
    account_id: str | None = None
    reader_db: Path | None = None
    require_complete: bool = False


@dataclass(frozen=True, slots=True)
class Chapter:
    """真实章节；参数：编号、顺序、标题、正文、资源路径和源信息；返回：不可变章节。"""

    chapter_id: str
    order: int
    title: str
    text: str
    asset_paths: tuple[str, ...] = ()
    source_file: str = ""
    source_sha256: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BookDocument:
    """已解析书籍；参数：身份、章节、资源、已知总数、缺失项及诊断；返回：统一书籍模型。"""

    channel: Channel
    book_id: str
    title: str
    author: str
    chapters: tuple[Chapter, ...]
    assets: dict[str, bytes] = field(default_factory=dict)
    expected_count: int | None = None
    missing_chapters: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    epub_assets: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """转换结果；参数：输出目录、文件、章节数和报告；返回：供CLI和GUI展示的结果。"""

    output_dir: Path
    files: tuple[Path, ...]
    chapter_count: int
    report_path: Path


type ProgressCallback = Callable[[str], None]


class ChannelAdapter(Protocol):
    """渠道适配端口；参数：转换请求；返回：与渠道格式无关的书籍。"""

    def load(self, request: ConversionRequest, progress: ProgressCallback) -> BookDocument:
        """解析本地备份。参数：request转换请求、progress进度回调。返回：完整校验后的书籍。"""
        ...


class Exporter(Protocol):
    """输出策略端口；参数：书籍与临时输出目录；返回：实际生成的文件。"""

    def export(self, book: BookDocument, directory: Path) -> tuple[Path, ...]:
        """导出真实内容。参数：book书籍、directory独立输出目录。返回：生成的文件路径。"""
        ...
