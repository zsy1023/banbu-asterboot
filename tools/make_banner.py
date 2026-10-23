"""生成 README 顶部的 banner.png（像素风，纯代码绘制，无外部素材）。

1280x320：左侧复用插件图标网格，右侧是手写 5x7 像素字体的标题 + 一条"打印层"进度条。

用法：
    python tools/make_banner.py            # 生成 banner.png
    python tools/make_banner.py --preview  # 只打印 ASCII 预览
"""

from __future__ import annotations

import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_logo import PALETTE, draw as draw_logo  # noqa: E402

W, H = 1280, 320

# 5x7 像素字体（只收录标题用得到的字符）
FONT: dict[str, list[str]] = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}


def new_canvas() -> list[list[str | None]]:
    return [[None for _ in range(W)] for _ in range(H)]


def rect(g, x0: int, y0: int, x1: int, y1: int, color: str | None) -> None:
    for y in range(max(0, y0), min(H - 1, y1) + 1):
        for x in range(max(0, x0), min(W - 1, x1) + 1):
            g[y][x] = color


def text_width(text: str, scale: int) -> int:
    return len(text) * 6 * scale - scale  # 每字符 5 列 + 1 列间距


def draw_text(g, text: str, x: int, y: int, scale: int, color: str) -> None:
    cursor = x
    for char in text.upper():
        glyph = FONT.get(char, FONT[" "])
        for row, pattern in enumerate(glyph):
            for col, bit in enumerate(pattern):
                if bit == "1":
                    rect(
                        g,
                        cursor + col * scale,
                        y + row * scale,
                        cursor + col * scale + scale - 1,
                        y + row * scale + scale - 1,
                        color,
                    )
        cursor += 6 * scale


def draw() -> list[list[str | None]]:
    g = new_canvas()

    # 背景：深绿黑 + 顶部/底部各一条更暗的边，模拟卡片
    rect(g, 0, 0, W - 1, H - 1, "bg")
    rect(g, 0, 0, W - 1, 3, "line")
    rect(g, 0, H - 4, W - 1, H - 1, "line")

    # 左侧：插件图标（32x32 网格 ×5 = 160px）
    logo = draw_logo()
    scale = 5
    ox, oy = 72, 80
    for y, row in enumerate(logo):
        for x, cell in enumerate(row):
            if not cell:
                continue
            rect(
                g,
                ox + x * scale,
                oy + y * scale,
                ox + x * scale + scale - 1,
                oy + y * scale + scale - 1,
                cell,
            )

    # 右侧：标题 + 副标题
    title = "BAMBU A1"
    text_scale = 9
    tx = 300
    draw_text(g, title, tx, 96, text_scale, "frame")

    subtitle = "ASTRBOT PLUGIN"
    sub_scale = 4
    draw_text(g, subtitle, tx, 96 + 7 * text_scale + 26, sub_scale, "bamboo")

    # 一条"打印层"进度条：从金到竹绿渐变（用三色分段模拟）
    bar_y = 96 + 7 * text_scale + 26 + 7 * sub_scale + 34
    bar_x0, bar_x1 = tx, tx + text_width("ASTRBOT PLUGIN", sub_scale) + 40
    third = (bar_x1 - bar_x0) // 3
    rect(g, bar_x0, bar_y, bar_x0 + third, bar_y + 9, "gold")
    rect(g, bar_x0 + third + 1, bar_y, bar_x0 + third * 2, bar_y + 9, "gold_dark")
    rect(g, bar_x0 + third * 2 + 1, bar_y, bar_x1, bar_y + 9, "bamboo_dark")

    # 右下角装饰：几段正在打印的层
    for i, width in enumerate((150, 210, 270)):
        y = 232 + i * 18
        rect(g, W - 90 - width, y, W - 90, y + 10, "gold" if i == 0 else "gold_dark")

    # 右上角：料盘剪影（呼应图标）
    rect(g, W - 150, 60, W - 96, 114, "steel")
    rect(g, W - 140, 70, W - 106, 104, "bg")
    rect(g, W - 126, 76, W - 120, 98, "gold")

    return g


def to_image(g: list[list[str | None]]) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = img.load()
    for y in range(H):
        for x in range(W):
            cell = g[y][x]
            if not cell:
                continue
            color = PALETTE.get(cell)
            if color is not None:
                px[x, y] = (*color, 255)
    return img


def preview(g: list[list[str | None]], cols: int = 96, rows: int = 22) -> None:
    step_x = max(1, W // cols)
    step_y = max(1, H // rows)
    marks = {None: " ", "bg": ".", "frame": "#", "line": "x", "steel": "-",
             "bamboo": "B", "bamboo_dark": "b", "gold": "G", "gold_dark": "g"}
    for y in range(0, H, step_y):
        line = []
        for x in range(0, W, step_x):
            cell = g[y][x]
            # 该格内只要有非背景像素就显示，避免缩略后看不出结构
            best = cell
            for dy in range(step_y):
                for dx in range(step_x):
                    if y + dy < H and x + dx < W:
                        cand = g[y + dy][x + dx]
                        if cand not in (None, "bg"):
                            best = cand
                            break
                if best not in (None, "bg"):
                    break
            line.append(marks.get(best, "?"))
        print("".join(line))


def main() -> int:
    g = draw()
    if "--preview" in sys.argv:
        preview(g)
        return 0
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "banner.png")
    to_image(g).save(out, "PNG", optimize=True)
    print(f"saved {out} ({os.path.getsize(out)} bytes, {W}x{H})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
