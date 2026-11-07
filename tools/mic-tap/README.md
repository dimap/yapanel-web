# Experimental microphone tap

On-device experiment, 2026-11-05. Not installed by the panel.

The Lite 2 has an AArch64 kernel but ARM32 hard-float userland. ALSA capture
`hw:0,1` has eight S16_LE channels at 48 kHz. The stock `multi_capture_raw`
configuration maps hardware channels 0 and 2 to microphones, and 6 to speaker
feedback. `maind --platformd` owns the hardware input while microphones are on.
A second hardware reader gets EBUSY. A dsnoop experiment opened both clients,
but the hardware remained PREPARED and produced no frames; configuration was
restored afterward.

`mic-tap.c` is a temporary LD_PRELOAD probe. It forwards ALSA calls and copies
capture data only while `/tmp/codex-mic-tap.enable` exists. It writes at most
4,608,000 bytes into `/tmp/codex-mic-tap.raw`, with per-chunk metadata in
`/tmp/codex-mic-tap.meta` (four little-endian uint32: method, bytes per frame,
requested copy size, previous output offset). Use a fresh process for each test.
The probe is intended for this device's local tmpfs and one capture thread;
it is not a production recording service or an arbitrary-format audio recorder.

Build example with Zig 0.14.1:

```sh
zig cc -target arm-linux-gnueabihf -fPIC -shared -nostdlib -O2 \
  -Wall -Wextra -o /tmp/codex-mic-tap.so tools/mic-tap/mic-tap.c
```

The host process must already load libdl and ALSA. Plain `/bin/true` cannot
validate this build because it does not supply `dlsym`. On the tested firmware,
`maind --platformd` supplies these dependencies.

Test result: 469 successful `snd_pcm_readi` chunks, 1,536 frames per chunk,
6 bytes per frame, 48 kHz (15.008 seconds). The copied data contains two raw
microphone channels and one speaker feedback channel. The user spoke and
confirmed Alice heard them during the recording. The microphone channels had
varying nonzero signal; the feedback channel was near zero. Semantic contents
of the recording were not independently transcribed or verified.

The test injected LD_PRELOAD only into the platformd task using a temporary
bind mount over launch.json. No permanent configuration was changed. Before
unmounting, stop the launcher and its children: the launcher can hold the mount
busy. SIGTERM did not reliably terminate platformd in prior tests; forced
termination and a stock restart were needed. Always arrange an independent
rollback and verify the original launch.json bytes, absence of the preload
mapping, and advancing hardware capture pointers after restoring.

Do not install this probe persistently. Production work needs a bounded stream
transport, explicit recording lifecycle, format negotiation, and verification
that copying does not delay Alice's audio thread. Raw microphone audio is not
echo-cancelled speech; the feedback channel is available separately.
