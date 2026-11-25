"""EPUB 3 导出策略：由真实正文生成章节页，并验证容器、清单与阅读顺序。"""

package com.readercache.mobile.export

import android.util.Log
import com.readercache.mobile.model.BookDocument
import com.readercache.mobile.model.ReaderCacheError
import java.io.File
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream
import javax.xml.parsers.DocumentBuilderFactory
import org.w3c.dom.Element

private const val OPF = "http://www.idpf.org/2007/opf"
private const val XHTML = "http://www.w3.org/1999/xhtml"
private const val NCX = "http://www.daisy.org/z3986/2005/ncx/"

/** 识别资源媒体类型；未知格式明确报错。 */
private fun mediaType(path: String): String = when (path.substringAfterLast('.').lowercase()) {
    "xhtml", "html", "htm" -> "application/xhtml+xml"
    "ncx" -> "application/x-dtbncx+xml"
    "opf" -> "application/oebps-package+xml"
    "css" -> "text/css"
    "png" -> "image/png"
    "jpg", "jpeg" -> "image/jpeg"
    "gif" -> "image/gif"
    "svg" -> "image/svg+xml"
    "otf" -> "font/otf"
    "ttf" -> "font/ttf"
    else -> throw ReaderCacheError("EPUB资源媒体类型尚未支持：$path")
}

/** 由路径哈希构成的稳定 XML ID。 */
private fun resourceId(path: String): String = "r-" + sha256Hex(path.toByteArray(Charsets.UTF_8))

/** 生成 EPUB 包清单。 */
private fun buildPackage(book: BookDocument, assets: Map<String, ByteArray>, spine: List<String>): ByteArray {
    val items = StringBuilder()
    items.append("<item id=\"nav\" href=\"toolkit-nav.xhtml\" media-type=\"application/xhtml+xml\" properties=\"nav\"/>")
    items.append("<item id=\"ncx\" href=\"toolkit-toc.ncx\" media-type=\"application/x-dtbncx+xml\"/>")
    for (path in assets.keys) {
        items.append(
            "<item id=\"${resourceId(path)}\" href=\"${escapeXml(path)}\" media-type=\"${mediaType(path)}\"/>",
        )
    }
    val references = spine.joinToString("") { "<itemref idref=\"${resourceId(it)}\"/>" }
    val identifier = "urn:qidian:${book.bookId}"
    val modified = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'").withZone(ZoneOffset.UTC).format(Instant.now())
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><package xmlns=\"$OPF\" version=\"3.0\" unique-identifier=\"book-id\">" +
            "<metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\">" +
            "<dc:identifier id=\"book-id\">${escapeXml(identifier)}</dc:identifier>" +
            "<dc:title>${escapeXml(book.title)}</dc:title><dc:creator>${escapeXml(book.author)}</dc:creator>" +
            "<dc:language>zh-CN</dc:language><dc:description>${escapeXml(scopeNote(book))}</dc:description>" +
            "<meta property=\"dcterms:modified\">$modified</meta></metadata>" +
            "<manifest>$items</manifest><spine toc=\"ncx\">$references</spine></package>"
        ).toByteArray(Charsets.UTF_8)
}

/** 生成现代与兼容目录。 */
private fun buildNavigation(book: BookDocument, navigation: List<String>): Pair<ByteArray, ByteArray> {
    val items = StringBuilder()
    val points = StringBuilder()
    book.chapters.forEachIndexed { index, chapter ->
        val target = escapeXml(navigation[index])
        val title = escapeXml(chapter.title)
        items.append("<li><a href=\"$target\">$title</a></li>")
        points.append(
            "<navPoint id=\"chapter-${index + 1}\" playOrder=\"${index + 1}\"><navLabel><text>$title</text>" +
                "</navLabel><content src=\"$target\"/></navPoint>",
        )
    }
    val nav = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><html xmlns=\"$XHTML\" xmlns:epub=\"http://www.idpf.org/2007/ops\" lang=\"zh-CN\">" +
            "<head><title>目录</title></head><body><h1>${escapeXml(book.title)}</h1><p>${escapeXml(scopeNote(book))}</p>" +
            "<nav epub:type=\"toc\" id=\"toc\"><ol>$items</ol></nav></body></html>"
        ).toByteArray(Charsets.UTF_8)
    val ncx = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><ncx xmlns=\"$NCX\" version=\"2005-1\">" +
            "<head><meta name=\"dtb:uid\" content=\"urn:qidian:${book.bookId}\"/>" +
            "<meta name=\"dtb:depth\" content=\"1\"/><meta name=\"dtb:totalPageCount\" content=\"0\"/>" +
            "<meta name=\"dtb:maxPageNumber\" content=\"0\"/></head>" +
            "<docTitle><text>${escapeXml(book.title)}</text></docTitle><navMap>$points</navMap></ncx>"
        ).toByteArray(Charsets.UTF_8)
    return nav to ncx
}

