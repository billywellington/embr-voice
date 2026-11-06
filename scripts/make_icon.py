"""Generate kaira.ico with multiple sizes — used by PyInstaller for the
.exe icon, and by Qt at runtime for the window title-bar icon.

Design: Warm Ember waveform bars (#ff9500) on a near-black rounded
square background (#0d0a08), matching the Kaira brand and the tray icon.

Run from the project root:
    .venv312\\Scripts\\python.exe scripts\\make_icon.py
"""

import os
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPainter, QColor, QPixmap, QImage
from PyQt6.QtCore import Qt, QBuffer, QIODevice

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "kaira.ico")
OUT_PATH = os.path.abspath(OUT_PATH)

BG_COLOR     = QColor("#0d0a08")   # Warm Ember near-black
BAR_COLOR    = QColor("#ff9500")   # Warm Ember orange
ICON_SIZES   = [16, 32, 48, 64, 128, 256]   # all standard Windows icon sizes


def render_kaira_icon(size: int) -> QPixmap:
    """Render the Kaira waveform-bars icon at a given size."""
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)

    # Rounded-square background — Warm Ember near-black.
    p.setBrush(BG_COLOR)
    corner_radius = max(3, size // 8)
    p.drawRoundedRect(0, 0, size, size, corner_radius, corner_radius)

    # Five waveform bars — pyramid heights: short, medium, tall, medium, short.
    p.setBrush(BAR_COLOR)
    bars = [0.34, 0.60, 0.86, 0.60, 0.34]   # as fraction of icon height
    bar_w_frac = 0.10
    gap_frac   = 0.06
    total_w    = 5 * bar_w_frac + 4 * gap_frac
    start_x    = (1.0 - total_w) / 2.0
    bar_w      = max(1, int(round(size * bar_w_frac)))
    gap        = max(1, int(round(size * gap_frac)))
    cy         = size // 2
    x_px       = int(round(size * start_x))
    for h_frac in bars:
        h = max(2, int(round(size * h_frac)))
        y = cy - h // 2
        p.drawRoundedRect(x_px, y, bar_w, h, max(1, bar_w // 2), max(1, bar_w // 2))
        x_px += bar_w + gap
    p.end()
    return pm


def save_multi_size_ico(sizes, out_path):
    """Write an .ico containing all requested sizes using QImage's ICO writer."""
    images = [render_kaira_icon(s).toImage() for s in sizes]
    # QImage can write ICO with multiple sizes via the Qt ICO handler,
    # but it only persists one size per QImage save. The robust approach:
    # use Pillow if available (handles multi-size ICO natively), otherwise
    # fall back to writing PNG frames and combining via struct.
    try:
        from PIL import Image
        from io import BytesIO
        pil_images = []
        for img in images:
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            pil_images.append(Image.open(BytesIO(bytes(buf.data()))).copy())
        # Pillow needs the largest image as the base; the rest go in `sizes`.
        pil_images[-1].save(
            out_path, format="ICO",
            sizes=[(s, s) for s in sizes],
        )
        print(f"[OK] Wrote multi-size .ico via Pillow: {out_path}")
        return
    except ImportError:
        pass

    # Fallback: write only the largest size. Better than nothing but
    # Windows won't have sharp 16/32px versions.
    images[-1].save(out_path, "ICO")
    print(f"[WARN] Pillow not installed — wrote only {sizes[-1]}x{sizes[-1]} "
          f"to {out_path}. Install Pillow for a proper multi-size icon:")
    print(f"       .venv312\\Scripts\\pip install Pillow")


def main():
    # QApplication needed because QPixmap rendering requires a QGuiApplication.
    # QApplication extends it and is fine for this script.
    app = QApplication.instance() or QApplication(sys.argv)
    save_multi_size_ico(ICON_SIZES, OUT_PATH)
    sz = os.path.getsize(OUT_PATH)
    print(f"[OK] kaira.ico — {sz:,} bytes, sizes: {ICON_SIZES}")


if __name__ == "__main__":
    main()
