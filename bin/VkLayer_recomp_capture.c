/* VkLayer_recomp_capture.c — VK_LAYER_RECOMP_CAPTURE: the Vulkan
 * producer for the Recomp Launcher's Steam-style game capture.
 *
 * librecompcapture.so taps GL presents via LD_PRELOAD, but Vulkan
 * presents cannot be intercepted that way. This implicit layer sits
 * in the Vulkan loader chain and wraps vkQueuePresentKHR — the
 * exact point where a finished frame is handed to the display —
 * copying the presented swapchain image into the same POSIX
 * shared-memory ring (recomp_capture_shm.h) that recomp-recorder
 * drains into ffmpeg. Recorder, SHM protocol and launcher
 * negotiation are identical to the GL route.
 *
 * Activation is doubly gated:
 *   - the manifest's enable_environment only lets the loader insert
 *     the layer when RECOMP_CAPTURE=1 is in the game's environment
 *     (the launcher sets it, plus the layer path, only for a launch
 *     the user asked to record), and
 *   - the layer itself re-checks the env and stays a pure
 *     pass-through chain when the gate is closed.
 * RECOMP_CAPTURE_NO_VULKAN=1 disables the layer even when the
 * loader inserted it (manifest disable_environment + check here).
 *
 * The game NEVER blocks on capture: frames are copied through a
 * small ring of host-visible staging buffers submitted on the
 * present queue itself, harvested on a later present once their
 * fence has signaled; a busy staging ring or a busy SHM slot drops
 * the frame and counts it — the same rule as the GL producer.
 *
 * Mixed GL/Vulkan processes: a recomp package's launcher wizard
 * presents through GL while the game presents through Vulkan, and
 * the GL producer may already own the capture stream. This layer
 * therefore ATTACHES to a valid existing stream instead of
 * reclaiming it, and only unlinks a stream it created itself.
 *
 * Supported swapchain formats: the 32-bit RGBA/BGRA family
 * (R8G8B8A8/B8G8R8A8, UNORM and SRGB — bytes are copied raw, so the
 * encoding the game presented is preserved exactly; BGRA is
 * swizzled to the ring's RGBA8). Anything else (10-bit, block-
 * compressed, ...) is a clean no-capture: the swapchain is marked
 * unsupported, the game presents untouched, and the launcher's
 * negotiation reports the recording unavailable.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

#include "recomp_capture_shm.h"

/* ------------------------------------------------------------------ */
/* helpers                                                             */

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void vk_log(const char *fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    fprintf(stderr, "[recomp-vk-layer] %s\n", buf);
    fflush(stderr);
}

static int capture_gate_open(void) {
    const char *on = getenv("RECOMP_CAPTURE");
    if (!on || strcmp(on, "1") != 0) return 0;
    const char *kill = getenv("RECOMP_CAPTURE_NO_VULKAN");
    if (kill && strcmp(kill, "1") == 0) return 0;
    return 1;
}

/* ------------------------------------------------------------------ */
/* SHM producer (mirrors librecompcapture.c semantics)                 */

#define CAP_MAX_W 2560
#define CAP_MAX_H 1440
#define CAP_SLOTS 4
#define CAP_AUDIO_SECONDS 4

static int s_active = 0;
static int s_created = 0;            /* we created (and may unlink) */
static pthread_once_t s_once = PTHREAD_ONCE_INIT;
static recomp_capture_header *s_hdr = 0;
static size_t s_map_bytes = 0;
static int s_shm_fd = -1;
static char s_shm_name[128] = "";
static int s_fps = 60;
static uint64_t s_seq_local = 0;

