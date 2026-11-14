# DataHorizon application icon

The application uses **4B Perspective Cylinder Grid** (`04b-slight-perspective-v2`),
selected on November 14, 2026: a rounded white 2×2 spreadsheet grid cap (with a
glowing emerald active cell) atop frosted cyan and steel-teal database tiers
with an emerald horizon seam, tapered slightly in downward perspective on a deep
ocean-teal squircle.

`source.png` is the approved 1024 × 1024 transparent-background master PNG for
**4B Perspective Cylinder Grid** (`04b-slight-perspective-v2`). It replaces the
initial November 3, 2026 **3B, Shared Cell** icon (preserved in
`output/imagegen/linked-table-variations/`).

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
