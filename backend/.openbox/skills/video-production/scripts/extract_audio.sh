#!/usr/bin/env bash
# Pull the audio track out of a generated clip, for transcription.
#
# video_transcribe takes an audio asset, so the flow is:
#   extract_audio.sh shot1.mp4 shot1.mp3
#   share_file(file_path="shot1.mp3", attach=false)   -> asset_id
#   video_transcribe(action="submit", asset_id=...)
#
# attach=false matters: an intermediate file the person never asked to see
# should not become a card in the conversation.
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: extract_audio.sh <input-video> <output-audio.mp3>" >&2
  exit 2
fi

ffmpeg -y -hide_banner -loglevel error \
  -i "$1" \
  -vn -acodec libmp3lame -ar 16000 -ac 1 -b:a 64k \
  "$2"

echo "wrote $2"
