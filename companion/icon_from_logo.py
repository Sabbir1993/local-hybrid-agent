"""Turn the product logo into the companion's app icon: build/icon.png (256 px) and a multi-size build/icon.ico.

usage: python icon_from_logo.py <logo> <out.png> <out.ico>

The logo is centred on a transparent square, never stretched. Use a logo with real transparency (static/logo/logo-mark.png):
the older logo.jpg has a gray/white "transparency" checkerboard baked into its pixels, which would end up behind the icon."""
import sys

from PIL import Image

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main(src, out_png, out_ico):
    img = Image.open(src).convert("RGBA")
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    square.resize((256, 256), Image.Resampling.LANCZOS).save(out_png)
    # the ICO plugin scales every size from the base image, so start from the largest square we have
    square.save(out_ico, sizes=[s for s in ICO_SIZES if s[0] <= side])


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
