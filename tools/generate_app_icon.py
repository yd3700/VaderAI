"""Render the temporary code-drawn VaderAI emblem (Pillow is build-time only)."""
from pathlib import Path

from PIL import Image, ImageDraw


def main():
    assets = Path(__file__).resolve().parents[1] / 'vader' / 'desktop' / 'assets'
    scale = 4
    image = Image.new('RGBA', (256 * scale, 256 * scale))
    draw = ImageDraw.Draw(image)

    def points(coords):
        return [(x * scale, y * scale) for x, y in coords]

    frame = [(80, 12), (176, 12), (244, 80), (244, 176),
             (176, 244), (80, 244), (12, 176), (12, 80)]
    draw.polygon(points(frame), fill='#0d111a')
    draw.line(points(frame + [frame[0]]), fill='#fa4859', width=10 * scale,
              joint='curve')
    for line in ([(66, 67), (66, 128), (128, 197), (190, 128), (190, 67)],
                 [(96, 74), (96, 120), (128, 157), (160, 120), (160, 74)]):
        draw.line(points(line), fill='#ff596a', width=15 * scale, joint='curve')
    image = image.resize((256, 256), Image.Resampling.LANCZOS)
    image.save(assets / 'vaderai.png')
    image.save(assets / 'vaderai.ico', sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


if __name__ == '__main__':
    main()
