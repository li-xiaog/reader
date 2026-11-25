"""起点适配器：将本地章节缓存转换为统一书籍模型。"""

from reader_cache_toolkit.codecs import qidian
from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.models import BookDocument, Channel, Chapter, ConversionRequest, ProgressCallback


class QidianAdapter:
    """隔离起点文件布局与字段差异；参数：无；返回：渠道适配器实例。"""

    def load(self, request: ConversionRequest, progress: ProgressCallback) -> BookDocument:
        """读取真实缓存并检查目录覆盖率。

        参数：request为转换请求；progress为阶段反馈函数。
        返回：包含真实章节、缺失目录和原始版权信息的书籍模型。
        """
        # 1. 根据备份元数据识别书籍和原用户目录，不猜测账号。
        root = request.source.resolve()
        metadata = qidian.read_metadata(root, request.book_id)
        entries = qidian.read_index(root / "book_index" / f"{request.book_id}.qd")
        cache = root / "book_cache" / request.book_id
        if not cache.is_dir():
            raise FileNotFoundError(cache)
        files = {path.stem: path for path in cache.glob("*.qd") if path.is_file()}
        orphan_ids = set(files) - {entry["chapter_id"] for entry in entries}
        if orphan_ids:
            raise ReaderCacheError(f"起点缓存无法对应章节索引：{sorted(orphan_ids)}")
        if not files:
            raise ReaderCacheError("起点备份中没有章节缓存")

        # 2. 读取配置库的真实历史设备标识，在内存中使用且不写入导出报告。
        candidates = qidian.read_device_candidates(root / "database" / "databases" / "QDConfig")
        user_id = request.account_id or metadata["qdUserDir"]
        chapters = []
        missing = []

        # 3. 逐章严格解码；缺失只记录，损坏或未知结构直接报错。
        for entry in entries:
            path = files.get(entry["chapter_id"])
            if path is None:
                missing.append(entry)
                continue
            item = qidian.decode_cached_chapter(path, entry, user_id, candidates)
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

        # 4. 返回完整范围信息，由公共服务决定是否允许导出缓存部分。
        progress(f"起点：{len(chapters)} 个缓存项已校验，目录缺失 {len(missing)} 项")
        return BookDocument(
            channel=Channel.QIDIAN,
            book_id=request.book_id,
            title=metadata["title"],
            author=metadata["author"],
            chapters=tuple(chapters),
            expected_count=len(entries),
            missing_chapters=tuple(missing),
            metadata={"verified_app_version": "7.9.460", "cache_layout": "length-prefixed-v1"},
            diagnostics=("目录项数包含版权页与作者说明；仅代表备份时的目录范围。",),
        )
