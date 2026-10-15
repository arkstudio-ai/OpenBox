#!/usr/bin/env bash
# Sample evenly spaced frames from a generated take, so the picture can be
# looked at — not just the audio.
#
#   extract_frames.sh shot4.mp4 shot4_frames [--frames 6] [--width 540]
#
# Prints one JSON object: the clip's duration and, per frame, its file and
# the second it was taken at. This is the FALLBACK picture check for card 3:
# the primary one is `video_analyze(action="screen_text")`, which samples at
# ~1 fps and asks a vision model about text. When that tool is refused or
# unavailable, extract frames here and look at each with `view_image`;
# when neither works, card 3 says the picture was not checked — it never says
# the take is clean.
#
# Why this exists (2026-10-15, Gaode production, slug crab-voucher): Seedance
# 2.0 burnt the spoken line into shots 4–5 as captions, with 限时 drawn as 随时,
# although every prompt said 无字幕. STT heard the right words and passed the
# take; the subtitles were only ever visible in the frames.
set -euo pipefail

FRAMES=6
WIDTH=540
POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --frames) FRAMES="$2"; shift 2 ;;
    --width) WIDTH="$2"; shift 2 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

if [ "${#POSITIONAL[@]}" -lt 2 ]; then
  echo "usage: extract_frames.sh <input-video> <out-dir> [--frames N] [--width W]" >&2
  exit 2
fi
INPUT="${POSITIONAL[0]}"
OUTDIR="${POSITIONAL[1]}"
case "$FRAMES" in
  ''|*[!0-9]*) echo "--frames must be a positive integer" >&2; exit 2 ;;
esac
if [ "$FRAMES" -lt 1 ]; then
  echo "--frames must be a positive integer" >&2; exit 2
fi

DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$INPUT" | tail -n 1)
case "$DURATION" in
  ''|N/A) echo "could not read the duration of $INPUT" >&2; exit 1 ;;
esac

mkdir -p "$OUTDIR"
rm -f "$OUTDIR"/frame_*.jpg

# fps = frames / duration puts frame k (1-based) at about (k-1) × duration/frames
# seconds: the first frame is the opening image, the last one sits near the end.
FPS=$(awk -v n="$FRAMES" -v d="$DURATION" 'BEGIN { if (d <= 0) d = 1; printf "%.6f", n / d }')
ffmpeg -y -hide_banner -loglevel error \
  -i "$INPUT" \
  -vf "fps=${FPS},scale=${WIDTH}:-2" -frames:v "$FRAMES" -q:v 3 \
  "$OUTDIR/frame_%02d.jpg"

awk -v n="$FRAMES" -v d="$DURATION" -v dir="$OUTDIR" 'BEGIN {
  printf "{\"duration\": %.2f, \"frames_requested\": %d, \"frames\": [", d, n
  produced = 0
  for (k = 1; k <= n; k++) {
    file = sprintf("%s/frame_%02d.jpg", dir, k)
    if ((getline line < file) < 0) { close(file); continue }
    close(file)
    if (produced > 0) printf ", "
    printf "{\"file\": \"%s\", \"sec\": %.1f}", file, (k - 1) * d / n
    produced++
  }
  printf "], \"frames_produced\": %d}\n", produced
}'
