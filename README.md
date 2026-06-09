# make_slideshow

Generates a self-contained HTML slideshow from a directory of photos and videos. Reads EXIF metadata for dates and GPS coordinates, reverse-geocodes locations, and converts HEIC files automatically.

## Requirements

- Python 3.10+
- [exiftool](https://exiftool.org/) (`brew install exiftool`)
- `sips` (macOS built-in, required only for HEIC conversion)

## Usage

```bash
uv run make_slideshow.py --album-dir path/to/photos --output slideshow.html
```

Open `slideshow.html` in a browser (must be served from the same directory as `slideshow_data.js`).

### Options

| Flag | Default | Description |
|---|---|---|
| `--album-dir` | `album` | Directory of photos/videos |
| `--output` | `slideshow.html` | Output HTML file |
| `--slide-duration` | `20.0` | Seconds per image slide |
| `--seed` | random | Shuffle seed for reproducible order |
| `--skip-geocoding` | off | Skip reverse geocoding |
| `--geocache-file` | `geocache.json` | Cache file for geocoding results |
| `--force-reconvert` | off | Re-convert HEIC files even if already converted |

## Supported formats

Images: `.jpg`, `.jpeg`, `.png`, `.heic` (case-insensitive)
Videos: `.mp4` (case-insensitive)

## Notes

- Geocoding uses [Nominatim](https://nominatim.openstreetmap.org/) (OpenStreetMap). Results are cached in `geocache.json` to avoid redundant requests.
- HEIC files are converted to JPEG via `sips` and stored in `<album-dir>/converted/`.
- Slideshow runs entirely in the browser with no external dependencies.
