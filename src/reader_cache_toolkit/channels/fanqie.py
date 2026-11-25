"""番茄小说本地备份适配器，将渠道细节隔离于统一导出流程之外。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from reader_cache_toolkit.codecs.fanqie import (
    decrypt_xhtml,
    inspect_catalog_record,
    load_keys,
    plain_text,
    public_metadata,
    read_serialized_json,
)
from reader_cache_toolkit.codecs.fanqie_epub import prepare_epub_assets
from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.infrastructure.database import open_readonly_snapshot
from reader_cache_toolkit.models import BookDocument, Channel, Chapter, ConversionRequest, ProgressCallback


def locate_layout(source: Path, book_id: str) -> tuple[Path, Path, Path]:
    """定位章节与配套数据根目录。参数：source 用户选择目录、book_id 书籍编号。返回：根目录、章节目录、MMKV 目录。"""
    if not book_id.isascii() or not book_id.isdecimal():
        raise ReaderCacheError("番茄书籍编号必须是十进制数字")
    source = source.resolve()
    if not source.is_dir():
        raise ReaderCacheError("番茄备份目录不存在")
    name = f"prefix_public_{book_id}"
    candidates = [source] if source.name == name else [source / name, source / "book_cache" / name]
    matches = [path for path in candidates if path.is_dir()]
    if len(matches) != 1:
        raise ReaderCacheError("没有找到唯一匹配的 prefix_public 章节目录")
    prefix = matches[0]
    root = prefix.parent.parent if prefix.parent.name == "book_cache" else prefix.parent
    mmkv = root / "private_data" / "files" / "mmkv"
    if not mmkv.is_dir():
        raise ReaderCacheError("备份缺少 private_data/files/mmkv 目录")
    return root, prefix, mmkv


def read_book_database(path: Path, book_id: str) -> dict[str, Any] | None:
    """从临时副本只读查询书籍元信息。参数：path 数据库、book_id 编号。返回：真实书籍记录，无匹配记录返回 None。"""
    if not path.is_file():
        raise ReaderCacheError("指定的番茄阅读数据库不存在")
    # 1. 由公共快照上下文复制并校验主库和日志，避免 SQLite 接触原始文件。
    with open_readonly_snapshot(path) as connection:
        # 2. 检查备份内部的一致性及当前版本的必需字段。
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ReaderCacheError("番茄阅读数据库完整性检查失败")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(t_book)")}
        required = {"book_id", "name", "author"}
        if not required <= columns:
            raise ReaderCacheError("番茄数据库 t_book 缺少 book_id/name/author 字段")
        # 3. 只查询实际存在且无账号信息的字段，不套用其他版本的 schema。
        names = ["book_id", "name", "author"] + [
            name for name in ("serial_count", "last_chapter_title", "is_finish", "source") if name in columns
        ]
        rows = connection.execute(
            f"SELECT {', '.join(names)} FROM t_book WHERE book_id = ?", (book_id,)
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ReaderCacheError("同一番茄数据库包含重复的书籍记录")
        # 4. 书名和作者必须来自实际记录，不填补虚构占位名称。
        result = dict(zip(names, rows[0], strict=True))
        if any(not isinstance(result[name], str) or not result[name].strip() for name in ("name", "author")):
            raise ReaderCacheError("番茄书籍元信息缺少真实书名或作者")
        return result


def load_book_info(root: Path, book_id: str, override: Path | None) -> dict[str, Any]:
    """选择含目标书籍的真实数据库记录。参数：root 备份根目录、book_id 编号、override 显式数据库。返回：无歧义元信息。"""
    databases = (
        [override.resolve()]
        if override
        else sorted(
            path
            for path in (root / "database").rglob("reading_db_*")
            if path.is_file() and not path.name.endswith(("-wal", "-shm", "-journal"))
        )
    )
    if not databases:
        raise ReaderCacheError("备份缺少 reading_db 数据库，请补充数据库以取得真实书名和作者")
    records = []
    for path in databases:
        record = read_book_database(path, book_id)
        if record is not None:
            records.append(record)
    if not records:
        raise ReaderCacheError("阅读数据库中没有目标书籍记录")
    if any(record != records[0] for record in records[1:]):
        raise ReaderCacheError("多份阅读数据库中的书籍信息不一致，请显式选择 reader_db")
    return records[0]


def validate_record(record: dict[str, Any], book_id: str, filename: str) -> None:
    """检查章节身份与必需内容字段。参数：record 元数据、book_id 目标书、filename 来源名。返回：无，错误立即抛出。"""
    for name in ("book_id", "chapter_id", "name", "content"):
        if not isinstance(record.get(name), str) or not record[name].strip():
            raise ReaderCacheError(f"缓存 {filename} 缺少有效 {name} 字段")
    if record["book_id"] != book_id:
        raise ReaderCacheError(f"缓存 {filename} 属于其他书籍")
    if not record["chapter_id"].isascii() or not record["chapter_id"].isdecimal():
        raise ReaderCacheError(f"缓存 {filename} 的章节编号无效")
    version = record.get("key_version")
    if isinstance(version, bool) or not isinstance(version, (str, int)) or not str(version).isdecimal():
        raise ReaderCacheError(f"缓存 {filename} 缺少有效 key_version")


class FanqieAdapter:
    """番茄渠道适配器。参数：无。返回：可供应用服务使用的加载端口。"""

    def load(self, request: ConversionRequest, progress: ProgressCallback) -> BookDocument:
        """加载并校验番茄本地备份。参数：request 转换请求、progress 进度回调。返回：含真实章节与原始 XHTML 的书籍。"""
        # 1. 从配套目录和真实 SQLite 记录取得身份，不依赖本机固定路径。
        if request.channel != Channel.FANQIE:
            raise ReaderCacheError("番茄适配器收到其他渠道的转换请求")
        root, prefix, mmkv = locate_layout(request.source, request.book_id)
        info = load_book_info(root, request.book_id, request.reader_db)
        keys = load_keys(mmkv)
        progress("正在检查番茄章节缓存与目录")
        records: list[tuple[Path, bytes, dict[str, Any]]] = []
        diagnostics: list[str] = []
        counts: list[int] = []
        seen: set[str] = set()
        for path in sorted(prefix.iterdir()):
            if path.is_dir():
                if path.name != "image":
                    raise ReaderCacheError(f"章节目录含未知子目录：{path.name}")
                continue
            raw = path.read_bytes()
            catalog = inspect_catalog_record(raw)
            if catalog is not None:
                kind, count = catalog
                diagnostics.append(f"已识别非正文章节记录：{path.name}（{kind}）")
                if count is not None:
                    counts.append(count)
                continue
            record = read_serialized_json(raw)
            validate_record(record, request.book_id, path.name)
            identifier = record["chapter_id"]
            if identifier in seen:
                raise ReaderCacheError(f"番茄缓存包含重复章节编号：{identifier}")
            seen.add(identifier)
            records.append((path, raw, record))
        if not records:
            raise ReaderCacheError("番茄备份中没有正文章节记录")
        if len(counts) > 1:
            raise ReaderCacheError("存在多个 ChapterItem 目录，无法确定权威章节总数")
        expected_count = counts[0] if counts else None
        if expected_count is not None and len(records) > expected_count:
            raise ReaderCacheError("正文章节数量超过备份目录记录总数，目录与正文不一致")

        # 2. 严格解码全部实际缓存，缺失密钥或任意损坏均终止，不静默跳章。
        records.sort(key=lambda item: int(item[2]["chapter_id"]))
        chapters: list[Chapter] = []
        assets: dict[str, bytes] = {}
        for order, (path, raw, record) in enumerate(records, start=1):
            version = str(record["key_version"])
            if version not in keys:
                raise ReaderCacheError(f"章节 {record['chapter_id']} 缺少对应的密钥版本 {version}")
            try:
                xhtml = decrypt_xhtml(record["content"], keys[version])
                text = plain_text(xhtml)
            except ReaderCacheError as exc:
                raise ReaderCacheError(f"章节 {record['chapter_id']} 解析失败：{exc}") from exc
            asset_path = f"xhtml/{record['chapter_id']}.xhtml"
            assets[asset_path] = xhtml
            chapters.append(
                Chapter(
                    chapter_id=record["chapter_id"],
                    order=order,
                    title=record["name"],
                    text=text,
                    asset_paths=(asset_path,),
                    source_file=str(path),
                    source_sha256=hashlib.sha256(raw).hexdigest(),
                    metadata=public_metadata(record),
                )
            )
            if order == 1 or order % 50 == 0 or order == len(records):
                progress(f"已解析番茄章节 {order}/{len(records)}")

        # 3. 原样保留 XHTML 相对图片资源，供统一 XHTML 和 EPUB 导出策略使用。
        image_directory = prefix / "image"
        if image_directory.is_dir():
            for path in sorted(image_directory.rglob("*")):
                if path.is_file():
                    assets[f"xhtml/image/{path.relative_to(image_directory).as_posix()}"] = path.read_bytes()

        epub_assets: dict[str, bytes] = {}
        epub_audit: dict[str, dict[str, object]] = {}
        epub_error = None
        try:
            epub_assets, epub_audit = prepare_epub_assets(tuple(chapters), assets)
        except ReaderCacheError as error:
            epub_error = str(error)
            diagnostics.append(f"原始正文可读取，但 EPUB 资源无法完整生成：{epub_error}")
        image_count = sum(len(item["images"]) for item in epub_audit.values())
        if image_count:
            diagnostics.append(
                f"EPUB 已按真实缓存映射保留 {image_count} 处插图；TXT/Markdown 为纯文本，不包含插图。"
            )

        # 4. 完整性只相对于备份中的真实目录，缺少可靠目录时明确标注未知。
        if expected_count is None:
            diagnostics.append("未找到可验证的 ChapterItem 目录，总章节数未知；不能确认缓存完整。")
        elif expected_count > len(chapters):
            diagnostics.append(
                f"真实目录共 {expected_count} 条，本地仅有 {len(chapters)} 条；未解析目录条目身份，无法列出缺失章节编号。"
            )
        diagnostics.append("章节按缓存记录的真实 chapter_id 数值排序；未反序列化完整目录顺序。")
        if request.require_complete and (expected_count is None or expected_count != len(chapters)):
            raise ReaderCacheError("番茄缓存未能通过完整备份目录校验，严格完整模式已终止")
        return BookDocument(
            channel=Channel.FANQIE,
            book_id=request.book_id,
            title=info["name"],
            author=info["author"],
            chapters=tuple(chapters),
            assets=assets,
            epub_assets=epub_assets,
            expected_count=expected_count,
            metadata={
                "book_info": info,
                "expected_count_source": "ChapterItem ArrayList 目录" if counts else None,
                "chapter_order_source": "chapter_id",
                "source_root": str(root),
                "epub_native_assets": True,
                "epub_error": epub_error,
                "epub_normalization": epub_audit,
            },
            diagnostics=tuple(diagnostics),
        )
