# Call tones

The tones of the personal-assistant voice call
(`docs/VOICE_CALL_SPEC.md` §8). They are generated, not recorded, so they can
be rebuilt byte for byte with the commands below.

| File | When | Sound |
| --- | --- | --- |
| `call_ringback.wav` | From the socket opening until `ready` (the server answers once the greeting is made), looped with 4 s of silence after each 1 s tone by the player — the Chinese ringback cadence | 450 Hz 1 s, 10 ms ramps |
| `call_ended.wav` | Hang-up / normal end | 660 Hz 90 ms → 440 Hz 90 ms |
| `call_error.wav` | Error end | 330 Hz 200 ms |

Format: PCM 16-bit little endian, mono, 24 kHz (the call's playback rate), a
canonical 44-byte header, peak amplitude 0.2 of full scale, 5 ms linear ramps
at every note edge (10 ms for the ringback) so nothing clicks. The app mixes them into the call output
on their own layer, which `playback.clear` never touches
(`lib/features/voice/audio/pcm_player.dart`).

## Regenerate

Run from this directory (`mobile/assets/sounds/`), ffmpeg 7 or later:

```sh
FF=/opt/homebrew/bin/ffmpeg

$FF -hide_banner -loglevel error -y -f lavfi \
  -i "aevalsrc='0.2*sin(2*PI*450*t)*clip(min(t,1-t)/0.01,0,1)':s=24000:d=1" \
  -ac 1 -ar 24000 -c:a pcm_s16le -fflags +bitexact -flags:a +bitexact -map_metadata -1 call_ringback.wav

$FF -hide_banner -loglevel error -y -f lavfi \
  -i "aevalsrc='0.2*(sin(2*PI*660*t)*clip(min(t,0.09-t)/0.005,0,1)+sin(2*PI*440*t)*clip(min(t-0.09,0.18-t)/0.005,0,1))':s=24000:d=0.18" \
  -ac 1 -ar 24000 -c:a pcm_s16le -fflags +bitexact -flags:a +bitexact -map_metadata -1 call_ended.wav

$FF -hide_banner -loglevel error -y -f lavfi \
  -i "aevalsrc='0.2*sin(2*PI*330*t)*clip(min(t,0.2-t)/0.005,0,1)':s=24000:d=0.2" \
  -ac 1 -ar 24000 -c:a pcm_s16le -fflags +bitexact -flags:a +bitexact -map_metadata -1 call_error.wav
```

`-fflags +bitexact -flags:a +bitexact -map_metadata -1` keeps ffmpeg from
writing its encoder name into a `LIST` chunk, so each file is exactly a
44-byte header followed by the samples (48,044 / 8,684 / 9,644 bytes).
