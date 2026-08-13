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

assets/icon.svg is GENERATED from docs/icon.png — iParking's own logo, supplied
by the maintainer, wrapped in an SVG as a base64 data URI. Homey requires the app
icon to be an SVG, and a trademark is the last thing to approximate by hand, so
the mark is embedded verbatim rather than traced. It has to be a data URI and not
a file reference: docs/ is in .homeyignore, so a relative path would resolve to
nothing once the app is packed.

drivers/*/assets/icon.svg is still maintained by hand and this script does not
touch it. The capability icons under assets/capabilities/ are vendored from
Material Design Icons and are not generated either; see NOTICE.
"""

import base64
import hashlib
import json
import struct
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
    # The app image is a viewBox crop over a photograph, so both files are its source.
    "assets/images": ["docs/app-image.svg", "docs/app-image.jpg"],
    "assets/icon.svg": ["docs/icon.png"],
    **{
        f"drivers/{driver_id}/assets/images": [f"docs/{source}"]
        for driver_id, source in DEVICE_SOURCES.items()
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG's IHDR — no image library needed for two integers."""
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")
    return struct.unpack(">II", header[16:24])


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


def app_icon() -> None:
    """Wrap docs/icon.png into assets/icon.svg as a base64 data URI.

    Homey wants the app icon as SVG; this one is a raster logo. Embedding rather than tracing
    is deliberate — it is a registered mark, and an approximation of somebody's trademark is
    worse than no logo at all.
    """
    source = DOCS / "icon.png"
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    width, height = _png_size(source)
    dst = ROOT / "assets/icon.svg"
    dst.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"\n'
        f'     viewBox="0 0 {width} {height}" width="{width}" height="{height}">\n'
        "  <!-- GENERATED by scripts/make_images.py from docs/icon.png. Do not hand-edit.\n"
        "\n"
        "       iParking's own logo, supplied by the maintainer and used on their instruction;\n"
        "       App Store review suggested it too. This app is not affiliated with iParking —\n"
        "       every README says so — and the mark belongs to them.\n"
        "\n"
        "       Embedded as a data URI rather than referenced: docs/ is in .homeyignore, so a\n"
        "       relative path would resolve to nothing in the packed app. Embedded rather than\n"
        "       traced: it is a registered mark, and an approximation would be worse than\n"
        "       none. -->\n"
        f'  <image xlink:href="data:image/png;base64,{encoded}"\n'
        f'         x="0" y="0" width="{width}" height="{height}"/>\n'
        "</svg>\n",
        encoding="utf-8",
    )
    print(f"app icon:\n  {dst.relative_to(ROOT)}  {width}x{height} (embedded)")


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
    app_icon()
    write_manifest()
    print("done")
