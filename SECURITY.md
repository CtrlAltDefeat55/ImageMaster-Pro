# Security Policy

## Reporting

If you discover a vulnerability or privacy issue, please open a **GitHub issue** with **SECURITY** in the title or email the maintainers if you prefer not to disclose publicly. Avoid publishing exploit details; a maintainer will coordinate next steps and timelines.

## Data & privacy

- The app stores **global preferences** locally in `image_master_settings_v2.json` next to the script. No telemetry is collected.
- Image edits are applied in‑memory and to files you explicitly export to an output folder of your choosing.
- The app does not require network access or elevated privileges.

## Permissions & risks

- Drag‑and‑drop uses OS file paths; only files you load or export are accessed.
- On Windows the app enables **high‑DPI awareness** for better rendering.
- Always keep backups of originals; batch operations can overwrite files if you choose an existing name.

## Supported versions

Target: **Python 3.9+** with **Tk 8.6+** on Windows/macOS/Linux.
