"""导出引擎：统一执行解析、校验、暂存导出与 SAF 发布；失败不发布半成品。"""

package com.readercache.mobile.export

import android.content.Context
import android.net.Uri
import com.readercache.mobile.model.BookDocument
import com.readercache.mobile.model.ExportFormat
import com.readercache.mobile.model.ProgressCallback
import com.readercache.mobile.model.ReaderCacheError
import com.readercache.mobile.qidian.BackupInfo
import com.readercache.mobile.qidian.QidianBackupReader
import com.readercache.mobile.saf.SafTree
import java.io.File

/** 一次成功导出的结果摘要。 */
data class ExportSummary(
    val outputName: String,
    val chapterCount: Int,
    val missingCount: Int?,
    val files: List<String>,
)

/** 校验解析结果，规则与桌面版 service.validate_book 一致。 */
fun validateBook(book: BookDocument, requireComplete: Boolean) {
    if (book.title.isBlank() || book.author.isBlank() || book.chapters.isEmpty()) {
        throw ReaderCacheError("书籍缺少真实书名、作者或有效章节")
    }
    val ids = book.chapters.map { it.chapterId }
    if (ids.size != ids.toSet().size) {
        throw ReaderCacheError("解析结果含重复章节编号")
    }
    for (chapter in book.chapters) {
        if (chapter.title.isBlank() || (chapter.title + chapter.text).contains('\uFFFD')) {
            throw ReaderCacheError("章节标题为空或含乱码替换字符：${chapter.chapterId}")
        }
    }
    val expected = book.expectedCount
    if (expected != null && expected < book.chapters.size) {
        throw ReaderCacheError("缓存章节数超过真实目录总数，无法确认顺序")
    }
    if (requireComplete && (expected == null || expected != book.chapters.size || book.missingChapters.isNotEmpty())) {
        throw ReaderCacheError("无法确认本地目录全部缓存，已按完整性要求终止")
    }
}

/** 移动端导出引擎；输入为 SAF 备份树，输出为 SAF 目标目录。 */
class ExportEngine(private val context: Context) {

    /** 完整执行一次导出：解析 → 校验 → 暂存生成 → 报告 → 复制到目标 → 清理。 */
    fun export(
        sourceTreeUri: Uri,
        info: BackupInfo,
        formats: List<ExportFormat>,
        requireComplete: Boolean,
        outputTreeUri: Uri,
        progress: ProgressCallback,
    ): ExportSummary {
        // 1. 输入校验：格式必须显式选择且不重复。
        if (formats.isEmpty() || formats.size != formats.toSet().size) {
            throw ReaderCacheError("请至少选择一个输出格式，且格式不能重复")
        }

        // 2. 适配器完整解析；只有校验通过的数据才进入导出阶段。
        progress("读取起点本地缓存")
        val book = QidianBackupReader.loadBook(context, sourceTreeUri, info, progress)
        for (diagnostic in book.diagnostics) {
            progress("提示：$diagnostic")
        }
        validateBook(book, requireComplete)

        // 3. 在应用私有缓存中生成全部文件与报告，全部成功后才向目标发布。
        val stage = File(context.cacheDir, ".reader-export-${System.nanoTime()}")
        if (!stage.mkdirs()) {
            throw ReaderCacheError("无法创建本地暂存目录，请重试")
        }
        try {
            val outputs = mutableListOf<File>()
            for (format in formats) {
                progress("正在生成并校验 ${format.value.uppercase()}")
                outputs.addAll(
                    when (format) {
                        ExportFormat.TXT -> TextExporter.export(book, stage)
                        ExportFormat.MARKDOWN -> MarkdownExporter.export(book, stage)
                        ExportFormat.EPUB -> EpubExporter.export(book, stage)
                        ExportFormat.XHTML -> XhtmlExporter.export(book, stage)
                    },
                )
            }
            writeReports(book, stage, outputs)
            val outputName = fileStem(book)
            progress("正在发布到所选目录：$outputName")
            val outputEntry = SafTree.createDirectory(context, outputTreeUri, SafTree.rootDocumentId(context, outputTreeUri), outputName)
            SafTree.copyDirectory(context, outputTreeUri, outputEntry.documentId, stage)
            val relativeFiles = outputs.map { it.relativeTo(stage).invariantSeparatorsPath }
            progress("导出完成：${book.chapters.size} 个目录项")
            return ExportSummary(
                outputName = outputName,
                chapterCount = book.chapters.size,
                missingCount = book.missingCount,
                files = relativeFiles,
            )
        } finally {
            stage.deleteRecursively()
        }
    }
}
