"""工厂注册表，集中创建渠道适配器与导出策略。"""

from reader_cache_toolkit.models import Channel, ChannelAdapter, Exporter, ExportFormat


def create_adapter(channel: Channel) -> ChannelAdapter:
    """按渠道创建适配器。参数：channel为受支持的渠道枚举。返回：渠道适配端口实现。"""
    from reader_cache_toolkit.channels.fanqie import FanqieAdapter
    from reader_cache_toolkit.channels.qidian import QidianAdapter
    from reader_cache_toolkit.channels.weread import WereadAdapter

    adapters = {Channel.FANQIE: FanqieAdapter, Channel.QIDIAN: QidianAdapter, Channel.WEREAD: WereadAdapter}
    return adapters[Channel(channel)]()


def create_exporter(format_name: ExportFormat) -> Exporter:
    """按格式创建策略。参数：format_name为输出格式枚举。返回：导出策略实现。"""
    from reader_cache_toolkit.exporters.epub import EpubExporter
    from reader_cache_toolkit.exporters.text import MarkdownExporter, TextExporter, XhtmlExporter

    strategies = {
        ExportFormat.TXT: TextExporter,
        ExportFormat.MARKDOWN: MarkdownExporter,
        ExportFormat.EPUB: EpubExporter,
        ExportFormat.XHTML: XhtmlExporter,
    }
    return strategies[ExportFormat(format_name)]()
