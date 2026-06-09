#!/usr/bin/env python3
"""Generate a browser-based slideshow HTML from a directory of photos and videos."""

import argparse
import contextlib
import json
import logging
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic"}
VIDEO_EXTS = {".mp4"}
FILENAME_DATE_RE = re.compile(r"(\d{4})[-_](\d{2})[-_](\d{2})")
HTTP_TOO_MANY_REQUESTS = 429

log = logging.getLogger(__name__)


@dataclass
class MediaFile:  # pylint: disable=too-many-instance-attributes
    """Represents a single image or video file with metadata."""

    path: Path
    kind: str  # "image" or "video"
    display_path: str
    date: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    rotation: int = 0
    duration: float | None = None
    location: str | None = field(default=None, compare=False)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
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
    """Return sorted list of image and video files in album_dir."""
    return [
        p for p in sorted(album_dir.iterdir())
        if p.suffix.lower() in IMAGE_EXTS or p.suffix.lower() in VIDEO_EXTS
    ]


def extract_exif(paths: list[Path]) -> dict[str, dict]:
    """Run exiftool on paths and return a dict keyed by source file path."""
    cmd = [
        "exiftool", "-json", "-n",
        "-DateTimeOriginal", "-CreateDate",
        "-GPSLatitude", "-GPSLongitude",
        "-FileModifyDate", "-Rotation", "-Duration",
    ] + [str(p) for p in paths]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        log.warning("exiftool exited %d: %s", result.returncode, result.stderr[:200])
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("could not parse exiftool JSON output")
        return {}
    return {item["SourceFile"]: item for item in data}


def parse_datetime(s: str | None) -> datetime | None:
    """Parse an EXIF datetime string into a datetime object."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def date_from_filename(path: Path) -> datetime | None:
    """Extract a date from a filename matching YYYY-MM-DD or YYYY_MM_DD."""
    m = FILENAME_DATE_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _resolve_display_path(path: Path, output_dir: Path) -> str:
    """Return path relative to output_dir's parent, or absolute if outside."""
    try:
        return str(path.relative_to(output_dir.parent))
    except ValueError:
        return str(path)


def build_media_files(paths: list[Path], exif_data: dict, output_dir: Path) -> list[MediaFile]:
    """Build MediaFile objects from paths, enriched with EXIF metadata."""
    media = []
    for p in paths:
        kind = "video" if p.suffix.lower() in VIDEO_EXTS else "image"
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
            with contextlib.suppress(TypeError, ValueError):
                rotation = int(float(raw_rot))

        duration = None
        raw_dur = exif.get("Duration")
        if raw_dur is not None:
            with contextlib.suppress(TypeError, ValueError):
                duration = float(raw_dur)

        media.append(MediaFile(
            path=p,
            kind=kind,
            display_path=_resolve_display_path(p, output_dir),
            date=date,
            lat=lat,
            lon=lon,
            rotation=rotation,
            duration=duration,
        ))
    return media


def convert_heic_files(media_files: list[MediaFile], converted_dir: Path, *, force: bool) -> None:
    """Convert HEIC files to JPEG using sips, returning updated MediaFile list."""
    heic_files = [m for m in media_files if m.path.suffix.lower() == ".heic"]
    if not heic_files:
        return
    converted_dir.mkdir(exist_ok=True)
    for i, m in enumerate(heic_files, 1):
        target = converted_dir / (m.path.stem + ".jpg")
        if not force and target.exists():
            m.display_path = _resolve_display_path(target, converted_dir.parent.parent / "x")
            continue
        log.info("Converting HEIC [%d/%d]: %s", i, len(heic_files), m.path.name)
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", str(m.path), "--out", str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            log.warning("sips failed for %s, keeping original path", m.path.name)
            continue
        m.display_path = _resolve_display_path(target, converted_dir.parent.parent / "x")


def _fetch_url(req: urllib.request.Request) -> dict | None:
    """Fetch a URL and return parsed JSON, or None on failure."""
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as e:
        log.warning("geocode request failed: %s", e)
        return None


