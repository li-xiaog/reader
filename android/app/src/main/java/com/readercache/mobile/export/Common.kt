"""导出器共享的命名、范围说明及文本处理函数，行为与桌面版 exporters/common.py 一致。"""

package com.readercache.mobile.export

import android.util.Xml
import com.readercache.mobile.model.BookDocument
import com.readercache.mobile.model.Chapter
import com.readercache.mobile.model.ReaderCacheError
import java.io.StringReader
import org.xmlpull.v1.XmlPullParser

private val UNSAFE_CHARS = Regex("[<>:\"/\\\\|?*\u0000-\u001F]")
private val RESERVED_NAMES = setOf(
    "CON", "PRN", "AUX", "NUL",
    *Array(9) { "COM${it + 1}" },
    *Array(9) { "LPT${it + 1}" },
)

/** 规范化文件名；无有效名称则报错。 */
fun safeName(value: String): String {
    var result = UNSAFE_CHARS.replace(value, "_").trim(' ', '.')
    if (result.length > 100) {
        result = result.substring(0, 100).trimEnd(' ', '.')
    }
    if (result.isEmpty()) {
        throw ReaderCacheError("真实书名或章节名称无法构成有效文件名")
    }
    if (result.substringBefore('.').uppercase() in RESERVED_NAMES) {
        result = "_$result"
    }
    return result
}

/** 生成标识缓存范围的书籍文件名。 */
fun fileStem(book: BookDocument): String =
    "${safeName(book.title)}_${book.bookId}_已缓存${book.chapters.size}项"

/** 说明真实导出范围；未知总数明确标注。 */
fun scopeNote(book: BookDocument): String =
    if (book.expectedCount == null) {
        "导出已缓存的 ${book.chapters.size} 个目录项；缺少可靠的完整目录，不能确认全书完整。"
    } else {
        "导出已缓存的 ${book.chapters.size} 个目录项；备份目录共 ${book.expectedCount} 项，" +
            "未缓存 ${book.expectedCount - book.chapters.size} 项。目录项可能包含版权页及作者说明。"
    }

/** 去除与索引完全相同的首行标题以免重复。 */
fun bodyWithoutHeading(chapter: Chapter): String {
    val text = chapter.text.replace("\r\n", "\n").replace("\r", "\n")
    val lines = text.split("\n")
    if (lines.isNotEmpty() && lines[0].trim() == chapter.title.trim()) {
        return lines.drop(1).joinToString("\n").trimStart('\n')
    }
    return text
}

/** XML 文本转义，与 Python html.escape(quote=True) 行为一致。 */
fun escapeXml(value: String): String = value
    .replace("&", "&amp;")
    .replace("<", "&lt;")
    .replace(">", "&gt;")
    .replace("\"", "&quot;")
    .replace("'", "&#x27;")

/** 生成可移植的文字章节页，并通过 XML 解析校验。 */
fun chapterXhtml(chapter: Chapter): ByteArray {
    val paragraphs = bodyWithoutHeading(chapter).split("\n").joinToString("\n") { line ->
        if (line.isEmpty()) "<p>&#160;</p>" else "<p>${escapeXml(line)}</p>"
    }
    val page = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n" +
            "<html xmlns=\"http://www.w3.org/1999/xhtml\" lang=\"zh-CN\" xml:lang=\"zh-CN\">" +
            "<head><title>${escapeXml(chapter.title)}</title><style>" +
            "body{font-family:serif;font-size:1em;line-height:1.8;margin:1em}" +
            "p{white-space:pre-wrap}h1{font-size:1.5em}" +
            "</style></head><body><h1>${escapeXml(chapter.title)}</h1>$paragraphs</body></html>"
        )
    assertWellFormedXml(page)
    return page.toByteArray(Charsets.UTF_8)
}

/** 校验字符串为格式良好的 XML。 */
fun assertWellFormedXml(content: String) {
    try {
        val parser = Xml.newPullParser()
        parser.setInput(StringReader(content))
        while (parser.next() != XmlPullParser.END_DOCUMENT) {
            // 仅推进事件；解析错误会在此抛出。
        }
    } catch (error: Exception) {
        throw ReaderCacheError("生成的 XML 校验失败：${error.message}")
    }
}

/** 计算 SHA-256 十六进制摘要。 */
fun sha256Hex(data: ByteArray): String =
    java.security.MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }
