#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <time.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <sound/asound.h>

#define SYS_COMMON "/data/data/com.yandex.io.sdk/files/system/sounds/common"
#define SYS_RURU   "/data/data/com.yandex.io.sdk/files/system/sounds/ru-RU"

// ALSA control numids for Silergy SY6045S on AML-AUGESOUND:
// numid=4:  Woofer Master Playback Switch
// numid=5:  Woofer Ch1 Playback Switch
// numid=6:  Woofer Ch2 Playback Switch
// numid=13: Tweeters Master Playback Switch
// numid=14: Tweeters Ch1 Playback Switch
// numid=15: Tweeters Ch2 Playback Switch
static const unsigned int MUTE_NUMIDS[] = { 4, 5, 6, 13, 14, 15 };
static const size_t NUM_MUTE_IDS = sizeof(MUTE_NUMIDS) / sizeof(MUTE_NUMIDS[0]);

static volatile int g_running = 1;
static int g_is_muted = 0;
static struct timespec g_mute_time;
static int g_alsa_fd = -1;

static void open_alsa() {
    if (g_alsa_fd < 0) {
        g_alsa_fd = open("/dev/snd/controlC0", O_RDWR);
    }
}

static void close_alsa() {
    if (g_alsa_fd >= 0) {
        close(g_alsa_fd);
        g_alsa_fd = -1;
    }
}

static int set_mute(int mute) {
    if (g_is_muted == mute) {
        return 0;
    }

    open_alsa();
    int val_to_write = mute ? 0 : 1; // 0 = Mute/Off, 1 = Playback On
    int ioctl_ok = 1;

    if (g_alsa_fd >= 0) {
        for (size_t i = 0; i < NUM_MUTE_IDS; i++) {
            struct snd_ctl_elem_value elem;
            memset(&elem, 0, sizeof(elem));
            elem.id.numid = MUTE_NUMIDS[i];
            elem.value.integer.value[0] = val_to_write;
            if (ioctl(g_alsa_fd, SNDRV_CTL_IOCTL_ELEM_WRITE, &elem) < 0) {
                ioctl_ok = 0;
            }
        }
    } else {
        ioctl_ok = 0;
    }

    if (!ioctl_ok) {
        // Fallback via tinymix2
        char cmd[128];
        snprintf(cmd, sizeof(cmd),
            "tinymix2 set 3 %d; tinymix2 set 4 %d; tinymix2 set 5 %d; "
            "tinymix2 set 12 %d; tinymix2 set 13 %d; tinymix2 set 14 %d >/dev/null 2>&1",
            val_to_write, val_to_write, val_to_write,
            val_to_write, val_to_write, val_to_write);
        system(cmd);
    }

    g_is_muted = mute;
    time_t now = time(NULL);
    struct tm* tm_info = localtime(&now);
    char time_buf[16];
    strftime(time_buf, sizeof(time_buf), "%H:%M:%S", tm_info);

    if (mute) {
        clock_gettime(CLOCK_MONOTONIC, &g_mute_time);
        printf("[%s] [bt-memes-native] MUTE: Alice speech suppressed (Hardware Amp Switches -> OFF)\n", time_buf);
    } else {
        printf("[%s] [bt-memes-native] UNMUTE: Amplifiers restored (Hardware Amp Switches -> ON)\n", time_buf);
    }
    fflush(stdout);
    return 0;
}

static void on_signal(int sig) {
    (void)sig;
    g_running = 0;
    set_mute(0); // Ensure sound is unmuted on exit
}

static int copy_file_if_needed(const char* src, const char* dst, size_t min_size, size_t max_size) {
    struct stat st;
    if (stat(dst, &st) == 0) {
        if (min_size > 0 && (size_t)st.st_size >= min_size && (max_size == 0 || (size_t)st.st_size <= max_size)) {
            return 0; // Already correct
        }
    }

    int in_fd = open(src, O_RDONLY);
    if (in_fd < 0) return -1;

    int out_fd = open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (out_fd < 0) {
        close(in_fd);
        return -1;
    }

    char buf[4096];
    ssize_t bytes;
    while ((bytes = read(in_fd, buf, sizeof(buf))) > 0) {
        write(out_fd, buf, bytes);
    }
    close(in_fd);
    close(out_fd);
    chmod(dst, 0644);
    return 1;
}

static void install_sounds(const char* sounds_dir) {
    char src[256], dst[256];

    // ruko_blud.opus -> bluetooth_turning_on_jingle.opus
    snprintf(src, sizeof(src), "%s/ruko_blud.opus", sounds_dir);
    snprintf(dst, sizeof(dst), "%s/bluetooth_turning_on_jingle.opus", SYS_COMMON);
    copy_file_if_needed(src, dst, 35000, 0);

    // penis_her.opus -> bluetooth_turning_off_jingle.opus
    snprintf(src, sizeof(src), "%s/penis_her.opus", sounds_dir);
    snprintf(dst, sizeof(dst), "%s/bluetooth_turning_off_jingle.opus", SYS_COMMON);
    copy_file_if_needed(src, dst, 20000, 0);

    // silent.mp3 -> ru-RU/bluetooth_turning_on.mp3
    snprintf(src, sizeof(src), "%s/silent.mp3", sounds_dir);
    snprintf(dst, sizeof(dst), "%s/bluetooth_turning_on.mp3", SYS_RURU);
    copy_file_if_needed(src, dst, 1000, 3000);

    // silent.mp3 -> ru-RU/bluetooth_turning_off.mp3
    snprintf(src, sizeof(src), "%s/silent.mp3", sounds_dir);
    snprintf(dst, sizeof(dst), "%s/bluetooth_turning_off.mp3", SYS_RURU);
    copy_file_if_needed(src, dst, 1000, 3000);
}

