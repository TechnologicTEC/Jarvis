"""Generate assets/jarvis.ico — the app icon used by shortcuts and the tray."""
import os

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "assets", "jarvis.ico")

BG_OUTER = (92, 198, 232, 255)
BG_INNER = (7, 13, 32, 255)
RING = (143, 220, 242, 255)


def frame(px: int) -> Image.Image:
    """The tray/desktop mark: a cyan rounded square with a ring and core,
    echoing the orb in the UI."""
    s = px * 4  # supersample, then downscale for clean edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(s * 0.06)
    d.rounded_rectangle((pad, pad, s - pad, s - pad),
                        radius=int(s * 0.24), fill=BG_OUTER)
    inset = int(s * 0.2)
    d.ellipse((inset, inset, s - inset, s - inset), fill=BG_INNER)
    ring = int(s * 0.30)
    d.ellipse((ring, ring, s - ring, s - ring), outline=RING, width=max(1, int(s * 0.035)))
    core = int(s * 0.42)
    d.ellipse((core, core, s - core, s - core), fill=RING)
    return img.resize((px, px), Image.Resampling.LANCZOS)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [frame(p) for p in sizes]
    frames[0].save(OUT, format="ICO",
                   sizes=[(p, p) for p in sizes], append_images=frames[1:])
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes, {len(sizes)} sizes)")


if __name__ == "__main__":
    main()
