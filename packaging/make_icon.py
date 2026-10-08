#!/usr/bin/env python3
"""生成占位图标 ``packaging/assets/safecopy-gui.ico``（design D-g4 的 Open Question）。

不依赖任何第三方库：直接按 ICO + BMP(DIB) 二进制格式写出 16/32/48/64/128 五个尺寸，
图案为「深蓝圆底 + 白色下行箭头」（表示把文件复制到外部存储）。正式图标素材就绪后
直接替换 ``packaging/assets/safecopy-gui.ico`` 即可，打包配置无需改动。

用法::

    python packaging/make_icon.py            # 写入默认路径
    python packaging/make_icon.py out.ico    # 指定输出路径
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

#: 生成的图标尺寸（像素，正方形），全部使用 32 位 BGRA 位图。
SIZES = (16, 32, 48, 64, 128)

#: 颜色一律用 RGBA 描述，写入 BMP 时再转成 BGRA 字节序。
BACKGROUND = (28, 78, 160, 255)  # 不透明深蓝
FOREGROUND = (255, 255, 255, 255)  # 白色箭头
TRANSPARENT = (0, 0, 0, 0)

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "assets" / "safecopy-gui.ico"


def _pixel(size: int, x: int, y: int) -> tuple[int, int, int, int]:
    """逐像素绘制：圆形底 + 竖直箭杆 + 三角箭头（坐标原点在左上角，返回 RGBA）。"""
    center = (size - 1) / 2
    radius = size * 0.46
    dx, dy = x - center, y - center
    if dx * dx + dy * dy > radius * radius:
        return TRANSPARENT

    shaft_half = max(size * 0.09, 0.6)
    shaft_top = size * 0.20
    shaft_bottom = size * 0.56
    if abs(dx) <= shaft_half and shaft_top <= y <= shaft_bottom:
        return FOREGROUND

    head_width = size * 0.26
    head_top = shaft_bottom
    head_bottom = size * 0.80
    if head_top <= y <= head_bottom:
        # 三角形：越往下可用半宽越小，直到尖端收敛为 0。
        progress = (y - head_top) / max(head_bottom - head_top, 1.0)
        if abs(dx) <= head_width * (1.0 - progress):
            return FOREGROUND

    return BACKGROUND


def _dib(size: int) -> bytes:
    """按 ICO 内嵌 BMP 规则生成一个尺寸的图像数据（BITMAPINFOHEADER + BGRA + AND 掩码）。"""
    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        size,  # biWidth
        size * 2,  # biHeight：XOR 位图 + AND 掩码，因此是两倍高度
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression = BI_RGB
        0,  # biSizeImage
        0,  # biXPelsPerMeter
        0,  # biYPelsPerMeter
        0,  # biClrUsed
        0,  # biClrImportant
    )

    rows = []
    for y in range(size - 1, -1, -1):  # BMP 自下而上存储
        row = bytearray()
        for x in range(size):
            red, green, blue, alpha = _pixel(size, x, y)
            row += bytes((blue, green, red, alpha))
        rows.append(bytes(row))
    pixels = b"".join(rows)

    # AND 掩码：32 位图下不使用，但格式要求存在；每行按 4 字节对齐。
    mask_row_bytes = ((size + 31) // 32) * 4
    mask = b"\x00" * (mask_row_bytes * size)

    return header + pixels + mask


def build_ico(sizes: tuple[int, ...] = SIZES) -> bytes:
    """生成完整 ICO 文件内容。"""
    images = [_dib(size) for size in sizes]

    directory = struct.pack("<HHH", 0, 1, len(images))  # reserved / type=icon / count
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for size, data in zip(sizes, images, strict=True):
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 宽度（256 记为 0）
            size if size < 256 else 0,  # 高度
            0,  # 调色板颜色数（真彩色为 0）
            0,  # 保留
            1,  # 颜色平面数
            32,  # 每像素位数
            len(data),
            offset,
        )
        offset += len(data)

    return directory + bytes(entries) + b"".join(images)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output = Path(args[0]).resolve() if args else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    data = build_ico()
    output.write_bytes(data)
    print(f"已生成占位图标: {output}（{len(data)} 字节，尺寸 {list(SIZES)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
