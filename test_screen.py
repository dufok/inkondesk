"""Render a test image for the Kindle Paperwhite 1 (758x1024, 8-bit grayscale)."""
import sys
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

W, H = 758, 1024
FONTS = "C:/Windows/Fonts/"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def font(name, size):
    return ImageFont.truetype(FONTS + name, size)


def main(out_path, battery="?"):
    now = datetime.now()
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)

    # Time and date
    d.text((W // 2, 150), now.strftime("%H:%M"), font=font("segoeuib.ttf", 200), fill=0, anchor="mm")
    d.text((W // 2, 300), f"{DAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}",
           font=font("segoeui.ttf", 40), fill=0, anchor="mm")

    d.line((60, 370, W - 60, 370), fill=0, width=3)

    # Status block
    f = font("segoeui.ttf", 34)
    rows = [
        ("Connection", "USB · 192.168.15.244"),
        ("Firmware", "5.6.1.1 + jailbreak"),
        ("Battery", f"{battery}"),
    ]
    y = 420
    for label, value in rows:
        d.text((60, y), label, font=f, fill=90)
        d.text((W - 60, y), value, font=font("segoeuib.ttf", 34), fill=0, anchor="ra")
        y += 70

    # 16-level grayscale strip: checks that every shade renders
    d.text((60, 680), "16 shades of gray", font=font("segoeui.ttf", 28), fill=90)
    sw = (W - 120) / 16
    for i in range(16):
        x0 = 60 + i * sw
        d.rectangle((x0, 730, x0 + sw, 830), fill=i * 17)
    d.rectangle((60, 730, W - 60, 830), outline=0, width=2)

    d.text((W // 2, 930), "Kindle as a USB screen — it works", font=font("segoeuib.ttf", 34), fill=0, anchor="mm")

    img.save(out_path, optimize=True)


if __name__ == "__main__":
    main(sys.argv[1], *(sys.argv[2:3]))