/** 归一化 POSIX 相对路径（等价 posixpath.normpath）。 */
private fun normalizePosix(path: String): String {
    val absolute = path.startsWith("/")
    val result = ArrayDeque<String>()
    for (part in path.split("/")) {
        when (part) {
            "", "." -> {}
            ".." -> {
                if (result.isNotEmpty() && result.last() != "..") {
                    result.removeLast()
                } else if (!absolute) {
                    result.addLast("..")
                }
            }
            else -> result.addLast(part)
        }
    }
    val joined = result.joinToString("/")
    return if (absolute) "/$joined" else joined
}

/** 内部相对引用解析结果（等价 posixpath.join(dirname, path) 后归一化）。 */
private fun resolveReference(documentName: String, target: String): String {
    val directory = documentName.substringBeforeLast('/', "")
    val joined = if (directory.isEmpty()) target else "$directory/$target"
    return normalizePosix(joined)
}

/** 核查最终 EPUB 的容器、XML、清单、目录与资源引用；失败禁止发布。 */
private fun verifyEpub(path: File, expectedSpine: List<String>, expectedNavigation: List<String>) {
    ZipFile(path).use { archive ->
        val entries = archive.entries().toList()
        val first = entries.firstOrNull() ?: throw ReaderCacheError("EPUB首项mimetype缺失或被压缩")
        if (first.name != "mimetype" || first.method != ZipEntry.STORED) {
            throw ReaderCacheError("EPUB首项mimetype缺失或被压缩")
        }
        if (archive.getInputStream(first).readBytes().toString(Charsets.UTF_8) != "application/epub+zip") {
            throw ReaderCacheError("EPUB类型校验失败")
        }
        val names = entries.map { it.name }
        if (names.size != names.toSet().size) {
            throw ReaderCacheError("EPUB存在重复资源名")
        }
        val builder = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }.newDocumentBuilder()
        val documents = mutableMapOf<String, Element>()
        for (name in names) {
            if (name.substringAfterLast('.').lowercase() in setOf("xml", "xhtml", "html", "htm", "opf", "ncx", "svg")) {
                documents[name] = try {
                    archive.getInputStream(archive.getEntry(name)).use { builder.parse(it).documentElement }
                } catch (error: Exception) {
                    throw ReaderCacheError("EPUB XML 校验失败：$name")
                }
            }
        }

        // 清单必须包含全部正文目标，阅读顺序与真实缓存一致。
        val packageElement = documents["OEBPS/package.opf"] ?: throw ReaderCacheError("EPUB缺少包清单")
        val manifest = packageElement.getElementsByTagNameNS(OPF, "item").let { list ->
            (0 until list.length).associate { index ->
                val node = list.item(index) as Element
                node.getAttribute("id") to node.getAttribute("href")
            }
        }
        for (href in manifest.values) {
            if ("OEBPS/$href" !in names) {
                throw ReaderCacheError("EPUB清单包含缺失的资源")
            }
        }
        val spine = packageElement.getElementsByTagNameNS(OPF, "itemref").let { list ->
            (0 until list.length).map { index ->
                val idref = (list.item(index) as Element).getAttribute("idref")
                manifest[idref] ?: throw ReaderCacheError("EPUB spine 引用了未知清单项")
            }
        }
        if (spine != expectedSpine) {
            throw ReaderCacheError("EPUB正文阅读顺序与真实缓存不一致")
        }

        // 两套目录必须逐一指向正确章节。
        val nav = documents["OEBPS/toolkit-nav.xhtml"] ?: throw ReaderCacheError("EPUB缺少导航页")
        val navTargets = nav.getElementsByTagNameNS(XHTML, "a").let { list ->
            (0 until list.length).map { (list.item(it) as Element).getAttribute("href") }
        }
        val ncx = documents["OEBPS/toolkit-toc.ncx"] ?: throw ReaderCacheError("EPUB缺少NCX目录")
        val ncxTargets = ncx.getElementsByTagNameNS(NCX, "content").let { list ->
            (0 until list.length).map { (list.item(it) as Element).getAttribute("src") }
        }
        if (navTargets != expectedNavigation || ncxTargets != expectedNavigation) {
            throw ReaderCacheError("EPUB章节目录与原始章节不一致")
        }

        // 验证页面内部引用的本地资源存在，外链保持原样不请求网络。
        for ((name, document) in documents) {
            if (!name.lowercase().endsWith(".xhtml") && !name.lowercase().endsWith(".html") && !name.lowercase().endsWith(".htm")) {
                continue
            }
            val nodes = document.getElementsByTagName("*")
            for (index in 0 until nodes.length) {
                val element = nodes.item(index) as Element
                for (attribute in listOf("src", "href")) {
                    val target = element.getAttribute(attribute)
                    if (target.isEmpty() || target.contains(":")) {
                        continue
                    }
                    val resolved = resolveReference(name, target.substringBefore('?').substringBefore('#'))
                    if (resolved !in names) {
                        throw ReaderCacheError("EPUB存在缺失的内部资源：$name -> $target")
                    }
                }
            }
        }
    }
}

