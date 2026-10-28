"""Render the application icon (halo motif) to bapet/assets/icon.png and icon.ico."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen, QRadialGradient

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "bapet" / "assets"


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256.0
    # rounded tile
    tile = QPainterPath()
    tile.addRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 52 * s, 52 * s)
    grad = QRadialGradient(QPointF(128 * s, 90 * s), 200 * s)
    grad.setColorAt(0.0, QColor("#6fd0ff"))
    grad.setColorAt(1.0, QColor("#1f6fd6"))
    p.fillPath(tile, QBrush(grad))
    # halo ring (ellipse) with glow
    for width, alpha in ((30, 50), (20, 110), (11, 255)):
        p.setPen(QPen(QColor(255, 255, 255, alpha), width * s))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(50 * s, 52 * s, 156 * s, 70 * s))
    # little star / sparkle in the middle
    star = QPainterPath()
    cx, cy, r1, r2 = 128 * s, 168 * s, 46 * s, 13 * s
    import math

    for i in range(8):
        r = r1 if i % 2 == 0 else r2
        a = -math.pi / 2 + i * math.pi / 4
        pt = QPointF(cx + r * math.cos(a), cy + r * math.sin(a))
        star.moveTo(pt) if i == 0 else star.lineTo(pt)
    star.closeSubpath()
    p.fillPath(star, QColor("#ffffff"))
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def write_ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    images = [png_bytes(render(n)) for n in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for n, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(header + entries + b"".join(images))


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    ASSETS.mkdir(parents=True, exist_ok=True)
    render(256).save(str(ASSETS / "icon.png"))
    write_ico(ASSETS / "icon.ico")
    print("wrote", ASSETS / "icon.png", ASSETS / "icon.ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
