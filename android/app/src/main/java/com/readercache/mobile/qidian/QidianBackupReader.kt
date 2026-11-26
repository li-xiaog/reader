"""起点备份读取器：智能扫描手动选择的目录、核对结构并解码为统一书籍模型。

扫描规则与桌面版 local_source.py 一致，支持三种目录形态：
1. 桌面工具备份目录（qidian_book_metadata_{ID}.json + book_index + book_cache + database/databases）；
2. 从模拟器手动复制的原始 QDReader/book 布局（{用户目录}/{书籍ID}.qd 索引 + 同名缓存子目录）；
3. 上述目录的任意上级目录（一次扫描可发现多本书）。

QDConfig 设备标识数据库（解密必需）可位于所选目录树任意位置。
"""

package com.readercache.mobile.qidian

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.net.Uri
import android.provider.DocumentsContract
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
    val layout: String,
    val indexFamily: List<SafEntry>,
    val cacheEntry: SafEntry?,
    val configFamily: List<SafEntry>,
    val ready: Boolean,
    val statusNote: String,
)

/** 章节索引行。 */
data class IndexEntry(val chapterId: String, val title: String, val isVip: Any?, val volume: Any?, val order: Any?)

/** 扫描期累积的书目信息，最终转换为 BackupInfo。 */
private class PendingBook(
    val bookId: String,
    val layout: String,
    val rootDocumentId: String,
) {
    val indexFamily: MutableList<SafEntry> = mutableListOf()
    var cacheEntry: SafEntry? = null
    val configFamily: MutableList<SafEntry> = mutableListOf()
    var title: String = ""
    var author: String = ""
    var userDir: String = ""
    val notes: MutableList<String> = mutableListOf()
}

object QidianBackupReader {
    private const val MAX_DEPTH = 4
    private val METADATA_PATTERN = Regex("""^qidian_book_metadata_(\d+)\.json$""")
    private val INDEX_PATTERN = Regex("""^(\d+)\.qd$""")
    private val QD_CONFIG_HINT =
        "缺少 QDConfig 设备标识数据库（解密必需）：请把 QDConfig 文件放进所选目录的任意位置。" +
            "桌面工具备份目录自带 database/databases/QDConfig。"

    /** 智能扫描手动选择的目录，自动识别备份布局与原始缓存布局。 */
    fun scan(context: Context, treeUri: Uri, progress: ProgressCallback? = null): List<BackupInfo> {
        val rootId = DocumentsContract.getTreeDocumentId(treeUri)
        val rootEntry = SafEntry(
            name = "",
            documentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, rootId),
            documentId = rootId,
            isDirectory = true,
        )
        val configs = mutableListOf<SafEntry>()
        val pending = mutableListOf<PendingBook>()
        val seenCaches = mutableSetOf<String>()

        walkDirectories(context, treeUri, rootEntry, 0) { directory, children ->
            // 1. 收集树中全部 QDConfig 设备标识数据库。
            for ((name, entry) in children) {
                if (!entry.isDirectory && name == "QDConfig") {
                    configs.add(entry)
                }
            }
            // 2. 备份布局：元数据文件所在目录即备份根。
            for ((name, entry) in children) {
                val match = METADATA_PATTERN.find(name) ?: continue
                if (entry.isDirectory) continue
                pending.add(buildBackupBook(context, treeUri, directory, children, entry, match.groupValues[1]))
            }
            // 3. 原始布局：{书籍ID}.qd 索引与同名缓存子目录并排出现。
            for ((name, entry) in children) {
                val match = INDEX_PATTERN.find(name) ?: continue
                if (entry.isDirectory) continue
                val cache = children[match.groupValues[1]]?.takeIf { it.isDirectory } ?: continue
                if (!seenCaches.add(cache.documentId)) continue
                pending.add(buildRawBook(children, directory, name, match.groupValues[1], cache))
            }
        }

