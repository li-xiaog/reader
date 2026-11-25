// 起点备份读取器：扫描标准备份目录、核对结构并解码为统一书籍模型。

package com.readercache.mobile.qidian

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.net.Uri
import com.readercache.mobile.model.BookDocument
import com.readercache.mobile.model.Chapter
import com.readercache.mobile.model.ProgressCallback
import com.readercache.mobile.model.ReaderCacheError
import com.readercache.mobile.saf.SafEntry
import com.readercache.mobile.saf.SafTree
import java.io.File
import org.json.JSONObject

/** 扫描到的备份书籍及结构检查结果。 */
data class BackupInfo(
    val bookId: String,
    val title: String,
    val author: String,
    val qdUserDir: String,
    val ready: Boolean,
    val statusNote: String,
)

/** 章节索引行。 */
data class IndexEntry(val chapterId: String, val title: String, val isVip: Any?, val volume: Any?, val order: Any?)

object QidianBackupReader {
    private val METADATA_PATTERN = Regex("""^qidian_book_metadata_(\d+)\.json$""")

    /** 扫描备份目录根级元数据并核对配套文件。 */
    fun scan(context: Context, treeUri: Uri): List<BackupInfo> {
        val root = SafTree.rootChildren(context, treeUri)
        val books = mutableListOf<BackupInfo>()
        for ((name, entry) in root) {
            val match = METADATA_PATTERN.find(name) ?: continue
            val bookId = match.groupValues[1]
            val metadata = readMetadata(context, entry, bookId)
            val note = checkStructure(context, treeUri, root, bookId)
            books.add(
                BackupInfo(
                    bookId = bookId,
                    title = metadata.optString("title", ""),
                    author = metadata.optString("author", ""),
                    qdUserDir = metadata.optString("qdUserDir", ""),
                    ready = note == null,
                    statusNote = note ?: "结构完整 · 可导出",
                )
            )
        }
        return books.sortedBy { it.title }
    }

    /** 读取并校验起点备份元数据，不推测缺失信息。 */
    private fun readMetadata(context: Context, entry: SafEntry, bookId: String): JSONObject {
        val metadata = JSONObject(String(SafTree.readBytes(context, entry.documentUri), Charsets.UTF_8))
        for (key in listOf("title", "author", "qdBookId", "qdUserDir")) {
            val value = metadata.opt(key)
            if (value !is String || value.isBlank()) {
                throw ReaderCacheError("备份元数据缺少有效字段：$key")
            }
        }
        if (metadata.getString("qdBookId") != bookId || !metadata.getString("qdUserDir").all { it in '0'..'9' }) {
            throw ReaderCacheError("书籍编号或备份用户目录编号不匹配")
        }
        return metadata
    }

    /** 核对索引、章节缓存与 QDConfig 数据库是否齐备。 */
    private fun checkStructure(context: Context, treeUri: Uri, root: Map<String, SafEntry>, bookId: String): String? {
        val bookIndex = root["book_index"]
            ?.takeIf { it.isDirectory }
            ?.let { SafTree.children(context, treeUri, it.documentId) }
            ?: return "缺少 book_index 目录"
        if (bookIndex.keys.none { it == "$bookId.qd" || it.startsWith("$bookId.qd-") }) {
            return "缺少章节索引 $bookId.qd"
        }
        val bookCacheDir = root["book_cache"]
            ?.takeIf { it.isDirectory }
            ?.let { SafTree.children(context, treeUri, it.documentId) }?.get(bookId)
            ?: return "缺少 book_cache/$bookId 目录"
        val cacheFiles = SafTree.children(context, treeUri, bookCacheDir.documentId)
        if (cacheFiles.keys.none { it.endsWith(".qd") }) {
            return "book_cache/$bookId 中没有 .qd 章节缓存"
        }
        val databases = root["database"]
            ?.takeIf { it.isDirectory }
            ?.let { SafTree.children(context, treeUri, it.documentId) }?.get("databases")
            ?.takeIf { it.isDirectory }
            ?.let { SafTree.children(context, treeUri, it.documentId) }
            ?: return "缺少 database/databases 目录"
        if (databases.keys.none { it == "QDConfig" || it.startsWith("QDConfig-") }) {
            return "缺少 QDConfig 设备标识数据库（解密必需）"
        }
        return null
    }

