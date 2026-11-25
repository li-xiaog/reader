"""领域模型：与桌面版 models.py 对齐的统一书籍与章节结构。"""

package com.readercache.mobile.model

/** 可直接向用户解释的业务错误。 */
class ReaderCacheError(message: String) : Exception(message)

/** 真实进度反馈函数。 */
typealias ProgressCallback = (String) -> Unit

/** 支持的输出格式。 */
enum class ExportFormat(val value: String, val label: String) {
    TXT("txt", "TXT"),
    MARKDOWN("md", "Markdown"),
    EPUB("epub", "EPUB"),
    XHTML("xhtml", "XHTML"),
}

/** 已解码章节；顺序由列表位置决定，编号与报告在导出阶段生成。 */
data class Chapter(
    val chapterId: String,
    val title: String,
    val text: String,
    val sourceSha256: String,
    val metadata: Map<String, Any?> = emptyMap(),
)

/** 已解析书籍；expectedCount 为 null 表示没有可靠的完整目录总数。 */
data class BookDocument(
    val bookId: String,
    val title: String,
    val author: String,
    val chapters: List<Chapter>,
    val expectedCount: Int?,
    val missingChapters: List<Map<String, Any?>>,
    val metadata: Map<String, Any?> = emptyMap(),
    val diagnostics: List<String> = emptyList(),
) {
    val missingCount: Int?
        get() = expectedCount?.let { it - chapters.size }
}
