"""生成桌面应用图标：青绿渐变底板、白色书本与缓存下载角标。

用途：构建可复现的品牌资源 scripts 侧脚本，输出包内 `src/reader_cache_toolkit/assets/app.ico`
与本地预览 `assets-preview.png`；参数：无命令行参数；返回：无，直接写出图标文件。
"""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "src" / "reader_cache_toolkit" / "assets"
ICON_PATH = ASSETS_DIR / "app.ico"
PREVIEW_PATH = PROJECT_ROOT / "assets-preview.png"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
CANVAS = 256
GRADIENT_TOP = (27, 138, 125)
GRADIENT_BOTTOM = (16, 82, 76)
COVER_COLOR = (12, 66, 61)
PAGE_COLOR = (255, 255, 255, 255)
TEXT_LINE_COLOR = (154, 209, 201, 255)


def build_canvas() -> Image.Image:
    """创建青绿垂直渐变的圆角方形底板。参数：无。返回：256 像素 RGBA 底板。"""
    # 1. 逐行绘制垂直渐变。
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for y in range(CANVAS):
        ratio = y / (CANVAS - 1)
        color = tuple(int(top + (bottom - top) * ratio) for top, bottom in zip(GRADIENT_TOP, GRADIENT_BOTTOM))
        draw.line([(0, y), (CANVAS, y)], fill=(*color, 255))
    # 2. 以圆角矩形蒙版裁出底板轮廓。
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, CANVAS - 1, CANVAS - 1], radius=56, fill=255)
    canvas.putalpha(mask)
    return canvas


def draw_book(draw: ImageDraw.ImageDraw) -> None:
    """绘制打开的书本：封面底板、左右白色书页与文字行。参数：draw 底板画笔。返回：无。"""
    # 1. 封面底板略大于书页，形成描边层次。
    draw.polygon(
        [(30, 114), (128, 92), (226, 114), (226, 190), (128, 216), (30, 190)],
        fill=COVER_COLOR,
    )
    # 2. 左右书页，中缝留出封面色形成书脊。
    draw.polygon([(44, 124), (122, 106), (122, 176), (44, 196)], fill=PAGE_COLOR)
    draw.polygon([(212, 124), (134, 106), (134, 176), (212, 196)], fill=PAGE_COLOR)
    # 3. 书页内侧绘制短横线示意正文。
    for offset, width in ((112, 56), (124, 56), (136, 50)):
        draw.line([(54, offset), (54 + width, offset - 6)], fill=TEXT_LINE_COLOR, width=4)
        draw.line([(202, offset), (202 - width, offset - 6)], fill=TEXT_LINE_COLOR, width=4)


def draw_cache_badge(draw: ImageDraw.ImageDraw) -> None:
    """在书本上方绘制白色圆形与向下箭头，示意缓存下载。参数：draw 底板画笔。返回：无。"""
    # 1. 白色圆形徽标。
    draw.ellipse([94, 26, 162, 94], fill=PAGE_COLOR)
    # 2. 徽标内以底板色绘制向下箭头：竖干与三角头部。
    draw.line([(128, 40), (128, 64)], fill=COVER_COLOR, width=10)
    draw.polygon([(110, 60), (146, 60), (128, 80)], fill=COVER_COLOR)


def render_icon() -> Image.Image:
    """合成完整图标。参数：无。返回：包含底板、书本与缓存角标的 RGBA 图像。"""
    canvas = build_canvas()
    draw = ImageDraw.Draw(canvas)
    draw_book(draw)
    draw_cache_badge(draw)
    return canvas


def save_outputs(canvas: Image.Image) -> None:
    """写出 ICO 多尺寸文件与 PNG 预览。参数：canvas 合成后的图标。返回：无。"""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    canvas.save(ICON_PATH, format="ICO", sizes=[(size, size) for size in ICO_SIZES])
    canvas.save(PREVIEW_PATH, format="PNG")
    print(f"图标已生成：{ICON_PATH}")
    print(f"预览已生成：{PREVIEW_PATH}")


def verify_ico(path: Path) -> None:
    """校验 ICO 文件头与包含尺寸。参数：path ICO 路径。返回：无，异常时抛出。"""
    data = path.read_bytes()
    reserved, icon_type, icon_count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or icon_type != 1 or icon_count != len(ICO_SIZES):
        raise ValueError(f"ICO 结构异常：type={icon_type} count={icon_count}")
    print(f"ICO 校验通过，包含 {icon_count} 个尺寸。")


if __name__ == "__main__":
    image = render_icon()
    save_outputs(image)
    verify_ico(ICON_PATH)
