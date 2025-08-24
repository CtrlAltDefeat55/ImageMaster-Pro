# ImageMaster Pro — Batch Image Editor & Converter (Tkinter)

ImageMaster Pro is a fast, no‑nonsense desktop app for **batch editing and converting images**. Drag & drop files or folders, preview edits live with **zoom & pan**, mask regions with **blur** or **blackout**, add **text or image watermarks**, stack **overlays**, tweak **brightness/contrast/saturation**, apply **filters**, rotate/flip, **resize**, and export to **PNG/JPEG/WEBP** — all in one window.

> **Core script:** `ImageMaster Pro_V4.py`  
> **Settings file (auto‑created):** `image_master_settings_v2.json`

---

## Table of Contents

- [Features](#features)
- [User Interface](#user-interface)
- [Installation](#installation)
- [Usage](#usage)
  - [Load images](#load-images)
  - [Edit tools](#edit-tools)
  - [Watermarks & overlays](#watermarks--overlays)
  - [Resize, filters, adjustments](#resize-filters-adjustments)
  - [Convert / export](#convert--export)
  - [Keyboard & mouse](#keyboard--mouse)
- [Dependencies](#dependencies)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **Drag & Drop** images and folders (uses `tkinterdnd2` if available; falls back to file picker).  
- **Live preview** with **Zoom** (10%–3200%) and **Pan**, plus quick **Fit** and **100%** buttons.
- **Undo/Redo** system for most actions.
- **Manual edits per image**: draw **rectangles or circles** to **Blur** or **Blackout** selected regions.
- **Watermarks**:
  - **Text watermark** with font size, color, opacity, and smart positions (including *Diagonal Fit*).
  - **Image watermark** with drag‑to‑place, rotate, scale, and opacity.
- **Overlays**: add multiple overlay images, re‑order layers, and set per‑overlay opacity.
- **Adjustments**: brightness, contrast, and saturation sliders.
- **Filters**: None, Grayscale, Sepia, Blur, Sharpen, Edge Enhance, Contour.
- **Transforms**: rotate (±90°), flip horizontal/vertical.
- **Resize**: presets (e.g., 1080p/4K/social sizes) and custom width/height (maintain aspect if one field left blank).
- **Batch conversion**: export **Current** or **All** images to **PNG/JPEG/WEBP** with quality control.
- **Themes**: multiple ttk themes (via `ttkthemes`), with your last choice saved.
- **Saves your global prefs** to `image_master_settings_v2.json`.

> Windows users: the app enables **high‑DPI awareness** so the UI looks crisp on 4K/HiDPI displays.

## User Interface

_Add a screenshot of the main window here (e.g., `docs/screenshot.png`)._

---

## Installation

Quick start below; see **[INSTALL.md](INSTALL.md)** for OS‑specific details.

```bash
git clone https://github.com/<you>/image-master-pro.git
cd image-master-pro

# (Recommended) create a virtual environment
python -m venv venv
# Windows
.\venv\Scripts\activate
# macOS/Linux
# source venv/bin/activate

python -m pip install -r requirements.txt
```

---

## Usage

### Load images

- **Drag & drop** files or folders into the drop zone (requires `tkinterdnd2`), or click **Browse**.
- Thumbnails/tabs show each image in the batch.

### Edit tools

- Use the **Blur/Blackout** tab to draw rectangles/circles over areas you want to mask.
- Use **Rotate**, **Flip H/V**, and **Clear** actions as needed.
- Use **Undo/Redo** if you change your mind.

### Watermarks & overlays

- **Text Watermark**: set text, size, color, opacity, and position (e.g., *Top Right*, *Diagonal Fit*).
- **Image Watermark**: browse a PNG/JPG, then drag, rotate, and set opacity on the canvas.
- **Overlays**: add one or more images as layers, reorder (▲/▼), and adjust opacity.

### Resize, filters, adjustments

- **Resize**: choose a preset or type custom width/height (leave one blank to keep aspect).
- **Filters**: pick from *None, Grayscale, Sepia, Blur, Sharpen, Edge Enhance, Contour*.
- **Adjust**: tune **Brightness**, **Contrast**, and **Saturation**.

### Convert / export

- Choose **Format** (PNG/JPEG/WEBP) and **Quality** (for JPEG/WEBP).
- Set an **Output Folder**.
- Click **Convert Current** or **Convert All**. A progress bar shows batch status.

### Keyboard & mouse

- **Undo/Redo**: `Ctrl+Z / Ctrl+Y` (Windows/Linux), `Cmd+Z / Cmd+Shift+Z` (macOS).  
- **Zoom**: mouse wheel/trackpad over the preview; **Fit**/**100%** buttons available.  
- **Pan**: click‑drag in the preview when zoomed.

---

## Dependencies

From **requirements.txt**:

- `Pillow` — image processing
- `ttkthemes` — extra ttk themes
- `tkinterdnd2` — optional, enables drag & drop

> `tkinter` itself ships with many Python installers; see INSTALL for per‑OS tips.

---

## Contributing

Contributions are welcome! See **[CONTRIBUTING.md](CONTRIBUTING.md)** for style, testing, and PR guidelines.

## License

This project is released under the MIT license.
