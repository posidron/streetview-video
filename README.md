# Streetview Video

Build a hyperlapse-style MP4 between any two points using Google Street View.

## Example

<table>
  <tr>
    <td><a href="https://youtube.com/shorts/MgUx6vFOtLU"><img src="https://img.youtube.com/vi/MgUx6vFOtLU/hqdefault.jpg" alt="Redmond to Aurora (Drive)" width="320"></a></td>
    <td><a href="https://youtube.com/shorts/hu3_zRD6uGI"><img src="https://img.youtube.com/vi/hu3_zRD6uGI/hqdefault.jpg" alt="Pike to Needle (Walk)" width="320"></a></td>
  </tr>
</table>

## Setup

```bash
uv sync
# ffmpeg must be on PATH. (brew install ffmpeg works on macOS;
# apt-get install ffmpeg on Ubuntu).
```

Get an API key from https://console.cloud.google.com → enable **Routes API** and
**Street View Static API** on the same project, then put it in a local `.env` file:

```bash
printf 'GOOGLE_MAPS_API_KEY=AIza...\n' > .env
```

You can still use a shell export instead if you prefer:

```bash
export GOOGLE_MAPS_API_KEY=AIza...
```

The script prefers the modern Routes API and falls back to the legacy Directions
API if that is what your project has enabled.

## Usage

```bash
# Quick test, walking pace, ~1 km
uv run streetview_movie.py \
  --start "Pike Place Market, Seattle" \
  --end   "Space Needle, Seattle" \
  --mode  walking \
  --step  8 \
  --stabilize \
  --out   pike_to_needle.mp4

# Longer driving trip — use a bigger step to control cost
uv run streetview_movie.py \
  --start "Seattle, WA" \
  --end   "Leavenworth, WA" \
  --step  25 \
  --fps   30 \
  --out   leavenworth.mp4

# Re-encode an existing frame set without any Google API calls
uv run streetview_movie.py \
  --encode-only \
  --frames-dir redmond_to_aurora_frames \
  --stabilize \
  --out   redmond_to_aurora_consistent.mp4
```

## Flags

| flag | default | notes |
|---|---|---|
| `--mode` | driving | also `walking` or `bicycling`; affects the routing profile. |
| `--step` | 10 m | smaller = smoother + way more $$. 15–25m is fine for driving. |
| `--fps` | 24 | lower = slower playback (more time per frame); 24 feels cinematic, 30–60 feels more like dashcam. |
| `--size` | 1280x720 | max 640x640 on the free tier; up to 2048x2048 otherwise. |
| `--pitch` | 0 | negative looks down, positive looks up. |
| `--fov` | 90 | 60 = telephoto feel, 120 = fisheye wide. |
| `--pano-radius` | 50 | raise to 100–200 for rural/sparse coverage. |
| `--heading-lookahead` | 3 | aim using the snapped pano path a few panos ahead to keep the road more centered. |
| `--frames-dir` | `frames` | where frames get written, and where `--encode-only` reads them from. |
| `--encode-only` | off | skip Google API calls and just run ffmpeg over an existing `frame_*.jpg` directory. |
| `--stabilize` | off | ffmpeg deflicker + luma-only denoise; smoother playback while preserving color better. |
| `--keep-frames` | off | skip the "frames dir already exists" warning; useful for resuming into an existing `--frames-dir`. |

## Cost math

Each sample point costs **2 API hits** (metadata + image). At $7/1000 per endpoint
that's ~$0.014/sample. The script prints an estimate before fetching. You also
get a $200/month free credit on new Google Cloud projects, which covers roughly
14k samples before you pay anything.

## Gotchas

- **Coverage holes.** Dirt roads, trails, recent construction, some countries
  outside major cities → sparse or no panos. The script logs how many samples
  returned no pano; if that number is huge, raise `--pano-radius` or accept that
  the route won't render well.
- **Duplicate frames.** Requested points often snap to the same pano. The script
  dedupes by `pano_id`, so your output frame count is usually lower than the
  sample count — that's expected, not a bug.
- **Heading wobble.** Already smoothed with a 7-frame circular rolling mean.
  Raise `--heading-lookahead` if some frames still glance too hard into turns
  or side roads.
- **Rate limits.** The 20ms sleep between requests keeps you well under the
  default QPS ceilings. Parallelizing with a thread pool works, but watch for
  429s.
