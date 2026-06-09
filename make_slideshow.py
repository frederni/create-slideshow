#!/usr/bin/env python3
import argparse
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".JPG", ".JPEG", ".png", ".PNG", ".HEIC", ".heic"}
VIDEO_EXTS = {".mp4", ".MP4"}
FILENAME_DATE_RE = re.compile(r"(\d{4})[-_](\d{2})[-_](\d{2})")


@dataclass
class MediaFile:  # pylint: disable=too-many-instance-attributes
    """Represents a single image or video file with metadata."""

    path: Path
    kind: str  # "image" or "video"
    date: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    rotation: int = 0
    duration: float | None = None
    location: str | None = None
    display_path: str = ""


def parse_args():
    p = argparse.ArgumentParser(description="Generate a slideshow HTML from a photo/video album.")
    p.add_argument("--album-dir", type=Path, default=Path("album"))
    p.add_argument("--output", type=Path, default=Path("slideshow.html"))
    p.add_argument("--slide-duration", type=float, default=20.0, help="Seconds per image slide")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--skip-geocoding", action="store_true")
    p.add_argument("--geocache-file", type=Path, default=Path("geocache.json"))
    p.add_argument("--force-reconvert", action="store_true")
    return p.parse_args()


def scan_files(album_dir: Path) -> list[Path]:
    files = []
    for p in sorted(album_dir.iterdir()):
        if p.suffix in IMAGE_EXTS or p.suffix in VIDEO_EXTS:
            files.append(p)
    return files


def extract_exif(paths: list[Path]) -> dict[str, dict]:
    cmd = [
        "exiftool", "-json", "-n",
        "-DateTimeOriginal", "-CreateDate",
        "-GPSLatitude", "-GPSLongitude",
        "-FileModifyDate", "-Rotation", "-Duration",
    ] + [str(p) for p in paths]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"Warning: exiftool exited {result.returncode}: {result.stderr[:200]}", file=sys.stderr)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("Warning: could not parse exiftool JSON output", file=sys.stderr)
        return {}
    return {item["SourceFile"]: item for item in data}


def parse_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def date_from_filename(path: Path) -> datetime | None:
    m = FILENAME_DATE_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def build_media_files(paths: list[Path], exif_data: dict, output_dir: Path) -> list[MediaFile]:
    media = []
    for p in paths:
        kind = "video" if p.suffix in VIDEO_EXTS else "image"
        exif = exif_data.get(str(p), {})

        date = (
            parse_datetime(exif.get("DateTimeOriginal"))
            or parse_datetime(exif.get("CreateDate"))
            or date_from_filename(p)
        )

        lat = exif.get("GPSLatitude")
        lon = exif.get("GPSLongitude")
        if lat is not None and lon is not None:
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                lat, lon = None, None
        else:
            lat, lon = None, None

        rotation = 0
        raw_rot = exif.get("Rotation")
        if raw_rot is not None:
            try:
                rotation = int(float(raw_rot))
            except (TypeError, ValueError):
                pass

        duration = None
        raw_dur = exif.get("Duration")
        if raw_dur is not None:
            try:
                duration = float(raw_dur)
            except (TypeError, ValueError):
                pass

        try:
            display_path = str(p.relative_to(output_dir.parent))
        except ValueError:
            display_path = str(p)

        media.append(MediaFile(
            path=p,
            kind=kind,
            date=date,
            lat=lat,
            lon=lon,
            rotation=rotation,
            duration=duration,
            display_path=display_path,
        ))
    return media


def convert_heic_files(media_files: list[MediaFile], converted_dir: Path, force: bool) -> None:
    heic_files = [m for m in media_files if m.path.suffix.upper() == ".HEIC"]
    if not heic_files:
        return
    converted_dir.mkdir(exist_ok=True)
    for i, m in enumerate(heic_files, 1):
        target = converted_dir / (m.path.stem + ".jpg")
        if not force and target.exists():
            # Update display_path even if already converted
            try:
                m.display_path = str(target.relative_to(converted_dir.parent.parent))
            except ValueError:
                m.display_path = str(target)
            continue
        print(f"Converting HEIC [{i}/{len(heic_files)}]: {m.path.name}")
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", str(m.path), "--out", str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"  Warning: sips failed for {m.path.name}, keeping original path", file=sys.stderr)
            continue
        try:
            m.display_path = str(target.relative_to(converted_dir.parent.parent))
        except ValueError:
            m.display_path = str(target)


def geocode_one(lat: float, lon: float) -> str:
    url = (
        f"https://nominatim.openstreetmap.org/reverse"
        f"?format=json&lat={lat}&lon={lon}&zoom=10"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "wedding-slideshow/1.0 (personal use)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(5)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
            except (urllib.error.URLError, OSError) as e2:
                print(f"  Warning: geocode request failed: {e2}", file=sys.stderr)
                return ""
        else:
            print(f"  Warning: geocode request failed: {e}", file=sys.stderr)
            return ""
    except (urllib.error.URLError, OSError) as e:
        print(f"  Warning: geocode request failed: {e}", file=sys.stderr)
        return ""

    addr = data.get("address", {})
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("county")
        or ""
    )
    country = addr.get("country", "")
    if city and country:
        return f"{city}, {country}"
    return city or country


