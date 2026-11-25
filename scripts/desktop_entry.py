"""PyInstaller桌面启动入口，不启动任何网络服务。"""

from reader_cache_toolkit.ui.desktop import run_gui

if __name__ == "__main__":
    run_gui()
