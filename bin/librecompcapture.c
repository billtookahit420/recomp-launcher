/* librecompcapture.so — Steam-style game capture for the Recomp
 * Launcher. The launcher preloads this library ONLY for a launch the
 * user asked to record, with RECOMP_CAPTURE=1 in the environment.
 *
 * It taps the game's own frames at the present boundary — the same
 * position Steam's recorder occupies — so the recorded frames are the
 * game's finished frames, taken before the display ever sees them:
 * tearing (a compositor/display artefact) cannot exist in them.
 *
 * How the tap binds, given that most recomp builds link SDL
 * statically and SDL fetches GLX entry points through
 * glXGetProcAddress (which returns GLVND's own stubs, bypassing both
 * plain symbol interposition and a bare dlsym hook):
 *
 *   dlsym            — redirected for the swap + proc-address names
 *   glXGetProcAddress(ARB) / eglGetProcAddress
 *                    — wrapped; swap names return our hooks
 *   glXSwapBuffers / eglSwapBuffers / SDL_GL_SwapWindow
 *                    — the hooks themselves (the SDL one covers
 *                      dynamically-linked SDL builds)
 *
 * Frames are published into a POSIX shared-memory ring
 * (recomp_capture_shm.h) that recomp-recorder drains into ffmpeg.
 * The game NEVER blocks on the ring: a busy slot means the frame is
 * dropped and counted. With RECOMP_CAPTURE unset or != 1 every hook
 * is a pure pass-through.
 *
 * Audio: not tapped by this build. The recomp packages link SDL
 * statically and SDL's audio backends resolve their symbols
 * handle-scoped, so there is no clean post-mix point to hook from
 * outside the process. Game-capture recordings are video-only;
 * the launcher's screen-capture path keeps desktop audio.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "recomp_capture_shm.h"

/* ------------------------------------------------------------------ */
/* small helpers                                                       */

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void cap_log(const char *fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    fprintf(stderr, "[recomp-capture] %s\n", buf);
    fflush(stderr);
}

static void *(*real_dlsym)(void *, const char *) = 0;
static void *resolve_real_dlsym(void) {
    void *f = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");
    if (!f) f = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");
    return f;
}
static void *lookup_next(const char *name) {
    if (!real_dlsym) real_dlsym = resolve_real_dlsym();
    return real_dlsym ? real_dlsym(RTLD_NEXT, name) : 0;
}
static void *lookup_default(const char *name) {
    if (!real_dlsym) real_dlsym = resolve_real_dlsym();
    return real_dlsym ? real_dlsym(RTLD_DEFAULT, name) : 0;
}

/* ------------------------------------------------------------------ */
/* producer state                                                      */

#define CAP_MAX_W 2560
#define CAP_MAX_H 1440
#define CAP_SLOTS 4
#define CAP_AUDIO_SECONDS 4

static int s_active = 0;              /* env gate passed + shm live */
static pthread_once_t s_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t s_lock = PTHREAD_MUTEX_INITIALIZER;
static recomp_capture_header *s_hdr = 0;
static size_t s_map_bytes = 0;
static int s_shm_fd = -1;
static char s_shm_name[128] = "";
static int s_fps = 60;
static uint64_t s_next_due_ns = 0;
static uint64_t s_seq = 0;
static uint8_t *s_rowbuf = 0;         /* one row, for the flip */
static size_t s_rowbuf_bytes = 0;
static uint64_t s_oversize_warned = 0;

typedef void (*gl_read_pixels_f)(int, int, int, int, unsigned, unsigned, void *);
static gl_read_pixels_f s_glReadPixels = 0;

static uint8_t *slot_payload(recomp_capture_slot *slot) {
    return (uint8_t *)slot + sizeof(recomp_capture_slot);
}