def geocode_all(media_files: list[MediaFile], geocache_file: Path, skip: bool) -> None:
    # Load existing cache
    cache: dict[str, str] = {}
    if geocache_file.exists():
        try:
            cache = json.loads(geocache_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if not skip:
        # Collect unique coords (rounded to ~1km)
        coords_needed = set()
        for m in media_files:
            if m.lat is not None and m.lon is not None:
                key = f"{round(m.lat, 2)},{round(m.lon, 2)}"
                if key not in cache:
                    coords_needed.add(key)

        total = len(coords_needed)
        if total:
            print(f"Geocoding {total} unique locations (may take ~{total * 1.1:.0f}s)...")
            for i, key in enumerate(sorted(coords_needed), 1):
                lat_s, lon_s = key.split(",")
                result = geocode_one(float(lat_s), float(lon_s))
                cache[key] = result
                print(f"  [{i}/{total}] {key} → {result or '(not found)'}")
                if i < total:
                    time.sleep(1.1)

            try:
                geocache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
            except OSError as e:
                print(f"Warning: could not save geocache: {e}", file=sys.stderr)

    # Apply cache to media files
    for m in media_files:
        if m.lat is not None and m.lon is not None:
            key = f"{round(m.lat, 2)},{round(m.lon, 2)}"
            m.location = cache.get(key) or None


def format_date(dt: datetime | None) -> str:
    if dt is None:
        return ""
    months = [
        "januar", "februar", "mars", "april", "mai", "juni",
        "juli", "august", "september", "oktober", "november", "desember",
    ]
    return f"{dt.day}. {months[dt.month - 1]} {dt.year}"


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lars Frederick</title>
<style>
* { box-sizing: border-box; }
html, body {
  background: #000;
  margin: 0;
  padding: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
#stage {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 100vw;
  height: 100vh;
  position: relative;
}
#media-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  max-width: 100vw;
  max-height: 80vh;
  flex-shrink: 0;
}
#media-wrap img, #media-wrap video {
  max-width: 100vw;
  max-height: 80vh;
  object-fit: contain;
  image-orientation: from-image;
  display: block;
}
#caption {
  color: #ccc;
  font-size: 1.05rem;
  margin-top: 1rem;
  text-align: center;
  line-height: 1.6;
  min-height: 3em;
  letter-spacing: 0.02em;
}
#caption .loc {
  color: #aaa;
  font-size: 0.9rem;
}
#progress {
  position: fixed;
  bottom: 0;
  left: 0;
  height: 3px;
  background: rgba(255,255,255,0.6);
  width: 100%;
  transform-origin: left;
}
#controls {
  position: fixed;
  bottom: 1.5rem;
  right: 1.5rem;
  display: flex;
  gap: 0.5rem;
  opacity: 0;
  transition: opacity 0.3s;
}
#controls.visible { opacity: 1; }
#controls button {
  background: rgba(255,255,255,0.15);
  border: 1px solid rgba(255,255,255,0.3);
  color: #fff;
  border-radius: 6px;
  padding: 0.4rem 0.8rem;
  cursor: pointer;
  font-size: 0.9rem;
  backdrop-filter: blur(4px);
}
#controls button:hover { background: rgba(255,255,255,0.3); }
#slide-counter {
  position: fixed;
  top: 1rem;
  right: 1rem;
  color: rgba(255,255,255,0.35);
  font-size: 0.8rem;
  opacity: 0;
  transition: opacity 0.3s;
}
#slide-counter.visible { opacity: 1; }
</style>
</head>
<body>
<div id="stage">
  <div id="media-wrap"></div>
  <div id="caption"></div>
</div>
<div id="progress"></div>
<div id="controls">
  <button id="btn-prev">&#8592;</button>
  <button id="btn-pause">&#9646;&#9646;</button>
  <button id="btn-next">&#8594;</button>
</div>
<div id="slide-counter"></div>
<script src="slideshow_data.js"></script>
<script>
let idx = 0;
let paused = false;
let timer = null;
let progressAnim = null;
let hideControlsTimer = null;

const wrap = document.getElementById('media-wrap');
const caption = document.getElementById('caption');
const progress = document.getElementById('progress');
const controls = document.getElementById('controls');
const counter = document.getElementById('slide-counter');
const btnPause = document.getElementById('btn-pause');

