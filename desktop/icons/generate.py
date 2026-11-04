# SPDX-License-Identifier: GPL-3.0-only
"""Package the approved artwork using macOS sips/iconutil and Python 3.

Only resizes and encodes the existing image; no artwork is redrawn. The ICO
container stores the resized PNG bytes directly, supported by modern Windows.
Generated assets are checked in, so application builds do not run this script.
"""

from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile


def main():
    if sys.platform != "darwin" or not all(shutil.which(tool) for tool in ("sips", "iconutil")):
        raise SystemExit("Regenerating icons requires macOS with sips and iconutil.")

    root = Path(__file__).resolve().parent
    source = root / "source.png"
    png_directory = root / "png"
    png_directory.mkdir(exist_ok=True)
    sizes = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
    images = {}
    for size in sizes:
        destination = root / "datahorizon.png" if size == 1024 else png_directory / f"datahorizon-{size}.png"
        subprocess.run(["sips", "-z", str(size), str(size), str(source), "--out", str(destination)],
                       check=True, stdout=subprocess.DEVNULL)
        images[size] = destination

    # ICONDIR followed by ICONDIRENTRY records and the unchanged PNG payloads.
    windows_sizes = (16, 24, 32, 48, 64, 128, 256)
    offset = 6 + 16 * len(windows_sizes)
    entries = bytearray()
    payloads = bytearray()
    for size in windows_sizes:
        data = images[size].read_bytes()
        dimension = 0 if size == 256 else size
        entries.extend(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset))
        payloads.extend(data)
        offset += len(data)
    (root / "datahorizon.ico").write_bytes(struct.pack("<HHH", 0, 1, len(windows_sizes)) + entries + payloads)

    with tempfile.TemporaryDirectory(prefix="datahorizon-icons-") as temporary:
        iconset = Path(temporary) / "datahorizon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            shutil.copyfile(images[size], iconset / f"icon_{size}x{size}.png")
            shutil.copyfile(images[size * 2], iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "--convert", "icns", str(iconset),
                        "--output", str(root / "datahorizon.icns")], check=True)
    print("Generated Qt/Linux PNGs, Windows ICO, and macOS ICNS from source.png")


if __name__ == "__main__":
    main()
