"""可直接向用户解释的领域错误。"""


class ReaderCacheError(Exception):
    """工具业务错误；参数：真实错误说明；返回：可在界面与CLI展示的异常。"""