        // 4. 为缺少 QDConfig 的书籍匹配：优先所在子树内的库，否则唯一全局库。
        for (book in pending) {
            if (book.configFamily.isEmpty()) {
                val prefix = "${book.rootDocumentId}/"
                val local = configs.filter { it.documentId.startsWith(prefix) }
                val family = configFamilyOf(if (local.isNotEmpty()) local else if (configs.size == 1) configs else emptyList())
                book.configFamily.addAll(family)
            }
        }
        val books = pending
            .map { it.toBackupInfo() }
            .sortedBy { it.title }
        if (books.isEmpty()) {
            progress?.invoke("所选目录中没有发现 qidian_book_metadata_*.json 或“数字.qd”索引文件")
            progress?.invoke("提示：请确认选择的是备份目录本身，或从模拟器复制的 QDReader/book 目录（需包含 QDConfig）")
        } else {
            progress?.invoke("扫描完成，发现 ${books.size} 本起点书籍")
        }
        return books
    }

    /** 递归枚举目录树并在每个目录上回调；key 为显示名。 */
    private fun walkDirectories(
        context: Context,
        treeUri: Uri,
        directory: SafEntry,
        depth: Int,
        visit: (SafEntry, Map<String, SafEntry>) -> Unit,
    ) {
        if (depth > MAX_DEPTH) return
        val children = SafTree.children(context, treeUri, directory.documentId)
        visit(directory, children)
        for ((_, child) in children) {
            if (child.isDirectory) {
                walkDirectories(context, treeUri, child, depth + 1, visit)
            }
        }
    }

    /** 从候选文件中取出 QDConfig 主库及日志家族。 */
    private fun configFamilyOf(candidates: List<SafEntry>): List<SafEntry> {
        val main = candidates.firstOrNull { it.name == "QDConfig" } ?: return emptyList()
        val family = mutableListOf(main)
        for (suffix in listOf("-wal", "-shm", "-journal")) {
            candidates.firstOrNull { it.documentId == main.documentId + suffix }?.let { family.add(it) }
        }
        return family
    }

    /** 同一目录内主文件及同名日志家族。 */
    private fun indexFamilyOf(children: Map<String, SafEntry>, mainName: String): List<SafEntry> {
        val main = children[mainName]?.takeIf { !it.isDirectory } ?: return emptyList()
        val family = mutableListOf(main)
        for (suffix in listOf("-wal", "-shm", "-journal")) {
            children["$mainName$suffix"]?.takeIf { !it.isDirectory }?.let { family.add(it) }
        }
        return family
    }

    /** 解析备份布局书籍：核对索引、缓存与元数据。 */
    private fun buildBackupBook(
        context: Context,
        treeUri: Uri,
        directory: SafEntry,
        children: Map<String, SafEntry>,
        metadataEntry: SafEntry,
        bookId: String,
    ): PendingBook {
        val book = PendingBook(bookId, layout = "backup", rootDocumentId = directory.documentId)
        book.indexFamily.addAll(indexFamilyOf(children["book_index"]?.takeIf { it.isDirectory }
            ?.let { SafTree.children(context, treeUri, it.documentId) } ?: emptyMap(), "$bookId.qd"))
        if (book.indexFamily.isEmpty()) {
            book.notes.add("缺少章节索引 $bookId.qd")
        }
        val cache = children["book_cache"]?.takeIf { it.isDirectory }
            ?.let { SafTree.children(context, treeUri, it.documentId) }?.get(bookId)?.takeIf { it.isDirectory }
        if (cache == null) {
            book.notes.add("缺少 book_cache/$bookId 章节缓存")
        } else {
            val cacheFiles = SafTree.children(context, treeUri, cache.documentId)
            if (cacheFiles.values.none { !it.isDirectory && it.name.endsWith(".qd") }) {
                book.notes.add("book_cache/$bookId 中没有 .qd 章节缓存")
            } else {
                book.cacheEntry = cache
            }
        }
        readMetadataLenient(context, metadataEntry, book)
        if (book.userDir.isNotEmpty() && !book.userDir.all { it in '0'..'9' }) {
            book.notes.add("元数据用户目录编号无效")
        }
        book.configFamily.addAll(configFamilyOf(children.values.filter { !it.isDirectory && it.name == "QDConfig" }))
        return book
    }

    /** 解析原始布局书籍：索引所在目录名即用户目录编号。 */
    private fun buildRawBook(
        children: Map<String, SafEntry>,
        directory: SafEntry,
        indexName: String,
        bookId: String,
        cache: SafEntry,
    ): PendingBook {
        val book = PendingBook(bookId, layout = "raw", rootDocumentId = directory.documentId)
        book.indexFamily.addAll(indexFamilyOf(children, indexName))
        book.cacheEntry = cache
        if (directory.name.isNotEmpty() && directory.name.all { it in '0'..'9' }) {
            book.userDir = directory.name
        } else {
            book.notes.add("无法识别原用户目录编号（索引所在目录名必须为数字）")
        }
        book.title = "书籍 $bookId"
        book.author = "未知作者"
        return book
    }

    /** 宽容读取备份元数据，失败只记录不中断扫描。 */
    private fun readMetadataLenient(context: Context, entry: SafEntry, book: PendingBook) {
        try {
            val metadata = JSONObject(String(SafTree.readBytes(context, entry.documentUri), Charsets.UTF_8))
            book.title = metadata.optString("title", "")
            book.author = metadata.optString("author", "")
            book.userDir = metadata.optString("qdUserDir", "")
            if (book.title.isBlank() || book.author.isBlank()) {
                book.notes.add("元数据缺少书名或作者")
            }
            if (metadata.optString("qdBookId", "") != book.bookId) {
                book.notes.add("元数据书籍编号与文件名不一致")
            }
        } catch (error: Exception) {
            book.notes.add("元数据读取失败：${error.message}")
        }
    }

    /** 汇总结构检查结果并生成不可变书目。 */
    private fun PendingBook.toBackupInfo(): BackupInfo {
        val cacheReady = cacheEntry != null
        val userReady = userDir.isNotEmpty() && userDir.all { it in '0'..'9' }
        val ready = indexFamily.isNotEmpty() && cacheReady && userReady && configFamily.isNotEmpty()
        val notes = mutableListOf<String>()
        notes.addAll(this.notes)
        if (indexFamily.isEmpty() && notes.none { it.contains("章节索引") }) {
            notes.add("缺少章节索引 $bookId.qd")
        }
        if (!cacheReady && notes.none { it.contains("缓存") }) {
            notes.add("缺少 book_cache/$bookId 章节缓存")
        }
        if (configFamily.isEmpty()) {
            notes.add(QD_CONFIG_HINT)
        }
        val note = if (ready) "结构完整 · 可导出" else notes.joinToString("；")
        return BackupInfo(
            bookId = bookId,
            title = title.ifBlank { "书籍 $bookId" },
            author = author.ifBlank { "未知作者" },
            qdUserDir = userDir,
            layout = layout,
            indexFamily = indexFamily.toList(),
            cacheEntry = cacheEntry,
            configFamily = configFamily.toList(),
            ready = ready,
            statusNote = note,
        )
    }

    /** 解析完整备份目录为统一书籍模型；逐章严格校验，缺失只记录。 */
    fun loadBook(context: Context, treeUri: Uri, info: BackupInfo, progress: ProgressCallback): BookDocument {
        // 1. 结构必须在扫描后仍然完整。
        val cacheEntry = info.cacheEntry
        if (info.indexFamily.isEmpty() || cacheEntry == null || !info.qdUserDir.all { it in '0'..'9' }) {
            throw ReaderCacheError("备份结构不完整：${info.statusNote}")
        }
        progress("读取章节索引：${info.bookId}.qd")
        val indexEntries = readIndexEntries(context, info)
        val candidates = readDeviceCandidates(context, info)

        // 2. 逐章严格解码；缺失只记录，损坏或未知结构直接报错。
        val files = SafTree.children(context, treeUri, cacheEntry.documentId)
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
            val decoded = decodeCachedChapter(raw, item.chapterId, item.title, info.qdUserDir, candidates)
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

        // 3. 原始布局没有元数据文件，用版权页的真实书名与作者补齐。
        var title = info.title
        var author = info.author
        if (info.layout == "raw") {
            for (chapter in chapters) {
                val copyright = chapter.metadata["copyright_metadata"] as? JSONObject ?: continue
                val name = copyright.optString("BookName", "")
                val writer = copyright.optString("Author", "")
                if (name.isNotBlank()) title = name
                if (writer.isNotBlank()) author = writer
                break
            }
            if (title == info.title) {
                progress("提示：缓存中没有版权页，书籍标题以编号代替")
            }
        }
        progress("起点：${chapters.size} 个缓存项已校验，目录缺失 ${missing.size} 项")
        return BookDocument(
            bookId = info.bookId,
            title = title,
            author = author,
            chapters = chapters,
            expectedCount = indexEntries.size,
            missingChapters = missing,
            metadata = mapOf(
                "verified_app_version" to "7.9.460",
                "cache_layout" to "length-prefixed-v1",
                "source_layout" to info.layout,
            ),
            diagnostics = listOf("目录项数包含版权页与作者说明；仅代表备份时的目录范围。"),
        )
    }

    /** 复制索引数据库家族到私有缓存目录并读取章节顺序。 */
    private fun readIndexEntries(context: Context, info: BackupInfo): List<IndexEntry> {
        val tempDir = File(context.cacheDir, "qd-index-${System.nanoTime()}")
        try {
            val main = copySqliteFamily(context, info.indexFamily, "${info.bookId}.qd", tempDir)
                ?: throw ReaderCacheError("缺少章节索引 ${info.bookId}.qd")
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

    /** 复制 QDConfig 数据库家族到私有缓存目录并读取真实设备标识候选。 */
    private fun readDeviceCandidates(context: Context, info: BackupInfo): List<Pair<String, String>> {
        if (info.configFamily.isEmpty()) {
            throw ReaderCacheError(QD_CONFIG_HINT)
        }
        val tempDir = File(context.cacheDir, "qd-config-${System.nanoTime()}")
        try {
            val main = copySqliteFamily(context, info.configFamily, "QDConfig", tempDir)
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

    /** 复制数据库主文件及同名日志到私有缓存目录，返回主文件。 */
    private fun copySqliteFamily(context: Context, family: List<SafEntry>, mainName: String, targetDir: File): File? {
        var main: File? = null
        for (entry in family) {
            if (entry.isDirectory) {
                continue
            }
            targetDir.mkdirs()
            val target = File(targetDir, entry.name)
            target.writeBytes(SafTree.readBytes(context, entry.documentUri))
            if (entry.name == mainName) {
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
