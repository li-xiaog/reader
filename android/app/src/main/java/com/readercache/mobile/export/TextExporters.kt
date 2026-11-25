// TXT、Markdown 与 XHTML 导出策略，行为与桌面版 exporters/text.py 一致。

package com.readercache.mobile.export

import com.readercache.mobile.model.BookDocument
import com.readercache.mobile.model.ReaderCacheError
import java.io.File

/** 写入 UTF-8 BOM 文本文件。 */
private fun writeText(path: File, content: String) {
    path.writeText("\uFEFF$content", Charsets.UTF_8)
}

/** 合并及分章 TXT 策略。 */
object TextExporter {
    fun export(book: BookDocument, directory: File): List<File> {
        val chapterDir = File(directory, "chapters_txt")
        val imageOnly = book.chapters.filter { bodyWithoutHeading(it).isBlank() }
        if (imageOnly.isNotEmpty()) {
            throw ReaderCacheError(
                "${imageOnly.size} 个目录项只有原生图片页面或标题，TXT无法表达其正文；请只选择EPUB或XHTML。",
            )
        }
        chapterDir.mkdir()
        val parts = mutableListOf(book.title, "作者：${book.author}", scopeNote(book))
        val paths = mutableListOf<File>()
        book.chapters.forEachIndexed { index, chapter ->
            val content = chapter.title + "\n\n" + bodyWithoutHeading(chapter)
            val path = File(chapterDir, paddedIndex(index + 1) + "_" + safeName(chapter.title) + ".txt")
            writeText(path, content)
            paths.add(path)
            parts.add(content)
        }
        val merged = File(directory, fileStem(book) + ".txt")
        writeText(merged, parts.joinToString("\n\n"))
        return listOf(merged) + paths
    }
}

/** Markdown 策略。 */
object MarkdownExporter {
    fun export(book: BookDocument, directory: File): List<File> {
        if (book.chapters.any { bodyWithoutHeading(it).isBlank() }) {
            throw ReaderCacheError(
                "存在只有原生图片页面或标题的目录项，纯文本Markdown不能表达其正文；请只选择EPUB或XHTML。",
            )
        }
        val parts = mutableListOf("# ${book.title}", "作者：${book.author}", scopeNote(book))
        for (chapter in book.chapters) {
            parts.add("## ${chapter.title}\n\n${bodyWithoutHeading(chapter)}")
        }
        val path = File(directory, fileStem(book) + ".md")
        writeText(path, parts.joinToString("\n\n"))
        return listOf(path)
    }
}

/** XHTML 与关联资源策略；起点缓存无原生页面，由正文生成章节页。 */
object XhtmlExporter {
    fun export(book: BookDocument, directory: File): List<File> {
        val assetDir = File(directory, "xhtml")
        assetDir.mkdir()
        val files = mutableListOf<File>()
        val normalizedNames = mutableSetOf<String>()
        book.chapters.forEachIndexed { index, chapter ->
            val name = "chapter-${paddedIndex(index + 1)}.xhtml"
            val normalized = name.lowercase()
            if (!normalizedNames.add(normalized)) {
                throw ReaderCacheError("资源名称在Windows中产生大小写碰撞：$name")
            }
            val path = File(assetDir, name)
            path.writeBytes(chapterXhtml(chapter))
            files.add(path)
        }
        return files
    }
}