    /** 解析完整备份目录为统一书籍模型；逐章严格校验，缺失只记录。 */
    fun loadBook(context: Context, treeUri: Uri, info: BackupInfo, progress: ProgressCallback): BookDocument {
        // 1. 备份根目录与元数据。
        val root = SafTree.rootChildren(context, treeUri)
        val metadataEntry = root.entries.firstOrNull { METADATA_PATTERN.matches(it.key) }
            ?: throw ReaderCacheError("备份目录中没有起点元数据文件")
        val metadata = readMetadata(context, metadataEntry.value, info.bookId)
        val userId = metadata.getString("qdUserDir")

        // 2. 章节索引与设备标识均来自备份内的只读快照。
        val indexEntries = readIndexFromBackup(context, treeUri, root, info.bookId)
        val candidates = readDeviceCandidatesFromBackup(context, treeUri, root)

        // 3. 逐章严格解码；缺失只记录，损坏或未知结构直接报错。
        val cacheEntry = root["book_cache"]?.takeIf { it.isDirectory }
            ?: throw ReaderCacheError("缺少 book_cache 目录")
        val bookCache = SafTree.children(context, treeUri, cacheEntry.documentId)[info.bookId]
            ?.takeIf { it.isDirectory }
            ?: throw ReaderCacheError("book_cache 目录中没有 ${info.bookId} 子目录")
        val files = SafTree.children(context, treeUri, bookCache.documentId)
            .filterValues { !it.isDirectory && it.name.endsWith(".qd") }
        val orphanIds = files.keys.map { it.removeSuffix(".qd") }.toSet() -
            indexEntries.map { it.chapterId }.toSet()
        if (orphanIds.isNotEmpty()) {
            throw ReaderCacheError("起点缓存无法对应章节索引：${orphanIds.sorted()}")
        }
        if (files.isEmpty()) {
            throw ReaderCacheError("起点备份中没有章节缓存")
        }

        val chapters = mutableListOf<Chapter>()
        val missing = mutableListOf<Map<String, Any?>>()
        for ((position, item) in indexEntries.withIndex()) {
            val cacheEntryItem = files["${item.chapterId}.qd"]
            if (cacheEntryItem == null) {
                missing.add(
                    mapOf(
                        "chapter_id" to item.chapterId,
                        "title" to item.title,
                        "is_vip" to item.isVip,
                        "volume" to item.volume,
                        "order" to item.order,
                    )
                )
                continue
            }
            val raw = SafTree.readBytes(context, cacheEntryItem.documentUri)
            val decoded = decodeCachedChapter(raw, item.chapterId, item.title, userId, candidates)
            val chapterMetadata = mutableMapOf<String, Any?>(
                "chapter_id" to item.chapterId,
                "title" to item.title,
                "is_vip" to item.isVip,
                "volume" to item.volume,
                "order" to item.order,
                "trailing_bytes" to decoded.trailingBytes,
            )
            decoded.copyrightMetadata?.let { chapterMetadata["copyright_metadata"] = it }
            chapters.add(
                Chapter(
                    chapterId = item.chapterId,
                    title = item.title,
                    text = decoded.text,
                    sourceSha256 = decoded.sourceSha256,
                    metadata = chapterMetadata,
                )
            )
            if (chapters.size == 1 || chapters.size % 25 == 0) {
                progress("起点：已校验 ${chapters.size}/${files.size} 个缓存项")
            }
        }

        // 4. 返回完整范围信息，由导出引擎决定是否允许导出缓存部分。
        progress("起点：${chapters.size} 个缓存项已校验，目录缺失 ${missing.size} 项")
        return BookDocument(
            bookId = info.bookId,
            title = metadata.getString("title"),
            author = metadata.getString("author"),
            chapters = chapters,
            expectedCount = indexEntries.size,
            missingChapters = missing,
            metadata = mapOf(
                "verified_app_version" to "7.9.460",
                "cache_layout" to "length-prefixed-v1",
            ),
            diagnostics = listOf("目录项数包含版权页与作者说明；仅代表备份时的目录范围。"),
        )
    }

    /** 复制索引数据库到私有缓存目录并读取章节顺序。 */
    private fun readIndexFromBackup(context: Context, treeUri: Uri, root: Map<String, SafEntry>, bookId: String): List<IndexEntry> {
        val bookIndex = root["book_index"]?.takeIf { it.isDirectory }
            ?: throw ReaderCacheError("缺少 book_index 目录")
        val indexDir = SafTree.children(context, treeUri, bookIndex.documentId)
        val tempDir = File(context.cacheDir, "qd-index-${System.nanoTime()}")
        try {
            val main = copySqliteFamily(context, indexDir, "$bookId.qd", tempDir)
                ?: throw ReaderCacheError("缺少章节索引 $bookId.qd")
            openSnapshot(context, main).use { connection ->
                val rows = connection.rawQuery(
                    "SELECT ChapterId, ChapterName, IsVip, VolumeCode, ShowOrder " +
                        "FROM chapter ORDER BY CAST(VolumeCode AS INTEGER), CAST(ShowOrder AS INTEGER)",
                    null,
                )
                val chapters = mutableListOf<IndexEntry>()
                val seen = mutableSetOf<String>()
                rows.use { cursor ->
                    while (cursor.moveToNext()) {
                        val identifier = cursor.getString(0) ?: ""
                        if (!identifier.trimStart('-').all { it in '0'..'9' } || !seen.add(identifier)) {
                            throw ReaderCacheError("章节编号无效或重复：$identifier")
                        }
                        val title = cursor.getString(1) ?: ""
                        if (title.isBlank()) {
                            throw ReaderCacheError("章节标题为空：$identifier")
                        }
                        chapters.add(
                            IndexEntry(
                                chapterId = identifier,
                                title = title,
                                isVip = cursor.getString(2),
                                volume = cursor.getString(3),
                                order = cursor.getString(4),
                            )
                        )
                    }
                }
                if (chapters.isEmpty()) {
                    throw ReaderCacheError("章节索引为空")
                }
                return chapters
            }
        } finally {
            tempDir.deleteRecursively()
        }
    }