static void shm_init(void) {
    if (!capture_gate_open()) return;
    const char *name = getenv("RECOMP_CAPTURE_SHM");
    if (!name || !name[0]) {
        vk_log("RECOMP_CAPTURE=1 but RECOMP_CAPTURE_SHM is unset; "
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

    /* Attach first if a valid stream already exists (the GL
     * producer of the same process may own it). */
    int fd = shm_open(s_shm_name, O_RDWR, 0600);
    if (fd >= 0) {
        int attached = 0;
        struct stat st;
        if (fstat(fd, &st) == 0 && st.st_size >= 4096) {
            void *probe = mmap(0, 4096, PROT_READ, MAP_SHARED, fd, 0);
            if (probe != MAP_FAILED) {
                recomp_capture_header *h =
                    (recomp_capture_header *)probe;
                uint64_t stream_total = 0;
                if (h->magic == RECOMP_CAPTURE_MAGIC &&
                    h->version == RECOMP_CAPTURE_VERSION &&
                    h->total_bytes >= total &&
                    h->total_bytes <= (uint64_t)st.st_size)
                    stream_total = h->total_bytes;
                munmap(probe, 4096);
                if (stream_total) {
                    void *map = mmap(0, (size_t)stream_total,
                                     PROT_READ | PROT_WRITE,
                                     MAP_SHARED, fd, 0);
                    if (map != MAP_FAILED) {
                        s_hdr = (recomp_capture_header *)map;
                        s_map_bytes = (size_t)stream_total;
                        s_shm_fd = fd;
                        s_created = 0;
                        s_active = 1;
                        __atomic_store_n(
                            &s_hdr->producer_heartbeat_ns, now_ns(),
                            __ATOMIC_RELEASE);
                        s_hdr->state = 1;
                        vk_log("attached to existing capture stream "
                               "%s (Vulkan producer joining)",
                               s_shm_name);
                        attached = 1;
                    }
                }
            }
        }
        if (attached) return;
        close(fd);
        fd = -1;
        /* exists but not a valid stream: stale, reclaim below */
        shm_unlink(s_shm_name);
    }
    fd = shm_open(s_shm_name, O_RDWR | O_CREAT | O_EXCL, 0600);
    if (fd < 0) {
        vk_log("shm_open(%s) failed; capture disabled", s_shm_name);
        return;
    }
    if (ftruncate(fd, (off_t)total) != 0) {
        vk_log("ftruncate(%s) failed; capture disabled", s_shm_name);
        close(fd);
        shm_unlink(s_shm_name);
        return;
    }
    void *map = mmap(0, total, PROT_READ | PROT_WRITE, MAP_SHARED,
                     fd, 0);
    if (map == MAP_FAILED) {
        vk_log("mmap(%s) failed; capture disabled", s_shm_name);
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
    s_hdr->audio_enabled = 0;
    s_hdr->audio_offset = audio_off;
    s_hdr->audio_capacity_frames = audio_frames;
    s_hdr->state = 1;
    __atomic_store_n(&s_hdr->producer_heartbeat_ns, now_ns(),
                     __ATOMIC_RELEASE);
    __atomic_store_n(&s_hdr->magic, RECOMP_CAPTURE_MAGIC,
                     __ATOMIC_RELEASE);
    s_shm_fd = fd;
    s_map_bytes = (size_t)total;
    s_created = 1;
    s_active = 1;
    vk_log("capture live: shm %s, %d fps (Vulkan producer)",
           s_shm_name, s_fps);
}

static void shm_shutdown(void) {
    if (s_hdr) {
        if (s_created) s_hdr->state = 0;
        munmap((void *)s_hdr, s_map_bytes);
        s_hdr = 0;
    }
    if (s_shm_fd >= 0) {
        close(s_shm_fd);
        s_shm_fd = -1;
    }
    if (s_created && s_shm_name[0]) shm_unlink(s_shm_name);
    s_active = 0;
}
__attribute__((destructor)) static void layer_destructor(void) {
    if (s_active) shm_shutdown();
}

static void shm_drop(void) {
    if (s_hdr)
        __atomic_fetch_add(&s_hdr->dropped_frames, 1, __ATOMIC_RELAXED);
}

/* Publish one harvested frame (host-memory RGBA-ordered bytes;
 * swizzle=1 when the source was BGRA-ordered). */
static void shm_publish(const uint8_t *src, int w, int h, int swizzle,
                        uint64_t ts_ns) {
    if (!s_active || !s_hdr) return;
    const uint32_t bytes = (uint32_t)w * (uint32_t)h * 4u;
    if (bytes > s_hdr->video_slot_bytes) {
        shm_drop();
        return;
    }
    uint64_t base_seq = __atomic_load_n(&s_hdr->published_seq,
                                        __ATOMIC_RELAXED);
    if (base_seq > s_seq_local) s_seq_local = base_seq;
    recomp_capture_slot *base = (recomp_capture_slot *)
        ((uint8_t *)s_hdr + s_hdr->video_offset);
    const uint64_t stride_bytes =
        sizeof(recomp_capture_slot) + s_hdr->video_slot_bytes;
    recomp_capture_slot *slot = 0;
    for (uint32_t i = 0; i < s_hdr->video_slots; i++) {
        recomp_capture_slot *cand = (recomp_capture_slot *)
            ((uint8_t *)base + (s_seq_local + i) % s_hdr->video_slots
             * stride_bytes);
        uint32_t st = __atomic_load_n(&cand->state, __ATOMIC_ACQUIRE);
        if (st != RCAP_SLOT_READING && st != RCAP_SLOT_WRITING) {
            slot = cand;
            break;
        }
    }
    if (!slot) {                       /* ring busy: drop, never block */
        shm_drop();
        return;
    }
    __atomic_store_n(&slot->state, RCAP_SLOT_WRITING, __ATOMIC_RELAXED);
    uint8_t *px = (uint8_t *)slot + sizeof(recomp_capture_slot);
    if (!swizzle) {
        memcpy(px, src, bytes);
    } else {
        const uint32_t *s32 = (const uint32_t *)src;
        uint32_t *d32 = (uint32_t *)px;
        const uint32_t n = bytes / 4u;
        for (uint32_t i = 0; i < n; i++) {
            uint32_t v = s32[i];       /* B G R A (LE) -> R G B A */
            d32[i] = (v & 0xFF00FF00u) | ((v & 0x00FF0000u) >> 16) |
                     ((v & 0x000000FFu) << 16);
        }
    }
    slot->bytes = bytes;
    slot->width = (uint32_t)w;
    slot->height = (uint32_t)h;
    slot->stride = (uint32_t)w * 4u;
    slot->ts_ns = ts_ns;
    slot->seq = ++s_seq_local;
    s_hdr->width = (uint32_t)w;
    s_hdr->height = (uint32_t)h;
    __atomic_store_n(&s_hdr->published_seq, s_seq_local,
                     __ATOMIC_RELEASE);
    __atomic_store_n(&slot->state, RCAP_SLOT_READY, __ATOMIC_RELEASE);
}

/* ------------------------------------------------------------------ */
/* layer bookkeeping                                                   */

#define MAX_INSTANCES 4
#define MAX_DEVICES 8
#define MAX_SWAPCHAINS 16
#define MAX_QUEUE_FAMILIES 64
#define STAGE_SLOTS 4
#define MAX_SWAPCHAIN_IMAGES 16

typedef struct {
    void *key;
    VkInstance instance;
    PFN_vkGetInstanceProcAddr gipa;
    PFN_vkGetDeviceProcAddr gdpa;
} instance_data;

typedef struct {
    VkBuffer buffer;
    VkDeviceMemory memory;
    void *mapped;
    VkCommandBuffer cmd;
    VkFence fence;
    int pending;                     /* copy submitted, not harvested */
    int w, h, swizzle;
    uint64_t ts_ns;
} stage_slot;

typedef struct device_data_s {
    void *key;
    VkDevice device;
    VkPhysicalDevice phys_dev;
    instance_data *inst;
    PFN_vkGetDeviceProcAddr gdpa;
    PFN_vkCreateSwapchainKHR CreateSwapchainKHR;
    PFN_vkDestroySwapchainKHR DestroySwapchainKHR;
    PFN_vkGetSwapchainImagesKHR GetSwapchainImagesKHR;
    PFN_vkQueuePresentKHR QueuePresentKHR;
    PFN_vkCreateCommandPool CreateCommandPool;
    PFN_vkDestroyCommandPool DestroyCommandPool;
    PFN_vkAllocateCommandBuffers AllocateCommandBuffers;
    PFN_vkBeginCommandBuffer BeginCommandBuffer;
    PFN_vkEndCommandBuffer EndCommandBuffer;
    PFN_vkResetCommandBuffer ResetCommandBuffer;
    PFN_vkCmdPipelineBarrier CmdPipelineBarrier;
    PFN_vkCmdCopyImageToBuffer CmdCopyImageToBuffer;
    PFN_vkQueueSubmit QueueSubmit;
    PFN_vkGetFenceStatus GetFenceStatus;
    PFN_vkResetFences ResetFences;
    PFN_vkWaitForFences WaitForFences;
    PFN_vkCreateBuffer CreateBuffer;
    PFN_vkDestroyBuffer DestroyBuffer;
    PFN_vkGetBufferMemoryRequirements GetBufferMemoryRequirements;
    PFN_vkAllocateMemory AllocateMemory;
    PFN_vkFreeMemory FreeMemory;
    PFN_vkBindBufferMemory BindBufferMemory;
    PFN_vkMapMemory MapMemory;
    PFN_vkCreateFence CreateFence;
    PFN_vkDestroyFence DestroyFence;
    PFN_vkDestroyDevice DestroyDevice;
    /* present capture state */
    VkQueue queue;                   /* queue that actually presents */
    uint32_t queue_family;
    int queue_family_known;
    VkCommandPool pool;
    stage_slot stage[STAGE_SLOTS];
    size_t stage_bytes;
    uint64_t next_due_ns;
    pthread_mutex_t cap_lock;
} device_data;

typedef struct {
    VkSwapchainKHR swapchain;
    device_data *dev;
    VkFormat format;
    VkExtent2D extent;
    VkImage images[MAX_SWAPCHAIN_IMAGES];
    uint32_t image_count;
    int supported;
    int oversize;
} swapchain_data;

typedef struct { VkQueue queue; uint32_t family; } queue_family_rec;

static pthread_mutex_t s_map_lock = PTHREAD_MUTEX_INITIALIZER;
static instance_data s_instances[MAX_INSTANCES];
static device_data s_devices[MAX_DEVICES];
static swapchain_data s_swapchains[MAX_SWAPCHAINS];
static queue_family_rec s_queue_families[MAX_QUEUE_FAMILIES];

static void *dispatch_key(const void *handle) {
    return handle ? *(void *const *)handle : 0;
}

static instance_data *find_instance(const void *handle) {
    void *key = dispatch_key(handle);
    if (!key) return 0;
    for (int i = 0; i < MAX_INSTANCES; i++)
        if (s_instances[i].key == key) return &s_instances[i];
    return 0;
}

static device_data *find_device(const void *handle) {
    void *key = dispatch_key(handle);
    if (!key) return 0;
    for (int i = 0; i < MAX_DEVICES; i++)
        if (s_devices[i].key == key) return &s_devices[i];
    return 0;
}

static swapchain_data *find_swapchain(VkSwapchainKHR sc) {
    for (int i = 0; i < MAX_SWAPCHAINS; i++)
        if (s_swapchains[i].swapchain == sc) return &s_swapchains[i];
    return 0;
}

static int format_supported(VkFormat f, int *swizzle) {
    switch (f) {
    case VK_FORMAT_R8G8B8A8_UNORM:
    case VK_FORMAT_R8G8B8A8_SRGB:
        *swizzle = 0;
        return 1;
    case VK_FORMAT_B8G8R8A8_UNORM:
    case VK_FORMAT_B8G8R8A8_SRGB:
        *swizzle = 1;
        return 1;
    default:
        return 0;
    }
}

/* ------------------------------------------------------------------ */
/* staging ring (per device)                                           */

static void stage_teardown(device_data *d) {
    if (!d || d->device == VK_NULL_HANDLE) return;
    for (int i = 0; i < STAGE_SLOTS; i++) {
        stage_slot *s = &d->stage[i];
        if (s->fence != VK_NULL_HANDLE && d->DestroyFence) {
            d->DestroyFence(d->device, s->fence, 0);
            s->fence = VK_NULL_HANDLE;
        }
        if (s->buffer != VK_NULL_HANDLE && d->DestroyBuffer) {
            d->DestroyBuffer(d->device, s->buffer, 0);
            s->buffer = VK_NULL_HANDLE;
        }
        if (s->memory != VK_NULL_HANDLE && d->FreeMemory) {
            d->FreeMemory(d->device, s->memory, 0);
            s->memory = VK_NULL_HANDLE;
        }
        s->mapped = 0;
        s->pending = 0;
    }
    if (d->pool != VK_NULL_HANDLE && d->DestroyCommandPool) {
        d->DestroyCommandPool(d->device, d->pool, 0);
        d->pool = VK_NULL_HANDLE;
    }
    d->stage_bytes = 0;
}

static int stage_setup(device_data *d, size_t bytes) {
    if (d->stage_bytes >= bytes && d->pool != VK_NULL_HANDLE) return 1;
    stage_teardown(d);
    if (!d->inst) return 0;
    PFN_vkGetPhysicalDeviceMemoryProperties getprops =
        (PFN_vkGetPhysicalDeviceMemoryProperties)
        d->inst->gipa(d->inst->instance,
                      "vkGetPhysicalDeviceMemoryProperties");
    if (!getprops) return 0;
    VkPhysicalDeviceMemoryProperties memprops;
    getprops(d->phys_dev, &memprops);

    VkCommandPoolCreateInfo pci = {
        VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, 0,
        VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,
        d->queue_family};
    if (d->CreateCommandPool(d->device, &pci, 0, &d->pool) != VK_SUCCESS)
        return 0;

    for (int i = 0; i < STAGE_SLOTS; i++) {
        stage_slot *s = &d->stage[i];
        VkBufferCreateInfo bci = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                  0, 0, bytes,
                                  VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                                  VK_SHARING_MODE_EXCLUSIVE, 0, 0};
        if (d->CreateBuffer(d->device, &bci, 0, &s->buffer) != VK_SUCCESS)
            goto fail;
        VkMemoryRequirements req;
        d->GetBufferMemoryRequirements(d->device, s->buffer, &req);
        uint32_t type = UINT32_MAX;
        for (uint32_t t = 0; t < memprops.memoryTypeCount; t++) {
            if (!(req.memoryTypeBits & (1u << t))) continue;
            VkMemoryPropertyFlags want =
                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
            if ((memprops.memoryTypes[t].propertyFlags & want) == want) {
                type = t;
                break;
            }
        }
        if (type == UINT32_MAX) goto fail;
        VkMemoryAllocateInfo mai = {
            VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, 0, req.size, type};
        if (d->AllocateMemory(d->device, &mai, 0, &s->memory)
                != VK_SUCCESS)
            goto fail;
        if (d->BindBufferMemory(d->device, s->buffer, s->memory, 0)
                != VK_SUCCESS)
            goto fail;
        if (d->MapMemory(d->device, s->memory, 0, VK_WHOLE_SIZE, 0,
                         &s->mapped) != VK_SUCCESS)
            goto fail;
        VkCommandBufferAllocateInfo cai = {
            VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, 0, d->pool,
            VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
        if (d->AllocateCommandBuffers(d->device, &cai, &s->cmd)
                != VK_SUCCESS)
            goto fail;
        VkFenceCreateInfo fci = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, 0,
                                 VK_FENCE_CREATE_SIGNALED_BIT};
        if (d->CreateFence(d->device, &fci, 0, &s->fence) != VK_SUCCESS)
            goto fail;
        s->pending = 0;
    }
    d->stage_bytes = bytes;
    return 1;
fail:
    stage_teardown(d);
    return 0;
}

/* Publish any staging slots whose copies have completed. */
static void stage_harvest(device_data *d) {
    for (int i = 0; i < STAGE_SLOTS; i++) {
        stage_slot *s = &d->stage[i];
        if (!s->pending) continue;
        if (d->GetFenceStatus(d->device, s->fence) != VK_SUCCESS)
            continue;                  /* still in flight: try later */
        s->pending = 0;
        shm_publish((const uint8_t *)s->mapped, s->w, s->h, s->swizzle,
                    s->ts_ns);
    }
}

/* Record + submit a copy of `image` (in PRESENT_SRC layout) into a
 * free staging slot on the present queue. 1 = submitted, 0 = the
 * frame was dropped (ring/fence busy or setup failed); the caller
 * counts the drop. Never waits on the GPU. */
static int stage_capture(device_data *d, VkQueue queue, VkImage image,
                         int w, int h, int swizzle, uint64_t ts_ns) {
    const size_t bytes = (size_t)w * (size_t)h * 4u;
    if (!stage_setup(d, bytes)) return 0;
    stage_slot *s = 0;
    for (int i = 0; i < STAGE_SLOTS; i++) {
        if (!d->stage[i].pending) { s = &d->stage[i]; break; }
    }
    if (!s) return 0;
    if (d->GetFenceStatus(d->device, s->fence) != VK_SUCCESS) return 0;
    if (d->ResetFences(d->device, 1, &s->fence) != VK_SUCCESS) return 0;
    if (d->ResetCommandBuffer(s->cmd, 0) != VK_SUCCESS) return 0;
    VkCommandBufferBeginInfo bbi = {
        VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, 0,
        VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, 0};
    if (d->BeginCommandBuffer(s->cmd, &bbi) != VK_SUCCESS) return 0;

    VkImageMemoryBarrier to_transfer = {
        VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, 0,
        VK_ACCESS_MEMORY_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT,
        VK_IMAGE_LAYOUT_PRESENT_SRC_KHR,
        VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        VK_QUEUE_FAMILY_IGNORED, VK_QUEUE_FAMILY_IGNORED, image,
        {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    VkImageMemoryBarrier back = to_transfer;
    back.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    back.dstAccessMask = 0;
    back.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    back.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    d->CmdPipelineBarrier(s->cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
                          VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, 0, 0, 0,
                          1, &to_transfer);
    VkBufferImageCopy region = {0, (uint32_t)w, (uint32_t)h,
                                {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
                                {0, 0, 0},
                                {(uint32_t)w, (uint32_t)h, 1}};
    d->CmdCopyImageToBuffer(s->cmd, image,
                            VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                            s->buffer, 1, &region);
    d->CmdPipelineBarrier(s->cmd, VK_PIPELINE_STAGE_TRANSFER_BIT,
                          VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, 0,
                          0, 0, 1, &back);
    if (d->EndCommandBuffer(s->cmd) != VK_SUCCESS) return 0;
    VkSubmitInfo submit = {VK_STRUCTURE_TYPE_SUBMIT_INFO, 0, 0, 0, 0,
                           1, &s->cmd, 0, 0};
    if (d->QueueSubmit(queue, 1, &submit, s->fence) != VK_SUCCESS)
        return 0;
    s->pending = 1;
    s->w = w;
    s->h = h;
    s->swizzle = swizzle;
    s->ts_ns = ts_ns;
    return 1;
}

/* ------------------------------------------------------------------ */
/* wrapped entry points                                                */

static VkResult VKAPI_CALL layer_CreateSwapchainKHR(
    VkDevice device, const VkSwapchainCreateInfoKHR *info,
    const VkAllocationCallbacks *alloc, VkSwapchainKHR *out) {
    device_data *d = find_device(device);
    if (!d || !d->CreateSwapchainKHR)
        return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = d->CreateSwapchainKHR(device, info, alloc, out);
    if (r == VK_SUCCESS && info) {
        pthread_mutex_lock(&s_map_lock);
        swapchain_data *sd = find_swapchain(*out);
        if (!sd) {
            for (int i = 0; i < MAX_SWAPCHAINS; i++)
                if (s_swapchains[i].swapchain == VK_NULL_HANDLE) {
                    sd = &s_swapchains[i];
                    break;
                }
        }
        if (sd) {
            memset(sd, 0, sizeof *sd);
            sd->swapchain = *out;
            sd->dev = d;
            sd->format = info->imageFormat;
            sd->extent = info->imageExtent;
            int sw = 0;
            sd->supported = format_supported(info->imageFormat, &sw);
            sd->oversize = info->imageExtent.width > CAP_MAX_W ||
                           info->imageExtent.height > CAP_MAX_H;
            if (!sd->supported)
                vk_log("swapchain format %d has no capture path; "
                       "frames will not be captured",
                       (int)info->imageFormat);
            if (sd->oversize)
                vk_log("swapchain %ux%u exceeds the %dx%d capture "
                       "cap; frames will be dropped",
                       info->imageExtent.width, info->imageExtent.height,
                       CAP_MAX_W, CAP_MAX_H);
        }
        pthread_mutex_unlock(&s_map_lock);
    }
    return r;
}

static void VKAPI_CALL layer_DestroySwapchainKHR(
    VkDevice device, VkSwapchainKHR swapchain,
    const VkAllocationCallbacks *alloc) {
    device_data *d = find_device(device);
    pthread_mutex_lock(&s_map_lock);
    swapchain_data *sd = find_swapchain(swapchain);
    if (sd) memset(sd, 0, sizeof *sd);
    pthread_mutex_unlock(&s_map_lock);
    if (d && d->DestroySwapchainKHR)
        d->DestroySwapchainKHR(device, swapchain, alloc);
}

static VkResult VKAPI_CALL layer_GetSwapchainImagesKHR(
    VkDevice device, VkSwapchainKHR swapchain, uint32_t *count,
    VkImage *images) {
    device_data *d = find_device(device);
    if (!d || !d->GetSwapchainImagesKHR)
        return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = d->GetSwapchainImagesKHR(device, swapchain, count,
                                          images);
    if ((r == VK_SUCCESS || r == VK_INCOMPLETE) && images && count) {
        pthread_mutex_lock(&s_map_lock);
        swapchain_data *sd = find_swapchain(swapchain);
        if (sd) {
            uint32_t n = *count;
            if (n > MAX_SWAPCHAIN_IMAGES) n = MAX_SWAPCHAIN_IMAGES;
            memcpy(sd->images, images, n * sizeof(VkImage));
            sd->image_count = n;
        }
        pthread_mutex_unlock(&s_map_lock);
    }
    return r;
}

static void note_present_queue(device_data *d, VkQueue queue) {
    pthread_mutex_lock(&s_map_lock);
    for (int i = 0; i < MAX_QUEUE_FAMILIES; i++) {
        if (s_queue_families[i].queue == queue) {
            uint32_t fam = s_queue_families[i].family;
            if (d->queue_family_known && d->queue_family != fam &&
                d->pool != VK_NULL_HANDLE) {
                /* pool was built for another family: retire the
                 * stage ring so it is rebuilt for this one */
                pthread_mutex_lock(&d->cap_lock);
                stage_teardown(d);
                pthread_mutex_unlock(&d->cap_lock);
            }
            d->queue_family = fam;
            d->queue_family_known = 1;
            d->queue = queue;
            break;
        }
    }
    pthread_mutex_unlock(&s_map_lock);
}

static VkResult VKAPI_CALL layer_QueuePresentKHR(
    VkQueue queue, const VkPresentInfoKHR *info) {
    device_data *d = find_device(queue);
    if (!d || !d->QueuePresentKHR || !info)
        return VK_ERROR_INITIALIZATION_FAILED;
    note_present_queue(d, queue);
    if (s_active && info->swapchainCount > 0) {
        pthread_mutex_lock(&d->cap_lock);
        stage_harvest(d);
        if (s_hdr)
            __atomic_store_n(&s_hdr->producer_heartbeat_ns, now_ns(),
                             __ATOMIC_RELAXED);
        uint64_t now = now_ns();
        if (!d->next_due_ns || now >= d->next_due_ns) {
            d->next_due_ns = now + 1000000000ull / (uint64_t)s_fps;
            for (uint32_t i = 0; i < info->swapchainCount; i++) {
                swapchain_data snap;
                int have = 0;
                pthread_mutex_lock(&s_map_lock);
                swapchain_data *found =
                    find_swapchain(info->pSwapchains[i]);
                if (found) {
                    snap = *found;
                    have = 1;
                }
                pthread_mutex_unlock(&s_map_lock);
                if (!have || !snap.supported || snap.oversize)
                    continue;
                uint32_t idx = info->pImageIndices[i];
                if (idx >= snap.image_count) continue;
                int swizzle = 0;
                format_supported(snap.format, &swizzle);
                if (!stage_capture(d, queue, snap.images[idx],
                                   (int)snap.extent.width,
                                   (int)snap.extent.height, swizzle,
                                   now))
                    shm_drop();
            }
        }
        pthread_mutex_unlock(&d->cap_lock);
    }
    return d->QueuePresentKHR(queue, info);
}

static void VKAPI_CALL layer_GetDeviceQueue(
    VkDevice device, uint32_t family, uint32_t index, VkQueue *out) {
    device_data *d = find_device(device);
    if (!d || !d->gdpa) return;
    PFN_vkGetDeviceQueue real = (PFN_vkGetDeviceQueue)
        d->gdpa(device, "vkGetDeviceQueue");
    if (!real) return;
    real(device, family, index, out);
    if (out && *out) {
        pthread_mutex_lock(&s_map_lock);
        for (int i = 0; i < MAX_QUEUE_FAMILIES; i++) {
            if (s_queue_families[i].queue == *out ||
                s_queue_families[i].queue == VK_NULL_HANDLE) {
                s_queue_families[i].queue = *out;
                s_queue_families[i].family = family;
                break;
            }
        }
        pthread_mutex_unlock(&s_map_lock);
    }
}

/* ------------------------------------------------------------------ */
/* instance / device creation                                          */

static PFN_vkVoidFunction VKAPI_CALL
layer_GetInstanceProcAddr(VkInstance instance, const char *name);
static PFN_vkVoidFunction VKAPI_CALL
layer_GetDeviceProcAddr(VkDevice device, const char *name);

static int s_gate_logged = 0;
static VkResult VKAPI_CALL layer_CreateInstance(
    const VkInstanceCreateInfo *info,
    const VkAllocationCallbacks *alloc, VkInstance *out) {
    const VkLayerInstanceCreateInfo *link = 0;
    const VkBaseInStructure *next =
        (const VkBaseInStructure *)info->pNext;
    while (next) {
        if (next->sType ==
                VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO &&
            ((const VkLayerInstanceCreateInfo *)next)->function ==
                VK_LAYER_LINK_INFO) {
            link = (const VkLayerInstanceCreateInfo *)next;
            break;
        }
        next = next->pNext;
    }
    if (!link || !link->u.pLayerInfo ||
        !link->u.pLayerInfo->pfnNextGetInstanceProcAddr)
        return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr next_gipa =
        link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkCreateInstance next_create =
        (PFN_vkCreateInstance)next_gipa(VK_NULL_HANDLE,
                                        "vkCreateInstance");
    if (!next_create) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = next_create(info, alloc, out);
    if (r != VK_SUCCESS) return r;

    pthread_mutex_lock(&s_map_lock);
    instance_data *slot = 0;
    for (int i = 0; i < MAX_INSTANCES; i++)
        if (!s_instances[i].key) { slot = &s_instances[i]; break; }
    if (slot) {
        memset(slot, 0, sizeof *slot);
        slot->key = dispatch_key(*out);
        slot->instance = *out;
        slot->gipa = next_gipa;
        slot->gdpa = (PFN_vkGetDeviceProcAddr)
            next_gipa(*out, "vkGetDeviceProcAddr");
    }
    pthread_mutex_unlock(&s_map_lock);
    if (!s_gate_logged) {
        s_gate_logged = 1;
        vk_log("layer in chain; capture gate %s",
               capture_gate_open() ? "OPEN (armed)"
                                   : "closed (pass-through)");
    }
    return r;
}

static void VKAPI_CALL layer_DestroyInstance(
    VkInstance instance, const VkAllocationCallbacks *alloc) {
    instance_data *idata = find_instance(instance);
    PFN_vkDestroyInstance real = idata ?
        (PFN_vkDestroyInstance)idata->gipa(instance,
                                           "vkDestroyInstance") : 0;
    pthread_mutex_lock(&s_map_lock);
    if (idata) memset(idata, 0, sizeof *idata);
    pthread_mutex_unlock(&s_map_lock);
    if (real) real(instance, alloc);
}

static VkResult VKAPI_CALL layer_CreateDevice(
    VkPhysicalDevice phys, const VkDeviceCreateInfo *info,
    const VkAllocationCallbacks *alloc, VkDevice *out) {
    const VkLayerDeviceCreateInfo *link = 0;
    const VkBaseInStructure *next =
        (const VkBaseInStructure *)info->pNext;
    while (next) {
        if (next->sType ==
                VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO &&
            ((const VkLayerDeviceCreateInfo *)next)->function ==
                VK_LAYER_LINK_INFO) {
            link = (const VkLayerDeviceCreateInfo *)next;
            break;
        }
        next = next->pNext;
    }
    if (!link || !link->u.pLayerInfo ||
        !link->u.pLayerInfo->pfnNextGetInstanceProcAddr ||
        !link->u.pLayerInfo->pfnNextGetDeviceProcAddr)
        return VK_ERROR_INITIALIZATION_FAILED;
    instance_data *idata = find_instance(phys);
    PFN_vkCreateDevice next_create = (PFN_vkCreateDevice)
        link->u.pLayerInfo->pfnNextGetInstanceProcAddr(
            VK_NULL_HANDLE, "vkCreateDevice");
    if (!next_create) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = next_create(phys, info, alloc, out);
    if (r != VK_SUCCESS) return r;

    PFN_vkGetDeviceProcAddr gdpa =
        link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    pthread_mutex_lock(&s_map_lock);
    device_data *slot = 0;
    for (int i = 0; i < MAX_DEVICES; i++)
        if (!s_devices[i].key) { slot = &s_devices[i]; break; }
    if (slot) {
        memset(slot, 0, sizeof *slot);
        pthread_mutex_init(&slot->cap_lock, 0);
        slot->key = dispatch_key(*out);
        slot->device = *out;
        slot->phys_dev = phys;
        slot->inst = idata;
        slot->gdpa = gdpa;
#define RESOLVE(field, sym) \
        slot->field = (PFN_vk##field)gdpa(*out, sym)
        RESOLVE(CreateSwapchainKHR, "vkCreateSwapchainKHR");
        RESOLVE(DestroySwapchainKHR, "vkDestroySwapchainKHR");
        RESOLVE(GetSwapchainImagesKHR, "vkGetSwapchainImagesKHR");
        RESOLVE(QueuePresentKHR, "vkQueuePresentKHR");
        RESOLVE(CreateCommandPool, "vkCreateCommandPool");
        RESOLVE(DestroyCommandPool, "vkDestroyCommandPool");
        RESOLVE(AllocateCommandBuffers, "vkAllocateCommandBuffers");
        RESOLVE(BeginCommandBuffer, "vkBeginCommandBuffer");
        RESOLVE(EndCommandBuffer, "vkEndCommandBuffer");
        RESOLVE(ResetCommandBuffer, "vkResetCommandBuffer");
        RESOLVE(CmdPipelineBarrier, "vkCmdPipelineBarrier");
        RESOLVE(CmdCopyImageToBuffer, "vkCmdCopyImageToBuffer");
        RESOLVE(QueueSubmit, "vkQueueSubmit");
        RESOLVE(GetFenceStatus, "vkGetFenceStatus");
        RESOLVE(ResetFences, "vkResetFences");
        RESOLVE(WaitForFences, "vkWaitForFences");
        RESOLVE(CreateBuffer, "vkCreateBuffer");
        RESOLVE(DestroyBuffer, "vkDestroyBuffer");
        RESOLVE(GetBufferMemoryRequirements,
                "vkGetBufferMemoryRequirements");
        RESOLVE(AllocateMemory, "vkAllocateMemory");
        RESOLVE(FreeMemory, "vkFreeMemory");
        RESOLVE(BindBufferMemory, "vkBindBufferMemory");
        RESOLVE(MapMemory, "vkMapMemory");
        RESOLVE(CreateFence, "vkCreateFence");
        RESOLVE(DestroyFence, "vkDestroyFence");
        RESOLVE(DestroyDevice, "vkDestroyDevice");
#undef RESOLVE
        slot->queue_family = 0;
        slot->queue_family_known = 0;
        if (info->pQueueCreateInfos && info->queueCreateInfoCount) {
            slot->queue_family =
                info->pQueueCreateInfos[0].queueFamilyIndex;
            slot->queue_family_known = 1;
        }
    }
    pthread_mutex_unlock(&s_map_lock);
    pthread_once(&s_once, shm_init);
    return r;
}

static void VKAPI_CALL layer_DestroyDevice(
    VkDevice device, const VkAllocationCallbacks *alloc) {
    device_data *d = find_device(device);
    PFN_vkDestroyDevice real = d ? d->DestroyDevice : 0;
    if (d) {
        /* drain pending copies so the staging memory can go away */
        pthread_mutex_lock(&d->cap_lock);
        for (int i = 0; i < STAGE_SLOTS; i++) {
            stage_slot *s = &d->stage[i];
            if (s->pending && s->fence != VK_NULL_HANDLE &&
                d->WaitForFences) {
                VkFence f = s->fence;
                d->WaitForFences(device, 1, &f, VK_TRUE,
                                 2000ull * 1000 * 1000);
                if (d->GetFenceStatus(device, f) == VK_SUCCESS) {
                    s->pending = 0;
                    if (s_active)
                        shm_publish((const uint8_t *)s->mapped,
                                    s->w, s->h, s->swizzle, s->ts_ns);
                }
            }
        }
        stage_teardown(d);
        pthread_mutex_unlock(&d->cap_lock);
        pthread_mutex_lock(&s_map_lock);
        for (int i = 0; i < MAX_SWAPCHAINS; i++)
            if (s_swapchains[i].dev == d)
                memset(&s_swapchains[i], 0, sizeof s_swapchains[i]);
        pthread_mutex_destroy(&d->cap_lock);
        memset(d, 0, sizeof *d);
        pthread_mutex_unlock(&s_map_lock);
    }
    if (real) real(device, alloc);
}

/* ------------------------------------------------------------------ */
/* proc-address plumbing                                               */

typedef struct { const char *name; PFN_vkVoidFunction fn; } hook_ent;
static const hook_ent s_instance_hooks[] = {
    {"vkGetInstanceProcAddr",
     (PFN_vkVoidFunction)layer_GetInstanceProcAddr},
    {"vkGetDeviceProcAddr", (PFN_vkVoidFunction)layer_GetDeviceProcAddr},
    {"vkCreateInstance", (PFN_vkVoidFunction)layer_CreateInstance},
    {"vkDestroyInstance", (PFN_vkVoidFunction)layer_DestroyInstance},
    {"vkCreateDevice", (PFN_vkVoidFunction)layer_CreateDevice},
    {0, 0},
};
static const hook_ent s_device_hooks[] = {
    {"vkGetDeviceProcAddr", (PFN_vkVoidFunction)layer_GetDeviceProcAddr},
    {"vkCreateSwapchainKHR",
     (PFN_vkVoidFunction)layer_CreateSwapchainKHR},
    {"vkDestroySwapchainKHR",
     (PFN_vkVoidFunction)layer_DestroySwapchainKHR},
    {"vkGetSwapchainImagesKHR",
     (PFN_vkVoidFunction)layer_GetSwapchainImagesKHR},
    {"vkQueuePresentKHR", (PFN_vkVoidFunction)layer_QueuePresentKHR},
    {"vkGetDeviceQueue", (PFN_vkVoidFunction)layer_GetDeviceQueue},
    {"vkDestroyDevice", (PFN_vkVoidFunction)layer_DestroyDevice},
    {0, 0},
};

static PFN_vkVoidFunction hook_lookup(const hook_ent *table,
                                      const char *name) {
    for (int i = 0; table[i].name; i++)
        if (strcmp(table[i].name, name) == 0) return table[i].fn;
    return 0;
}

static PFN_vkVoidFunction VKAPI_CALL
layer_GetInstanceProcAddr(VkInstance instance, const char *name) {
    PFN_vkVoidFunction hook = hook_lookup(s_instance_hooks, name);
    if (hook) return hook;
    hook = hook_lookup(s_device_hooks, name);
    if (hook) return hook;
    instance_data *idata = find_instance(instance);
    if (idata) return idata->gipa(instance, name);
    return 0;
}

static PFN_vkVoidFunction VKAPI_CALL
layer_GetDeviceProcAddr(VkDevice device, const char *name) {
    PFN_vkVoidFunction hook = hook_lookup(s_device_hooks, name);
    if (hook) return hook;
    device_data *d = find_device(device);
    if (d) return d->gdpa(device, name);
    return 0;
}

/* ------------------------------------------------------------------ */
/* loader negotiation                                                  */

VKAPI_ATTR VkResult VKAPI_CALL
vkNegotiateLoaderLayerInterfaceVersion(
    VkNegotiateLayerInterface *pVersionStruct) {
    if (!pVersionStruct) return VK_ERROR_INITIALIZATION_FAILED;
    if (pVersionStruct->loaderLayerInterfaceVersion >
        CURRENT_LOADER_LAYER_INTERFACE_VERSION) {
        pVersionStruct->loaderLayerInterfaceVersion =
            CURRENT_LOADER_LAYER_INTERFACE_VERSION;
    }
    pVersionStruct->pfnGetInstanceProcAddr =
        (PFN_vkGetInstanceProcAddr)layer_GetInstanceProcAddr;
    pVersionStruct->pfnGetDeviceProcAddr = layer_GetDeviceProcAddr;
    if (pVersionStruct->loaderLayerInterfaceVersion >= 2) {
        pVersionStruct->pfnGetPhysicalDeviceProcAddr = 0;
    }
    return VK_SUCCESS;
}

/* entry points named by the manifest's "functions" block */
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL
recomp_vkGetInstanceProcAddr(VkInstance instance, const char *name) {
    return layer_GetInstanceProcAddr(instance, name);
}
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL
recomp_vkGetDeviceProcAddr(VkDevice device, const char *name) {
    return layer_GetDeviceProcAddr(device, name);
}
