from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=52, fill=(15, 20, 26, 255),
                    outline=(37, 99, 235, 255), width=6)

def arrow(y, x0, x1, color):
    d.line([(x0, y), (x1 - 16, y)], fill=color, width=16)
    d.polygon([(x1, y), (x1 - 30, y - 20), (x1 - 30, y + 20)], fill=color)

arrow(96, 62, 196, (56, 189, 248, 255))
arrow(160, 194, 60, (167, 139, 250, 255))
d.ellipse([50, 88, 66, 104], fill=(34, 197, 94, 255))
d.ellipse([190, 152, 206, 168], fill=(34, 197, 94, 255))

img.save("app.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written")
