#!/usr/bin/env bash
# Concatenate the shots into one vertical video, optionally burning in captions.
#
#   compose.sh --out final.mp4 --width 720 --height 1280 \
#     [--ass captions.ass] shot1.mp4 shot2.mp4 shot3.mp4
#
# Each clip is scaled and cropped to fill the frame, then normalised to a
# single timebase and audio format before concat: clips that differ in fps,
# sample rate or SAR otherwise drift out of sync partway through, which is the
# failure this filter graph exists to prevent.
set -euo pipefail

OUT=final.mp4
WIDTH=720
HEIGHT=1280
FPS=24
CRF=21
PRESET=veryfast
ABR=160k
ASS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --width) WIDTH="$2"; shift 2 ;;
    --height) HEIGHT="$2"; shift 2 ;;
    --fps) FPS="$2"; shift 2 ;;
    --ass) ASS="$2"; shift 2 ;;
    --) shift; break ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done

if [ $# -lt 1 ]; then
  echo "usage: compose.sh [--out f.mp4] [--ass c.ass] clip1.mp4 [clip2.mp4 ...]" >&2
  exit 2
fi

INPUTS=()
FILTERS=""
CONCAT=""
INDEX=0
for clip in "$@"; do
  INPUTS+=(-i "$clip")
  FILTERS+="[${INDEX}:v:0]scale=${WIDTH}:${HEIGHT}:force_original_aspect_ratio=increase,"
  FILTERS+="crop=${WIDTH}:${HEIGHT},fps=${FPS},setsar=1,settb=AVTB,setpts=PTS-STARTPTS[v${INDEX}];"
  FILTERS+="[${INDEX}:a:0]aresample=48000:async=1:first_pts=0,"
  FILTERS+="aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
  FILTERS+="asetpts=PTS-STARTPTS[a${INDEX}];"
  CONCAT+="[v${INDEX}][a${INDEX}]"
  INDEX=$((INDEX + 1))
done
FILTERS+="${CONCAT}concat=n=${INDEX}:v=1:a=1[vcat][acat];"

if [ -n "$ASS" ]; then
  # libass resolves the filename relative to the working directory, so run
  # this from the directory holding the .ass file (or pass a bare name).
  FILTERS+="[vcat]ass=filename=${ASS}[vout]"
else
  FILTERS+="[vcat]null[vout]"
fi

ffmpeg -y -hide_banner -loglevel error "${INPUTS[@]}" \
  -filter_complex "$FILTERS" \
  -map "[vout]" -map "[acat]" \
  -c:v libx264 -preset "$PRESET" -crf "$CRF" \
  -pix_fmt yuv420p -vsync cfr \
  -c:a aac -b:a "$ABR" -ar 48000 \
  -movflags +faststart -max_muxing_queue_size 2048 \
  "$OUT"

echo "wrote $OUT"
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 "$OUT"
