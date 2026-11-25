"""真实章节索引与不含密钥的核验报告，字段与桌面版 service.py 一致。"""

package com.readercache.mobile.export

import com.readercache.mobile.model.BookDocument
import java.io.File
import org.json.JSONArray
import org.json.JSONObject

/** 写入 index.csv、chapters_meta 与 conversion_report.json；返回报告文件。 */
fun writeReports(book: BookDocument, directory: File, outputs: List<File>): File {
    val chapterMeta = File(directory, "chapters_meta")
    chapterMeta.mkdir()
    val indexFile = File(directory, "index.csv")
    val lines = StringBuilder("\uFEFF")
    lines.append("order,chapter_id,title,text_chars,source_sha256\r\n")
    book.chapters.forEachIndexed { index, chapter ->
        val entry = JSONObject(
            linkedMapOf(
                "order" to index + 1,
                "chapter_id" to chapter.chapterId,
                "title" to chapter.title,
                "text_chars" to chapter.text.length,
                "source_sha256" to chapter.sourceSha256,
            ),
        )
        lines.append(csvRow(entry))
        lines.append("\r\n")
        val details = JSONObject(entry.toMap() as Map<*, *>)
        details.put("details", JSONObject(chapter.metadata))
        File(chapterMeta, "%04d.json".format(index + 1)).writeText(details.toString(2), Charsets.UTF_8)
    }
    indexFile.writeText(lines.toString(), Charsets.UTF_8)

    val report = JSONObject()
    report.put("channel", "qidian")
    report.put("book_id", book.bookId)
    report.put("title", book.title)
    report.put("author", book.author)
    report.put("generated_at", java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC).toString())
    report.put("decoded_count", book.chapters.size)
    report.put("expected_count", book.expectedCount ?: JSONObject.NULL)
    report.put("missing_count", book.missingCount ?: JSONObject.NULL)
    report.put("missing_chapters", JSONArray(book.missingChapters.map { JSONObject(it) }))
    report.put("diagnostics", JSONArray(book.diagnostics))
    report.put("metadata", JSONObject(book.metadata))
    report.put("replacement_characters", 0)
    report.put(
        "files",
        JSONArray(outputs.map { file ->
            JSONObject(
                linkedMapOf(
                    "path" to file.relativeTo(directory).invariantSeparatorsPath,
                    "bytes" to file.length(),
                    "sha256" to sha256Hex(file.readBytes()),
                ),
            )
        }),
    )
    val reportFile = File(directory, "conversion_report.json")
    reportFile.writeText(report.toString(2), Charsets.UTF_8)
    return reportFile
}

/** 单行 CSV（按 RFC 4180 转义引号与逗号）。 */
private fun csvRow(entry: JSONObject): String =
    listOf("order", "chapter_id", "title", "text_chars", "source_sha256").joinToString(",") { key ->
        val value = entry.get(key).toString()
        if (value.contains(',') || value.contains('"') || value.contains('\n')) {
            "\"" + value.replace("\"", "\"\"") + "\""
        } else {
            value
        }
    }