static void cap_init(void) {
    const char *on = getenv("RECOMP_CAPTURE");
    if (!on || strcmp(on, "1") != 0) return;      /* pure pass-through */
    const char *name = getenv("RECOMP_CAPTURE_SHM");
    if (!name || !name[0]) {
        cap_log("RECOMP_CAPTURE=1 but RECOMP_CAPTURE_SHM is unset; "
                "capture disabled");
        return;
    }
    const char *fps_env = getenv("RECOMP_CAPTURE_FPS");
    if (fps_env) {
        s_fps = atoi(fps_env);
        if (s_fps < 1) s_fps = 1;
        if (s_fps > 240) s_fps = 240;
    }
    snprintf(s_shm_name, sizeof s_shm_name, "/%s",
             name[0] == '/' ? name + 1 : name);

    const uint32_t slot_bytes = CAP_MAX_W * CAP_MAX_H * 4u;
    const uint64_t video_off = 4096;
    const uint64_t video_bytes =
        (uint64_t)CAP_SLOTS * (sizeof(recomp_capture_slot) + slot_bytes);
    const uint64_t audio_frames = 44100ull * CAP_AUDIO_SECONDS;
    const uint64_t audio_off = video_off + video_bytes;
    const uint64_t total = audio_off + audio_frames * 4ull;

    int fd = shm_open(s_shm_name, O_RDWR | O_CREAT | O_EXCL, 0600);
    if (fd < 0) {                                  /* stale: reclaim */
        shm_unlink(s_shm_name);
        fd = shm_open(s_shm_name, O_RDWR | O_CREAT | O_EXCL, 0600);
    }
    if (fd < 0) {
        cap_log("shm_open(%s) failed; capture disabled", s_shm_name);
        return;
    }
    if (ftruncate(fd, (off_t)total) != 0) {
        cap_log("ftruncate(%s) failed; capture disabled", s_shm_name);
        close(fd);
        shm_unlink(s_shm_name);
        return;
    }
    void *map = mmap(0, total, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED) {
        cap_log("mmap(%s) failed; capture disabled", s_shm_name);
        close(fd);
        shm_unlink(s_shm_name);
        return;
    }
    memset(map, 0, 4096);
    s_hdr = (recomp_capture_header *)map;
    s_hdr->version = RECOMP_CAPTURE_VERSION;
    s_hdr->header_bytes = sizeof(recomp_capture_header);
    s_hdr->total_bytes = total;
    s_hdr->fps = (uint32_t)s_fps;
    s_hdr->format = RECOMP_CAPTURE_FMT_RGBA8_TOPDOWN;
    s_hdr->video_slots = CAP_SLOTS;
    s_hdr->video_slot_bytes = slot_bytes;
    s_hdr->video_offset = video_off;
    s_hdr->audio_enabled = 0;          /* video-only build: see header */
    s_hdr->audio_offset = audio_off;
    s_hdr->audio_capacity_frames = audio_frames;
    s_hdr->state = 1;
    __atomic_store_n(&s_hdr->producer_heartbeat_ns, now_ns(),
                     __ATOMIC_RELEASE);
    /* magic last: the consumer treats it as "header complete" */
    __atomic_store_n(&s_hdr->magic, RECOMP_CAPTURE_MAGIC,
                     __ATOMIC_RELEASE);
    s_shm_fd = fd;
    s_map_bytes = (size_t)total;
    s_active = 1;
    cap_log("capture live: shm %s, %d fps, slots %dx<=%dx%d",
            s_shm_name, s_fps, CAP_SLOTS, CAP_MAX_W, CAP_MAX_H);
}

static void cap_shutdown(void) {
    if (s_hdr) {
        uint64_t drops = __atomic_load_n(&s_hdr->dropped_frames,
                                         __ATOMIC_RELAXED);
        cap_log("capture end: %llu frames published, %llu dropped",
                (unsigned long long)s_seq,
                (unsigned long long)drops);
        s_hdr->state = 0;
        munmap((void *)s_hdr, s_map_bytes);
        s_hdr = 0;
    }
    if (s_shm_fd >= 0) {
        close(s_shm_fd);
        s_shm_fd = -1;
    }
    if (s_shm_name[0]) shm_unlink(s_shm_name);
}
__attribute__((destructor)) static void cap_destructor(void) {
    if (s_active) cap_shutdown();
}

/* Publish one frame. w/h are the drawable size; the default
 * framebuffer must still hold the composed frame (pre-swap). */