    /** 复制 QDConfig 数据库到私有缓存目录并读取真实设备标识候选。 */
    private fun readDeviceCandidatesFromBackup(context: Context, treeUri: Uri, root: Map<String, SafEntry>): List<Pair<String, String>> {
        val databases = root["database"]?.takeIf { it.isDirectory }
            ?.let { SafTree.children(context, treeUri, it.documentId).get("databases")?.takeIf { entry -> entry.isDirectory } }
            ?: throw ReaderCacheError("缺少 database/databases 目录")
        val tempDir = File(context.cacheDir, "qd-config-${System.nanoTime()}")
        try {
            val main = copySqliteFamily(context, SafTree.children(context, treeUri, databases.documentId), "QDConfig", tempDir)
                ?: throw ReaderCacheError("缺少 QDConfig 设备标识数据库")
            openSnapshot(context, main).use { connection ->
                val rows = mutableListOf<Pair<String, String>>()
                connection.rawQuery(
                    "SELECT Key, Value FROM setting WHERE Key IN " +
                        "('SettingIMEIList', 'imei_release', 'android_release', 'UUID') " +
                        "ORDER BY CASE Key WHEN 'SettingIMEIList' THEN 0 ELSE 1 END, Key",
                    null,
                ).use { cursor ->
                    while (cursor.moveToNext()) {
                        val value = cursor.getString(1)
                        if (!value.isNullOrEmpty()) {
                            rows.add(cursor.getString(0) to value)
                        }
                    }
                }
                val tables = mutableSetOf<String>()
                connection.rawQuery("SELECT name FROM sqlite_master WHERE type='table'", null).use { cursor ->
                    while (cursor.moveToNext()) {
                        tables.add(cursor.getString(0))
                    }
                }
                if ("ImeiTable" in tables) {
                    connection.rawQuery("SELECT Imei FROM ImeiTable", null).use { cursor ->
                        while (cursor.moveToNext()) {
                            val value = cursor.getString(0)
                            if (!value.isNullOrEmpty()) {
                                rows.add("ImeiTable" to value)
                            }
                        }
                    }
                }
                val candidates = mutableListOf<Pair<String, String>>()
                val seen = mutableSetOf<String>()
                for ((source, value) in rows) {
                    val values = if (source == "SettingIMEIList") value.split("|") else listOf(value)
                    for (device in values) {
                        if (device.isNotEmpty() && seen.add(device)) {
                            candidates.add(device to source)
                        }
                    }
                }
                if (candidates.isEmpty()) {
                    throw ReaderCacheError("QDConfig 未保存可用于校验的设备标识")
                }
                return candidates
            }
        } finally {
            tempDir.deleteRecursively()
        }
    }

    /** 复制主数据库及同名附属日志文件，返回主文件。 */
    private fun copySqliteFamily(context: Context, entries: Map<String, SafEntry>, mainName: String, targetDir: File): File? {
        var main: File? = null
        for ((name, entry) in entries) {
            if (name != mainName && !name.startsWith("$mainName-")) {
                continue
            }
            if (entry.isDirectory) {
                continue
            }
            targetDir.mkdirs()
            val target = File(targetDir, name)
            target.writeBytes(SafTree.readBytes(context, entry.documentUri))
            if (name == mainName) {
                main = target
            }
        }
        return main
    }

    /** 以只读方式打开私有缓存目录中的数据库快照。 */
    private fun openSnapshot(context: Context, file: File): SQLiteDatabase {
        val connection = SQLiteDatabase.openDatabase(file.absolutePath, null, SQLiteDatabase.OPEN_READONLY)
        val result = connection.rawQuery("PRAGMA quick_check", null).use { cursor ->
            cursor.moveToFirst()
            cursor.getString(0)
        }
        if (result != "ok") {
            connection.close()
            throw ReaderCacheError("数据库完整性检查失败：${file.name}")
        }
        return connection
    }
}
