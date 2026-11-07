# Lite 2 microphone capture

`tap.c` is an ARM32 LD_PRELOAD library for the stock Lite 2 audio process.
The assistant daemon remains ARM64. Build with Zig 0.14.1 using `sh build.sh`;
release assets, Windows archives and Docker builds include the library.

The stock `multi_capture` ALSA input contains 48 kHz S16_LE samples with three
channels: microphone 0, microphone 1, and speaker feedback. The tap copies these
frames before returning them to Alice. It sends bounded, nonblocking Unix
datagrams to `/tmp/webpanel-mic/audio.sock`; audio is never written to disk.
The receiver directory is mode 0700 and socket mode 0600.

Exclusive routing requires a receiver accepting packets and an `exclusive` lease
refreshed within two seconds. Only the two microphone channels are silenced for
Alice; speaker feedback is unchanged. If the daemon exits or the lease expires,
Alice gets the original samples. The Go receiver downsamples to 24 kHz mono,
detects speech boundaries and uses either the selected STT or native Realtime.
Playback is half duplex: our own output is not treated as a new question. The
panel stop/mode buttons remain available independently of STT accuracy.

Capture is opt-in via the microphone button. `manage.sh` temporarily bind-mounts
an altered launch configuration and restarts Quasar. It refuses an unrelated
existing mount. An independent watchdog restores the original configuration
unless a live receiver confirms capture. Disabling capture stops the processes
holding the bind mount, unmounts it and restarts the stock launch configuration.
Only a user-enabled installation is restored at boot. The read-only firmware
files are never overwritten.

The hardware layout check deliberately limits this feature to the tested
PDM/fs2105 configuration. Other speakers need separate investigation.