static void capture_frame(int w, int h) {
    pthread_once(&s_once, cap_init);
    if (!s_active) return;
    const uint64_t now = now_ns();
    __atomic_store_n(&s_hdr->producer_heartbeat_ns, now,
                     __ATOMIC_RELAXED);
    if (s_next_due_ns && now < s_next_due_ns) return;   /* fps gate */
    s_next_due_ns = now + 1000000000ull / (uint64_t)s_fps;
    if (w <= 0 || h <= 0) return;
    const uint32_t bytes = (uint32_t)w * (uint32_t)h * 4u;
    if (bytes > s_hdr->video_slot_bytes) {
        __atomic_fetch_add(&s_hdr->dropped_frames, 1, __ATOMIC_RELAXED);
        if (!s_oversize_warned) {
            s_oversize_warned = 1;
            cap_log("frame %dx%d exceeds the %dx%d capture cap; "
                    "dropping oversize frames", w, h, CAP_MAX_W, CAP_MAX_H);
        }
        return;
    }
    if (!s_glReadPixels)
        s_glReadPixels = (gl_read_pixels_f)lookup_default("glReadPixels");
    if (!s_glReadPixels) return;

    pthread_mutex_lock(&s_lock);
    recomp_capture_slot *base = (recomp_capture_slot *)
        ((uint8_t *)s_hdr + s_hdr->video_offset);
    const uint64_t stride_bytes =
        sizeof(recomp_capture_slot) + s_hdr->video_slot_bytes;
    recomp_capture_slot *slot = 0;
    for (uint32_t i = 0; i < s_hdr->video_slots; i++) {
        recomp_capture_slot *cand = (recomp_capture_slot *)
            ((uint8_t *)base + (s_seq + i) % s_hdr->video_slots
             * stride_bytes);
        uint32_t st = __atomic_load_n(&cand->state, __ATOMIC_ACQUIRE);
        if (st != RCAP_SLOT_READING && st != RCAP_SLOT_WRITING) {
            slot = cand;
            break;
        }
    }
    if (!slot) {                       /* ring busy: drop, never block */
        __atomic_fetch_add(&s_hdr->dropped_frames, 1, __ATOMIC_RELAXED);
        pthread_mutex_unlock(&s_lock);
        return;
    }
    __atomic_store_n(&slot->state, RCAP_SLOT_WRITING, __ATOMIC_RELAXED);
    uint8_t *px = slot_payload(slot);
    /* GL reads bottom-up; read into the slot, then flip rows so the
     * ring contract is top-down. */
    s_glReadPixels(0, 0, w, h, 0x1908 /* GL_RGBA */,
                   0x1401 /* GL_UNSIGNED_BYTE */, px);
    const size_t row = (size_t)w * 4;
    if (s_rowbuf_bytes < row) {
        free(s_rowbuf);
        s_rowbuf = (uint8_t *)malloc(row);
        s_rowbuf_bytes = s_rowbuf ? row : 0;
    }
    if (s_rowbuf) {
        for (int y = 0; y < h / 2; y++) {
            uint8_t *a = px + (size_t)y * row;
            uint8_t *b = px + (size_t)(h - 1 - y) * row;
            memcpy(s_rowbuf, a, row);
            memcpy(a, b, row);
            memcpy(b, s_rowbuf, row);
        }
    }
    slot->bytes = bytes;
    slot->width = (uint32_t)w;
    slot->height = (uint32_t)h;
    slot->stride = (uint32_t)row;
    slot->ts_ns = now_ns();
    slot->seq = ++s_seq;
    s_hdr->width = (uint32_t)w;
    s_hdr->height = (uint32_t)h;
    __atomic_store_n(&s_hdr->published_seq, s_seq, __ATOMIC_RELEASE);
    __atomic_store_n(&slot->state, RCAP_SLOT_READY, __ATOMIC_RELEASE);
    pthread_mutex_unlock(&s_lock);
}

/* ------------------------------------------------------------------ */
/* size queries (best source first, viewport as last resort)           */

typedef void (*glx_query_drawable_f)(void *, unsigned long, int, unsigned *);
typedef unsigned (*egl_query_surface_f)(void *, void *, int, int *);
typedef void (*gl_get_integerv_f)(unsigned, int *);

static void viewport_size(int *w, int *h) {
    static gl_get_integerv_f glGetIntegerv_fn = 0;
    if (!glGetIntegerv_fn)
        glGetIntegerv_fn =
            (gl_get_integerv_f)lookup_default("glGetIntegerv");
    int vp[4] = {0, 0, 0, 0};
    if (glGetIntegerv_fn) glGetIntegerv_fn(0x0BA2 /* GL_VIEWPORT */, vp);
    *w = vp[2];
    *h = vp[3];
}

static void glx_drawable_size(void *dpy, unsigned long drawable,
                              int *w, int *h) {
    static glx_query_drawable_f q = 0;
    if (!q) q = (glx_query_drawable_f)lookup_next("glXQueryDrawable");
    unsigned int uw = 0, uh = 0;
    if (q && dpy) {
        q(dpy, drawable, 0x801D /* GLX_WIDTH */, &uw);
        q(dpy, drawable, 0x801E /* GLX_HEIGHT */, &uh);
    }
    if (uw && uh) { *w = (int)uw; *h = (int)uh; }
    else viewport_size(w, h);
}

static void egl_surface_size(void *dpy, void *surf, int *w, int *h) {
    static egl_query_surface_f q = 0;
    if (!q) q = (egl_query_surface_f)lookup_next("eglQuerySurface");
    int iw = 0, ih = 0;
    if (q && dpy) {
        q(dpy, surf, 0x3057 /* EGL_WIDTH */, &iw);
        q(dpy, surf, 0x3056 /* EGL_HEIGHT */, &ih);
    }
    if (iw && ih) { *w = iw; *h = ih; }
    else viewport_size(w, h);
}

