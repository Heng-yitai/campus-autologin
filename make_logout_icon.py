# -*- coding: utf-8 -*-
"""生成「校园网下线」的图标（多尺寸 ICO）。

和登录图标刻意做出区分：
  · 登录图标 = 冷色（蓝→青）+ Wi-Fi 符号  → "连上"
  · 下线图标 = 暖色（深红→橙）+ 电源符号  → "关掉/释放"
外形沿用同一个超椭圆，这样放在一起看得出是一套，但颜色和符号一眼能分开。

想换配色只改 COLORS；改完重跑本脚本，再用 _apply_logout_icon.py 挂到快捷方式上。
"""
import os

from PIL import Image, ImageDraw, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
PNG = os.path.join(BASE, "icon_logout.png")
ICO = os.path.join(BASE, "icon_logout.ico")
SMALL = os.path.join(BASE, "icon_logout_small.png")

COLORS = [(136, 19, 55), (220, 38, 38), (249, 115, 22)]   # 深玫红 -> 红 -> 橙
SQUIRCLE_N = 4.2


def squircle_mask(size: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    px = m.load()
    a = size / 2.0
    n = SQUIRCLE_N
    for y in range(size):
        dy = abs((y + 0.5 - a) / a)
        dyn = dy ** n
        if dyn > 1.0:
            continue
        w = a * (1.0 - dyn) ** (1.0 / n)
        for x in range(max(0, int(a - w)), min(size, int(a + w) + 1)):
            px[x, y] = 255
    return m


def gradient(size: int) -> Image.Image:
    g = Image.new("RGB", (size, size))
    px = g.load()
    segs = len(COLORS) - 1
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2.0 * (size - 1))
            t = min(max(t, 0.0), 0.9999)
            i = int(t * segs)
            f = t * segs - i
            c0, c1 = COLORS[i], COLORS[i + 1]
            px[x, y] = tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
    return g


def build(size: int = 1024) -> Image.Image:
    img = gradient(size).convert("RGBA")
    img.putalpha(squircle_mask(size))

    # 左上柔光（很轻，只提亮不发灰）
    glow = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(glow)
    r = int(size * 0.52)
    gd.ellipse([-int(size * 0.34), -int(size * 0.42), r, r * 0.55], fill=16)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.11))
    img.alpha_composite(Image.merge("RGBA", (
        Image.new("L", (size, size), 255),
        Image.new("L", (size, size), 255),
        Image.new("L", (size, size), 255), glow)))

    d = ImageDraw.Draw(img)
    W = (255, 255, 255, 255)

    # 电源符号：开口圆环 + 一根竖线
    cx, cy = size / 2.0, size * 0.545
    r = size * 0.285
    lw = int(size * 0.088)
    d.arc([cx - r, cy - r, cx + r, cy + r], start=-56, end=236, fill=W, width=lw)

    y1, y2 = cy - r * 1.12, cy - r * 0.02
    d.line([(cx, y1), (cx, y2)], fill=W, width=lw)
    for yc in (y1, y2):                       # 补成圆头
        d.ellipse([cx - lw / 2.0, yc - lw / 2.0, cx + lw / 2.0, yc + lw / 2.0], fill=W)

    return img


if __name__ == "__main__":
    base = build(1024)
    base.resize((256, 256), Image.LANCZOS).save(PNG)
    base.save(ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    print("PNG:", PNG)
    print("ICO:", ICO, os.path.getsize(ICO), "字节")

    # 小尺寸预览（拼成一行，方便肉眼确认 16px 还认得出）
    strip = Image.new("RGB", (16 + 24 + 32 + 48 + 64 + 20, 64), (24, 24, 28))
    x = 0
    for s in (16, 24, 32, 48, 64):
        im = base.resize((s, s), Image.LANCZOS)
        strip.paste(im, (x, (64 - s) // 2), im)
        x += s + 4
    strip.save(SMALL)
    print("小尺寸预览:", SMALL)
