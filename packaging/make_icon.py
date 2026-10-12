"""Рисует иконку Wave (packaging/wave.ico). Нужен Pillow; вызывается из build_windows.bat."""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
BARS = [0.30, 0.62, 0.42, 0.88, 1.00, 0.70, 0.46, 0.64, 0.32]   # высоты столбцов, как на обложке «Моя Волна»


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, SIZE - 8, SIZE - 8), radius=52, fill="#0E0E10")
    n, bar_w, gap = len(BARS), 14, 8
    x = (SIZE - (n * bar_w + (n - 1) * gap)) // 2
    for h in BARS:
        height = int(150 * h)
        y = (SIZE - height) // 2
        d.rounded_rectangle((x, y, x + bar_w, y + height), radius=7, fill="#FF6A4D")
        x += bar_w + gap
    return img


if __name__ == "__main__":
    out = Path(__file__).with_name("wave.ico")
    draw().save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Иконка:", out)