/** EPUB 导出策略。 */
object EpubExporter {
    fun export(book: BookDocument, directory: File): List<File> {
        // 1. 起点缓存无原生页面，全部由真实正文生成章节页。
        val assets = linkedMapOf<String, ByteArray>()
        val spine = mutableListOf<String>()
        val navigation = mutableListOf<String>()
        book.chapters.forEachIndexed { index, chapter ->
            val path = "text/chapter-%04d.xhtml".format(index + 1)
            assets[path] = chapterXhtml(chapter)
            navigation.add(path)
            spine.add(path)
        }
        val (nav, ncx) = buildNavigation(book, navigation)
        val packageBytes = buildPackage(book, assets, spine)

        // 2. 按规范写入容器与未压缩的 mimetype。
        val output = File(directory, fileStem(book) + ".epub")
        ZipOutputStream(output.outputStream()).use { archive ->
            val mimetype = "application/epub+zip".toByteArray(Charsets.UTF_8)
            val mimetypeEntry = ZipEntry("mimetype").apply {
                method = ZipEntry.STORED
                size = mimetype.size.toLong()
                crc = CRC32().apply { update(mimetype) }.value
            }
            archive.putNextEntry(mimetypeEntry)
            archive.write(mimetype)
            archive.closeEntry()
            archive.setLevel(9)
            archive.putNextEntry(ZipEntry("META-INF/container.xml"))
            archive.write(
                ("<?xml version=\"1.0\"?><container version=\"1.0\" " +
                    "xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\"><rootfiles><rootfile " +
                    "full-path=\"OEBPS/package.opf\" media-type=\"application/oebps-package+xml\"/></rootfiles></container>")
                    .toByteArray(Charsets.UTF_8),
            )
            archive.closeEntry()
            archive.putNextEntry(ZipEntry("OEBPS/package.opf"))
            archive.write(packageBytes)
            archive.closeEntry()
            archive.putNextEntry(ZipEntry("OEBPS/toolkit-nav.xhtml"))
            archive.write(nav)
            archive.closeEntry()
            archive.putNextEntry(ZipEntry("OEBPS/toolkit-toc.ncx"))
            archive.write(ncx)
            archive.closeEntry()
            for ((name, data) in assets) {
                archive.putNextEntry(ZipEntry("OEBPS/$name"))
                archive.write(data)
                archive.closeEntry()
            }
        }
        // 3. 结构与引用全部通过后才交回导出引擎。
        verifyEpub(output, spine, navigation)
        Log.d("EpubExporter", "EPUB 校验通过：${output.name}")
        return listOf(output)
    }
}
