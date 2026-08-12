"""Regenerate the app-store images from the source graphic in docs/.

    python3 scripts/make_images.py

Rasterizes docs/app-image.svg straight into the exact 10:7 landscape sizes the
Homey App Store requires: 250x175 / 500x350 / 1000x700.

**Straight from the SVG, with no intermediate PNG.** It used to resize a
hand-made docs/app-image.png, which meant the editable source and the shipped
images could disagree and nothing would say so — and they did: after the App
Store review rewrite, the PNG was still nine days older than the drawing it
claimed to come from. `rsvg-convert` honours an exact pixel size, so the extra
step bought nothing but a way to be wrong.

There is no real product to photograph here (this app is a software client for
a private API, not a controller for a physical appliance), so docs/app-image.svg
is original vector artwork rather than a photo. The rule it keeps from
com.lomohome.navien's script still holds: one source, every size generated from
it, never hand-edited per size.

Driver images are the same idea one shape over: square (75 / 500 / 1000) on an
opaque background, per Homey's driver-image guideline, rasterized from
docs/device-image-<driver>.svg.

assets/icon.svg and drivers/*/assets/icon.svg are maintained by hand — this
script deliberately does NOT touch them, so it cannot overwrite hand-drawn
artwork. The capability icons under assets/capabilities/ are vendored from
Material Design Icons and are not generated either; see NOTICE.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

APP_SIZES = {"small": (250, 175), "large": (500, 350), "xlarge": (1000, 700)}

#: Driver images are square, unlike the 10:7 app-store ones.
DEVICE_SIZES = {"small": 75, "large": 500, "xlarge": 1000}

#: driver id -> source graphic in docs/.
DEVICE_SOURCES = {"visitcar": "device-image-visitcar.svg"}


def _rsvg(src: Path, w: int, h: int, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rsvg-convert", "-w", str(w), "-h", str(h), str(src), "-o", str(dst)],
        check=True,
    )
    print(f"  {dst.relative_to(ROOT)}  {w}x{h}")


def resize_images() -> None:
    print("app store images:")
    for name, (w, h) in APP_SIZES.items():
        _rsvg(DOCS / "app-image.svg", w, h, ROOT / "assets/images" / f"{name}.png")


def driver_images() -> None:
    print("driver images:")
    for driver_id, source in DEVICE_SOURCES.items():
        for name, size in DEVICE_SIZES.items():
            _rsvg(DOCS / source, size, size,
                  ROOT / "drivers" / driver_id / "assets/images" / f"{name}.png")


if __name__ == "__main__":
    resize_images()
    driver_images()
    print("done")
