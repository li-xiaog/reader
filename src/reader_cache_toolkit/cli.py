"""统一命令行入口，与桌面界面共用应用服务。"""

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from reader_cache_toolkit import __version__
from reader_cache_toolkit.models import Channel, ConversionRequest, ExportFormat
from reader_cache_toolkit.service import ConversionService


def print_progress(message: str) -> None:
    """将真实执行阶段写到标准错误。参数：message为阶段说明。返回：无。"""
    print(message, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    """建立统一参数定义。参数：无。返回：包含GUI、转换和设备操作的解析器。"""
    parser = argparse.ArgumentParser(prog="reader-cache", description="本地阅读缓存工具")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--debug", action="store_true", help="输出完整异常堆栈以便本地排查")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("gui", help="打开桌面界面")
    convert = commands.add_parser("convert", help="从本地备份转换真实缓存")
    convert.add_argument("channel", choices=list(Channel))
    convert.add_argument("--source", type=Path, required=True, help="备份根目录或微信单书缓存目录")
    convert.add_argument("--book-id", required=True)
    convert.add_argument("--output", type=Path, required=True, help="尚不存在的独立输出目录")
    convert.add_argument("--formats", choices=list(ExportFormat), nargs="+", default=["txt", "epub"])
    convert.add_argument("--account-id", help="原备份账号编号，仅参与本地解析")
    convert.add_argument("--reader-db", type=Path, help="微信WRReader数据库，标准备份可自动识别")
    convert.add_argument("--require-complete", action="store_true", help="无法确认全部目录已缓存时终止")
    for name, description in (
        ("devices", "列出已连接设备"),
        ("scan", "只读扫描指定设备缓存"),
        ("backup", "备份指定设备内已经下载的书籍"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--adb", type=Path, help="ADB可执行文件路径")
        if name != "devices":
            command.add_argument("channel", choices=list(Channel))
            command.add_argument("--serial", required=True, help="devices命令显示的明确设备编号")
        if name == "backup":
            command.add_argument("--book-id", required=True)
            command.add_argument("--account-id", help="多账号存在同一本书时指定原账号")
            command.add_argument("--output", type=Path, required=True)
    return parser


def execute(args: argparse.Namespace) -> int:
    """分派命令到应用服务。参数：args为已解析命令。返回：成功退出码0。"""
    # 1. 桌面和本地转换共用应用服务，不通过shell串联脚本。
    if args.command == "gui":
        from reader_cache_toolkit.ui.desktop import run_gui

        run_gui()
        return 0
    if args.command == "convert":
        request = ConversionRequest(
            Channel(args.channel),
            args.source,
            args.book_id,
            args.output,
            tuple(ExportFormat(value) for value in args.formats),
            args.account_id,
            args.reader_db,
            args.require_complete,
        )
        result = ConversionService().convert(request, print_progress)
        print(
            json.dumps(
                {
                    "output_dir": str(result.output_dir),
                    "chapter_count": result.chapter_count,
                    "report": str(result.report_path),
                    "file_count": len(result.files),
                },
                ensure_ascii=False,
            )
        )
        return 0
    # 2. 显式连接用户选择的设备；基础设施负责只读ADB操作。
    from reader_cache_toolkit.infrastructure.backup import AdbBackupService

    service = AdbBackupService(args.adb)
    if args.command == "devices":
        print(json.dumps([asdict(device) for device in service.devices()], ensure_ascii=False))
        return 0
    books = service.scan(Channel(args.channel), args.serial, print_progress)
    # 3. 扫描只显示书目，不把用户账号编号或设备解析参数输出到日志。
    if args.command == "scan":
        print(
            json.dumps(
                [
                    {
                        "channel": book.channel,
                        "book_id": book.book_id,
                        "title": book.title,
                        "author": book.author,
                    }
                    for book in books
                ],
                ensure_ascii=False,
            )
        )
        return 0
    matches = [
        book
        for book in books
        if book.book_id == args.book_id and (args.account_id is None or book.account_id == args.account_id)
    ]
    if len(matches) != 1:
        raise ValueError(f"指定书籍对应 {len(matches)} 条缓存记录，需提供正确书籍编号或账号编号")
    # 4. 备份完成才输出最终目录，失败由主入口统一报告。
    output = service.backup(matches[0], args.serial, args.output, print_progress)
    print(json.dumps({"output_dir": str(output), "book_id": args.book_id}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    """统一错误边界。参数：argv为可选命令参数列表。返回：成功0、业务失败1、中断130。"""
    args = build_parser().parse_args(argv)
    try:
        return execute(args)
    except KeyboardInterrupt:
        print("操作已中断。", file=sys.stderr)
        return 130
    except Exception as error:
        if args.debug:
            logging.exception("执行失败")
        else:
            print(f"执行失败：{error}", file=sys.stderr)
        return 1
