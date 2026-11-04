# DataHorizon application icon

The application uses **3B, Shared Cell**, selected on November 3, 2026: two
overlapping white tables share a green cell on a deep blue rounded tile.

`source.png` is the original approved 1254 × 1254 PNG generated with built-in
imagegen. It is preserved without artwork changes. The initial exploration and
exact prompts are in `output/imagegen/linked-table-variations/README.md`.

The checked-in deployment assets are derived only by resizing and encoding:

- `datahorizon.png`: 1024-pixel PNG embedded in Qt and installed on Linux.
- `png/`: 16, 24, 32, 48, 64, 128, 256, and 512-pixel PNGs for Linux launchers.
- `datahorizon.ico`: 16 through 256-pixel PNG frames in a Windows icon container.
- `datahorizon.icns`: standard and Retina macOS icon representations through
  1024 pixels.
- `datahorizon.rc`: Windows executable icon resource.

To regenerate the assets on macOS with Python 3 and Apple's built-in tools:

```sh
python3 desktop/icons/generate.py
```

Normal CMake builds on all platforms consume the prebuilt assets directly.
The GUI and its assets fall within the desktop GPLv3 scope in `LICENSES.md`.
