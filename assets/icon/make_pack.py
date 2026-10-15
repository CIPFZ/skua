#!/usr/bin/env python3
"""Skua 图标打包：从 1024 源图裁出透明 squircle、抠出单色 glyph，产出多尺寸 PNG / .icns / .ico。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
SRC_ICON = HERE / "skua-icon-1024.png"
SRC_GLYPH = HERE / "skua-glyph-1024.png"
OUT = HERE / "pack"

PNG_SIZES = [16, 32, 48, 64, 128, 256, 512, 1024]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def squircle_mask(size: int, bbox: tuple[int, int, int, int], n: float = 5.0) -> Image.Image:
    """macOS 风格超椭圆 |x|^n + |y|^n = 1，bbox 为 squircle 外接框。"""
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    u = (xx + 0.5 - (x0 + w / 2)) / (w / 2)
    v = (yy + 0.5 - (y0 + h / 2)) / (h / 2)
    r = (np.abs(u) ** n + np.abs(v) ** n) ** (1.0 / n)
    # 1px 抗锯齿过渡
    edge = 1.0 / (w / 2)
    alpha = np.clip((1.0 - r) / edge + 0.5, 0.0, 1.0)
    return Image.fromarray((alpha * 255).astype(np.uint8), mode="L")


def detect_navy_bbox(im: Image.Image) -> tuple[int, int, int, int]:
    """沿中心十字线找深色背景的范围，得到 squircle 外接框。"""
    a = np.asarray(im.convert("RGB")).astype(np.int32)
    dark = a.sum(axis=2) < 200  # 深蓝黑背景
    h, w = dark.shape
    col = dark[:, w // 2]
    row = dark[h // 2, :]
    ys = np.where(col)[0]
    xs = np.where(row)[0]
    # 中心横线会被胶片打断，取纵向范围推断正方形外框
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    side = y1 - y0
    cx = (int(xs.min()) + int(xs.max()) + 1) / 2 if len(xs) else w / 2
    x0 = int(round(cx - side / 2))
    return (x0, y0, x0 + side, y0 + side)


def build_icon() -> Image.Image:
    im = Image.open(SRC_ICON).convert("RGBA")
    bbox = detect_navy_bbox(im)
    mask = squircle_mask(im.width, bbox)
    im.putalpha(mask)
    # 裁到 squircle 外接框并留 macOS 推荐的约 10% 透明边距
    x0, y0, x1, y1 = bbox
    side = x1 - x0
    pad = int(round(side * 0.1))
    canvas = Image.new("RGBA", (side + 2 * pad, side + 2 * pad), (0, 0, 0, 0))
    canvas.paste(im.crop(bbox), (pad, pad))
    return canvas.resize((1024, 1024), Image.LANCZOS)


def build_glyph() -> Image.Image:
    """把带假棋盘格背景的图转成纯白 + alpha。"""
    a = np.asarray(Image.open(SRC_GLYPH).convert("RGB")).astype(np.float64)
    mn = a.min(axis=2)
    alpha = np.clip((mn - 150.0) / (245.0 - 150.0), 0.0, 1.0)
    rgba = np.zeros((*alpha.shape, 4), dtype=np.uint8)
    rgba[..., :3] = 255
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    im = Image.fromarray(rgba, "RGBA")
    # 去孤立噪点
    al = im.getchannel("A").filter(ImageFilter.MedianFilter(5))
    im.putalpha(al)
    # 按内容裁剪后居中到正方形并留边
    box = al.getbbox()
    im = im.crop(box)
    side = int(max(im.size) * 1.15)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    return canvas.resize((1024, 1024), Image.LANCZOS)


def write_sizes(base: Image.Image, prefix: str, sizes: list[int]) -> dict[int, Path]:
    paths: dict[int, Path] = {}
    for s in sizes:
        p = OUT / f"{prefix}-{s}.png"
        base.resize((s, s), Image.LANCZOS).save(p, optimize=True)
        paths[s] = p
    return paths


def write_icns(paths: dict[int, Path], out: Path) -> None:
    """显式指定 icns 类型键：1x 与 @2x 各一套，同一位图可复用。"""
    import icnsutil

    keys = {
        "icp4": 16, "ic11": 32,   # 16 / 16@2x
        "icp5": 32, "ic12": 64,   # 32 / 32@2x
        "ic07": 128, "ic13": 256,  # 128 / 128@2x
        "ic08": 256, "ic14": 512,  # 256 / 256@2x
        "ic09": 512, "ic10": 1024,  # 512 / 512@2x
    }
    icns = icnsutil.IcnsFile()
    for key, size in keys.items():
        icns.add_media(key, file=str(paths[size]))
    icns.write(str(out))
    # 同时导出 Apple iconutil 可用的 .iconset 目录
    iconset = out.with_suffix(".iconset")
    iconset.mkdir(exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        Image.open(paths[base]).save(iconset / f"icon_{base}x{base}.png")
        Image.open(paths[base * 2]).save(iconset / f"icon_{base}x{base}@2x.png")


def write_ico(base: Image.Image, out: Path) -> None:
    base.save(out, format="ICO", sizes=[(s, s) for s in ICO_SIZES])


def main() -> int:
    OUT.mkdir(exist_ok=True)
    icon = build_icon()
    icon.save(OUT / "skua-icon-1024.png")
    icon_paths = write_sizes(icon, "skua-icon", PNG_SIZES)
    write_icns(icon_paths, OUT / "skua.icns")
    write_ico(icon, OUT / "skua.ico")

    glyph = build_glyph()
    glyph.save(OUT / "skua-glyph-1024.png")
    write_sizes(glyph, "skua-glyph", [16, 22, 24, 32, 44, 64, 128, 256])
    print(f"written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
