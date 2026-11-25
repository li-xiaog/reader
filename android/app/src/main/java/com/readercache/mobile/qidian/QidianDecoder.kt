// 起点缓存解码器：复现已验证 APK 本地读取逻辑，行为与桌面版 codecs/qidian.py 一致。

package com.readercache.mobile.qidian

import com.readercache.mobile.model.ReaderCacheError
import java.security.MessageDigest
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec
import org.json.JSONArray
import org.json.JSONObject

/** 与桌面版一致的固定内容盐值，仅配合备份内真实密钥材料使用。 */
private const val CONTENT_SALT = "2EEE1433A152E84B3756301D8FA3E69A"

/** 单章缓存解码结果。 */
data class DecodedChapter(
    val chapterId: String,
    val title: String,
    val text: String,
    val sourceSha256: String,
    val deviceSource: String,
    val trailingBytes: Int,
    val copyrightMetadata: JSONObject? = null,
)

/** 五个带长度前缀字段的解析结果。 */
private class CacheFields(val fields: List<ByteArray>, val trailing: Int)

/** 按应用读取顺序解析五个带长度的二进制字段；截断或未支持格式直接报错。 */
private fun readCacheFields(raw: ByteArray): CacheFields {
    var position = 0
    val fields = mutableListOf<ByteArray>()
    repeat(5) {
        if (position + 4 > raw.size) {
            throw ReaderCacheError("缓存字段长度被截断")
        }
        var size = 0
        for (shift in 0 until 4) {
            size = size or ((raw[position + shift].toInt() and 0xFF) shl (shift * 8))
        }
        position += 4
        if (position + size > raw.size) {
            throw ReaderCacheError("缓存字段内容被截断")
        }
        fields.add(raw.copyOfRange(position, position + size))
        position += size
    }
    if (fields[0].isNotEmpty()) {
        throw ReaderCacheError("当前脚本尚未验证含头信息的起点缓存格式")
    }
    if (fields[1].isEmpty() || fields[1].size % 8 != 0) {
        throw ReaderCacheError("章节负载为空或块长度无效")
    }
    val block3 = String(fields[3], Charsets.UTF_8)
    if (fields[2].isNotEmpty() || fields[4].isNotEmpty() || block3 !in setOf("", "null")) {
        throw ReaderCacheError("当前缓存含尚未支持的独立作者说明、资源或内容块")
    }
    return CacheFields(fields, raw.size - position)
}

/** 解开应用格式中的一层章节数据并严格验证填充。 */
private fun decryptLayer(payload: ByteArray, key: String): ByteArray {
    val cipher = Cipher.getInstance("DESede/CBC/PKCS5Padding")
    cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key.toByteArray(Charsets.UTF_8), "DESede"), IvParameterSpec(ByteArray(8)))
    return cipher.doFinal(payload)
}

/** HMAC 摘要的 Base64 编码。 */
private fun hmacBase64(algorithm: String, keySource: String, message: String): ByteArray {
    val mac = Mac.getInstance(algorithm)
    mac.init(SecretKeySpec(keySource.toByteArray(Charsets.UTF_8), algorithm))
    return Base64.getEncoder().encode(mac.doFinal(message.toByteArray(Charsets.UTF_8)))
}

/** 复现已核对的 APK 本地读取逻辑；密钥不匹配或数据损坏时抛出异常。 */
fun decodePayload(payload: ByteArray, userId: String, chapterId: String, device: String): JSONObject {
    val key1 = String(hmacBase64("HmacSHA1", device, userId + device + chapterId + CONTENT_SALT), Charsets.US_ASCII).take(24)
    val key2 = String(hmacBase64("HmacMD5", userId, key1 + device), Charsets.US_ASCII)
    val decoded = decryptLayer(decryptLayer(payload, key2), key1)
    val document = JSONObject(String(decoded, Charsets.UTF_8))
    if (!document.has("Content")) {
        throw ReaderCacheError("章节 JSON 缺少 Content 字段")
    }
    return document
}

/** 将缓存中结构化的版权页转换为可读文字，保留原有声明和简介。 */
fun renderCopyrightPage(content: JSONObject): String {
    val labels = linkedMapOf(
        "BookName" to "书名",
        "Author" to "作者",
        "BookStatus" to "状态",
        "CategoryName" to "分类",
        "SubCategoryName" to "子分类",
        "PunishTime" to "发行日期",
        "PunishInfo" to "发行信息",
        "Copyright" to "版权声明",
        "AuthorWords" to "作者寄语",
        "WelcomeSpeech" to "欢迎语",
        "Description" to "简介",
    )
    val sections = mutableListOf<String>()
    for ((key, label) in labels) {
        val value = content.opt(key) ?: continue
        if (value == JSONObject.NULL || value == "") {
            continue
        }
        if (value !is String) {
            throw ReaderCacheError("版权页字段类型异常：$key")
        }
        sections.add("$label：$value")
    }
    val bookName = content.optString("BookName", "")
    val copyright = content.optString("Copyright", "")
    if (bookName.isBlank() || copyright.isBlank()) {
        throw ReaderCacheError("版权页缺少书名或版权声明")
    }
    return sections.joinToString("\n\n")
}

/** 判断章节 JSON 中尚未支持的扩展字段是否为空。 */
private fun extraFieldEmpty(document: JSONObject, key: String): Boolean {
    if (!document.has(key)) {
        return true
    }
    return when (val value = document.opt(key)) {
        null, JSONObject.NULL -> true
        is String -> value.isEmpty()
        is JSONArray -> value.length() == 0
        is JSONObject -> value.length() == 0
        else -> throw ReaderCacheError("章节含尚未支持的非空 $key")
    }
}

/** 用真实历史设备标识逐一校验单章并解码正文；所有候选失败则明确报错。 */
fun decodeCachedChapter(raw: ByteArray, chapterId: String, title: String, userId: String, candidates: List<Pair<String, String>>): DecodedChapter {
    val cacheFields = readCacheFields(raw)
    for ((device, source) in candidates) {
        val document = try {
            decodePayload(cacheFields.fields[1], userId, chapterId, device)
        } catch (error: Exception) {
            // 解密失败、填充非法或 JSON 不合法时尝试下一个历史设备标识，与桌面版行为一致。
            continue
        }
        val content = document["Content"]
        var copyrightMetadata: JSONObject? = null
        val text = when {
            chapterId == "-10000" && content is JSONObject -> {
                copyrightMetadata = content
                renderCopyrightPage(content)
            }
            content is String -> content
            else -> throw ReaderCacheError("章节 Content 字段类型尚未支持：$chapterId")
        }
        if (text.isBlank() || text.contains('\uFFFD')) {
            throw ReaderCacheError("章节正文为空或含乱码替换字符：$chapterId")
        }
        if (text.any { it.code < 32 && it !in "\r\n\t" }) {
            throw ReaderCacheError("章节含不可显示控制字符：$chapterId")
        }
        for (key in listOf("AuthorComments", "Resources", "Blocks")) {
            if (!extraFieldEmpty(document, key)) {
                throw ReaderCacheError("章节含尚未支持的非空 $key：$chapterId")
            }
        }
        return DecodedChapter(
            chapterId = chapterId,
            title = title,
            text = text.replace("\r\n", "\n").replace("\r", "\n"),
            sourceSha256 = MessageDigest.getInstance("SHA-256").digest(raw).joinToString("") { "%02x".format(it) },
            deviceSource = source,
            trailingBytes = cacheFields.trailing,
            copyrightMetadata = copyrightMetadata,
        )
    }
    throw ReaderCacheError("章节 $chapterId 没有匹配的设备标识，或缓存损坏")
}