function showSlide(i) {
  idx = ((i % SLIDES.length) + SLIDES.length) % SLIDES.length;
  const s = SLIDES[idx];

  clearTimeout(timer);
  if (progressAnim) { progressAnim.cancel(); progressAnim = null; }

  // Build media element
  let el;
  if (s.kind === 'video') {
    el = document.createElement('video');
    el.src = s.src;
    el.muted = true;
    el.playsInline = true;
    el.autoplay = true;
    el.controls = false;
    const dur = (s.duration || SLIDE_SECONDS) + 1.5;
    if (s.rotation && s.rotation !== 0) {
      el.style.transform = `rotate(${-s.rotation}deg)`;
      if (s.rotation === 90 || s.rotation === 270) {
        el.style.maxWidth = '80vh';
        el.style.maxHeight = '100vw';
      }
    }
    el.addEventListener('canplay', () => { if (!paused) el.play(); }, { once: true });
    el.addEventListener('ended', () => { if (!paused) advance(); }, { once: true });
    timer = setTimeout(advance, dur * 1000);
    startProgress(dur);
  } else {
    el = document.createElement('img');
    el.src = s.src;
    el.alt = '';
    if (s.rotation && s.rotation !== 0) {
      el.style.transform = `rotate(${s.rotation}deg)`;
    }
    timer = setTimeout(advance, SLIDE_SECONDS * 1000);
    startProgress(SLIDE_SECONDS);
  }

  wrap.replaceChildren(el);

  // Caption
  let html = '';
  if (s.date) html += `<div>${s.date}</div>`;
  if (s.location) html += `<div class="loc">${s.location}</div>`;
  caption.innerHTML = html;

  // Counter
  counter.textContent = `${idx + 1} / ${SLIDES.length}`;
}

function startProgress(seconds) {
  progress.style.transition = 'none';
  progress.style.width = '100%';
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      progress.style.transition = `width ${seconds}s linear`;
      progress.style.width = '0%';
    });
  });
}

function advance() { if (!paused) showSlide(idx + 1); }
function prev() { showSlide(idx - 1); }

function togglePause() {
  paused = !paused;
  btnPause.textContent = paused ? '▶' : '⏸';
  const vid = wrap.querySelector('video');
  if (paused) {
    clearTimeout(timer);
    if (progressAnim) { progressAnim.cancel(); progressAnim = null; }
    progress.style.transition = 'none';
    if (vid) vid.pause();
  } else {
    if (vid) { vid.play(); }
    else showSlide(idx); // restart timer for image
  }
}

function showControls() {
  controls.classList.add('visible');
  counter.classList.add('visible');
  clearTimeout(hideControlsTimer);
  hideControlsTimer = setTimeout(() => {
    controls.classList.remove('visible');
    counter.classList.remove('visible');
  }, 3000);
}

document.getElementById('btn-prev').addEventListener('click', () => { showSlide(idx - 1); });
document.getElementById('btn-next').addEventListener('click', () => { advance(); });
document.getElementById('btn-pause').addEventListener('click', togglePause);

document.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); showSlide(idx + 1); }
  else if (e.key === 'ArrowLeft') { e.preventDefault(); prev(); }
  else if (e.key === 'p' || e.key === 'P') togglePause();
  else if (e.key === 'f' || e.key === 'F') document.documentElement.requestFullscreen?.();
  showControls();
});

document.addEventListener('mousemove', showControls);
document.addEventListener('click', showControls);

showSlide(0);
</script>
</body>
</html>
"""


def render_html(
    media_files: list[MediaFile],
    slide_seconds: float,
    seed: int | None,
    output_path: Path,
) -> None:
    shuffled = list(media_files)
    random.seed(seed)
    random.shuffle(shuffled)

    slides = []
    for m in shuffled:
        slides.append({
            "src": m.display_path,
            "kind": m.kind,
            "date": format_date(m.date),
            "location": m.location or "",
            "rotation": m.rotation,
            "duration": m.duration,
        })

    data_path = output_path.parent / "slideshow_data.js"
    slides_json = json.dumps(slides, ensure_ascii=False, indent=2)
    data_path.write_text(
        f"const SLIDES = {slides_json};\nconst SLIDE_SECONDS = {slide_seconds};\n",
        encoding="utf-8",
    )

    output_path.write_text(HTML_TEMPLATE, encoding="utf-8")


def main():
    args = parse_args()

    album_dir = args.album_dir.resolve()
    if not album_dir.is_dir():
        print(f"Error: album directory not found: {album_dir}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output.resolve()
    converted_dir = album_dir / "converted"

    print(f"Scanning {album_dir}...")
    paths = scan_files(album_dir)
    print(f"Found {len(paths)} files ({sum(1 for p in paths if p.suffix in IMAGE_EXTS)} images, "
          f"{sum(1 for p in paths if p.suffix in VIDEO_EXTS)} videos)")

    print("Extracting EXIF data...")
    exif_data = extract_exif(paths)

    media_files = build_media_files(paths, exif_data, output_path)

    print("Converting HEIC files...")
    convert_heic_files(media_files, converted_dir, args.force_reconvert)

    geocode_all(media_files, args.geocache_file.resolve(), args.skip_geocoding)

    with_date = sum(1 for m in media_files if m.date)
    with_loc = sum(1 for m in media_files if m.location)
    print(f"Dates found: {with_date}/{len(media_files)}, Locations found: {with_loc}/{len(media_files)}")

    print(f"Rendering HTML → {output_path}")
    render_html(media_files, args.slide_duration, args.seed, output_path)

    print("\nDone! Open with:")
    print(f"  open {output_path}")


if __name__ == "__main__":
    main()
