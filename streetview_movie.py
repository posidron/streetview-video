#!/usr/bin/env python3
"""
streetview_movie.py — build a hyperlapse-style video between two points
using Google Street View.

Pipeline: Routes API / Directions API → densify polyline → resolve pano IDs →
fetch Static Street View frames with smoothed headings → (optional)
temporal smoothing → encode to mp4.

Usage:
    export GOOGLE_MAPS_API_KEY=...
    python streetview_movie.py --start "Pike Place Market, Seattle" \\
                               --end   "Kirkland, WA" \\
                               --out route.mp4
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv  # pip install python-dotenv
import polyline   # pip install polyline
import requests   # pip install requests

load_dotenv(Path(__file__).resolve().with_name(".env"))

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

DIRECTIONS_URL     = "https://maps.googleapis.com/maps/api/directions/json"
ROUTES_URL         = "https://routes.googleapis.com/directions/v2:computeRoutes"
STREETVIEW_URL     = "https://maps.googleapis.com/maps/api/streetview"
STREETVIEW_META    = "https://maps.googleapis.com/maps/api/streetview/metadata"

SESSION = requests.Session()
TRAVEL_MODES = {
    "driving": "DRIVE",
    "walking": "WALK",
    "bicycling": "BICYCLE",
    "transit": "TRANSIT",
}


@dataclass(frozen=True)
class PanoMetadata:
    pano_id: str
    lat: float
    lng: float


def require_api_key() -> None:
    if not API_KEY:
        sys.exit("GOOGLE_MAPS_API_KEY env var not set (env var or .env file)")


# ---------- geometry ------------------------------------------------------

def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6_371_000
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def densify(points: list[tuple[float, float]], step_m: float) -> list[tuple[float, float]]:
    """Insert intermediate lat/lng samples every step_m meters along a polyline."""
    out: list[tuple[float, float]] = [points[0]]
    for a, b in zip(points, points[1:]):
        d = haversine(a, b)
        if d < step_m:
            out.append(b)
            continue
        n = int(d // step_m)
        for j in range(1, n + 1):
            t = j * step_m / d
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        if out[-1] != b:
            out.append(b)
    return out


def smooth_headings(headings: list[float], window: int = 7) -> list[float]:
    """Circular rolling mean — avoids 359° → 0° jump artifacts."""
    out = []
    half = window // 2
    for i in range(len(headings)):
        lo, hi = max(0, i - half), min(len(headings), i + half + 1)
        xs = [math.cos(math.radians(h)) for h in headings[lo:hi]]
        ys = [math.sin(math.radians(h)) for h in headings[lo:hi]]
        out.append(math.degrees(math.atan2(sum(ys) / len(ys), sum(xs) / len(xs))) % 360)
    return out


def compute_pano_headings(panos: list[PanoMetadata], lookahead: int) -> list[float]:
    if not panos:
        return []
    if len(panos) == 1:
        return [0.0]

    lookahead = max(1, lookahead)
    headings: list[float] = []
    last_index = len(panos) - 1
    for index, pano in enumerate(panos[:-1]):
        target = panos[min(last_index, index + lookahead)]
        headings.append(bearing((pano.lat, pano.lng), (target.lat, target.lng)))
    headings.append(headings[-1])
    return smooth_headings(headings)


# ---------- API calls -----------------------------------------------------

def parse_lat_lng(value: str) -> tuple[float, float] | None:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def make_waypoint(value: str) -> dict[str, object]:
    lat_lng = parse_lat_lng(value)
    if lat_lng is None:
        return {"address": value}
    lat, lng = lat_lng
    return {
        "location": {
            "latLng": {
                "latitude": lat,
                "longitude": lng,
            }
        }
    }


def get_route_routes_api(start: str, end: str, mode: str) -> list[tuple[float, float]]:
    r = SESSION.post(
        ROUTES_URL,
        headers={
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": "routes.polyline.encodedPolyline",
        },
        json={
            "origin": make_waypoint(start),
            "destination": make_waypoint(end),
            "travelMode": TRAVEL_MODES[mode],
            "polylineQuality": "HIGH_QUALITY",
            "polylineEncoding": "ENCODED_POLYLINE",
        },
        timeout=30,
    )
    data = r.json()
    if r.status_code != 200:
        message = data.get("error", {}).get("message", r.text)
        raise RuntimeError(f"Routes API: {message}")
    routes = data.get("routes", [])
    if not routes:
        raise RuntimeError("Routes API: no route found")
    encoded = routes[0].get("polyline", {}).get("encodedPolyline")
    if not encoded:
        raise RuntimeError("Routes API: response missing encoded polyline")
    return polyline.decode(encoded)


def get_route_directions_api(start: str, end: str, mode: str) -> list[tuple[float, float]]:
    r = SESSION.get(DIRECTIONS_URL, params={
        "origin": start, "destination": end, "mode": mode, "key": API_KEY,
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data["status"] != "OK":
        raise RuntimeError(f"Directions API: {data['status']} — {data.get('error_message','')}")
    return polyline.decode(data["routes"][0]["overview_polyline"]["points"])


def get_route(start: str, end: str, mode: str) -> list[tuple[float, float]]:
    errors: list[str] = []
    for route_getter in (get_route_routes_api, get_route_directions_api):
        try:
            return route_getter(start, end, mode)
        except RuntimeError as exc:
            errors.append(str(exc))
    raise RuntimeError(" / ".join(errors))


def summarize_response_error(r: requests.Response) -> str:
    content_type = r.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = r.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                status = error.get("status")
                message = error.get("message")
                if status and message:
                    return f"{status} — {message}"
                if message:
                    return str(message)
            status = data.get("status")
            message = data.get("error_message")
            if status and message:
                return f"{status} — {message}"
            if message:
                return str(message)
    text = r.text.strip().replace("\n", " ")
    return text[:300] or f"HTTP {r.status_code}"


def get_pano_metadata(lat: float, lng: float, radius: int = 50) -> PanoMetadata | None:
    r = SESSION.get(STREETVIEW_META, params={
        "location": f"{lat},{lng}",
        "radius":   radius,
        "source":   "outdoor",
        "key":      API_KEY,
    }, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Street View metadata HTTP {r.status_code}: {summarize_response_error(r)}")
    d = r.json()
    status = d.get("status")
    if status == "OK":
        location = d.get("location", {})
        return PanoMetadata(
            pano_id=d["pano_id"],
            lat=float(location.get("lat", lat)),
            lng=float(location.get("lng", lng)),
        )
    if status in {"ZERO_RESULTS", "NOT_FOUND"}:
        return None
    raise RuntimeError(
        f"Street View metadata: {status or 'UNKNOWN'} — {d.get('error_message', 'unexpected response')}"
    )


def fetch_frame(pano_id: str, heading: float, size: str, pitch: int, fov: int) -> bytes | None:
    r = SESSION.get(STREETVIEW_URL, params={
        "size":    size,
        "pano":    pano_id,
        "heading": f"{heading:.2f}",
        "pitch":   pitch,
        "fov":     fov,
        "key":     API_KEY,
    }, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Street View image HTTP {r.status_code}: {summarize_response_error(r)}")
    return r.content


# ---------- pipeline ------------------------------------------------------

def build_frames(points, out_dir: Path, size, pitch, fov, pano_radius, heading_lookahead):
    out_dir.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    panos: list[PanoMetadata] = []
    skipped_nopano = skipped_dup = 0

    for lat, lng in points:
        pano = get_pano_metadata(lat, lng, radius=pano_radius)
        if pano is None:
            skipped_nopano += 1
            continue
        if pano.pano_id in seen:
            skipped_dup += 1
            continue
        seen.add(pano.pano_id)
        panos.append(pano)

    if not panos:
        print(f"  total: 0 frames written, {skipped_nopano} gaps, {skipped_dup} duplicates")
        return 0

    headings = compute_pano_headings(panos, heading_lookahead)
    written = 0

    for pano, h in zip(panos, headings):
        img = fetch_frame(pano.pano_id, h, size, pitch, fov)
        if img is None:
            continue

        (out_dir / f"frame_{written:06d}.jpg").write_bytes(img)
        written += 1
        if written % 25 == 0:
            print(f"  frames: {written}  (no-pano: {skipped_nopano}, dup: {skipped_dup})")
        # be nice to the API
        time.sleep(0.02)

    print(f"  total: {written} frames written, {skipped_nopano} gaps, {skipped_dup} duplicates")
    return written


def has_existing_frames(frames_dir: Path) -> bool:
    return any(frames_dir.glob("frame_*.jpg"))


STABILIZE_FILTER = (
    "deflicker=size=11:mode=median,"
    "hqdn3d=1.2:0:6:0"
)


def stabilize_and_encode(
    frames_dir: Path,
    out: Path,
    fps: int,
    stabilize: bool,
):
    pattern = str(frames_dir / "frame_%06d.jpg")
    filters: list[str] = []
    if stabilize:
        print("      stabilization: applying temporal consistency filter chain")
        filters.append(STABILIZE_FILTER)

    command = [
        "ffmpeg", "-y", "-framerate", str(fps), "-i", pattern,
    ]
    if filters:
        command.extend(["-vf", ",".join(filters)])
    command.extend([
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-crf", "18" if filters else "20", "-preset", "slow", str(out),
    ])
    subprocess.run(command, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--start", help="origin (address or 'lat,lng')")
    p.add_argument("--end",   help="destination (address or 'lat,lng')")
    p.add_argument("--out",   default="route.mp4")
    p.add_argument("--mode",  default="driving",
                   choices=["driving", "walking", "bicycling", "transit"])
    p.add_argument("--step",  type=float, default=10.0, help="meters between samples")
    p.add_argument("--fps",   type=int,   default=24)
    p.add_argument("--size",  default="1280x720", help="frame size, max 640x640 on free tier")
    p.add_argument("--pitch", type=int,   default=0, help="-90..90, 0 is horizon")
    p.add_argument("--fov",   type=int,   default=90, help="10..120, lower = zoomed in")
    p.add_argument("--pano-radius", type=int, default=50,
                   help="metadata search radius, m — raise for sparse rural coverage")
    p.add_argument("--heading-lookahead", type=int, default=3,
                   help="number of unique panos to look ahead when aiming camera; higher keeps the road more centered")
    p.add_argument("--frames-dir", default="frames")
    p.add_argument("--encode-only", action="store_true",
                   help="skip routing/fetching and encode existing frame_*.jpg files from --frames-dir")
    p.add_argument("--stabilize", action="store_true",
                   help="run ffmpeg color-preserving temporal smoothing for flicker/noise consistency")
    p.add_argument("--keep-frames", action="store_true",
                   help="don't warn if frames dir exists before fetching; useful for resuming")
    args = p.parse_args()

    frames_dir = Path(args.frames_dir)
    if args.encode_only:
        if not frames_dir.is_dir():
            sys.exit(f"frames dir not found: {frames_dir}")
        if not has_existing_frames(frames_dir):
            sys.exit(f"no frame_*.jpg files found in {frames_dir}")
        print(
            f"[1/1] Encoding existing frames from {frames_dir} → {args.out}"
            + ("  (with stabilization)" if args.stabilize else "")
        )
        stabilize_and_encode(
            frames_dir,
            Path(args.out),
            args.fps,
            args.stabilize,
        )
        print("done.")
        return

    if not args.start or not args.end:
        p.error("--start and --end are required unless --encode-only is set")

    require_api_key()

    if frames_dir.exists() and any(frames_dir.iterdir()) and not args.keep_frames:
        print(f"warning: {frames_dir} is not empty; existing frames will be overwritten",
              file=sys.stderr)

    print(f"[1/3] Routing {args.start!r} → {args.end!r} via {args.mode}")
    route = get_route(args.start, args.end, args.mode)
    print(f"      polyline: {len(route)} points")

    print(f"[2/3] Densifying at {args.step}m and fetching frames")
    dense = densify(route, args.step)
    print(f"      sample points: {len(dense)} (estimated cost ≈ ${len(dense) * 2 * 7 / 1000:.2f} "
          f"@ $7/1k for meta+image)")
    n = build_frames(
        dense,
        frames_dir,
        args.size,
        args.pitch,
        args.fov,
        args.pano_radius,
        args.heading_lookahead,
    )
    if n == 0:
        sys.exit("no frames fetched — route may lack Street View coverage")

    print(
        f"[3/3] Encoding → {args.out}"
        + ("  (with stabilization)" if args.stabilize else "")
    )
    stabilize_and_encode(
        frames_dir,
        Path(args.out),
        args.fps,
        args.stabilize,
    )
    print("done.")


if __name__ == "__main__":
    main()
