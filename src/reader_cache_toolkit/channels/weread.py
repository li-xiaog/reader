"""微信读书本地缓存适配器：严格核对元数据并保留全部原生电子书资产。"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from lxml import etree

from reader_cache_toolkit.codecs.weread import (
    ChapterData,
    discover_bundles,
    normalize_archive_path,
    parse_bundle,
)
from reader_cache_toolkit.infrastructure.database import open_readonly_snapshot
from reader_cache_toolkit.models import (
    BookDocument,
    Channel,
    Chapter,
    ConversionRequest,
    ProgressCallback,
)


def _text(value: object, label: str, required: bool = False) -> str:
    """严格读取数据库文本，不替换损坏字节或伪造缺失字段。

    参数:
        value: 数据库原始值。
        label: 错误提示字段名。
        required: 是否要求非空文本。
    返回:
        str: 严格 UTF-8 解码的真实值；非法值或缺失必需值时抛出异常。
    """

    result = value.decode("utf-8") if isinstance(value, bytes) else "" if value is None else str(value)
    if "\ufffd" in result or (required and not result.strip()):
        raise ValueError(f"{label} 缺失或包含乱码替换字符")
    return result


def _locate_source(source: Path, book_id: str) -> tuple[Path, tuple[Path, ...]]:
    """定位单书缓存并确定允许查找元数据的本地备份目录。

    参数:
        source: 单书缓存目录或备份根目录。
        book_id: 请求的真实书籍 ID。
    返回:
        tuple: 单书目录以及有限范围的备份元数据候选目录。
    """

    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("微信读书输入必须是缓存目录")
    candidates = [
        path
        for path in (source, source / "book_cache" / book_id, source / book_id)
        if path.is_dir() and any(path.glob("*.res"))
    ]
    if len(candidates) != 1:
        raise ValueError("无法唯一定位微信读书单书 RES 目录，请直接选择书籍缓存目录")
    cache_dir = candidates[0]
    roots = [source, cache_dir.parent]
    if cache_dir.parent.name == "book_cache":
        roots.append(cache_dir.parent.parent)
    return cache_dir, tuple(dict.fromkeys(path.resolve() for path in roots))


def _locate_database(explicit: Path | None, roots: tuple[Path, ...]) -> Path:
    """定位 WRReader，只接受用户指定路径或备份目录内的唯一数据库。

    参数:
        explicit: 可选的显式 WRReader 路径。
        roots: 已确定的本地备份元数据范围。
    返回:
        Path: 存在且唯一的数据库绝对路径；缺失或歧义时抛出异常。
    """

    if explicit is not None:
        path = explicit.resolve(strict=True)
        if not path.is_file():
            raise ValueError("指定的 WRReader 不是文件")
        return path
    relatives = ("database/WRReader", "database/databases/WRReader", "databases/WRReader", "WRReader")
    candidates = {
        path.resolve() for root in roots for relative in relatives if (path := root / relative).is_file()
    }
    if len(candidates) != 1:
        raise ValueError("备份中没有唯一 WRReader 数据库，请显式指定 reader_db")
    return candidates.pop()


def _recorded_account(explicit: str | None, roots: tuple[Path, ...], book_id: str) -> str:
    """从显式参数或备份明确的 VID 字段读取账号，不猜测数据库内部账号散列。

    参数:
        explicit: 用户指定的账号 VID。
        roots: 允许读取备份说明文件的目录。
        book_id: 当前请求的书籍 ID，用于检查备份说明的身份。
    返回:
        str: 唯一的数字 VID；无记录或记录冲突时抛出异常且不泄露具体值。
    """

    values: set[str] = set()
    if explicit is not None:
        values.add(str(explicit).strip())
    names = ("backup_manifest.json", "manifest.json", "metadata.json", "weread_metadata.json")
    for root in roots:
        for name in names:
            path = root / name
            if not path.is_file():
                continue
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(document, dict):
                raise ValueError(f"备份元数据必须是对象：{path.name}")
            if "channel" in document and document["channel"] != Channel.WEREAD.value:
                raise ValueError("备份元数据的渠道与微信读书请求冲突")
            if "book_id" in document and str(document["book_id"]) != book_id:
                raise ValueError("备份元数据的书籍 ID 与请求冲突")
            objects = [document]
            objects.extend(
                document[key] for key in ("account", "weread") if isinstance(document.get(key), dict)
            )
            for item in objects:
                for key in ("account_id", "vid", "accountId"):
                    if key in item and item[key] not in (None, ""):
                        values.add(str(item[key]).strip())
    if not values:
        raise ValueError("缺少微信读书账号 VID；请显式填写或提供明确记录 VID 的备份元数据")
    if len(values) != 1:
        raise ValueError("显式账号与备份元数据中的账号记录冲突")
    account = values.pop()
    if not account.isascii() or not account.isdecimal() or int(account) <= 0:
        raise ValueError("微信读书账号 VID 必须是有效的正整数")
    return account


def _load_catalog(database: Path, book_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """通过临时只读快照读取元数据和章节目录，避免修改源库共享内存。

    参数:
        database: WRReader 的实际路径。
        book_id: 用户要求转换的书籍标识。
    返回:
        tuple: 书籍元数据字典和按章节顺序排列的真实目录项。
    """

    with open_readonly_snapshot(database) as connection:
        connection.text_factory = bytes
        rows = connection.execute(
            "SELECT title,author,publisher,isbn,totalWords,chapterSize FROM Book WHERE bookId=?",
            (book_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("WRReader 中书籍元数据缺失或存在重复记录")
        row = rows[0]
        metadata = {
            "title": _text(row[0], "书名", True),
            "author": _text(row[1], "作者"),
            "publisher": _text(row[2], "出版社"),
            "isbn": _text(row[3], "ISBN"),
            "total_words": row[4],
            "book_chapter_size": row[5],
        }
        chapter_rows = connection.execute(
            "SELECT chapterUid,chapterIdx,title,files FROM Chapter WHERE bookId=? ORDER BY chapterIdx,chapterUid",
            (book_id,),
        ).fetchall()
    catalog: list[dict[str, Any]] = []
    seen_uids: set[int] = set()
    seen_orders: set[int] = set()
    for uid, index, title, raw_files in chapter_rows:
        if uid is None or index is None or uid in seen_uids or index in seen_orders:
            raise ValueError("WRReader 章节 ID 或顺序缺失、重复")
        seen_uids.add(uid)
        seen_orders.add(index)
        files_value = _text(raw_files, "章节文件列表")
        files = json.loads(files_value) if files_value else None
        if files is not None and (
            not isinstance(files, list) or not all(isinstance(name, str) for name in files)
        ):
            raise ValueError("WRReader 章节资源列表结构不合法")
        catalog.append(
            {
                "chapter_id": str(uid),
                "order": int(index),
                "title": _text(title, "章节标题", True),
                "files": tuple(normalize_archive_path(name) for name in files) if files else (),
            }
        )
    return metadata, catalog


def _check_catalog(
    chapters: tuple[Chapter, ...], catalog: list[dict[str, Any]], declared_count: object
) -> tuple[int | None, tuple[dict[str, Any], ...], tuple[str, ...]]:
    """交叉核对缓存与数据库目录，只有可靠匹配时才声明预期总章节数。

    参数:
        chapters: 已成功解码的真实章节。
        catalog: 数据库目录。
        declared_count: Book.chapterSize 原始记录。
    返回:
        tuple: 可靠的预期数量或 None、已知缺失章节，以及完整性诊断说明。
    """

    by_uid = {entry["chapter_id"]: entry for entry in catalog}
    unmatched: list[str] = []
    for chapter in chapters:
        entry = by_uid.get(chapter.chapter_id)
        if entry is None:
            unmatched.append(chapter.chapter_id)
            continue
        if entry["order"] != chapter.order or entry["title"] != chapter.title:
            raise ValueError(f"缓存与 WRReader 目录的章节顺序或标题冲突：{chapter.chapter_id}")
        if entry["files"] and entry["files"] != chapter.asset_paths:
            raise ValueError(f"缓存与 WRReader 章节文件列表冲突：{chapter.chapter_id}")
    found = {chapter.chapter_id for chapter in chapters}
    missing = tuple(
        {key: entry[key] for key in ("chapter_id", "order", "title")}
        for entry in catalog
        if entry["chapter_id"] not in found
    )
    diagnostics: list[str] = []
    if not catalog:
        diagnostics.append("WRReader 没有该书的真实 Chapter 目录，无法验证全书完整性。")
    if unmatched:
        diagnostics.append(
            f"有 {len(unmatched)} 个缓存章节无法在 WRReader 目录中匹配，无法可靠核验全书范围。"
        )
    if type(declared_count) is not int or declared_count <= 0:
        diagnostics.append("Book.chapterSize 未提供有效总章节数，无法可靠核验全书范围。")
    elif declared_count != len(catalog):
        diagnostics.append(
            f"Book.chapterSize={declared_count}，真实 Chapter 目录={len(catalog)}，记录不一致。"
        )
    expected = declared_count if not diagnostics else None
    return expected, missing, tuple(diagnostics)


def _check_resources(assets: dict[str, bytes]) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    """检查本地图片、样式及字体依赖，明确报告原始远程资源的离线限制。

    参数:
        assets: 已完成确定性规范化的资源路径和字节映射。
    返回:
        tuple: 远程资源诊断和真实缺失的本地依赖；不安全引用直接抛出异常。
    """

    references: list[tuple[str, str]] = []
    for name, data in assets.items():
        if Path(name).suffix.lower() in {".xhtml", ".html", ".htm"}:
            root = etree.fromstring(
                data, etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
            )
            for node in root.iter():
                if not isinstance(node.tag, str):
                    continue
                local_name = etree.QName(node).localname.lower()
                if local_name in {"img", "script", "audio", "video", "source"} and node.get("src"):
                    references.append((name, node.get("src")))
                elif local_name == "link" and node.get("href"):
                    references.append((name, node.get("href")))
                elif local_name == "image":
                    href = node.get("href") or node.get("{http://www.w3.org/1999/xlink}href")
                    if href:
                        references.append((name, href))
        elif name.lower().endswith(".css"):
            stylesheet = data.decode("utf-8-sig")
            references.extend(
                (name, match.group(1).strip().strip("\"'"))
                for match in re.finditer(r"url\(\s*([^)]*?)\s*\)", stylesheet, re.IGNORECASE)
            )
    remote: set[str] = set()
    missing: list[dict[str, str]] = []
    for source, reference in references:
        parts = urlsplit(reference)
        if parts.scheme in {"http", "https"} or parts.netloc:
            remote.add(reference)
            continue
        if parts.scheme == "data" or not parts.path:
            continue
        if parts.scheme or parts.path.startswith("/"):
            raise ValueError(f"原生资源使用不受支持的本地引用：{source}")
        target = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(parts.path)))
        if target.startswith("../"):
            raise ValueError(f"微信读书本地资源引用越界：{source}")
        if target not in assets:
            item = {"source": source, "reference": reference, "target": target}
            if item not in missing:
                missing.append(item)
    if remote:
        return (
            f"原生 XHTML/CSS 引用了 {len(remote)} 个远程资源，离线资源不完整；已保留原始引用，未联网下载。",
        ), tuple(missing)
    return (), tuple(missing)


def _include_loose_assets(
    cache_dir: Path, assets: dict[str, bytes]
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...], dict[str, dict[str, str]]]:
    """读取缓存中确实存在的散装依赖资源，保留无法找到的缺失清单。

    参数:
        cache_dir: 单书原始缓存目录。
        assets: 当前资源映射，找到真实散装文件时加入此映射。
    返回:
        tuple: 远程诊断、仍然缺失的本地资源、已加入文件的来源与 SHA-256。
    """

    loaded: dict[str, dict[str, str]] = {}
    while True:
        diagnostics, missing = _check_resources(assets)
        added = False
        for item in missing:
            target = item["target"]
            candidates = [path for path in (cache_dir / target, cache_dir / "0" / target) if path.is_file()]
            if not candidates:
                continue
            sources: list[tuple[Path, bytes]] = []
            for candidate in candidates:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(cache_dir):
                    raise ValueError(f"散装资源链接指向书籍目录之外：{target}")
                sources.append((resolved, resolved.read_bytes()))
            if len({data for _, data in sources}) != 1:
                raise ValueError(f"多个散装资源的内容冲突：{target}")
            path, data = sources[0]
            assets[target] = data
            loaded[target] = {"source_file": str(path), "sha256": hashlib.sha256(data).hexdigest()}
            added = True
        if not added:
            return diagnostics, missing, loaded


class WereadAdapter:
    """微信读书渠道适配器；参数：无构造参数；返回：保留原生资产的书籍模型。"""

    def load(self, request: ConversionRequest, progress: ProgressCallback) -> BookDocument:
        """读取微信读书缓存、核对目录并生成统一书籍对象。

        参数:
            request: 包含本地路径、书籍 ID 和可选账号及数据库的转换请求。
            progress: 输出不含账号敏感值的进度信息回调。
        返回:
            BookDocument: 已解码真实章节、原始资源、可靠完整性范围和诊断。
        """

        # 1. 定位本地缓存、数据库和明确记录的账号，禁止推测身份。
        if request.channel != Channel.WEREAD:
            raise ValueError("微信读书适配器收到其他渠道的转换请求")
        cache_dir, roots = _locate_source(request.source, request.book_id)
        database = _locate_database(request.reader_db, roots)
        account_id = _recorded_account(request.account_id, roots, request.book_id)
        metadata, catalog = _load_catalog(database, request.book_id)
        bundles = discover_bundles(cache_dir)
        alias_count = sum(1 for path in cache_dir.glob("*.res") if path.is_file() or path.is_symlink()) - len(
            bundles
        )
        progress(f"微信读书：找到 {len(bundles)} 个真实章节包，开始读取。")

        # 2. 完整读取所有章节包和资源；重复内容允许合并，任何冲突直接报错。
        assets: dict[str, bytes] = {}
        normalized_assets: dict[str, dict[str, str | int]] = {}
        decoded: dict[int, tuple[ChapterData, Path, str]] = {}
        for bundle_index, path in enumerate(bundles, 1):
            parsed = parse_bundle(path, account_id, request.book_id)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            for name, data in parsed.assets.items():
                if name in assets and assets[name] != data:
                    raise ValueError(f"不同章节包含内容冲突的同名资源：{name}")
                assets[name] = data
            for name, audit in parsed.normalized_assets.items():
                if name in normalized_assets and normalized_assets[name] != audit:
                    raise ValueError(f"同名 XHTML 资源的原始内容存在冲突：{name}")
                normalized_assets[name] = audit
            for chapter in parsed.chapters:
                previous = decoded.get(chapter.uid)
                if previous is not None and previous[0] != chapter:
                    raise ValueError(f"不同章节包含互相冲突的 UID：{chapter.uid}")
                if previous is None:
                    decoded[chapter.uid] = (chapter, path, digest)
            progress(f"微信读书：已读取 {bundle_index}/{len(bundles)} 个章节包。")

        # 3. 按真实目录顺序建立章节模型，并保留纯图片页面的原始资源。
        ordered = sorted(decoded.values(), key=lambda item: (item[0].index, item[0].uid))
        if len({item[0].index for item in ordered}) != len(ordered):
            raise ValueError("微信读书缓存中不同章节使用了重复阅读顺序")
        chapters = tuple(
            Chapter(
                chapter_id=str(chapter.uid),
                order=chapter.index,
                title=chapter.title,
                text=chapter.text,
                asset_paths=tuple(chapter.source_file.split("|")),
                source_file=str(path),
                source_sha256=digest,
                metadata={
                    "text_extraction": "original_xhtml",
                    "text_is_empty": not bool(chapter.text.strip()),
                },
            )
            for chapter, path, digest in ordered
        )
        if not chapters:
            raise ValueError("没有成功读取的微信读书章节")

        # 4. 核对完整性证据并返回模型；最终 require_complete 由应用服务统一决定。
        expected, missing, diagnostics = _check_catalog(chapters, catalog, metadata["book_chapter_size"])
        resource_diagnostics, missing_assets, loose_assets = _include_loose_assets(cache_dir, assets)
        missing_diagnostics = ()
        if missing_assets:
            missing_diagnostics = (
                f"已成功读取正文，但本地缓存缺少 {len(missing_assets)} 个资源引用目标；"
                "TXT/Markdown 可导出真实正文，EPUB 应拒绝生成，XHTML 仅保留现有资源并列明缺失。",
            )
        normalization_diagnostics = ()
        if normalized_assets:
            removed_count = sum(int(item["removed_attribute_count"]) for item in normalized_assets.values())
            normalization_diagnostics = (
                f"已在 {len(normalized_assets)} 个 XHTML 资源中去重 {removed_count} 个值完全相同的重复 data-* 属性；"
                "已记录原始与输出 SHA-256，原始备份未修改。",
            )
        alias_diagnostics = ()
        if alias_count:
            alias_diagnostics = (
                f"已核对并去重 {alias_count} 个单章别名副本，复合包的文件名、文件头和目录仍完整校验。",
            )
        progress(f"微信读书：已读取 {len(chapters)} 个章节，保留 {len(assets)} 个原始资源。")
        return BookDocument(
            channel=Channel.WEREAD,
            book_id=request.book_id,
            title=metadata["title"],
            author=metadata["author"],
            chapters=chapters,
            assets=assets,
            expected_count=expected,
            missing_chapters=missing,
            metadata={
                **metadata,
                "reader_db": str(database),
                "source": str(cache_dir),
                "epub_native_assets": True,
                "catalog_count": len(catalog),
                "bundle_count": len(bundles),
                "normalized_assets": normalized_assets,
                "remote_resources_present": bool(resource_diagnostics),
                "missing_assets": missing_assets,
                "loose_assets": loose_assets,
                "duplicate_alias_count": alias_count,
            },
            diagnostics=diagnostics
            + alias_diagnostics
            + normalization_diagnostics
            + resource_diagnostics
            + missing_diagnostics,
        )
