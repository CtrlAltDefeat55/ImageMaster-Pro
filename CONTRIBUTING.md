# Contributing

Thanks for helping improve **ImageMaster Pro**! PRs and issues are welcome.

## Dev setup

```bash
git clone https://github.com/<you>/image-master-pro.git
cd image-master-pro
python -m venv venv
# Windows: .\venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
python -m pip install -r requirements.txt
```

## Project specifics

- **GUI:** Tkinter + `ttkthemes`; optional `tkinterdnd2` enables drag‑and‑drop.
- **Images:** `Pillow` for all processing (filters, transforms, resizing, compositing).
- **State:** Global preferences saved to `image_master_settings_v2.json`. Per‑image edits live in memory only.
- **Platforms:** Windows/macOS/Linux; Windows enables per‑monitor DPI awareness.

## Style & quality

- Follow **PEP 8**; docstring any new/changed functions.
- Prefer small, focused PRs; keep UI text concise and accessible.
- Recommended tooling:
  ```bash
  python -m pip install black ruff mypy
  black .
  ruff check .
  mypy .  # type hints optional but encouraged
  ```

## Testing checklist

Please click through these before submitting:

- ✅ App launches on your OS; themes load (fallback to `clam` works).
- ✅ Drag & drop works (or degrades gracefully when `tkinterdnd2` is missing).
- ✅ Load multiple images; switch tabs; live preview renders.
- ✅ Draw **Blur** and **Blackout** regions (rect & circle); undo/redo works.
- ✅ Text watermark color/size/position & opacity apply; image watermark placement/opacity apply.
- ✅ Overlays can be added, re‑ordered, removed; opacity updates.
- ✅ Zoom, Fit, 100%, and Pan behave as expected; zoom range 10%–3200%.
- ✅ Filters (Grayscale, Sepia, Blur, Sharpen, Edge Enhance, Contour) apply.
- ✅ Resize presets and custom values work (aspect preserved when one field empty).
- ✅ **Convert Current** and **Convert All** export PNG/JPEG/WEBP with correct quality.

## Commit messages

Use clear, descriptive messages (e.g., `undo: fix redo stack`, `wm: diagonal fit placement bug`).

## Docs

If behavior changes (filters, formats, themes, shortcuts), update **README.md** and **INSTALL.md**.
