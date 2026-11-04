/* recomp_capture_shm.h — shared-memory contract between the capture
 * producer (librecompcapture.so inside the game, or a future in-runtime
 * tap) and the consumer (recomp-recorder).
 *
 * Layout: one header page, then the video slot array, then the audio
 * ring. All cross-process counters are plain integers accessed ONLY
 * through __atomic builtins (lock-free on Linux x86_64/aarch64), so
 * the producer and consumer can be built by different compilers.
 *
 * Video slots cycle: the producer publishes finished frames with a
 * monotonically increasing sequence number; it NEVER blocks — if its
 * target slot is being read, the frame is dropped and counted.
 *
 * Pixel format 1 = RGBA8, rows top-down (the producer flips GL's
 * bottom-up readback), stride = width * 4, no row padding.
 *
 * Audio (optional; the LD_PRELOAD library build leaves it disabled —
 * see librecompcapture.c): S16 interleaved ring addressed in sample
 * frames. audio_base_frames/audio_base_ts_ns anchor the ring position
 * to the same CLOCK_MONOTONIC clock as the video timestamps.
 */
#ifndef RECOMP_CAPTURE_SHM_H
#define RECOMP_CAPTURE_SHM_H

#include <stdint.h>

#define RECOMP_CAPTURE_MAGIC 0x3150414352454352ULL /* "RECAP" + v1 */
#define RECOMP_CAPTURE_VERSION 1u
#define RECOMP_CAPTURE_FMT_RGBA8_TOPDOWN 1u

#define RCAP_SLOT_FREE 0u
#define RCAP_SLOT_WRITING 1u
#define RCAP_SLOT_READY 2u
#define RCAP_SLOT_READING 3u

typedef struct {
    uint64_t magic;              /* RECOMP_CAPTURE_MAGIC, stored last */
    uint32_t version;            /* RECOMP_CAPTURE_VERSION */
    uint32_t header_bytes;       /* sizeof(recomp_capture_header) */
    uint64_t total_bytes;        /* whole mapping */
    uint32_t state;              /* 1 = streaming */
    uint32_t fps;                /* capture rate the producer targets */
    uint32_t format;             /* RECOMP_CAPTURE_FMT_* */
    uint32_t width;              /* current frame size (may change) */
    uint32_t height;
    uint32_t video_slots;        /* slot count */
    uint32_t video_slot_bytes;   /* payload bytes per slot */
    uint64_t video_offset;       /* byte offset of slot array */
    uint64_t published_seq;      /* atomic: last published frame seq */
    uint64_t dropped_frames;     /* atomic: producer-side drops */
    uint64_t producer_heartbeat_ns; /* atomic: CLOCK_MONOTONIC ns */
    uint64_t consumer_heartbeat_ns; /* atomic: CLOCK_MONOTONIC ns */
    /* audio ring (all zero when the producer has no audio tap) */
    uint32_t audio_enabled;
    uint32_t audio_rate;         /* e.g. 44100 */
    uint32_t audio_channels;     /* e.g. 2 */
    uint32_t audio_frame_bytes;  /* channels * 2 (S16) */
    uint64_t audio_offset;       /* byte offset of the ring */
    uint64_t audio_capacity_frames;
    uint64_t audio_write_frames; /* atomic: total frames ever written */
    uint64_t audio_base_frames;  /* write position at base_ts */
    uint64_t audio_base_ts_ns;   /* CLOCK_MONOTONIC ns anchor */
    uint8_t reserved[256];
} recomp_capture_header;

typedef struct {
    uint32_t state;              /* atomic: RCAP_SLOT_* */
    uint32_t bytes;              /* payload bytes (w * h * 4) */
    uint64_t seq;
    uint64_t ts_ns;              /* CLOCK_MONOTONIC at capture */
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    uint32_t reserved;
    uint8_t pad[64 - 40];
} recomp_capture_slot;           /* 64 bytes, payload follows */

#endif /* RECOMP_CAPTURE_SHM_H */
