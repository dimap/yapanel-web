import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.os.Handler;
import android.os.Looper;

import java.io.File;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Minimal Android audio player for the on-device YapAnel TTS fallback.
 *
 * Run through app_process with CLASSPATH pointing to the compiled DEX.
 * Unlike external playback tools, this class only depends on Android APIs.
 */
public final class StationAudio {
    private static final int MAX_PLAYBACK_MS = 60000;

    private StationAudio() {
    }

    public static void main(String[] args) {
        int code = play(args);
        if (code != 0) {
            System.exit(code);
        }
    }

    private static int play(String[] args) {
        if (args.length != 1) {
            System.err.println("Usage: StationAudio <audio-file>");
            return 2;
        }

        File audio = new File(args[0]);
        if (!audio.isFile() || !audio.canRead()) {
            System.err.println("Audio file does not exist or is not readable");
            return 2;
        }

        if (Looper.myLooper() == null) {
            Looper.prepare();
        }

        final Looper looper = Looper.myLooper();
        final AtomicInteger exitCode = new AtomicInteger(124);
        MediaPlayer player = new MediaPlayer();

        try {
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());

            player.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override
                public void onCompletion(MediaPlayer mp) {
                    exitCode.set(0);
                    looper.quit();
                }
            });

            player.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                @Override
                public boolean onError(MediaPlayer mp, int what, int extra) {
                    System.err.println("MediaPlayer error: what=" + what + ", extra=" + extra);
                    exitCode.set(1);
                    looper.quit();
                    return true;
                }
            });

            player.setDataSource(audio.getAbsolutePath());
            player.prepare();
            player.start();

            new Handler(looper).postDelayed(new Runnable() {
                @Override
                public void run() {
                    if (exitCode.get() == 124) {
                        System.err.println("Audio playback timed out");
                        looper.quit();
                    }
                }
            }, MAX_PLAYBACK_MS);

            Looper.loop();
            return exitCode.get();
        } catch (Exception ex) {
            System.err.println("Could not play audio: " + ex.getMessage());
            return 1;
        } finally {
            player.release();
        }
    }
}
