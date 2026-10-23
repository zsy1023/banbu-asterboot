"""生成插件图标：32x32 像素风，最近邻放大到 256x256。

为什么用像素风：小尺寸下噪点最少，缩到 64px 也还认得出来，
且不依赖任何外部素材，改几个坐标就能重画。

用法：
    python tools/make_logo.py            # 生成 logo.png
    python tools/make_logo.py --preview  # 只在终端打印 ASCII 预览，不落盘
"""

from __future__ import annotations

import os
import sys

from PIL import Image

GRID = 32
SCALE = 8  # 32 * 8 = 256

# 调色板（像素风要点：颜色少、对比明确）
PALETTE: dict[str, tuple[int, int, int] | None] = {
    "bg": (23, 33, 27),          # 深绿黑底
    "frame": (232, 237, 232),    # 打印机机身（浅）
    "line": (12, 19, 16),        # 描边/阴影
    "bamboo": (93, 201, 122),    # 竹子
    "bamboo_dark": (46, 124, 74),  # 竹节与叶子
    "gold": (242, 193, 78),      # 正在打印的耗材层
    "gold_dark": (188, 141, 44),  # 耗材阴影
    "steel": (150, 163, 152),    # 滑轨次要色
}

PREVIEW = {
    None: " ",
    "bg": ".",
    "frame": "#",
    "line": "x",
    "bamboo": "B",
    "bamboo_dark": "b",
    "gold": "G",
    "gold_dark": "g",
    "steel": "-",
}


def new_canvas() -> list[list[str | None]]:
    return [[None for _ in range(GRID)] for _ in range(GRID)]


def rect(g, x0: int, y0: int, x1: int, y1: int, color: str | None) -> None:
    for y in range(max(0, y0), min(GRID - 1, y1) + 1):
        for x in range(max(0, x0), min(GRID - 1, x1) + 1):
            g[y][x] = color


def draw() -> list[list[str | None]]:
    g = new_canvas()

    # 1) 底板：圆角（切掉四角 3px）
    rect(g, 0, 0, GRID - 1, GRID - 1, "bg")
    for i in range(3):
        cut = 3 - i
        rect(g, 0, i, cut - 1, i, None)
        rect(g, GRID - cut, i, GRID - 1, i, None)
        rect(g, 0, GRID - 1 - i, cut - 1, GRID - 1 - i, None)
        rect(g, GRID - cut, GRID - 1 - i, GRID - 1, GRID - 1 - i, None)

    # 2) 竹子：左侧竖杆 + 竹节 + 两片叶（Bambu 的谐音梗，兼作左边框）
    rect(g, 2, 8, 3, 24, "bamboo")
    rect(g, 2, 8, 3, 8, "bamboo_dark")       # 断口
    for node in (13, 19):
        rect(g, 2, node, 3, node, "bamboo_dark")
    rect(g, 4, 10, 6, 10, "bamboo_dark")     # 叶
    rect(g, 4, 12, 5, 12, "bamboo_dark")

    # 3) 打印机框架：顶梁 + 两根立柱 + 龙门横轨
    rect(g, 8, 5, 24, 5, "frame")            # 顶梁
    rect(g, 8, 6, 24, 6, "line")             # 顶梁下沿阴影
    rect(g, 9, 5, 9, 27, "steel")            # 左立柱暗面
    rect(g, 23, 5, 23, 27, "steel")          # 右立柱暗面
    rect(g, 8, 5, 8, 27, "frame")            # 左立柱受光面
    rect(g, 24, 5, 24, 27, "frame")          # 右立柱受光面
    rect(g, 10, 13, 22, 13, "steel")         # 龙门横轨（打印头在上面跑）
    rect(g, 10, 14, 22, 14, "line")          # 横轨下沿

    # 4) 料盘（右上角，暗示耗材来源）
    rect(g, 17, 8, 21, 11, "steel")
    rect(g, 18, 9, 20, 10, "bg")
    rect(g, 19, 9, 19, 10, "gold")           # 轴心
    rect(g, 16, 9, 16, 10, "steel")          # 与打印头之间的走料方向感

    # 4) 打印头 + 喷嘴 + 正在挤出的料
    rect(g, 13, 15, 17, 16, "frame")
    rect(g, 13, 17, 17, 17, "line")
    rect(g, 14, 18, 16, 18, "gold")          # 喷嘴
    rect(g, 15, 19, 15, 19, "gold_dark")     # 拉出的一小段料
    rect(g, 15, 20, 15, 20, "gold_dark")

    # 5) 正在成型的物件：分层收窄，右缘压暗做体积
    rect(g, 11, 24, 21, 24, "gold")
    rect(g, 21, 24, 21, 24, "gold_dark")     # 右下角压暗
    rect(g, 12, 23, 20, 23, "gold")
    rect(g, 20, 23, 20, 23, "gold_dark")
    rect(g, 13, 22, 19, 22, "gold")
    rect(g, 19, 22, 19, 22, "gold_dark")
    rect(g, 15, 21, 17, 21, "gold")

    # 6) 热床 + 投影
    rect(g, 7, 25, 25, 26, "frame")
    rect(g, 7, 27, 25, 27, "line")

    return g


def to_image(g: list[list[str | None]]) -> Image.Image:
    small = Image.new("RGBA", (GRID, GRID), (0, 0, 0, 0))
    px = small.load()
    for y in range(GRID):
        for x in range(GRID):
            color = PALETTE.get(g[y][x]) if g[y][x] else None
            if color is not None:
                px[x, y] = (*color, 255)
    return small.resize((GRID * SCALE, GRID * SCALE), Image.NEAREST)


def main() -> int:
    g = draw()
    print("=" * GRID)
    for row in g:
        print("".join(PREVIEW[cell] for cell in row))
    print("=" * GRID)
    if "--preview" in sys.argv:
        return 0

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logo.png")
    to_image(g).save(out, "PNG", optimize=True)
    print(f"saved {out} ({os.path.getsize(out)} bytes, {GRID * SCALE}px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