typedef void (*sdl_drawable_size_f)(void *, int *, int *);
static void sdl_window_size(void *win, int *w, int *h) {
    static sdl_drawable_size_f q = 0;
    if (!q) q = (sdl_drawable_size_f)lookup_next("SDL_GL_GetDrawableSize");
    if (!q) q = (sdl_drawable_size_f)lookup_next("SDL_GetWindowSizeInPixels");
    if (q && win) q(win, w, h);
    if (!*w || !*h) viewport_size(w, h);
}

/* ------------------------------------------------------------------ */
/* swap hooks                                                          */

/* Raised while our SDL_GL_SwapWindow hook is inside the real swap:
 * the GLX/EGL hook it chains into must not capture a second time. */
static _Thread_local int s_in_sdl_swap = 0;

typedef void (*glx_swap_f)(void *, unsigned long);
static glx_swap_f real_glx_swap = 0;
void glXSwapBuffers(void *dpy, unsigned long drawable) {
    if (!real_glx_swap)
        real_glx_swap = (glx_swap_f)lookup_next("glXSwapBuffers");
    if (!s_in_sdl_swap) {
        int w = 0, h = 0;
        glx_drawable_size(dpy, drawable, &w, &h);
        capture_frame(w, h);
    }
    if (real_glx_swap) real_glx_swap(dpy, drawable);
}

typedef unsigned int (*egl_swap_f)(void *, void *);
static egl_swap_f real_egl_swap = 0;
unsigned int eglSwapBuffers(void *dpy, void *surf) {
    if (!real_egl_swap)
        real_egl_swap = (egl_swap_f)lookup_next("eglSwapBuffers");
    if (!s_in_sdl_swap) {
        int w = 0, h = 0;
        egl_surface_size(dpy, surf, &w, &h);
        capture_frame(w, h);
    }
    return real_egl_swap ? real_egl_swap(dpy, surf) : 0;
}

typedef int (*sdl_swap_f)(void *);
static sdl_swap_f real_sdl_swap = 0;
int SDL_GL_SwapWindow(void *win) {
    if (!real_sdl_swap)
        real_sdl_swap = (sdl_swap_f)lookup_next("SDL_GL_SwapWindow");
    int w = 0, h = 0;
    sdl_window_size(win, &w, &h);
    capture_frame(w, h);
    s_in_sdl_swap = 1;
    int r = real_sdl_swap ? real_sdl_swap(win) : 0;
    s_in_sdl_swap = 0;
    return r;
}

/* ------------------------------------------------------------------ */
/* proc-address wrappers: hand out the hooks for the swap names        */

typedef void *(*getproc_f)(const unsigned char *);
static getproc_f real_glx_getproc = 0;
void *glXGetProcAddress(const unsigned char *name) {
    if (!real_glx_getproc)
        real_glx_getproc = (getproc_f)lookup_next("glXGetProcAddress");
    if (name && strcmp((const char *)name, "glXSwapBuffers") == 0)
        return (void *)glXSwapBuffers;
    return real_glx_getproc ? real_glx_getproc(name) : 0;
}
void *glXGetProcAddressARB(const unsigned char *name) {
    return glXGetProcAddress(name);
}

static getproc_f real_egl_getproc = 0;
void *eglGetProcAddress(const char *name) {
    if (!real_egl_getproc)
        real_egl_getproc = (getproc_f)lookup_next("eglGetProcAddress");
    if (name && strcmp(name, "eglSwapBuffers") == 0)
        return (void *)eglSwapBuffers;
    return real_egl_getproc
        ? real_egl_getproc((const unsigned char *)name) : 0;
}

/* ------------------------------------------------------------------ */
/* dlsym interposition: route the lookups above to this library        */

void *dlsym(void *handle, const char *symbol) {
    if (!real_dlsym) real_dlsym = resolve_real_dlsym();
    if (symbol) {
        if (strcmp(symbol, "glXSwapBuffers") == 0)
            return (void *)glXSwapBuffers;
        if (strcmp(symbol, "eglSwapBuffers") == 0)
            return (void *)eglSwapBuffers;
        if (strcmp(symbol, "glXGetProcAddress") == 0 ||
            strcmp(symbol, "glXGetProcAddressARB") == 0)
            return (void *)glXGetProcAddress;
        if (strcmp(symbol, "eglGetProcAddress") == 0)
            return (void *)eglGetProcAddress;
        if (strcmp(symbol, "SDL_GL_SwapWindow") == 0)
            return (void *)SDL_GL_SwapWindow;
    }
    return real_dlsym(handle, symbol);
}