def geocode_one(lat: float, lon: float) -> str:
    """Reverse-geocode a coordinate pair to a human-readable location string."""
    url = (
        f"https://nominatim.openstreetmap.org/reverse"
        f"?format=json&lat={lat}&lon={lon}&zoom=10"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "wedding-slideshow/1.0 (personal use)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code != HTTP_TOO_MANY_REQUESTS:
            log.warning("geocode request failed: %s", e)
            return ""
        time.sleep(5)
        data = _fetch_url(req)
        if data is None:
            return ""
    except (urllib.error.URLError, OSError) as e:
        log.warning("geocode request failed: %s", e)
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


def geocode_all(media_files: list[MediaFile], geocache_file: Path, *, skip: bool) -> None:
    """Reverse-geocode all media files with GPS coords, using and updating a local cache."""
    cache: dict[str, str] = {}
    with contextlib.suppress(json.JSONDecodeError, OSError):
        if geocache_file.exists():
            cache = json.loads(geocache_file.read_text())

    if not skip:
        coords_needed = {
            f"{round(m.lat, 2)},{round(m.lon, 2)}"
            for m in media_files
            if m.lat is not None and m.lon is not None
            if f"{round(m.lat, 2)},{round(m.lon, 2)}" not in cache
        }

        total = len(coords_needed)
        if total:
            log.info("Geocoding %d unique locations (may take ~%.0fs)...", total, total * 1.1)
            for i, key in enumerate(sorted(coords_needed), 1):
                lat_s, lon_s = key.split(",")
                result = geocode_one(float(lat_s), float(lon_s))
                cache[key] = result
                log.info("  [%d/%d] %s -> %s", i, total, key, result or "(not found)")
                if i < total:
                    time.sleep(1.1)

            try:
                geocache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
            except OSError as e:
                log.warning("could not save geocache: %s", e)

    for m in media_files:
        if m.lat is not None and m.lon is not None:
            key = f"{round(m.lat, 2)},{round(m.lon, 2)}"
            m.location = cache.get(key) or None


def format_date(dt: datetime | None) -> str:
    """Format a datetime as a Norwegian date string, e.g. '1. januar 2024'."""
    if dt is None:
        return ""
    months = [
        "januar", "februar", "mars", "april", "mai", "juni",
        "juli", "august", "september", "oktober", "november", "desember",
    ]
    return f"{dt.day}. {months[dt.month - 1]} {dt.year}"


TEMPLATE_PATH = Path(__file__).parent / "slideshow_template.html"


def render_html(
    media_files: list[MediaFile],
    slide_seconds: float,
    seed: int | None,
    output_path: Path,
) -> None:
    """Shuffle media files and write slideshow.html + slideshow_data.js to output_path's directory."""
    shuffled = list(media_files)
    random.seed(seed)
    random.shuffle(shuffled)

    slides = [
        {
            "src": m.display_path,
            "kind": m.kind,
            "date": format_date(m.date),
            "location": m.location or "",
            "rotation": m.rotation,
            "duration": m.duration,
        }
        for m in shuffled
    ]

    data_path = output_path.parent / "slideshow_data.js"
    slides_json = json.dumps(slides, ensure_ascii=False, indent=2)
    data_path.write_text(
        f"const SLIDES = {slides_json};\nconst SLIDE_SECONDS = {slide_seconds};\n",
        encoding="utf-8",
    )

    output_path.write_text(TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")


def main() -> None:
    """Entry point: parse args, process media, and generate the slideshow."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    args = parse_args()

    album_dir = args.album_dir.resolve()
    if not album_dir.is_dir():
        log.error("album directory not found: %s", album_dir)
        sys.exit(1)

    output_path = args.output.resolve()
    converted_dir = album_dir / "converted"

    log.info("Scanning %s...", album_dir)
    paths = scan_files(album_dir)
    log.info(
        "Found %d files (%d images, %d videos)",
        len(paths),
        sum(1 for p in paths if p.suffix.lower() in IMAGE_EXTS),
        sum(1 for p in paths if p.suffix.lower() in VIDEO_EXTS),
    )

    log.info("Extracting EXIF data...")
    exif_data = extract_exif(paths)

    media_files = build_media_files(paths, exif_data, output_path)

    log.info("Converting HEIC files...")
    convert_heic_files(media_files, converted_dir, force=args.force_reconvert)

    geocode_all(media_files, args.geocache_file.resolve(), skip=args.skip_geocoding)

    with_date = sum(1 for m in media_files if m.date)
    with_loc = sum(1 for m in media_files if m.location)
    log.info("Dates found: %d/%d, Locations found: %d/%d", with_date, len(media_files), with_loc, len(media_files))

    log.info("Rendering HTML -> %s", output_path)
    render_html(media_files, args.slide_duration, args.seed, output_path)

    log.info("\nDone! Open with:\n  open %s", output_path)


if __name__ == "__main__":
    main()
