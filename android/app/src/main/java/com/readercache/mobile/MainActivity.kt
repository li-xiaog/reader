"""单页桌面界面：选择备份目录、扫描书籍、选择格式并导出；后台协程执行，主线程更新。"""

package com.readercache.mobile

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.text.method.ScrollingMovementMethod
import android.view.View
import android.widget.ArrayAdapter
import android.widget.CheckBox
import android.widget.Spinner
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.readercache.mobile.export.ExportEngine
import com.readercache.mobile.export.ExportSummary
import com.readercache.mobile.model.ExportFormat
import com.readercache.mobile.qidian.BackupInfo
import com.readercache.mobile.qidian.QidianBackupReader
import com.readercache.mobile.saf.SafTree
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity() {

    private var sourceUri: Uri? = null
    private var outputUri: Uri? = null
    private var books: List<BackupInfo> = emptyList()
    private var busy = false

    private lateinit var btnSelectSource: MaterialButton
    private lateinit var btnSelectOutput: MaterialButton
    private lateinit var btnExport: MaterialButton
    private lateinit var spinnerBook: Spinner
    private lateinit var tvSource: TextView
    private lateinit var tvOutput: TextView
    private lateinit var tvBookInfo: TextView
    private lateinit var tvStatus: TextView
    private lateinit var tvLog: TextView
    private lateinit var cbTxt: CheckBox
    private lateinit var cbMd: CheckBox
    private lateinit var cbEpub: CheckBox
    private lateinit var cbXhtml: CheckBox
    private lateinit var cbComplete: CheckBox

    private val sourceLauncher = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        uri ?: return@registerForActivityResult
        takePersistablePermission(uri)
        sourceUri = uri
        tvSource.text = displayName(uri)
        scanBackup(uri)
    }

    private val outputLauncher = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        uri ?: return@registerForActivityResult
        takePersistablePermission(uri)
        outputUri = uri
        tvOutput.text = displayName(uri)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        btnSelectSource = findViewById(R.id.btn_select_source)
        btnSelectOutput = findViewById(R.id.btn_select_output)
        btnExport = findViewById(R.id.btn_export)
        spinnerBook = findViewById(R.id.spinner_book)
        tvSource = findViewById(R.id.tv_source)
        tvOutput = findViewById(R.id.tv_output)
        tvBookInfo = findViewById(R.id.tv_book_info)
        tvStatus = findViewById(R.id.tv_status)
        tvLog = findViewById(R.id.tv_log)
        cbTxt = findViewById(R.id.cb_txt)
        cbMd = findViewById(R.id.cb_md)
        cbEpub = findViewById(R.id.cb_epub)
        cbXhtml = findViewById(R.id.cb_xhtml)
        cbComplete = findViewById(R.id.cb_complete)
        tvLog.movementMethod = ScrollingMovementMethod()

        btnSelectSource.setOnClickListener {
            sourceLauncher.launch(null)
        }
        btnSelectOutput.setOnClickListener {
            outputLauncher.launch(null)
        }
        btnExport.setOnClickListener {
            startExport()
        }
        spinnerBook.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long) {
                showBookInfo(position)
            }

            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
        }
    }

    private fun takePersistablePermission(uri: Uri) {
        try {
            contentResolver.takePersistableUriPermission(
                uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
        } catch (error: SecurityException) {
            appendLog("提示：目录授权未能持久化，本次会话内仍然可用。")
        }
    }

    private fun displayName(uri: Uri): String {
        val documentId = SafTree.rootDocumentId(this, uri)
        return documentId.substringAfterLast(':', documentId)
    }

    private fun setBusy(value: Boolean) {
        busy = value
        btnSelectSource.isEnabled = !value
        btnSelectOutput.isEnabled = !value
        btnExport.isEnabled = !value
        spinnerBook.isEnabled = !value
    }

    private fun appendLog(message: String) {
        tvLog.append(message + "\n")
    }

    private fun setStatus(message: String) {
        tvStatus.text = message
    }

    private fun scanBackup(uri: Uri) {
        setBusy(true)
        setStatus("正在扫描备份目录…")
        lifecycleScope.launch {
            try {
                val found = withContext(Dispatchers.IO) {
                    QidianBackupReader.scan(this@MainActivity, uri)
                }
                books = found
                val labels = found.map { "${it.title}（${it.bookId}）" }
                spinnerBook.adapter = ArrayAdapter(
                    this@MainActivity,
                    android.R.layout.simple_spinner_dropdown_item,
                    if (labels.isEmpty()) listOf("此备份目录中没有起点书籍") else labels,
                )
                showBookInfo(0)
                setBusy(false)
                setStatus(if (found.isEmpty()) "未找到起点书籍" else "找到 ${found.size} 本备份书籍 · 请选择")
                appendLog("扫描完成，共 ${found.size} 本起点备份书籍。")
            } catch (error: Exception) {
                setBusy(false)
                setStatus("扫描失败")
                appendLog("错误：${error.message}")
            }
        }
    }

    private fun selectedBook(): BackupInfo? {
        if (books.isEmpty()) {
            return null
        }
        val position = spinnerBook.selectedItemPosition
        return books.getOrNull(position)
    }

    private fun showBookInfo(position: Int) {
        val info = books.getOrNull(position)
        tvBookInfo.text = info?.let {
            "作者：${it.author}\n书籍 ID：${it.bookId}\n用户目录：${it.qdUserDir}\n结构检查：${it.statusNote}"
        } ?: "尚未选择书籍"
    }

    private fun startExport() {
        val source = sourceUri
        val output = outputUri
        val info = selectedBook()
        val formats = buildList {
            if (cbTxt.isChecked) add(ExportFormat.TXT)
            if (cbMd.isChecked) add(ExportFormat.MARKDOWN)
            if (cbEpub.isChecked) add(ExportFormat.EPUB)
            if (cbXhtml.isChecked) add(ExportFormat.XHTML)
        }
        when {
            busy -> return
            source == null -> {
                setStatus("请先选择备份目录")
                return
            }
            info == null -> {
                setStatus("备份目录中没有可导出的书籍")
                return
            }
            !info.ready -> {
                setStatus("备份结构不完整：${info.statusNote}")
                return
            }
            output == null -> {
                setStatus("请先选择输出位置")
                return
            }
            formats.isEmpty() -> {
                setStatus("请至少选择一个导出格式")
                return
            }
        }
        setBusy(true)
        setStatus("正在导出…")
        appendLog("开始导出《${info.title}》，格式：${formats.joinToString("/") { it.value }}")
        lifecycleScope.launch {
            try {
                val summary: ExportSummary = withContext(Dispatchers.IO) {
                    val engine = ExportEngine(applicationContext)
                    engine.export(
                        sourceTreeUri = source,
                        info = info,
                        formats = formats,
                        requireComplete = cbComplete.isChecked,
                        outputTreeUri = output,
                        progress = { message -> runOnUiThread { appendLog(message) } },
                    )
                }
                setBusy(false)
                setStatus("导出完成 · ${summary.chapterCount} 个目录项")
                appendLog("输出目录：${summary.outputName}")
                summary.missingCount?.let { appendLog("目录缺失 $it 项，详见 conversion_report.json") }
                appendLog("生成文件：\n" + summary.files.joinToString("\n"))
            } catch (error: Exception) {
                setBusy(false)
                setStatus("导出失败")
                appendLog("错误：${error.message}")
            }
        }
    }
}
