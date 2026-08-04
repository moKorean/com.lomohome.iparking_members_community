"""Regenerate the app-store images from the source graphic in docs/.

    python3 scripts/make_images.py

Resizes docs/app-image.png (2000x1400, itself rasterized from the editable
docs/app-image.svg via `rsvg-convert`) into the exact 10:7 landscape sizes the
Homey App Store requires: 250x175 / 500x350 / 1000x700. Needs `sips` (macOS).

There is no real product to photograph here (this app is a software client for
a private API, not a controller for a physical appliance), so docs/app-image.svg
is original vector artwork rather than a photo — but the resize step mirrors
com.lomohome.navien's scripts/make_images.py exactly: one high-resolution
source, resized to the required sizes by script, never hand-edited per size.

assets/icon.svg and assets/capabilities/parking.svg are maintained by hand —
this script deliberately does NOT touch them, so it can't overwrite hand-drawn
artwork.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

APP_SIZES = {"small": (250, 175), "large": (500, 350), "xlarge": (1000, 700)}


def _sips(src: Path, w: int, h: int, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["sips", "-s", "format", "png", "-z", str(h), str(w), str(src), "--out", str(dst)],
        check=True, stdout=subprocess.DEVNULL,
    )
    print(f"  {dst.relative_to(ROOT)}  {w}x{h}")


def resize_images() -> None:
    print("app store images:")
    for name, (w, h) in APP_SIZES.items():
        _sips(DOCS / "app-image.png", w, h, ROOT / "assets/images" / f"{name}.png")


if __name__ == "__main__":
    resize_images()
    print("done")
