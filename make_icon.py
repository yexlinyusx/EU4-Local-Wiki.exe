# -*- coding: utf-8 -*-
"""make_icon.py — 生成应用图标（深蓝圆角方块 + 米黄「百科」字样）。"""
import os

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 深蓝圆角背景（EU4 百科风格配色）
d.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=44, fill=(26, 46, 74, 255))
d.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=44, outline=(217, 165, 20, 255), width=6)

# 羊皮纸书页效果：浅色内框
d.rounded_rectangle([40, 52, SIZE - 40, SIZE - 40], radius=18,
                    fill=(240, 234, 216, 255))

# 中间「百科」两字，微软雅黑粗体
font_path = r"C:\Windows\Fonts\msyhbd.ttc"
if not os.path.isfile(font_path):
    font_path = r"C:\Windows\Fonts\msyh.ttc"
font = ImageFont.truetype(font_path, 96)
text = "百科"
bbox = d.textbbox((0, 0), text, font=font)
tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
d.text(((SIZE - tw) / 2 - bbox[0], (SIZE - th) / 2 - bbox[3]),
       text, font=font, fill=(26, 46, 74, 255))

img.save(OUT, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("图标已生成:", OUT)
