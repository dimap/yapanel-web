/* Lite 2 ARM32 ALSA tap. Copies the stock 48 kHz, 3-channel capture before
 * optionally silencing Alice's two microphone channels. No disk audio, waits,
 * allocations or network calls in the audio thread: local nonblocking datagrams.
 * Suppression requires both a live receiver and a lease refreshed within 2 s. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#ifndef MIC_SOCKET
#define MIC_SOCKET "/tmp/webpanel-mic/audio.sock"
#endif
#ifndef MIC_LEASE
#define MIC_LEASE "/tmp/webpanel-mic/exclusive"
#endif

typedef long frames_t;
typedef unsigned long uframes_t;
static frames_t (*real_read)(void *, void *, uframes_t);
static frames_t (*real_mmap_read)(void *, void *, uframes_t);
static int (*stream)(void *);
static long (*frame_bytes)(void *, long);
static int sock = -1;
static struct sockaddr_un destination;
static __thread unsigned depth;
static __thread uint32_t sequence;

__attribute__((constructor)) static void initialize(void) {
    real_read = dlsym(RTLD_NEXT, "snd_pcm_readi");
    real_mmap_read = dlsym(RTLD_NEXT, "snd_pcm_mmap_readi");
    stream = dlsym(RTLD_NEXT, "snd_pcm_stream");
    frame_bytes = dlsym(RTLD_NEXT, "snd_pcm_frames_to_bytes");
    sock = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    destination.sun_family = AF_UNIX;
    strcpy(destination.sun_path, MIC_SOCKET);
}

static void captured(void *pcm, void *buffer, frames_t frames) {
    if (depth || frames <= 0 || !stream || !frame_bytes || sock < 0 ||
        stream(pcm) != 1 || frame_bytes(pcm, 1) != 6) return;
    /* Wire header (little endian on the target): magic, rate, channels/bits,
     * frames, sequence. Split long reads; never send partial PCM frames. */
    unsigned char packet[20 + 1024 * 6];
    const uint32_t fixed[3] = {0x314d5057, 48000, (16U << 16) | 3};
    memcpy(packet, fixed, 12);
    unsigned char *input = buffer;
    int delivered = 1;
    for (frames_t offset = 0; offset < frames; offset += 1024) {
        uint32_t n = (uint32_t)(frames - offset);
        if (n > 1024) n = 1024;
        uint32_t seq = sequence++;
        memcpy(packet + 12, &n, 4);
        memcpy(packet + 16, &seq, 4);
        memcpy(packet + 20, input + offset * 6, n * 6);
        if (sendto(sock, packet, 20 + n * 6, MSG_DONTWAIT | MSG_NOSIGNAL,
                   (struct sockaddr *)&destination, sizeof destination) < 0)
            delivered = 0;
    }
    struct stat lease;
    time_t now = time(NULL);
    if (delivered && stat(MIC_LEASE, &lease) == 0 &&
        now >= lease.st_mtime && now - lease.st_mtime <= 2) {
        /* Feedback remains intact for Alice's echo processing. */
        int16_t *samples = buffer;
        for (frames_t i = 0; i < frames; i++) samples[i*3] = samples[i*3+1] = 0;
    }
}

frames_t snd_pcm_readi(void *pcm, void *buffer, uframes_t count) {
    if (!real_read) return -38; /* ENOSYS, only if loaded without ALSA. */
    depth++;
    frames_t n = real_read(pcm, buffer, count);
    depth--;
    captured(pcm, buffer, n);
    return n;
}
frames_t snd_pcm_mmap_readi(void *pcm, void *buffer, uframes_t count) {
    if (!real_mmap_read) return -38;
    depth++;
    frames_t n = real_mmap_read(pcm, buffer, count);
    depth--;
    captured(pcm, buffer, n);
    return n;
}
