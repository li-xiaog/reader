"""中文原生桌面界面：收集请求、呈现真实进度，并委托应用服务完成任务。"""

from __future__ import annotations

import os
import queue
import sys
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import TYPE_CHECKING

from reader_cache_toolkit import __version__
from reader_cache_toolkit.models import (
    Channel,
    ConversionRequest,
    ConversionResult,
    ExportFormat,
)

if TYPE_CHECKING:
    from reader_cache_toolkit.infrastructure.local_source import LocalBook
    from reader_cache_toolkit.service import ConversionService


CHANNEL_NAMES: dict[str, Channel] = {
    "番茄小说": Channel.FANQIE,
    "起点读书": Channel.QIDIAN,
    "微信读书": Channel.WEREAD,
}


def locate_asset(name: str) -> Path | None:
    """定位包内资源文件，兼容源码运行与 PyInstaller 解包两种布局。参数：name 资源文件名。返回：实际存在的资源路径；缺失时返回 None。"""
    # 1. PyInstaller 打包后资源位于解包目录的 assets 子目录。
    bases: list[Path] = []
    bundle_base = getattr(sys, "_MEIPASS", None)
    if bundle_base:
        bases.append(Path(bundle_base))
    # 2. 源码运行时资源随包存放于 src/reader_cache_toolkit/assets。
    bases.append(Path(__file__).resolve().parent.parent)
    for base in bases:
        candidate = base / "assets" / name
        if candidate.is_file():
            return candidate
    return None


