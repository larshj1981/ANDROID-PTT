"""Tegn programikoner (.ico) til PTTRadio.exe og PTTHub.exe."""
import sys
from PIL import Image, ImageDraw


def radio(color, dot=None):
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), radius=48, fill=(27, 42, 34, 255))
    d.rectangle((150, 26, 170, 96), fill=color)
    d.rounded_rectangle((70, 84, 186, 230), radius=22, fill=color)
    d.rectangle((90, 108, 166, 146), fill=(27, 42, 34, 255))
    for y in (164, 184, 204):
        d.rectangle((90, y, 166, y + 8), fill=(27, 42, 34, 255))
    if dot:
        d.ellipse((168, 168, 240, 240), fill=dot, outline=(27, 42, 34, 255), width=8)
    return img


out = sys.argv[1] if len(sys.argv) > 1 else "."
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
radio((156, 255, 176, 255)).save(f"{out}/pttradio.ico", sizes=sizes)
radio((156, 255, 176, 255), dot=(224, 176, 80, 255)).save(f"{out}/ptthub.ico", sizes=sizes)
print("ikoner lavet")
