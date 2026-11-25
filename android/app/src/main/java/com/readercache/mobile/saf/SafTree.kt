// SAF 文档树读写工具：目录枚举、文件读取与递归写出，全程不申请敏感权限。

package com.readercache.mobile.saf

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import com.readercache.mobile.model.ReaderCacheError

/** SAF 树中的一个子项。 */
data class SafEntry(val name: String, val documentUri: Uri, val documentId: String, val isDirectory: Boolean)

/** 在 SAF 文档树上的常用操作集合。 */
object SafTree {
    /** 枚举目录直接子项；key 为显示名。 */
    fun children(context: Context, treeUri: Uri, parentDocumentId: String): LinkedHashMap<String, SafEntry> {
        val result = LinkedHashMap<String, SafEntry>()
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentDocumentId)
        val projection = arrayOf(
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
        )
        context.contentResolver.query(childrenUri, projection, null, null, null)?.use { cursor ->
            while (cursor.moveToNext()) {
                val name = cursor.getString(1) ?: continue
                val documentId = cursor.getString(0)
                val mime = cursor.getString(2)
                result[name] = SafEntry(
                    name = name,
                    documentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, documentId),
                    documentId = documentId,
                    isDirectory = mime == DocumentsContract.Document.MIME_TYPE_DIR,
                )
            }
        } ?: throw ReaderCacheError("无法读取所选目录，请重新选择备份目录")
        return result
    }

    /** 枚举树根的直接子项。 */
    fun rootChildren(context: Context, treeUri: Uri): LinkedHashMap<String, SafEntry> =
        children(context, treeUri, DocumentsContract.getTreeDocumentId(treeUri))

    /** 获取树根的文档 ID。 */
    fun rootDocumentId(context: Context, treeUri: Uri): String = DocumentsContract.getTreeDocumentId(treeUri)

    /** 读取单个文档全部字节。 */
    fun readBytes(context: Context, uri: Uri): ByteArray =
        context.contentResolver.openInputStream(uri)?.use { it.readBytes() }
            ?: throw ReaderCacheError("文件读取失败：$uri")

    /** 在父目录下创建子目录；名称已存在时报错。 */
    fun createDirectory(context: Context, treeUri: Uri, parentDocumentId: String, name: String): SafEntry {
        val existing = children(context, treeUri, parentDocumentId)[name]
        if (existing != null) {
            throw ReaderCacheError("输出位置已存在同名条目：$name")
        }
        val parentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, parentDocumentId)
        val documentUri = DocumentsContract.createDocument(
            context.contentResolver, parentUri, DocumentsContract.Document.MIME_TYPE_DIR, name,
        )
            ?: throw ReaderCacheError("无法创建输出目录：$name")
        val documentId = DocumentsContract.getDocumentId(documentUri)
        return SafEntry(name, documentUri, documentId, true)
    }

    /** 在父目录下写入新文件。 */
    fun writeFile(context: Context, treeUri: Uri, parentDocumentId: String, name: String, mimeType: String, data: ByteArray): SafEntry {
        val parentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, parentDocumentId)
        val documentUri = DocumentsContract.createDocument(context.contentResolver, parentUri, mimeType, name)
            ?: throw ReaderCacheError("无法创建输出文件：$name")
        context.contentResolver.openOutputStream(documentUri)?.use { output -> output.write(data) }
            ?: throw ReaderCacheError("无法写入输出文件：$name")
        return SafEntry(name, documentUri, DocumentsContract.getDocumentId(documentUri), false)
    }

    /** 递归把本地暂存目录整体复制到 SAF 目标目录。 */
    fun copyDirectory(context: Context, treeUri: Uri, parentDocumentId: String, source: java.io.File) {
        val files = source.listFiles()?.sortedBy { it.name } ?: return
        for (file in files) {
            if (file.isDirectory) {
                val entry = createDirectory(context, treeUri, parentDocumentId, file.name)
                copyDirectory(context, treeUri, entry.documentId, file)
            } else {
                val mime = mimeTypeFor(file.name)
                writeFile(context, treeUri, parentDocumentId, file.name, mime, file.readBytes())
            }
        }
    }

    /** 根据文件名给出 SAF 写入用的 MIME 类型。 */
    fun mimeTypeFor(name: String): String = when {
        name.endsWith(".txt") -> "text/plain"
        name.endsWith(".md") -> "text/markdown"
        name.endsWith(".csv") -> "text/csv"
        name.endsWith(".json") -> "application/json"
        name.endsWith(".epub") -> "application/epub+zip"
        name.endsWith(".xhtml") -> "application/xhtml+xml"
        name.endsWith(".xml") -> "application/xml"
        name.endsWith(".ncx") -> "application/x-dtbncx+xml"
        else -> "application/octet-stream"
    }
}
