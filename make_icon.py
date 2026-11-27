# -*- coding: utf-8 -*-
"""生成「校园网一键登录」的图标（多尺寸 ICO）。

设计要点：
  · 超椭圆（squircle）外形 —— 比圆角矩形更现代，视觉重心更稳
  · 三色对角渐变：深靛 → 天蓝 → 青
  · 左上柔光 + 底部微光，做出玻璃质感
  · Wi-Fi 符号用「两条弧 + 圆点」，小尺寸下也不会糊成一团
想换配色只改 COLORS 即可，改完重跑本脚本。
"""
import os
from PIL import Image, ImageDraw, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
PNG = os.path.join(BASE, "icon.png")
ICO = os.path.join(BASE, "icon.ico")

COLORS = [(30, 58, 138), (37, 99, 235), (6, 182, 212)]    # 深蓝 -> 宝蓝 -> 青
SQUIRCLE_N = 4.2                                          # 超椭圆指数，4 左右最像 squircle


def squircle_mask(size: int) -> Image.Image:
    """生成超椭圆遮罩。"""
    m = Image.new("L", (size, size), 0)
    px = m.load()
    a = size / 2.0
    n = SQUIRCLE_N
    for y in range(size):
        dy = abs((y + 0.5 - a) / a)
        dyn = dy ** n
        if dyn > 1.0:
            continue
        # 由纵向位置反解该行的横向边界，避免逐点判断
        w = a * (1.0 - dyn) ** (1.0 / n)
        x0 = int(a - w)
        x1 = int(a + w)
        for x in range(max(0, x0), min(size, x1 + 1)):
            px[x, y] = 255
    return m


def gradient(size: int) -> Image.Image:
    """对角三色渐变。"""
    g = Image.new("RGB", (size, size))
    px = g.load()
    segs = len(COLORS) - 1
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2.0 * (size - 1))          # 左上 0 -> 右下 1
            t = min(max(t, 0.0), 0.9999)
            i = int(t * segs)
            f = t * segs - i
            c0, c1 = COLORS[i], COLORS[i + 1]
            px[x, y] = tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
    return g


def build(size: int = 1024) -> Image.Image:
    img = gradient(size).convert("RGBA")
    img.putalpha(squircle_mask(size))

    # 左上柔光
    glow = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(glow)
    r = int(size * 0.52)
    gd.ellipse([-int(size * 0.34), -int(size * 0.42), r, r * 0.55], fill=15)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.11))
    img.alpha_composite(Image.merge("RGBA", (
        Image.new("L", (size, size), 255),
        Image.new("L", (size, size), 255),
        Image.new("L", (size, size), 255), glow)))

    d = ImageDraw.Draw(img)
    W = (255, 255, 255, 255)
    cx, cy = size / 2, size * 0.665
    lw = int(size * 0.074)
    for rr in (0.335, 0.228, 0.121):                  # 三条弧，间距均匀
        r = size * rr
        d.arc([cx - r, cy - r, cx + r, cy + r], start=203, end=337, fill=W, width=lw)
    dot = size * 0.045
    d.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=W)
    return img


if __name__ == "__main__":
    base = build(1024)
    base.resize((256, 256), Image.LANCZOS).save(PNG)
    base.save(ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])

    # 小尺寸实况预览（放大 6 倍并排），确认缩到 16px 还认得出
    zoom = 6
    sizes = [16, 24, 32, 48, 64]
    canvas = Image.new("RGB", (sum(s * zoom + 18 for s in sizes) + 18, 64 * zoom + 20),
                       (245, 245, 247))
    x = 9
    for s in sizes:
        small = base.resize((s, s), Image.LANCZOS).resize((s * zoom, s * zoom), Image.NEAREST)
        canvas.paste(small, (x, 10), small)
        x += s * zoom + 18
    canvas.save(os.path.join(BASE, "preview_small.png"))

    print("PNG :", PNG)
    print("ICO :", ICO, os.path.getsize(ICO), "字节")
    print("预览:", os.path.join(BASE, "preview_small.png"))
