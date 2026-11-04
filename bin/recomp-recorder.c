/* recomp-recorder — drains a recomp capture shared-memory ring
 * (recomp_capture_shm.h) into an MP4 via ffmpeg.
 *
 *   recomp-recorder --shm NAME --out FILE.mp4 [--fps N]
 *                   [--ffmpeg PATH] [--timeout SEC]
 *
 * The producer (librecompcapture.so in the game) publishes finished
 * game frames at a constant capture rate; this consumer writes them
 * to ffmpeg as a constant-framerate raw stream, repeating the last
 * frame when the producer's sequence shows a gap (a frame the ring
 * dropped), so the output timeline stays true. Audio, when the
 * producer provides it, is written against the video frames'
 * CLOCK_MONOTONIC timestamps.
 *
 * SIGINT/SIGTERM finalize the MP4 cleanly. Exit codes:
 *   0 recorded (or finalized early by signal / producer exit)
 *   2 no capture stream appeared within --timeout
 *   3 capture header version mismatch
 *   4 ffmpeg failed
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "recomp_capture_shm.h"

static volatile sig_atomic_t s_stop = 0;
static void on_signal(int sig) { (void)sig; s_stop = 1; }

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static int write_all(int fd, const void *buf, size_t bytes) {
    const uint8_t *p = (const uint8_t *)buf;
    while (bytes) {
        ssize_t n = write(fd, p, bytes);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += n;
        bytes -= (size_t)n;
    }
    return 0;
}

/* Run a command, return its exit status (or -1). */
static int run_quiet(char *const argv[]) {
    pid_t pid = fork();
    if (pid == 0) {
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, 1);
            dup2(devnull, 2);
        }
        execvp(argv[0], argv);
        _exit(127);
    }
    if (pid < 0) return -1;
    int st = 0;
    while (waitpid(pid, &st, 0) < 0 && errno == EINTR) { }
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

static int encoder_usable(const char *ffmpeg, const char *enc) {
    if (strcmp(enc, "libx264") == 0) return 1;      /* always present */
    char *args[] = {(char *)ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=black:s=64x64",
                    "-frames:v", "1", "-c:v", (char *)enc,
                    "-f", "null", "-", 0};
    return run_quiet(args) == 0;
}

