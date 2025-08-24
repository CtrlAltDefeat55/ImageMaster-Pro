# Install & Run — ImageMaster Pro (Tkinter)

ImageMaster Pro is a **Python Tkinter GUI** that runs on **Windows, macOS, and Linux**. It uses Pillow for image processing, `ttkthemes` for themes, and (optionally) `tkinterdnd2` for drag‑and‑drop.

> **Compatibility:** Python **3.9+** recommended, Tk **8.6+**.  
> **OS notes:** The app sets **high‑DPI awareness on Windows** automatically to keep the UI crisp.

## 1) Get the code

```bash
git clone https://github.com/<you>/image-master-pro.git
cd image-master-pro
```

## 2) Create a virtual environment (recommended)

```bash
python -m venv venv
# Windows
.\venv\Scripts\activate
# macOS/Linux
# source venv/bin/activate
```

## 3) Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Linux: install Tkinter (if missing)

Debian/Ubuntu:
```bash
sudo apt-get update
sudo apt-get install -y python3-tk
```

Fedora:
```bash
sudo dnf install -y python3-tkinter
```

### macOS notes

- If using Homebrew Python, you may need `brew install tcl-tk` and ensure your Python links against it.
- On some macOS setups, `tkinterdnd2` wheel support can vary; the app still works without drag‑and‑drop.

### Windows notes

- No extra setup should be required. The app attempts to enable **per‑monitor DPI awareness** for sharp rendering on HiDPI displays.

## 4) Run the app

```bash
# Quote the filename because it contains spaces
python "ImageMaster Pro_V4.py"
```

## 5) Uninstall / cleanup

Just remove the cloned folder. Global preferences are stored next to the script as:

- `image_master_settings_v2.json`
