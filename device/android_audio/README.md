# StationAudio for Station Max

This directory contains the first-party Android 9 audio playback helper used by
the on-device `/api/say` Google TTS fallback.

`StationAudio.java` uses Android `MediaPlayer` with the media audio stream.
It is built into `StationAudio.dex` using Android SDK 34, Java 8 bytecode
and D8 (`--min-api 28`). The reproducible build workflow checks that the DEX
matches both the committed file and the base64 embedded in `max_profile.py`.

On-device YapAnel unpacks this verified embedded DEX into
`/data/yapanel-state/bin/StationAudio.dex` when it first needs TTS.
It does not modify `/odm` or require `start-stop-daemon`, `aplay`,
`ffmpeg` or externally licensed playback binaries.

To test the player itself on an authorized Android 9 Station Max, copy the
DEX to `/data/local/tmp/StationAudio.dex` and play an MP3 file already
stored in `/data/local/tmp`:

```sh
CLASSPATH=/data/local/tmp/StationAudio.dex \
    /system/bin/app_process /system/bin StationAudio \
    /data/local/tmp/yapanel-say-test.mp3
```

A successful exit code means MediaPlayer's completion callback fired, but
audible output should also be verified on real hardware.

The helper runs with the YapAnel process privileges and needs a suitable
Android audio service available to the calling user. The TTS request uses
Google Translate over HTTPS and requires internet access.

Copyright applies under the repository's MIT license.
