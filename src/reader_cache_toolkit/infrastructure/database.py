"""通过临时快照查询 SQLite，避免数据库引擎改写原始备份的共享内存文件。"""

import hashlib
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


def _file_digest(path: Path) -> str:
    """只读计算文件 SHA-256，用于确认快照复制期间源文件没有变化。

    参数:
        path: 需要校验的主库或伴随文件路径。
    返回:
        str: 文件内容的 SHA-256 十六进制摘要。
    """

    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def open_readonly_snapshot(database: Path) -> Iterator[sqlite3.Connection]:
    """复制 SQLite 主库和伴随文件后，仅连接临时副本执行只读查询。

    参数:
        database: 已存在的本地 SQLite 主库路径；原文件始终只按字节读取。
    返回:
        Iterator[sqlite3.Connection]: 上下文中的 mode=ro 连接，退出时关闭并清理临时副本。
    """

    # 1. 固定源文件集合并记录内容摘要，不让 SQLite 打开原始文件。
    database = database.resolve(strict=True)
    if not database.is_file():
        raise ValueError("SQLite 主库路径不是普通文件")
    possible = [
        database,
        *(database.with_name(database.name + suffix) for suffix in ("-wal", "-shm", "-journal")),
    ]
    sources = [path for path in possible if path.is_file()]
    before = {path: _file_digest(path) for path in sources}

    with TemporaryDirectory(prefix="reader-cache-db-") as temporary:
        # 2. 复制主库与实际存在的 WAL、SHM、回滚日志，保留真实未合并事务。
        directory = Path(temporary)
        for source in sources:
            shutil.copyfile(source, directory / source.name)

        # 3. 校验源和副本的一致性，复制过程中发生变化时明确报错。
        if {path for path in possible if path.is_file()} != set(sources):
            raise ValueError("SQLite 伴随文件在复制期间发生变化，请先取得稳定的本地备份")
        for source, digest in before.items():
            if _file_digest(source) != digest or _file_digest(directory / source.name) != digest:
                raise ValueError("SQLite 文件在复制期间发生变化，请先取得稳定的本地备份")

        # 4. 仅以 mode=ro 查询临时主库，任何 SHM 变化都限制在临时目录。
        snapshot = directory / database.name
        connection = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()
