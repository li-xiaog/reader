"""显式设备选择下的 ADB 只读扫描与原始缓存备份。"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from reader_cache_toolkit.errors import ReaderCacheError
from reader_cache_toolkit.models import Channel, ProgressCallback

PACKAGES = {
    Channel.FANQIE: "com.dragon.read",
    Channel.QIDIAN: "com.qidian.QDReader",
    Channel.WEREAD: "com.tencent.weread",
}
SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_-]+\Z")
FANQIE_CACHE = re.compile(r"prefix_public_([0-9]+)\Z")


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """ADB 设备记录。参数：serial 序列号、state 状态、description 描述。返回：不可变的真实设备信息。"""

    serial: str
    state: str
    description: str


@dataclass(frozen=True, slots=True)
class CachedBook:
    """已定位的缓存书籍。参数：渠道、身份、账号、远端目录及元数据。返回：供界面选择和备份的不可变记录。"""

    channel: Channel
    book_id: str
    title: str
    author: str
    account_id: str
    remote_cache: str
    metadata: dict[str, Any] = field(default_factory=dict)


def find_adb(explicit: Path | None) -> Path:
    """定位已安装的 ADB。参数：explicit 用户指定程序路径。返回：存在的程序绝对路径，不安装或启动服务。"""
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise ReaderCacheError("指定的 ADB 程序不存在")
        return path
    located = shutil.which("adb")
    if located:
        return Path(located).resolve()
    executable = "adb.exe" if os.name == "nt" else "adb"
    candidates: list[Path] = []
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if value := os.environ.get(variable):
            candidates.append(Path(value) / "platform-tools" / executable)
    if local := os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(local) / "Android" / "Sdk" / "platform-tools" / executable)
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise ReaderCacheError("未找到 ADB，请在界面中指定已有 adb 程序")


def valid_component(value: object, label: str, numeric: bool = False) -> str:
    """校验用于远端路径的单个真实编号。参数：value 原值、label 字段名、numeric 数字限制。返回：安全组件字符串。"""
    text = str(value) if value is not None else ""
    if not SAFE_COMPONENT.fullmatch(text) or (numeric and not text.isdecimal()):
        raise ReaderCacheError(f"{label} 缺失或包含无效字符")
    return text


def required_text(value: object, label: str, allow_empty: bool = False) -> str:
    """读取真实文本字段。参数：value 数据库值、label 字段名、allow_empty 是否允许原值为空。返回：未经伪造的文本。"""
    if value is None and allow_empty:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str) or "\ufffd" in value or (not allow_empty and not value.strip()):
        raise ReaderCacheError(f"{label} 缺失或损坏")
    return value


def sqlite_rows(
    path: Path, table: str, fields: tuple[str, ...], identity: str | None = None, value: str | None = None
) -> list[dict[str, Any]]:
    """按已验证字段查询临时数据库副本。参数：path、table、fields 及可选匹配字段和值。返回：真实记录列表。"""
    if not path.is_file():
        raise ReaderCacheError("拉取的 SQLite 主库不存在")
    if not all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        for name in (table, *fields, *((identity,) if identity else ()))
    ):
        raise ReaderCacheError("数据库查询字段不合法")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        check = connection.execute(f'PRAGMA quick_check("{table}")').fetchall()
        if len(check) != 1 or check[0][0] != "ok":
            raise ReaderCacheError("数据库快照不完整或正在变化，请重新扫描")
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        needed = {*fields, *((identity,) if identity else ())}
        if not needed <= columns:
            raise ReaderCacheError(f"数据库 {table} 缺少字段：{', '.join(sorted(needed - columns))}")
        selected = ", ".join(f'"{name}"' for name in fields)
        condition = f' WHERE "{identity}" = ?' if identity else ""
        return [
            dict(row)
            for row in connection.execute(
                f'SELECT {selected} FROM "{table}"{condition}',
                (value,) if identity else (),
            ).fetchall()
        ]
    finally:
        connection.close()


class AdbBackupService:
    """ADB 缓存服务。参数：可选的已有 ADB 路径。返回：只读设备扫描及本地备份接口。"""

    def __init__(self, adb_path: Path | None = None) -> None:
        """保存 ADB 配置。参数：adb_path 显式程序路径。返回：无；程序到实际操作时定位。"""
        self._configured_adb = adb_path

    def _run(self, arguments: list[str], *, directory: Path | None = None, timeout: int = 120) -> str:
        """运行确定的 ADB 读取命令。参数：arguments 参数、directory 本地工作目录、timeout 秒数。返回：标准输出。"""
        executable = find_adb(self._configured_adb)
        try:
            result = subprocess.run(
                [str(executable), *arguments],
                cwd=directory,
                capture_output=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReaderCacheError("ADB 读取超时，请检查设备连接后重试") from exc
        except (OSError, UnicodeError) as exc:
            raise ReaderCacheError(f"无法执行 ADB 读取：{exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}"
            raise ReaderCacheError(f"ADB 读取失败：{detail}")
        return result.stdout

    def devices(self) -> tuple[DeviceInfo, ...]:
        """列出现有 ADB 设备并保留授权状态。参数：无。返回：设备元组，绝不隐式选择其中一台。"""
        output = self._run(["devices", "-l"])
        devices: list[DeviceInfo] = []
        seen: set[str] = set()
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith(("List of devices attached", "*")):
                continue
            fields = line.split(maxsplit=2)
            if len(fields) < 2 or fields[0] in seen:
                raise ReaderCacheError("ADB 返回了无效或重复的设备记录")
            seen.add(fields[0])
            devices.append(DeviceInfo(fields[0], fields[1], fields[2] if len(fields) == 3 else ""))
        return tuple(devices)

    def _ensure_device(self, serial: str) -> None:
        """确认用户明确选择的设备可读。参数：serial 非空设备序列号。返回：无，离线或未授权直接报错。"""
        if not isinstance(serial, str) or not serial.strip() or serial != serial.strip():
            raise ReaderCacheError("必须明确选择设备序列号")
        matches = [device for device in self.devices() if device.serial == serial]
        if len(matches) != 1:
            raise ReaderCacheError("所选设备已断开或不存在，请重新获取设备列表")
        if matches[0].state != "device":
            raise ReaderCacheError(f"所选设备当前状态为 {matches[0].state}，无法读取")

    def _shell(self, serial: str, command: list[str]) -> str:
        """以正确引用执行远端只读命令。参数：serial 设备、command 命令及独立参数。返回：输出文本。"""
        if not command or command[0] not in {"find", "ls", "stat"}:
            raise ReaderCacheError("缓存服务只允许远端 find、ls、stat 读取")
        return self._run(["-s", serial, "shell", shlex.join(command)])

    def _directories(self, serial: str, root: str, depth: int = 1) -> tuple[str, ...]:
        """读取指定层级目录。参数：serial 设备、root 已知应用根目录、depth 层级。返回：真实绝对目录路径。"""
        output = self._shell(
            serial, ["find", root, "-mindepth", str(depth), "-maxdepth", str(depth), "-type", "d"]
        )
        return tuple(line for line in output.splitlines() if line)

    def _names(self, serial: str, directory: str) -> set[str]:
        """列出可读目录中的真实名称。参数：serial 设备、directory 目录。返回：名称集合；权限失败不当作空目录。"""
        return {line for line in self._shell(serial, ["ls", "-1", "-A", directory]).splitlines() if line}

    def _pull(self, serial: str, remote: str, destination: Path, *, is_directory: bool = False) -> None:
        """将远端缓存复制到新建本地位置。参数：serial、remote、destination、is_directory 类型。返回：无，失败报错。"""
        if destination.exists():
            raise ReaderCacheError("ADB 临时目标已存在，拒绝覆盖")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(["-s", serial, "pull", remote, destination.name], directory=destination.parent, timeout=600)
        if (is_directory and not destination.is_dir()) or (not is_directory and not destination.is_file()):
            raise ReaderCacheError("ADB 报告成功但本地目标未生成")

    def _sqlite_family(self, serial: str, remote: str, directory: Path) -> Path:
        """拉取 SQLite 主库及实际存在的日志。参数：serial 设备、remote 主库、directory 临时目录。返回：本地主库路径。"""
        parent = str(PurePosixPath(remote).parent)
        name = PurePosixPath(remote).name
        names = self._names(serial, parent)
        if name not in names:
            raise ReaderCacheError(f"应用备份缺少必需数据库：{name}")
        for suffix in SQLITE_SUFFIXES:
            if name + suffix in names:
                self._pull(serial, remote + suffix, directory / (name + suffix))
        return directory / name

    def scan(self, channel: Channel, serial: str, progress: ProgressCallback) -> tuple[CachedBook, ...]:
        """扫描用户指定设备中的真实缓存书籍。参数：channel 渠道、serial 设备、progress 反馈。返回：可备份书籍元组。"""
        # 1. 只接受显式设备和受支持渠道，不切换权限或启动应用。
        self._ensure_device(serial)
        channel = Channel(channel)
        progress("正在读取应用目录和数据库快照")
        # 2. 所有 SQLite 读取都来自本次临时拉取的主库和日志。
        with tempfile.TemporaryDirectory(prefix="reader-cache-scan-") as temporary:
            directory = Path(temporary)
            handlers = {
                Channel.FANQIE: self._scan_fanqie,
                Channel.QIDIAN: self._scan_qidian,
                Channel.WEREAD: self._scan_weread,
            }
            books = handlers[channel](serial, directory, progress)
        # 3. 保留同书不同账号，重复身份是错误，交由用户明确选择记录。
        identities = [(book.channel, book.account_id, book.book_id) for book in books]
        if len(identities) != len(set(identities)):
            raise ReaderCacheError("扫描出现相同账号下的重复书籍记录")
        # 4. 临时数据自动清理，返回真实数量供界面展示。
        progress(f"扫描完成，找到 {len(books)} 本有实际缓存的书籍")
        return tuple(books)

    def _scan_fanqie(self, serial: str, temporary: Path, progress: ProgressCallback) -> list[CachedBook]:
        """扫描番茄账号目录和 reading_db。参数：serial、temporary 临时目录、progress 反馈。返回：真实缓存书籍列表。"""
        # 1. 找到真实数字账号目录，目录权限失败会直接终止。
        root = f"/data/user/0/{PACKAGES[Channel.FANQIE]}/files"
        books: list[CachedBook] = []
        for profile in self._directories(serial, root):
            account = PurePosixPath(profile).name
            if not account.isascii() or not account.isdecimal():
                continue
            names = self._names(serial, profile)
            caches = [
                (name, match.group(1)) for name in sorted(names) if (match := FANQIE_CACHE.fullmatch(name))
            ]
            if not caches:
                continue
            # 2. 复制该账号实际主库和日志，按真实 schema 查询书籍身份。
            remote_db = f"{profile}/reading_db_{account}"
            database = self._sqlite_family(serial, remote_db, temporary / account)
            for name, book_id in caches:
                remote_cache = f"{profile}/{name}"
                if not (self._names(serial, remote_cache) - {"image"}):
                    continue
                rows = sqlite_rows(database, "t_book", ("book_id", "name", "author"), "book_id", book_id)
                if len(rows) != 1:
                    raise ReaderCacheError("番茄实际缓存没有唯一的书籍元数据")
                # 3. 下载记录只保留原字段含义，不把 download_size 当作章节数。
                downloaded = sqlite_rows(
                    database,
                    "t_download_book",
                    ("book_id", "status", "percent", "download_size", "total_file_length"),
                    "book_id",
                    book_id,
                )
                row = rows[0]
                books.append(
                    CachedBook(
                        Channel.FANQIE,
                        book_id,
                        required_text(row["name"], "番茄书名"),
                        required_text(row["author"], "番茄作者"),
                        account,
                        remote_cache,
                        {
                            "remote_database": remote_db,
                            "device_serial": serial,
                            "download_records": downloaded,
                        },
                    )
                )
            # 4. 只反馈数量，不把账号标识写到日志。
            progress(f"番茄：已找到 {len(books)} 本缓存书籍")
        return books

    def _scan_qidian(self, serial: str, temporary: Path, progress: ProgressCallback) -> list[CachedBook]:
        """扫描起点真实用户缓存目录。参数：serial、temporary 临时目录、progress 反馈。返回：真实缓存书籍列表。"""
        # 1. 起点书籍身份取自主库，账号编号取自缓存实际父目录。
        private = f"/data/user/0/{PACKAGES[Channel.QIDIAN]}/databases"
        remote_db = f"{private}/QDReader"
        database = self._sqlite_family(serial, remote_db, temporary / "databases")
        root = f"/sdcard/Android/data/{PACKAGES[Channel.QIDIAN]}/files/QDReader/book"
        books: list[CachedBook] = []
        for remote_cache in self._directories(serial, root, 2):
            path = PurePosixPath(remote_cache)
            if (
                not path.name.isascii()
                or not path.name.isdecimal()
                or not path.parent.name.isascii()
                or not path.parent.name.isdecimal()
            ):
                continue
            # 2. 只有确实含 .qd 文件的目录进入列表。
            if not any(name.endswith(".qd") for name in self._names(serial, remote_cache)):
                continue
            book_id = path.name
            rows = sqlite_rows(database, "book", ("QDBookId", "BookName", "Author"), "QDBookId", book_id)
            if len(rows) != 1:
                raise ReaderCacheError("起点实际缓存没有唯一的书籍元数据")
            # 3. 索引是转换必需文件，缺失时明确报错。
            index_name = f"{book_id}.qd"
            if index_name not in self._names(serial, str(path.parent)):
                raise ReaderCacheError("起点缓存缺少配套 .qd 章节索引")
            row = rows[0]
            books.append(
                CachedBook(
                    Channel.QIDIAN,
                    book_id,
                    required_text(row["BookName"], "起点书名"),
                    required_text(row["Author"], "起点作者"),
                    path.parent.name,
                    remote_cache,
                    {
                        "remote_database": remote_db,
                        "remote_index": str(path.parent / index_name),
                        "device_serial": serial,
                    },
                )
            )
        # 4. 不用最近阅读记录或第一个用户目录代替用户选择。
        progress(f"起点：已找到 {len(books)} 本缓存书籍")
        return books

    def _scan_weread(self, serial: str, temporary: Path, progress: ProgressCallback) -> list[CachedBook]:
        """扫描微信读书真实 VID 目录。参数：serial、temporary 临时目录、progress 反馈。返回：独立账号缓存书籍列表。"""
        # 1. VID 来自真实数字账号目录，不使用其他意义的 Setting.accountid。
        root = f"/data/user/0/{PACKAGES[Channel.WEREAD]}/databases"
        books: list[CachedBook] = []
        for account_root in self._directories(serial, root):
            account = PurePosixPath(account_root).name
            if not account.isascii() or not account.isdecimal():
                continue
            names = self._names(serial, account_root)
            if "books" not in names:
                continue
            if "WRReader" not in names:
                if self._directories(serial, f"{account_root}/books"):
                    raise ReaderCacheError("微信读书账号存在缓存目录但缺少 WRReader 数据库")
                continue
            # 2. 每个账号独立拉取 SQLite family 并保留多账号中的同书记录。
            remote_db = f"{account_root}/WRReader"
            database = self._sqlite_family(serial, remote_db, temporary / account)
            for remote_cache in self._directories(serial, f"{account_root}/books"):
                book_id = valid_component(PurePosixPath(remote_cache).name, "微信读书书籍编号")
                if not any(name.endswith(".res") for name in self._names(serial, remote_cache)):
                    continue
                rows = sqlite_rows(
                    database,
                    "Book",
                    ("bookId", "title", "author", "format", "chapterSize"),
                    "bookId",
                    book_id,
                )
                if len(rows) != 1:
                    raise ReaderCacheError("微信读书实际缓存没有唯一的 Book 元数据")
                # 3. 离线状态保留为证据；部分缓存也可备份，不虚构下载完成状态。
                offline = sqlite_rows(
                    database,
                    "OfflineBook",
                    ("bookId", "downloadedChapterCount", "wholeChapterCount", "status"),
                    "bookId",
                    book_id,
                )
                row = rows[0]
                books.append(
                    CachedBook(
                        Channel.WEREAD,
                        book_id,
                        required_text(row["title"], "微信读书书名"),
                        required_text(row["author"], "微信读书作者", allow_empty=True),
                        account,
                        remote_cache,
                        {
                            "remote_database": remote_db,
                            "device_serial": serial,
                            "format": row["format"],
                            "chapter_size": row["chapterSize"],
                            "offline_records": offline,
                        },
                    )
                )
            # 4. 仅公布发现数量，不输出账号标识。
            progress(f"微信读书：已找到 {len(books)} 本缓存书籍")
        return books

    def _validate_book(self, book: CachedBook, serial: str) -> None:
        """核对缓存记录与设备和应用边界。参数：book 已扫描记录、serial 明确设备。返回：无，记录冲突即报错。"""
        if book.channel not in PACKAGES:
            raise ReaderCacheError("备份渠道不受支持")
        account = valid_component(book.account_id, "账号目录编号", numeric=True)
        identifier = valid_component(book.book_id, "书籍编号", numeric=book.channel != Channel.WEREAD)
        required_text(book.title, "书名")
        required_text(book.author, "作者", allow_empty=book.channel == Channel.WEREAD)
        package = PACKAGES[book.channel]
        if book.channel == Channel.FANQIE:
            expected = f"/data/user/0/{package}/files/{account}/prefix_public_{identifier}"
        elif book.channel == Channel.QIDIAN:
            expected = f"/sdcard/Android/data/{package}/files/QDReader/book/{account}/{identifier}"
        else:
            expected = f"/data/user/0/{package}/databases/{account}/books/{identifier}"
        if book.remote_cache != expected:
            raise ReaderCacheError("缓存路径与扫描记录的渠道、书籍或账号不一致")
        if book.metadata.get("device_serial") != serial:
            raise ReaderCacheError("备份设备与该书籍扫描时的设备不一致，请重新扫描")

    def _verify_snapshot(self, book: CachedBook, stage: Path) -> None:
        """验证即将发布的数据库快照。参数：book 扫描记录、stage 临时备份。返回：无，结构或身份不一致时终止发布。"""
        if book.channel == Channel.FANQIE:
            database = stage / "database" / "reading_profile" / f"reading_db_{book.account_id}"
            rows = sqlite_rows(database, "t_book", ("book_id", "name", "author"), "book_id", book.book_id)
        elif book.channel == Channel.QIDIAN:
            database = stage / "database" / "databases" / "QDReader"
            rows = sqlite_rows(database, "book", ("QDBookId", "BookName", "Author"), "QDBookId", book.book_id)
            catalog = sqlite_rows(
                stage / "book_index" / f"{book.book_id}.qd", "chapter", ("ChapterId", "ChapterName")
            )
            if not catalog:
                raise ReaderCacheError("起点备份的章节索引为空，未发布备份")
            sqlite_rows(stage / "database" / "databases" / "QDConfig", "setting", ("Key",))
        else:
            database = stage / "database" / "WRReader"
            rows = sqlite_rows(database, "Book", ("bookId", "title", "author"), "bookId", book.book_id)
            sqlite_rows(database, "Chapter", ("chapterUid", "chapterIdx", "title"), "bookId", book.book_id)
        if len(rows) != 1:
            raise ReaderCacheError("备份时书籍数据库记录发生变化，请重新扫描；未发布备份")

    def backup(self, book: CachedBook, serial: str, output_dir: Path, progress: ProgressCallback) -> Path:
        """原子保存一份新的原始备份。参数：book 扫描记录、serial 设备、output_dir 新目录、progress 反馈。返回：最终备份目录。"""
        # 1. 先验证设备和身份边界；已有目录绝不覆盖。
        self._ensure_device(serial)
        self._validate_book(book, serial)
        if output_dir.expanduser().is_symlink():
            raise ReaderCacheError("备份输出位置是符号链接，请选择新的普通目录")
        output = output_dir.expanduser().resolve()
        if output.exists():
            raise ReaderCacheError("备份输出目录已经存在，请选择新目录")
        output.parent.mkdir(parents=True, exist_ok=True)
        # 2. 所有原始文件先拉到同卷独立临时目录，失败只清理本次临时目录。
        with tempfile.TemporaryDirectory(prefix=".reader-cache-backup-", dir=output.parent) as temporary:
            stage = Path(temporary)
            progress("正在备份原始章节缓存")
            cache_name = f"prefix_public_{book.book_id}" if book.channel == Channel.FANQIE else book.book_id
            cache = stage / "book_cache" / cache_name
            self._pull(serial, book.remote_cache, cache, is_directory=True)
            package = PACKAGES[book.channel]
            if book.channel == Channel.FANQIE:
                remote_db = f"/data/user/0/{package}/files/{book.account_id}/reading_db_{book.account_id}"
                self._sqlite_family(serial, remote_db, stage / "database" / "reading_profile")
                self._pull(
                    serial,
                    f"/data/user/0/{package}/files/mmkv",
                    stage / "private_data" / "files" / "mmkv",
                    is_directory=True,
                )
            elif book.channel == Channel.QIDIAN:
                self._sqlite_family(serial, f"{book.remote_cache}.qd", stage / "book_index")
                for name in ("QDReader", "QDConfig"):
                    self._sqlite_family(
                        serial, f"/data/user/0/{package}/databases/{name}", stage / "database" / "databases"
                    )
            else:
                self._sqlite_family(
                    serial, f"/data/user/0/{package}/databases/{book.account_id}/WRReader", stage / "database"
                )
            # 3. 核对实际文件并写出适配器可识别的真实元数据。
            files = [path for path in cache.rglob("*") if path.is_file()]
            if not files:
                raise ReaderCacheError("拉取的章节目录为空，未发布备份")
            self._verify_snapshot(book, stage)
            metadata = {
                "channel": book.channel.value,
                "book_id": book.book_id,
                "account_id": book.account_id,
                "title": book.title,
                "author": book.author,
                "remote_cache": book.remote_cache,
                "cache_file_count": len(files),
                "cache_bytes": sum(path.stat().st_size for path in files),
                "generated_at": datetime.now(UTC).isoformat(),
                "scope": "原始缓存与数据库备份；未修改设备或补写正文。",
            }
            (stage / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if book.channel == Channel.QIDIAN:
                legacy = {
                    "title": book.title,
                    "author": book.author,
                    "qdBookId": book.book_id,
                    "qdUserDir": book.account_id,
                }
                (stage / f"qidian_book_metadata_{book.book_id}.json").write_text(
                    json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            # 4. 再次确认目标未被其他操作创建，随后同卷原子发布。
            if output.exists():
                raise ReaderCacheError("备份期间目标目录已被创建，未覆盖现有内容")
            stage.rename(output)
        progress(f"原始备份完成，共 {len(files)} 个缓存文件")
        return output
