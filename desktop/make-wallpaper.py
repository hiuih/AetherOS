#!/usr/bin/env python3
"""Generate the AetherOS nebula wallpaper. Usage: make-wallpaper.py <out.png>"""
import sys, math, random
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/aetheros/wallpaper.png"
random.seed(7474)
W, H = 3840, 2160

img = Image.new("RGB", (W, H), (8, 6, 18))
draw = ImageDraw.Draw(img)
for y in range(H):
    t = y / H
    draw.line([(0, y), (W, y)], fill=(int(8 + t*14), int(6 + t*10), int(18 + t*26)))

img = img.convert("RGBA")
blobs = [
    (W*0.14, H*0.22, 720, (150, 60, 255, 40)),
    (W*0.86, H*0.78, 640, (40, 90, 255, 34)),
    (W*0.50, H*0.46, 920, (120, 30, 200, 22)),
    (W*0.74, H*0.16, 500, (200, 60, 160, 28)),
    (W*0.26, H*0.84, 560, (20, 150, 255, 24)),
    (W*0.60, H*0.74, 420, (90, 200, 180, 18)),
]
for bx, by, br, (cr, cg, cb, ca) in blobs:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for i in range(12):
        fac = 1 - i/12
        od.ellipse([bx-br*fac, by-br*fac, bx+br*fac, by+br*fac],
                   fill=(cr, cg, cb, int(ca*fac*0.85)))
    img = Image.alpha_composite(img, ov.filter(ImageFilter.GaussianBlur(110)))

img = img.convert("RGB")
draw = ImageDraw.Draw(img)
for _ in range(380):
    px, py = random.randint(0, W), random.randint(0, H)
    b = random.randint(160, 255)
    s = random.choices([0, 0, 1, 1, 1, 2], k=1)[0]
    draw.ellipse([px-s, py-s, px+s, py+s], fill=(b, b, min(255, b+random.randint(0, 60))))

pts = [(random.randint(50, W-50), random.randint(50, H-50)) for _ in range(26)]
for i, (x1, y1) in enumerate(pts):
    for x2, y2 in pts[i+1:i+3]:
        if math.hypot(x2-x1, y2-y1) < 300:
            draw.line([(x1, y1), (x2, y2)], fill=(120, 100, 220), width=1)

cx, cy = W//2, H//2
for radius in (80, 160, 240, 320, 400):
    draw.ellipse([cx-radius, cy-radius, cx+radius, cy+radius], outline=(150, 120, 230), width=1)
s = 92
draw.polygon([(cx, cy-s), (cx+s, cy), (cx, cy+s), (cx-s, cy)], outline=(203, 166, 247))
si = 58
draw.polygon([(cx, cy-si), (cx+si, cy), (cx, cy+si), (cx-si, cy)], outline=(166, 140, 255))
draw.line([(cx-s, cy), (cx+s, cy)], fill=(203, 166, 247), width=2)
draw.line([(cx, cy-s), (cx, cy+s)], fill=(203, 166, 247), width=2)

try:
    for fp in ("/usr/share/fonts/truetype/inter/Inter-Bold.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        try:
            f = ImageFont.truetype(fp, 34); fs = ImageFont.truetype(fp, 18); break
        except Exception:
            f = fs = None
    if f:
        draw.text((W//2, H-118), "AetherOS", fill=(170, 150, 230), anchor="mm", font=f)
        draw.text((W//2, H-78), "AI-NATIVE LINUX", fill=(110, 90, 170), anchor="mm", font=fs)
except Exception:
    pass

import os
os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.save(OUT, "PNG", optimize=True)
print(f"wallpaper -> {OUT}")