int main(int argc, char **argv) {
    const char *shm_arg = 0, *out_path = 0, *ffmpeg = "ffmpeg";
    int fps_override = 0, timeout_s = 30;
    long game_pid = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--shm") == 0 && i + 1 < argc) shm_arg = argv[++i];
        else if (strcmp(argv[i], "--out") == 0 && i + 1 < argc) out_path = argv[++i];
        else if (strcmp(argv[i], "--ffmpeg") == 0 && i + 1 < argc) ffmpeg = argv[++i];
        else if (strcmp(argv[i], "--fps") == 0 && i + 1 < argc) fps_override = atoi(argv[++i]);
        else if (strcmp(argv[i], "--timeout") == 0 && i + 1 < argc) timeout_s = atoi(argv[++i]);
        else if (strcmp(argv[i], "--game-pid") == 0 && i + 1 < argc) game_pid = atol(argv[++i]);
        else {
            fprintf(stderr, "recomp-recorder: bad argument '%s'\n", argv[i]);
            return 64;
        }
    }
    if (!shm_arg || !out_path) {
        fprintf(stderr, "usage: recomp-recorder --shm NAME --out FILE "
                        "[--fps N] [--ffmpeg PATH] [--timeout SEC] [--game-pid PID]\n");
        return 64;
    }
    char shm_name[128];
    snprintf(shm_name, sizeof shm_name, "/%s",
             shm_arg[0] == '/' ? shm_arg + 1 : shm_arg);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, 0);
    sigaction(SIGTERM, &sa, 0);
    signal(SIGPIPE, SIG_IGN);

    /* ---- wait for the producer's stream -------------------------- */
    int fd = -1;
    uint64_t deadline = now_ns() + (uint64_t)timeout_s * 1000000000ull;
    while (!s_stop && now_ns() < deadline) {
        fd = shm_open(shm_name, O_RDWR, 0600);
        if (fd >= 0) break;
        usleep(100 * 1000);
    }
    if (fd < 0) {
        fprintf(stderr, "recomp-recorder: no capture stream '%s' "
                        "within %ds\n", shm_name, timeout_s);
        return 2;
    }
    void *page = mmap(0, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (page == MAP_FAILED) {
        fprintf(stderr, "recomp-recorder: header mmap failed\n");
        return 2;
    }
    recomp_capture_header *hdr = (recomp_capture_header *)page;
    while (!s_stop && now_ns() < deadline) {
        if (__atomic_load_n(&hdr->magic, __ATOMIC_ACQUIRE)
                == RECOMP_CAPTURE_MAGIC)
            break;
        usleep(50 * 1000);
    }
    if (hdr->magic != RECOMP_CAPTURE_MAGIC) {
        fprintf(stderr, "recomp-recorder: stream '%s' never became "
                        "ready\n", shm_name);
        return 2;
    }
    if (hdr->version != RECOMP_CAPTURE_VERSION) {
        fprintf(stderr, "recomp-recorder: capture version %u, "
                        "expected %u\n", hdr->version,
                RECOMP_CAPTURE_VERSION);
        return 3;
    }
    size_t total = (size_t)hdr->total_bytes;
    munmap(page, 4096);
    void *map = mmap(0, total, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED) {
        fprintf(stderr, "recomp-recorder: full mmap failed\n");
        return 2;
    }
    hdr = (recomp_capture_header *)map;
    const int fps = fps_override > 0 ? fps_override : (int)hdr->fps;
    const int have_audio = hdr->audio_enabled && hdr->audio_rate > 0;
    fprintf(stderr, "recomp-recorder: stream %s at %d fps, "
                    "audio %s\n", shm_name, fps,
            have_audio ? "present" : "absent (video-only)");

    /* ---- wait for the first frame (fixes the stream size) -------- */
    while (!s_stop && now_ns() < deadline) {
        if (__atomic_load_n(&hdr->published_seq, __ATOMIC_ACQUIRE) >= 1)
            break;
        if (__atomic_load_n(&hdr->state, __ATOMIC_RELAXED) == 0 &&
            __atomic_load_n(&hdr->published_seq, __ATOMIC_RELAXED) == 0)
            break;
        usleep(20 * 1000);
    }
    if (__atomic_load_n(&hdr->published_seq, __ATOMIC_RELAXED) < 1) {
        fprintf(stderr, "recomp-recorder: producer published no "
                        "frames\n");
        return 2;
    }

    /* ---- choose the encoder ------------------------------------- */
    const char *enc = encoder_usable(ffmpeg, "h264_nvenc")
        ? "h264_nvenc" : "libx264";

    /* ---- consume ----------------------------------------------- */
    recomp_capture_slot *slots = (recomp_capture_slot *)
        ((uint8_t *)map + hdr->video_offset);
    const uint64_t slot_stride =
        sizeof(recomp_capture_slot) + hdr->video_slot_bytes;
    uint8_t *frame_buf = (uint8_t *)malloc(hdr->video_slot_bytes);
    uint8_t *out_buf = 0;
    if (!frame_buf) return 70;

    pid_t ff_pid = -1;
    int ff_video_fd = -1, ff_audio_fd = -1;
    int out_w = 0, out_h = 0;
    uint64_t last_seq = 0, last_written_seq = 0;
    uint64_t frames_written = 0, frames_repeated = 0;
    int have_last = 0;
    uint64_t last_heartbeat_touch = 0;
    uint64_t stream_t0 = 0;         /* ts of first written frame */
    uint64_t seq_origin = 0;        /* producer seq at attach */
    uint64_t audio_read_pos = 0;
    int audio_started = 0;
    uint64_t audio_frames_written = 0;
    int failed = 0;
    /* Stream-size settle: the first frames can be a launcher/UI
     * window at one size and the game itself at another (the user
     * may sit in that UI for minutes). ffmpeg's raw input size is
     * fixed at spawn, so wait for a size that (a) differs from the
     * first size seen and has then been stable for ~45 frames, or
     * (b) is the only size ever seen after 10 s (a game that boots
     * straight in, skipping its launcher). Pre-settle frames are
     * consumed and dropped: the recording starts at the game. */
    int cand_w = 0, cand_h = 0, cand_count = 0;
    int first_w = 0, first_h = 0, size_changed = 0;
    uint64_t first_frame_wall = 0;

    while (!s_stop && !failed) {
        const uint64_t now = now_ns();
        if (now - last_heartbeat_touch > 500 * 1000 * 1000ull) {
            __atomic_store_n(&hdr->consumer_heartbeat_ns, now,
                             __ATOMIC_RELAXED);
            last_heartbeat_touch = now;
        }
        /* find the oldest READY slot with seq > last_seq */
        recomp_capture_slot *best = 0;
        uint64_t best_seq = 0;
        for (uint32_t i = 0; i < hdr->video_slots; i++) {
            recomp_capture_slot *s = (recomp_capture_slot *)
                ((uint8_t *)slots + i * slot_stride);
            if (__atomic_load_n(&s->state, __ATOMIC_ACQUIRE)
                    != RCAP_SLOT_READY)
                continue;
            if (s->seq > last_seq && (!best || s->seq < best_seq)) {
                best = s;
                best_seq = s->seq;
            }
        }
        if (!best) {
            /* End of stream: the producer's destructor clears state
             * on a clean game exit; the game pid dying is the other
             * authoritative signal; the launcher's SIGINT is the
             * third. A stale heartbeat is NOT one: this runtime
             * presents dirty-gated, so a static screen legitimately
             * goes many seconds between presents. */
            if (__atomic_load_n(&hdr->state, __ATOMIC_RELAXED) == 0)
                break;
            if (game_pid > 0 && kill((pid_t)game_pid, 0) != 0
                && errno == ESRCH)
                break;
            usleep(1000);
            continue;
        }
        __atomic_store_n(&best->state, RCAP_SLOT_READING,
                         __ATOMIC_RELAXED);
        if (getenv("RCAP_DEBUG"))
            fprintf(stderr, "recomp-recorder: consume seq=%llu "
                            "last_seq=%llu\n",
                    (unsigned long long)best->seq,
                    (unsigned long long)last_seq);
        const uint32_t fw = best->width, fh = best->height;
        const uint32_t fbytes = best->bytes;
        const uint64_t fseq = best->seq, fts = best->ts_ns;
        if (fbytes <= hdr->video_slot_bytes)
            memcpy(frame_buf, (uint8_t *)best + sizeof(*best), fbytes);
        __atomic_store_n(&best->state, RCAP_SLOT_FREE, __ATOMIC_RELEASE);
        if (fbytes > hdr->video_slot_bytes) continue;
        last_seq = fseq;
        if (!seq_origin) seq_origin = fseq;

        if (ff_pid < 0) {
            /* size-settle gate (see declarations): consume and drop
             * until the frame size is known to be the game's */
            if (!first_frame_wall) {
                first_frame_wall = now_ns();
                first_w = (int)fw;
                first_h = (int)fh;
                cand_w = (int)fw;
                cand_h = (int)fh;
                cand_count = 1;
            } else if ((int)fw == cand_w && (int)fh == cand_h) {
                cand_count++;
            } else {
                if ((int)fw != first_w || (int)fh != first_h)
                    size_changed = 1;
                cand_w = (int)fw;
                cand_h = (int)fh;
                cand_count = 1;
            }
            const int settled = cand_count >= 45 &&
                (size_changed ||
                 now_ns() - first_frame_wall > 10000 * 1000 * 1000ull);
            if (!settled) {
                last_written_seq = fseq;
                have_last = 0;
                continue;
            }
            fprintf(stderr, "recomp-recorder: frame size settled "
                            "at %dx%d\n", cand_w, cand_h);
        }

        if (ff_pid < 0) {
            /* first frame: spawn ffmpeg at this frame's size */
            out_w = (int)fw;
            out_h = (int)fh;
            out_buf = (uint8_t *)calloc(1, (size_t)out_w * out_h * 4);
            if (!out_buf) return 70;
            int vpipe[2], apipe[2] = {-1, -1};
            if (pipe(vpipe) != 0) return 70;
            if (have_audio && pipe(apipe) != 0) return 70;
            ff_pid = fork();
            if (ff_pid == 0) {
                dup2(vpipe[0], 0);
                close(vpipe[0]);
                close(vpipe[1]);   /* else ffmpeg never sees EOF */
                if (have_audio) {
                    dup2(apipe[0], 3);
                    close(apipe[0]);
                    close(apipe[1]);
                }
                char sdim[32], sfps[16];
                snprintf(sdim, sizeof sdim, "%dx%d", out_w, out_h);
                snprintf(sfps, sizeof sfps, "%d", fps);
                char *args[40];
                int a = 0;
                args[a++] = (char *)ffmpeg;
                args[a++] = "-hide_banner";
                args[a++] = "-loglevel"; args[a++] = "warning";
                args[a++] = "-y";
                args[a++] = "-f"; args[a++] = "rawvideo";
                args[a++] = "-pix_fmt"; args[a++] = "rgba";
                args[a++] = "-s"; args[a++] = sdim;
                args[a++] = "-r"; args[a++] = sfps;
                args[a++] = "-i"; args[a++] = "pipe:0";
                if (have_audio) {
                    char sar[16], sac[8];
                    snprintf(sar, sizeof sar, "%u", hdr->audio_rate);
                    snprintf(sac, sizeof sac, "%u", hdr->audio_channels);
                    args[a++] = "-f"; args[a++] = "s16le";
                    args[a++] = "-ar"; args[a++] = sar;
                    args[a++] = "-ac"; args[a++] = sac;
                    args[a++] = "-i"; args[a++] = "pipe:3";
                }
                args[a++] = "-map"; args[a++] = "0:v:0";
                if (have_audio) { args[a++] = "-map"; args[a++] = "1:a:0"; }
                args[a++] = "-c:v"; args[a++] = (char *)enc;
                if (strcmp(enc, "h264_nvenc") == 0) {
                    args[a++] = "-preset"; args[a++] = "p4";
                    args[a++] = "-rc"; args[a++] = "vbr";
                    args[a++] = "-cq"; args[a++] = "19";
                } else {
                    args[a++] = "-preset"; args[a++] = "veryfast";
                    args[a++] = "-crf"; args[a++] = "18";
                }
                args[a++] = "-pix_fmt"; args[a++] = "yuv420p";
                if (have_audio) {
                    args[a++] = "-c:a"; args[a++] = "aac";
                    args[a++] = "-b:a"; args[a++] = "160k";
                }
                args[a++] = (char *)out_path;
                args[a] = 0;
                execvp(ffmpeg, args);
                _exit(127);
            }
            close(vpipe[0]);
            ff_video_fd = vpipe[1];
            if (have_audio) {
                close(apipe[0]);
                ff_audio_fd = apipe[1];
            }
            fprintf(stderr, "recomp-recorder: encoding %dx%d with "
                            "%s -> %s\n", out_w, out_h, enc, out_path);
        }

        /* constant-rate timeline: repeat the last frame to fill
         * both sequence gaps (ring drops) and presentation-rate
         * gaps (a game presenting slower than the capture rate),
         * driven by the frames' own timestamps, so the output
         * duration stays true */
        if (have_last) {
            uint64_t by_time = stream_t0
                ? (fts - stream_t0) * (uint64_t)fps / 1000000000ull + 1
                : 0;
            /* seq numbers are the producer's absolute counter —
             * normalize to frames-since-attach or a mid-stream
             * attach would "fill" thousands of repeats */
            uint64_t by_seq = seq_origin ? fseq - seq_origin + 1 : 0;
            uint64_t target = by_time > by_seq ? by_time : by_seq;
            while (frames_written + 1 < target) {
                if (write_all(ff_video_fd, out_buf,
                              (size_t)out_w * out_h * 4) != 0) {
                    failed = 4;
                    break;
                }
                frames_written++;
                frames_repeated++;
            }
            if (failed) break;
        } else {
            stream_t0 = fts;
        }
        /* blit into the stream-sized buffer (crop/pad on resize) */
        memset(out_buf, 0, (size_t)out_w * out_h * 4);
        const uint32_t rows = fh < (uint32_t)out_h ? fh : (uint32_t)out_h;
        const uint32_t cols = fw < (uint32_t)out_w ? fw : (uint32_t)out_w;
        for (uint32_t y = 0; y < rows; y++)
            memcpy(out_buf + (size_t)y * out_w * 4,
                   frame_buf + (size_t)y * fw * 4, (size_t)cols * 4);
        if (write_all(ff_video_fd, out_buf,
                      (size_t)out_w * out_h * 4) != 0) {
            failed = 4;
            break;
        }
        frames_written++;
        last_written_seq = fseq;
        have_last = 1;

        /* audio, paced against this frame's timestamp */
        if (have_audio && ff_audio_fd >= 0) {
            const uint64_t rate = hdr->audio_rate;
            const uint64_t base_f = hdr->audio_base_frames;
            const uint64_t base_ts = hdr->audio_base_ts_ns;
            if (!audio_started) {
                audio_started = 1;
                int64_t pos = (int64_t)base_f +
                    (int64_t)(((int64_t)fts - (int64_t)base_ts)
                              * (int64_t)rate / 1000000000ll);
                if (pos < (int64_t)base_f) {
                    /* audio starts after the video: pad the gap */
                    uint64_t pad = base_f - (uint64_t)pos;
                    uint8_t *silence = (uint8_t *)calloc(1, 4096);
                    while (pad && silence) {
                        uint64_t chunk = pad * 4 > 4096 ? 1024 : pad;
                        if (write_all(ff_audio_fd, silence,
                                      (size_t)chunk * 4) != 0) break;
                        audio_frames_written += chunk;
                        pad -= chunk;
                    }
                    free(silence);
                    audio_read_pos = base_f;
                } else {
                    audio_read_pos = (uint64_t)pos;
                }
            }
            int64_t target = (int64_t)base_f +
                (int64_t)(((int64_t)fts - (int64_t)base_ts)
                          * (int64_t)rate / 1000000000ll)
                + (int64_t)(rate / (uint64_t)fps);
            uint64_t written = __atomic_load_n(
                &hdr->audio_write_frames, __ATOMIC_RELAXED);
            if (target > (int64_t)written) target = (int64_t)written;
            if (target > (int64_t)audio_read_pos) {
                uint64_t want = (uint64_t)target - audio_read_pos;
                /* lapped by the producer? resync to the oldest kept */
                if (want > hdr->audio_capacity_frames) {
                    audio_read_pos = written
                        - hdr->audio_capacity_frames;
                    want = (uint64_t)target - audio_read_pos;
                }
                uint8_t *aring = (uint8_t *)map + hdr->audio_offset;
                const uint64_t capb =
                    hdr->audio_capacity_frames * hdr->audio_frame_bytes;
                while (want) {
                    uint64_t pos = audio_read_pos
                        % hdr->audio_capacity_frames;
                    uint64_t chunk = hdr->audio_capacity_frames - pos;
                    if (chunk > want) chunk = want;
                    if (write_all(ff_audio_fd,
                                  aring + pos * hdr->audio_frame_bytes,
                                  (size_t)chunk
                                      * hdr->audio_frame_bytes) != 0) {
                        failed = 4;
                        break;
                    }
                    audio_read_pos += chunk;
                    audio_frames_written += chunk;
                    want -= chunk;
                    (void)capb;
                }
                if (failed) break;
            }
        }
    }

    /* ---- finalize ------------------------------------------------- */
    if (s_stop) {
        fprintf(stderr, "recomp-recorder: ending on signal, "
                        "finalizing\n");
        /* user stop: hold the last frame up to now so the video's
         * duration covers the full session, not just up to the
         * last presented frame — capped at ~2 s so a slow encoder
         * can't stretch the finalize into a hang */
        if (ff_pid > 0 && have_last && stream_t0) {
            uint64_t target = (now_ns() - stream_t0)
                * (uint64_t)fps / 1000000000ull + 1;
            if (target > frames_written + (uint64_t)fps * 2)
                target = frames_written + (uint64_t)fps * 2;
            while (frames_written < target) {
                if (write_all(ff_video_fd, out_buf,
                              (size_t)out_w * out_h * 4) != 0)
                    break;
                frames_written++;
                frames_repeated++;
            }
        }
    }
    if (ff_video_fd >= 0) close(ff_video_fd);
    if (ff_audio_fd >= 0) close(ff_audio_fd);
    int ff_status = 0;
    if (ff_pid > 0) {
        /* EOF on its inputs makes ffmpeg finalize and exit; give it
         * time, then ask politely (SIGINT finalizes too), and only
         * as a last resort kill it (a killed ffmpeg loses the MP4). */
        int st = 0;
        pid_t r = 0;
        uint64_t wait_end = now_ns() + 20000 * 1000 * 1000ull;
        while ((r = waitpid(ff_pid, &st, WNOHANG)) == 0
               && now_ns() < wait_end)
            usleep(100 * 1000);
        if (r == 0) {
            kill(ff_pid, SIGINT);
            wait_end = now_ns() + 5000 * 1000 * 1000ull;
            while ((r = waitpid(ff_pid, &st, WNOHANG)) == 0
                   && now_ns() < wait_end)
                usleep(100 * 1000);
        }
        if (r == 0) {
            kill(ff_pid, SIGKILL);
            waitpid(ff_pid, &st, 0);
            ff_status = -1;
        } else {
            ff_status = WIFEXITED(st) ? WEXITSTATUS(st) : -1;
        }
    }
    const uint64_t drops = __atomic_load_n(&hdr->dropped_frames,
                                           __ATOMIC_RELAXED);
    printf("recomp-recorder: encoder=%s frames=%llu repeated=%llu "
           "producer_dropped=%llu audio_frames=%llu duration=%.1fs\n",
           enc, (unsigned long long)frames_written,
           (unsigned long long)frames_repeated,
           (unsigned long long)drops,
           (unsigned long long)audio_frames_written,
           fps ? (double)frames_written / fps : 0.0);
    if (failed) return failed;
    if (ff_pid > 0 && ff_status != 0) {
        fprintf(stderr, "recomp-recorder: ffmpeg exited %d\n", ff_status);
        return 4;
    }
    return frames_written > 0 ? 0 : 2;
}