class DesktopWindow:
    """桌面呈现层。参数：Tk 根窗口、转换服务。返回：管理界面生命周期的实例。"""

    def __init__(self, root: tk.Tk, service: ConversionService) -> None:
        """创建界面与后台调度。参数：root 主窗口、service 应用服务。返回：无。"""
        # 1. 建立界面状态与单任务执行器，后台线程只向队列写入事件。
        self.root = root
        self.service = service
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reader-cache")
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False
        self._closed = False
        self._poll_job: str | None = None
        self._editable: list[tuple[ttk.Widget, str]] = []
        self._output_dir: Path | None = None
        self._result_files: dict[str, Path] = {}
        self._active_task = "convert"
        self._local_books: dict[str, LocalBook] = {}
        self.channel = tk.StringVar(value="番茄小说")
        self.source = tk.StringVar()
        self.book_id = tk.StringVar()
        self.output = tk.StringVar()
        self.account_id = tk.StringVar()
        self.reader_db = tk.StringVar()
        self.require_complete = tk.BooleanVar(value=False)
        self.formats = {
            item: tk.BooleanVar(value=item in (ExportFormat.TXT, ExportFormat.EPUB)) for item in ExportFormat
        }
        self.status = tk.StringVar(value="就绪 · 请选择本地缓存目录")
        self.feedback = tk.StringVar()
        self.local_source = tk.StringVar()
        self.local_qdconfig = tk.StringVar()
        self.local_output = tk.StringVar()
        self.local_status = tk.StringVar(value="请选择缓存目录并扫描书籍")
        self.local_feedback = tk.StringVar()

        # 2. 配置原生窗口、菜单栏、可读字号和统一间距。
        self._configure_window()
        self._configure_styles()
        self._build_menu()
        self._build_layout()

        # 3. 同步渠道专属输入的可用性，所有 Tk 调用保持在主线程。
        self._update_channel()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 4. 定时消费工作线程事件，关闭时取消定时器。
        self._poll_job = self.root.after(100, self._pump_events)

    def _configure_window(self) -> None:
        """设置标题、图标、初始大小与最小尺寸。参数：无。返回：无。"""
        self.root.title(f"Reader Cache Toolkit v{__version__} · 阅读缓存工具")
        self._load_window_icon()
        width = min(1180, self.root.winfo_screenwidth() - 80)
        height = min(900, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{max(920, width)}x{max(700, height)}")
        self.root.minsize(920, 700)
        self.root.configure(background="#F3F5F7")
        self.root.option_add("*Font", ("Microsoft YaHei UI", 12))

    def _load_window_icon(self) -> None:
        """为窗口加载应用图标。参数：无。返回：无；资源缺失或平台不支持时保持默认外观。"""
        icon_path = locate_asset("app.ico")
        if icon_path is None:
            return
        try:
            self.root.iconbitmap(str(icon_path))
        except tk.TclError:
            self.root.title(self.root.title())

    def _build_menu(self) -> None:
        """建立顶部菜单栏与关于入口。参数：无。返回：无。"""
        menu = tk.Menu(self.root)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="关于阅读缓存工具", command=self._show_about)
        menu.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menu)

    def _show_about(self) -> None:
        """展示版本号与工具定位说明。参数：无。返回：无。"""
        messagebox.showinfo(
            "关于阅读缓存工具",
            f"Reader Cache Toolkit v{__version__}\n\n"
            "本地安卓阅读缓存备份与多格式导出工具，支持番茄小说、起点读书、微信读书，"
            "可导出 TXT / Markdown / EPUB / XHTML。\n\n"
            "原始缓存只读处理；缺失章节与错误如实记录；不联网获取书籍，不启动网络服务。",
            parent=self.root,
        )

    def _configure_styles(self) -> None:
        """定义共享的原生控件样式。参数：无。返回：无。"""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 12), foreground="#203343")
        style.configure("TFrame", background="#F3F5F7")
        style.configure("TLabel", background="#F3F5F7")
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 11), foreground="#536775")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Status.TLabel", foreground="#176F69", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Error.TLabel", foreground="#AD2C35", font=("Microsoft YaHei UI", 11))
        style.configure("TButton", padding=(14, 8), borderwidth=1)
        style.configure(
            "Primary.TButton",
            background="#176F69",
            foreground="white",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("disabled", "#A7BDBB"), ("active", "#125C57")],
            foreground=[("disabled", "#F3F5F7")],
        )
        style.configure("TEntry", padding=(8, 7), fieldbackground="white")
        style.configure("TCombobox", padding=(8, 7), fieldbackground="white")
        style.configure("TCheckbutton", background="#F3F5F7", padding=(0, 5))
        style.configure("TNotebook", background="#F3F5F7", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 10), font=("Microsoft YaHei UI", 12))
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", "#176F69")])
        style.configure(
            "Treeview",
            font=("Microsoft YaHei UI", 11),
            rowheight=34,
            background="white",
            fieldbackground="white",
        )
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 11, "bold"), padding=(8, 7))

    def _build_layout(self) -> None:
        """构建标题、功能页签和任务反馈区域。参数：无。返回：无。"""
        shell = ttk.Frame(self.root, padding=(24, 18, 24, 16))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        ttk.Label(header, text="阅读缓存工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="番茄小说 / 起点读书 / 微信读书    ·    本地缓存整理与多格式导出",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        self.tabs = ttk.Notebook(shell)
        self.tabs.grid(row=1, column=0, sticky="nsew")
        self.local_tab = ttk.Frame(self.tabs, padding=(18, 18))
        self.convert_tab = ttk.Frame(self.tabs, padding=(18, 16))
        self.tabs.add(self.local_tab, text="选择目录导出")
        self.tabs.add(self.convert_tab, text="本地缓存转换")
        self._build_local_tab()
        self._build_conversion_tab()

        ttk.Label(
            shell,
            text="原始缓存只读处理  ·  导出写入独立目录  ·  缺失章节与错误会如实报告",
            style="Hint.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(12, 0))

    def _build_conversion_tab(self) -> None:
        """构建本地转换表单、操作区与输出区域。参数：无。返回：无。"""
        # 1. 创建渠道和输入路径字段。
        self.convert_tab.columnconfigure(0, weight=1)
        self.convert_tab.rowconfigure(4, weight=1)
        form = ttk.Frame(self.convert_tab)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text="书籍渠道").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 12))
        channels = ttk.Combobox(
            form, textvariable=self.channel, values=tuple(CHANNEL_NAMES), state="readonly", width=16
        )
        channels.grid(row=0, column=1, sticky="ew", pady=(0, 12))
        channels.bind("<<ComboboxSelected>>", self._update_channel)
        self._editable.append((channels, "readonly"))
        ttk.Label(form, text="书籍 ID").grid(row=0, column=2, sticky="w", padx=(24, 12), pady=(0, 12))
        book_entry = ttk.Entry(form, textvariable=self.book_id)
        book_entry.grid(row=0, column=3, columnspan=2, sticky="ew", pady=(0, 12))
        self._editable.append((book_entry, "normal"))
        self._path_row(form, 1, "缓存目录", self.source, self._choose_source, "选择目录…")
        self._path_row(form, 2, "输出新目录", self.output, self._choose_output, "选择父目录…")
        ttk.Label(
            form, text="输出目录须尚不存在；选择父目录会自动填写一个新的子目录路径。", style="Hint.TLabel"
        ).grid(row=3, column=1, columnspan=4, sticky="w", pady=(0, 10))

        # 2. 微信专属配置与导出策略共用同一应用请求。
        self.weread_form = ttk.Frame(form)
        self.weread_form.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(0, 6))
        self.weread_form.columnconfigure(1, weight=1)
        self.weread_form.columnconfigure(3, weight=3)
        ttk.Label(self.weread_form, text="微信账号 ID").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.account_entry = ttk.Entry(self.weread_form, textvariable=self.account_id, width=16)
        self.account_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(self.weread_form, text="WRReader 数据库").grid(row=0, column=2, sticky="w", padx=(20, 12))
        self.db_entry = ttk.Entry(self.weread_form, textvariable=self.reader_db)
        self.db_entry.grid(row=0, column=3, sticky="ew")
        self.db_button = ttk.Button(self.weread_form, text="选择文件…", command=self._choose_reader_db)
        self.db_button.grid(row=0, column=4, padx=(10, 0))
        self._editable.extend(
            ((self.account_entry, "normal"), (self.db_entry, "normal"), (self.db_button, "normal"))
        )

        options = ttk.Frame(self.convert_tab)
        options.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        ttk.Label(options, text="导出格式", style="Section.TLabel").pack(side="left", padx=(0, 18))
        for export_format, variable in self.formats.items():
            label = "Markdown" if export_format is ExportFormat.MARKDOWN else export_format.value.upper()
            checkbox = ttk.Checkbutton(options, text=label, variable=variable)
            checkbox.pack(side="left", padx=(0, 18))
            self._editable.append((checkbox, "normal"))
        complete = ttk.Checkbutton(options, text="仅在章节完整时导出", variable=self.require_complete)
        complete.pack(side="right")
        self._editable.append((complete, "normal"))

        # 3. 提供明确的执行状态，不以虚构百分比表示进度。
        actions = ttk.Frame(self.convert_tab)
        actions.grid(row=2, column=0, sticky="ew", pady=(6, 10))
        self.convert_button = ttk.Button(
            actions, text="开始转换", style="Primary.TButton", command=self._start_conversion
        )
        self.convert_button.pack(side="left")
        self.open_output_button = ttk.Button(
            actions, text="打开输出目录", command=self._open_output, state="disabled"
        )
        self.open_output_button.pack(side="left", padx=10)
        ttk.Label(actions, textvariable=self.status, style="Status.TLabel").pack(side="left", padx=(8, 0))
        self.feedback_label = ttk.Label(
            self.convert_tab, textvariable=self.feedback, style="Error.TLabel", wraplength=900
        )
        self.feedback_label.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.convert_tab.bind("<Configure>", self._resize_feedback)

        # 4. 日志与结果分开呈现，结果仅来自服务实际返回的文件。
        self.activity = ttk.Notebook(self.convert_tab)
        self.activity.grid(row=4, column=0, sticky="nsew")
        log_frame = ttk.Frame(self.activity)
        results_frame = ttk.Frame(self.activity)
        self.activity.add(log_frame, text="执行日志")
        self.activity.add(results_frame, text="导出文件")
        self.log = ScrolledText(
            log_frame,
            height=7,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 11),
            background="white",
            foreground="#203343",
            relief="flat",
            padx=14,
            pady=12,
        )
        self.log.pack(fill="both", expand=True)
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)
        self.result_tree = ttk.Treeview(
            results_frame, columns=("kind", "path"), show="headings", selectmode="browse", height=5
        )
        self.result_tree.heading("kind", text="格式")
        self.result_tree.heading("path", text="文件路径（双击打开）")
        self.result_tree.column("kind", width=100, minwidth=80, stretch=False)
        self.result_tree.column("path", width=650, minwidth=300)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(results_frame, orient="vertical", command=self.result_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(results_frame, orient="horizontal", command=self.result_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.result_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.result_tree.bind("<Double-1>", self._open_selected_result)

    def _build_local_tab(self) -> None:
        """构建“选择目录导出”页：智能扫描、书籍选择、格式与输出。参数：无。返回：无。"""
        # 1. 三个路径输入：缓存目录、可选 QDConfig 与输出新目录。
        self.local_tab.columnconfigure(0, weight=1)
        self.local_tab.rowconfigure(2, weight=2)
        self.local_tab.rowconfigure(6, weight=1)
        form = ttk.Frame(self.local_tab)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self._path_row(form, 0, "缓存目录", self.local_source, self._choose_local_source, "选择目录…")
        ttk.Label(
            form,
            text="支持桌面工具备份目录、从模拟器手动复制的 QDReader/book 缓存目录，"
            "或包含多个备份的上级目录（一次扫描可发现多本书）。",
            style="Hint.TLabel",
        ).grid(row=1, column=1, columnspan=4, sticky="w", pady=(0, 8))
        self._path_row(form, 2, "QDConfig 数据库", self.local_qdconfig, self._choose_local_qdconfig, "选择文件…")
        ttk.Label(
            form,
            text="解密必需；扫描通常能在目录中自动找到，找不到时请手动指定（桌面备份在 database/databases 内）。",
            style="Hint.TLabel",
        ).grid(row=3, column=1, columnspan=4, sticky="w", pady=(0, 8))
        self._path_row(form, 4, "输出新目录", self.local_output, self._choose_local_output, "选择父目录…")

        # 2. 扫描按钮与书籍列表：自动识别书籍，无需手动填写书籍 ID。
        scan_actions = ttk.Frame(self.local_tab)
        scan_actions.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.scan_local_button = ttk.Button(scan_actions, text="扫描书籍", command=self._start_local_scan)
        self.scan_local_button.pack(side="left")
        self._editable.append((self.scan_local_button, "normal"))
        ttk.Label(
            scan_actions,
            text="自动识别备份目录与原始缓存中的起点书籍",
            style="Hint.TLabel",
        ).pack(side="left", padx=(12, 0))

        book_frame = ttk.Frame(self.local_tab)
        book_frame.grid(row=2, column=0, sticky="nsew")
        book_frame.columnconfigure(0, weight=1)
        book_frame.rowconfigure(0, weight=1)
        self.local_tree = ttk.Treeview(
            book_frame,
            columns=("title", "id", "author", "note"),
            show="headings",
            selectmode="browse",
            height=4,
        )
        for column, title, width in (
            ("title", "书籍", 300),
            ("id", "书籍 ID", 140),
            ("author", "作者", 110),
            ("note", "结构检查", 320),
        ):
            self.local_tree.heading(column, text=title)
            self.local_tree.column(column, width=width, minwidth=80)
        self.local_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(book_frame, orient="vertical", command=self.local_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.local_tree.configure(yscrollcommand=scroll.set)
        self.local_tree.bind("<<TreeviewSelect>>", self._on_local_book_selected)
        self.local_tree.bind("<Button-1>", self._guard_local_interaction)
        self.local_tree.bind("<Key>", self._guard_local_interaction)

        # 3. 导出格式与完整性要求与本地转换页共享同一偏好。
        options = ttk.Frame(self.local_tab)
        options.grid(row=3, column=0, sticky="ew", pady=(8, 4))
        ttk.Label(options, text="导出格式", style="Section.TLabel").pack(side="left", padx=(0, 18))
        for export_format, variable in self.formats.items():
            label = "Markdown" if export_format is ExportFormat.MARKDOWN else export_format.value.upper()
            checkbox = ttk.Checkbutton(options, text=label, variable=variable)
            checkbox.pack(side="left", padx=(0, 18))
            self._editable.append((checkbox, "normal"))
        complete = ttk.Checkbutton(options, text="仅在章节完整时导出", variable=self.require_complete)
        complete.pack(side="right")
        self._editable.append((complete, "normal"))

        # 4. 导出操作与真实状态。
        actions = ttk.Frame(self.local_tab)
        actions.grid(row=4, column=0, sticky="ew", pady=(6, 6))
        self.local_export_button = ttk.Button(
            actions,
            text="开始导出",
            style="Primary.TButton",
            command=self._start_local_export,
            state="disabled",
        )
        self.local_export_button.pack(side="left")
        self.local_open_button = ttk.Button(
            actions, text="打开输出目录", command=self._open_output, state="disabled"
        )
        self.local_open_button.pack(side="left", padx=10)
        ttk.Label(actions, textvariable=self.local_status, style="Status.TLabel").pack(side="left", padx=(8, 0))
        self.local_feedback_label = ttk.Label(
            self.local_tab, textvariable=self.local_feedback, style="Error.TLabel", wraplength=900
        )
        self.local_feedback_label.grid(row=5, column=0, sticky="ew", pady=(0, 6))

        # 5. 扫描与导出的真实进度日志。
        self.local_log = ScrolledText(
            self.local_tab,
            height=4,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 11),
            background="white",
            foreground="#203343",
            relief="flat",
            padx=12,
            pady=10,
        )
        self.local_log.grid(row=6, column=0, sticky="nsew")

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Callable[[], None],
        button_text: str,
    ) -> None:
        """创建路径输入行。参数：parent 父容器、row 行号、label 标题、variable 绑定值、command 选择操作、button_text 按钮文本。返回：无。"""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 10))
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, columnspan=3, sticky="ew", pady=(0, 10))
        button = ttk.Button(parent, text=button_text, command=command)
        button.grid(row=row, column=4, padx=(10, 0), pady=(0, 10))
        self._editable.extend(((entry, "normal"), (button, "normal")))

    def _choose_source(self) -> None:
        """通过目录选择器填写真实缓存目录。参数：无。返回：无。"""
        path = filedialog.askdirectory(parent=self.root, title="选择本地缓存或备份目录", mustexist=True)
        if path:
            self.source.set(path)

    def _choose_output(self) -> None:
        """选择输出父目录并建议新的子目录路径。参数：无。返回：无。"""
        path = filedialog.askdirectory(
            parent=self.root, title="选择输出父目录，将写入新的子目录", mustexist=True
        )
        if path:
            channel = CHANNEL_NAMES[self.channel.get()]
            name = f"{channel.value}_export_{datetime.now():%Y%m%d_%H%M%S}"
            self.output.set(str(Path(path) / name))

    def _choose_reader_db(self) -> None:
        """选择微信读书实际数据库文件。参数：无。返回：无。"""
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择 WRReader 数据库",
            filetypes=(("数据库文件", "*.db *.sqlite *.sqlite3"), ("所有文件", "*")),
        )
        if path:
            self.reader_db.set(path)

    def _choose_local_source(self) -> None:
        """通过目录选择器填写真实缓存目录。参数：无。返回：无。"""
        path = filedialog.askdirectory(
            parent=self.root, title="选择缓存目录（备份目录或 QDReader/book）", mustexist=True
        )
        if path:
            self.local_source.set(path)

    def _choose_local_qdconfig(self) -> None:
        """选择 QDConfig 设备标识数据库文件。参数：无。返回：无。"""
        path = filedialog.askopenfilename(
            parent=self.root, title="选择 QDConfig 数据库（解密必需）", filetypes=(("所有文件", "*"),)
        )
        if path:
            self.local_qdconfig.set(path)

    def _choose_local_output(self) -> None:
        """选择输出父目录并建议新的子目录路径。参数：无。返回：无。"""
        path = filedialog.askdirectory(
            parent=self.root, title="选择输出父目录，将写入新的子目录", mustexist=True
        )
        if path:
            name = f"qidian_export_{datetime.now():%Y%m%d_%H%M%S}"
            self.local_output.set(str(Path(path) / name))

    def _guard_local_interaction(self, event: tk.Event) -> str | None:
        """执行任务时阻止修改书籍选择。参数：event 鼠标或键盘事件。返回：忙碌时返回 break，否则返回 None。"""
        return "break" if self._busy else None

    def _update_local_actions(self) -> None:
        """根据书籍选择、结构完整性与任务状态启用导出操作。参数：无。返回：无。"""
        selected = self.local_tree.selection()
        book = self._local_books.get(selected[0]) if selected else None
        self.local_export_button.configure(
            state="normal" if book is not None and book.ready and not self._busy else "disabled"
        )

    def _on_local_book_selected(self, event: tk.Event | None = None) -> None:
        """书籍选中后预填扫描到的 QDConfig 并刷新按钮状态。参数：event 可选 Tk 选择事件。返回：无。"""
        selected = self.local_tree.selection()
        book = self._local_books.get(selected[0]) if selected else None
        if book is not None and book.qdconfig_path is not None:
            self.local_qdconfig.set(str(book.qdconfig_path))
        self._update_local_actions()

    def _start_local_scan(self) -> None:
        """异步扫描用户手动选择的缓存目录。参数：无。返回：无。"""
        # 1. 拦截重复提交与非法目录。
        if self._busy:
            return
        text = self.local_source.get().strip()
        if not text:
            self.local_feedback.set("请先选择缓存目录。")
            return
        root = Path(text).expanduser()
        if not root.is_dir():
            self.local_feedback.set(f"缓存目录不存在或不是文件夹：{root}")
            return
        self._active_task = "local-scan"
        self.local_feedback.set("")
        self.local_status.set("正在扫描缓存目录…")
        self.local_log.configure(state="normal")
        self.local_log.delete("1.0", "end")
        self.local_log.configure(state="disabled")
        self._set_busy(True)
        # 2. 后台线程执行扫描，结果通过队列回到主线程。
        self._executor.submit(self._local_scan_worker, root)

    def _local_scan_worker(self, root: Path) -> None:
        """在工作线程执行智能目录扫描。参数：root 缓存目录。返回：无。"""
        try:
            from reader_cache_toolkit.infrastructure.local_source import scan_local_source

            books = scan_local_source(root, self._publish_progress)
        except Exception as error:
            self._events.put(("error", f"{type(error).__name__}: {error}"))
        else:
            self._events.put(("local-books", (root, books)))

    def _show_local_books(self, root: Path, books: tuple) -> None:
        """呈现扫描返回的实际书籍列表。参数：root 扫描目录、books 书籍记录。返回：无。"""
        self._local_books.clear()
        for item in self.local_tree.get_children():
            self.local_tree.delete(item)
        for book in books:
            item = self.local_tree.insert(
                "", "end", values=(book.title, book.book_id, book.author, book.note)
            )
            self._local_books[item] = book
        self._set_busy(False)
        self.local_status.set(
            f"找到 {len(books)} 本书 · 请选择要导出的书籍" if books else "此目录中没有发现可导出的起点书籍"
        )
        self._append_log(f"扫描完成：{root}")
        self._append_log(f"共发现 {len(books)} 本起点书籍，选中书籍即可开始导出。")

    def _start_local_export(self) -> None:
        """校验扫描结果与输出目录后提交导出任务。参数：无。返回：无。"""
        # 1. 确认所选书籍来自当前扫描结果且结构完整。
        if self._busy:
            return
        try:
            selected = self.local_tree.selection()
            book = self._local_books.get(selected[0]) if selected else None
            if book is None:
                raise ValueError("请先扫描并选择一本书籍。")
            if not book.ready:
                raise ValueError(f"备份结构不完整：{book.note}")
            if not self.local_output.get().strip():
                raise ValueError("请填写输出新目录，或选择一个输出父目录。")
            output = Path(self.local_output.get().strip()).expanduser()
            if output.exists():
                raise ValueError("输出目录已经存在，请填写新的目录名称，避免覆盖已有文件。")
            selected_formats = tuple(item for item, variable in self.formats.items() if variable.get())
            if not selected_formats:
                raise ValueError("请至少选择一种导出格式。")
            override_text = self.local_qdconfig.get().strip()
            override = Path(override_text).expanduser() if override_text else None
            if override is not None and not override.is_file():
                raise ValueError(f"QDConfig 数据库不存在或不是文件：{override}")
        except (ValueError, OSError) as error:
            self.local_feedback.set(str(error))
            return
        # 2. 清除上次任务结果，锁定输入并显示任务阶段。
        self._output_dir = None
        self.local_open_button.configure(state="disabled")
        self.result_tree.delete(*self.result_tree.get_children())
        self.local_log.configure(state="normal")
        self.local_log.delete("1.0", "end")
        self.local_log.configure(state="disabled")
        self._active_task = "local-export"
        self.local_feedback.set("")
        self.local_status.set("正在导出 · 请查看执行日志")
        self._append_log(f"开始导出：《{book.title}》书籍 {book.book_id}（{book.layout} 布局）")
        self._append_log(f"输出目录：{output}")
        self._set_busy(True)
        # 3. 后台线程完成解码与统一导出服务调用。
        self._executor.submit(
            self._local_export_worker, book, override, output, selected_formats, self.require_complete.get()
        )

    def _local_export_worker(
        self,
        book: LocalBook,
        override: Path | None,
        output: Path,
        formats: tuple[ExportFormat, ...],
        require_complete: bool,
    ) -> None:
        """在工作线程解码书籍并调用统一导出服务。参数：book 扫描书籍、override QDConfig 覆盖、output 输出新目录、formats 格式、require_complete 完整性要求。返回：无。"""
        try:
            from reader_cache_toolkit.infrastructure.local_source import load_local_book, resolve_qdconfig

            qdconfig = resolve_qdconfig(book, override)
            document = load_local_book(book, qdconfig, self._publish_progress)
            result = self.service.export_book(
                document, output, formats, require_complete, self._publish_progress
            )
        except Exception as error:
            self._events.put(("error", f"{type(error).__name__}: {error}"))
        else:
            self._events.put(("result", result))

    def _update_channel(self, event: tk.Event | None = None) -> None:
        """按渠道显示微信读书专属字段。参数：event 可选 Tk 选择事件。返回：无。"""
        if CHANNEL_NAMES[self.channel.get()] is Channel.WEREAD:
            self.weread_form.grid()
        else:
            self.weread_form.grid_remove()

    def _resize_feedback(self, event: tk.Event) -> None:
        """根据窗口宽度换行错误说明。参数：event 容器尺寸事件。返回：无。"""
        self.feedback_label.configure(wraplength=max(300, event.width - 40))
        self.local_feedback_label.configure(wraplength=max(300, event.width - 40))

    def _make_request(self) -> ConversionRequest:
        """验证必填项并构造统一请求。参数：无。返回：ConversionRequest；输入不完整时抛出 ValueError。"""
        # 1. 检查输入身份与真实文件路径。
        if not self.source.get().strip():
            raise ValueError("请选择缓存目录。")
        source = Path(self.source.get().strip()).expanduser()
        if not source.is_dir():
            raise ValueError(f"缓存目录不存在或不是文件夹：{source}")
        book_id = self.book_id.get().strip()
        if not book_id:
            raise ValueError("请填写书籍 ID。")

        # 2. 输出必须指向尚未创建的独立目录。
        if not self.output.get().strip():
            raise ValueError("请填写输出新目录，或选择一个输出父目录。")
        output_dir = Path(self.output.get().strip()).expanduser()
        if output_dir.exists():
            raise ValueError("输出目录已经存在，请填写新的目录名称，避免覆盖已有文件。")
        selected_formats = tuple(item for item, variable in self.formats.items() if variable.get())
        if not selected_formats:
            raise ValueError("请至少选择一种导出格式。")

        # 3. 微信参数只在选择微信读书时传入，详细要求由适配器校验。
        channel = CHANNEL_NAMES[self.channel.get()]
        account_id = self.account_id.get().strip() if channel is Channel.WEREAD else None
        database_text = self.reader_db.get().strip() if channel is Channel.WEREAD else ""
        reader_db = Path(database_text).expanduser() if database_text else None
        if reader_db is not None and not reader_db.is_file():
            raise ValueError(f"WRReader 数据库不存在或不是文件：{reader_db}")

        # 4. 使用不可变领域请求交给与命令行共用的应用服务。
        return ConversionRequest(
            channel=channel,
            source=source,
            book_id=book_id,
            output_dir=output_dir,
            formats=selected_formats,
            account_id=account_id or None,
            reader_db=reader_db,
            require_complete=self.require_complete.get(),
        )

    def _start_conversion(self) -> None:
        """校验并提交单个转换任务。参数：无。返回：无；校验错误显示在界面中。"""
        # 1. 拦截重复提交与非法请求。
        if self._busy:
            return
        try:
            request = self._make_request()
        except (ValueError, OSError) as error:
            self.feedback.set(str(error))
            self.status.set("请检查输入")
            return

        # 2. 清除上一个任务的结果，避免展示过期文件。
        self.feedback.set("")
        self._output_dir = None
        self._result_files.clear()
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)
        self.open_output_button.configure(state="disabled")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.activity.select(0)

        # 3. 锁定可变输入并明确显示任务阶段。
        self._active_task = "convert"
        self._set_busy(True)
        self.status.set("正在转换 · 请查看执行日志")
        self._append_log(f"开始转换：{self.channel.get()} / 书籍 {request.book_id}")
        self._append_log(f"输入目录：{request.source}")
        self._append_log(f"输出目录：{request.output_dir}")

        # 4. 后台线程只调用应用服务，结果通过队列回到主线程。
        self._executor.submit(self._convert_worker, request)

    def _set_busy(self, busy: bool) -> None:
        """同步任务状态与可编辑控件。参数：busy 是否正在执行。返回：无。"""
        self._busy = busy
        for widget, normal_state in self._editable:
            widget.configure(state="disabled" if busy else normal_state)
        self.convert_button.configure(state="disabled" if busy else "normal")
        self.local_tree.state(("disabled",) if busy else ("!disabled",))
        self._update_local_actions()

    def _convert_worker(self, request: ConversionRequest) -> None:
        """在工作线程执行转换并发送结果。参数：request 不可变转换请求。返回：无。"""
        try:
            result = self.service.convert(request, self._publish_progress)
        except Exception as error:
            self._events.put(("error", f"{type(error).__name__}: {error}"))
        else:
            self._events.put(("result", result))

    def _publish_progress(self, message: str) -> None:
        """把应用服务真实进度写入线程安全队列。参数：message 进度文本。返回：无。"""
        self._events.put(("progress", message))

    def _pump_events(self) -> None:
        """在 Tk 主线程消费任务消息。参数：无。返回：无。"""
        # 1. 窗口关闭后不再操作 Tk 控件。
        if self._closed:
            return
        # 2. 每轮处理有限事件，避免大量日志阻塞窗口交互。
        for _ in range(200):
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            # 3. 进度与完成事件只在主线程更新界面。
            if kind == "progress":
                self._append_log(str(payload))
            elif kind == "result" and isinstance(payload, ConversionResult):
                self._show_result(payload)
            elif kind == "local-books":
                root, books = payload
                self._show_local_books(root, books)
            elif kind == "error":
                self._show_error(str(payload))
        # 4. 窗口存续时安排下一轮；后台线程从不调用 after。
        if not self._closed:
            self._poll_job = self.root.after(100, self._pump_events)

    def _append_log(self, message: str) -> None:
        """追加可复制的时间戳日志。参数：message 真实事件文本。返回：无。"""
        target = self.log if self._active_task == "convert" else self.local_log
        target.configure(state="normal")
        target.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        target.see("end")
        target.configure(state="disabled")

    def _show_result(self, result: ConversionResult) -> None:
        """显示服务返回的实际输出文件。参数：result 转换结果。返回：无。"""
        self._set_busy(False)
        self._output_dir = result.output_dir
        self.open_output_button.configure(state="normal")
        self.local_open_button.configure(state="normal")
        if self._active_task == "convert":
            self.status.set(f"转换完成 · {result.chapter_count} 个目录项")
        else:
            self.local_status.set(f"导出完成 · {result.chapter_count} 个目录项")
        files = tuple(dict.fromkeys((*result.files, result.report_path)))
        for path in files:
            kind = "转换报告" if path == result.report_path else path.suffix.lstrip(".").upper()
            item = self.result_tree.insert("", "end", values=(kind, str(path)))
            self._result_files[item] = path
        self._append_log(f"转换完成，实际导出 {result.chapter_count} 个目录项。")
        self._append_log(f"转换报告：{result.report_path}")
        if self._active_task == "convert":
            self.activity.select(1)

    def _show_error(self, message: str) -> None:
        """明确呈现任务失败原因。参数：message 实际异常文本。返回：无。"""
        self._set_busy(False)
        if self._active_task == "convert":
            self.status.set("转换失败")
            self.feedback.set(message)
            self.activity.select(0)
        else:
            self.local_status.set("操作失败 · 请查看错误详情")
            self.local_feedback.set(message)
        self._append_log(f"错误：{message}")

    def _open_output(self) -> None:
        """用系统文件管理器打开完成任务的目录。参数：无。返回：无。"""
        if self._output_dir is not None:
            self._open_path(self._output_dir)

    def _open_selected_result(self, event: tk.Event | None = None) -> None:
        """用系统关联应用打开选中的真实结果。参数：event 可选 Tk 双击事件。返回：无。"""
        selected = self.result_tree.selection()
        if selected and selected[0] in self._result_files:
            self._open_path(self._result_files[selected[0]])

    def _open_path(self, path: Path) -> None:
        """打开本机现存文件或目录并呈现失败。参数：path 实际输出路径。返回：无。"""
        try:
            if not path.exists():
                raise FileNotFoundError(f"文件或目录已经不存在：{path}")
            os.startfile(str(path))
        except OSError as error:
            self.feedback.set(f"无法打开：{error}")

    def _on_close(self) -> None:
        """关闭窗口并清理调度器。参数：无。返回：无；正在执行的文件任务继续直至完成。"""
        if self._busy and not messagebox.askyesno(
            "任务仍在进行",
            "关闭窗口后，当前任务仍会继续，完成后程序退出。\n\n是否关闭窗口？",
            parent=self.root,
        ):
            return
        self._closed = True
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def run_gui() -> None:
    """启动中文桌面应用。参数：无。返回：窗口关闭后返回 None。"""
    from reader_cache_toolkit.service import ConversionService

    root = tk.Tk()
    DesktopWindow(root, ConversionService())
    root.mainloop()
