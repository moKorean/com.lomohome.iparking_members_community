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

assets/icon.svg and drivers/*/assets/icon.svg are hand-authored and this script
does not touch them. The app icon is a **vector trace** of iParking's logo, its
coordinates measured off docs/icon.png; embedding that PNG in an SVG instead
validated and previewed fine and then came out wrong on the hub, so the geometry
is real. docs/icon.png stays in the repo as the reference it was traced from, not
as an input to anything here. The capability icons under assets/capabilities/ are
vendored from Material Design Icons; see NOTICE.
"""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

APP_SIZES = {"small": (250, 175), "large": (500, 350), "xlarge": (1000, 700)}

#: Driver images are square, unlike the 10:7 app-store ones.
DEVICE_SIZES = {"small": 75, "large": 500, "xlarge": 1000}

#: driver id -> source graphic in docs/.
DEVICE_SOURCES = {"visitcar": "device-image-visitcar.svg"}

#: Written after every run: output folder -> {source path: sha256}. `tests/test_visitcar.py`
#: recomputes those hashes and fails if any source has changed since, which is how "somebody
#: edited the art and forgot to re-run this script" becomes a test failure.
#:
#: **Hashes rather than mtimes.** The first version of that check compared file timestamps and
#: passed locally and failed on every CI run — git does not record mtimes, so a fresh checkout
#: stamps every file with the checkout time in whatever order it happened to write them, and
#: the images lost by three milliseconds. Content is the only thing that survives a clone.
#:
#: Not a hash of the *outputs*, deliberately: librsvg renders slightly differently between
#: versions, so pinning output bytes would fail for anyone on a different librsvg while the
#: images were perfectly correct.
MANIFEST = DOCS / "generated-images.json"

#: output (repo-relative file or folder) -> the source files it is generated from.
GENERATED_FROM = {
    # Both of these frames are a viewBox over a supplied bitmap, so the bitmap is a source
    # too: re-framing edits the SVG, a new artwork replaces the PNG/JPEG, and either one
    # alone leaves the shipped images stale.
    "assets/images": ["docs/app-image.svg", "docs/app-image.jpg"],
    "drivers/visitcar/assets/images": [
        "docs/device-image-visitcar.svg",
        "docs/app_intro_img03.png",
    ],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def write_manifest() -> None:
    """Record what the images were generated from, so staleness is checkable after a clone."""
    manifest = {
        folder: {name: _sha256(ROOT / name) for name in sorted(sources)}
        for folder, sources in sorted(GENERATED_FROM.items())
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"manifest:\n  {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    resize_images()
    driver_images()
    write_manifest()
    print("done")