static int contains_ignore_case(const char* haystack, const char* needle) {
    if (!haystack || !needle) return 0;
    return (strcasestr(haystack, needle) != NULL);
}

int main(int argc, char** argv) {
    char exe_dir[256] = "/data/local/tmp/max-platform/store/pkgs/bt-memes";
    char sounds_dir[256];
    
    // Determine path
    if (argc > 0 && argv[0]) {
        char* last_slash = strrchr(argv[0], '/');
        if (last_slash) {
            size_t len = last_slash - argv[0];
            if (len < sizeof(exe_dir)) {
                strncpy(exe_dir, argv[0], len);
                exe_dir[len] = '\0';
            }
        }
    }
    snprintf(sounds_dir, sizeof(sounds_dir), "%s/sounds", exe_dir);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGHUP, on_signal);

    printf("[bt-memes-native] Starting Native High-Performance Bluetooth Sound Supervisor v1.1\n");
    printf("[bt-memes-native] Sounds directory: %s\n", sounds_dir);
    fflush(stdout);

    // Ensure audio hardware starts in unmuted state
    open_alsa();
    set_mute(0);

    // First sound verification
    install_sounds(sounds_dir);

    // Open logcat pipe
    FILE* fp = popen("logcat -v brief -b main -b system -T 1", "r");
    if (!fp) {
        fprintf(stderr, "[bt-memes-native] Failed to open logcat pipe!\n");
        return 1;
    }

    // Set non-blocking or line-buffered
    int fd = fileno(fp);
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    char line[1024];
    time_t last_check = time(NULL);

    while (g_running) {
        // Safety timeout for mute: max 3.0 seconds
        if (g_is_muted) {
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            double elapsed = (now.tv_sec - g_mute_time.tv_sec) + (now.tv_nsec - g_mute_time.tv_nsec) / 1e9;
            if (elapsed > 3.0) {
                set_mute(0);
            }
        }

        // Periodic sound check every 30s
        time_t cur_time = time(NULL);
        if (cur_time - last_check > 30) {
            install_sounds(sounds_dir);
            last_check = cur_time;
        }

        // Read logcat line
        if (fgets(line, sizeof(line), fp) != NULL) {
            // Check MUTE triggers
            // 1) recognizedPhrase contains Bluetooth/блютус/синий
            int is_bt_phrase = 0;
            if (strstr(line, "recognizedPhrase=") != NULL) {
                if (contains_ignore_case(line, "bluetooth") ||
                    contains_ignore_case(line, "блютус") ||
                    contains_ignore_case(line, "блютуз") ||
                    contains_ignore_case(line, "блюту") ||
                    contains_ignore_case(line, "синий")) {
                    is_bt_phrase = 1;
                }
            }

            // 2) Directive received
            int is_bt_directive = 0;
            if (strstr(line, "because of start_bluetooth") != NULL ||
                strstr(line, "because of stop_bluetooth") != NULL ||
                strstr(line, "start_bluetooth.activation") != NULL ||
                strstr(line, "stop_bluetooth.activation") != NULL) {
                is_bt_directive = 1;
            }

            if (is_bt_phrase || is_bt_directive) {
                set_mute(1);
            }

            // Check UNMUTE triggers (only if currently muted)
            if (g_is_muted) {
                int is_unmute = 0;

                // a) Alice TTS finished speaking (exact completion pattern)
                if (strstr(line, "onHandleDirectiveCompleted completed=SUCCESS") != NULL &&
                    strstr(line, "tts_play_placeholder.activation:") != NULL) {
                    is_unmute = 1;
                }
                // b) Local audio file play capability routing
                else if (strstr(line, "routeDirective") != NULL &&
                         strstr(line, "local_audio_file_play") != NULL) {
                    is_unmute = 1;
                }
                // c) Meme jingle pipeline initializes
                else if (strstr(line, "initPipeline: filesrc") != NULL &&
                         (strstr(line, "bluetooth_turning_on_jingle") != NULL ||
                          strstr(line, "bluetooth_turning_off_jingle") != NULL)) {
                    is_unmute = 1;
                }

                if (is_unmute) {
                    set_mute(0);
                }
            }
        } else {
            usleep(10000); // 10ms sleep
        }
    }

    set_mute(0); // Ensure unmuted
    pclose(fp);
    close_alsa();
    return 0;
}
