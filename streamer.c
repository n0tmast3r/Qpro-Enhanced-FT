#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define CAMERA_MAP_BYTES ((size_t)0xC4000)
#define SENSOR_WIDTH 2000
#define SENSOR_HEIGHT 400
#define SENSOR_BYTES ((size_t)SENSOR_WIDTH * SENSOR_HEIGHT)
#define FRAME_COUNTER_OFFSET (SENSOR_BYTES + (size_t)24)
#define FACE_X 800
#define MAX_CAMERA_MAPS 16
#define LOG_PATH "/data/local/tmp/questpro-live-v8.log"
#define SHARED_PATH "/data/local/tmp/questpro-live-v8-shared.bin"
#define SHARED_HEADER_BYTES ((size_t)80)
#define SHARED_BYTES (SHARED_HEADER_BYTES + SENSOR_BYTES)
#define SHARED_TORN_COUNT_OFFSET ((size_t)56)
#define SHARED_ACTIVE_UNTIL_OFFSET ((size_t)64)
#define SHARED_REQUESTED_MAX_FPS_OFFSET ((size_t)72)

typedef struct {
    uint8_t *address;
} CameraMap;

static CameraMap g_maps[MAX_CAMERA_MAPS];
static size_t g_map_count;
static pthread_t g_worker;
static int g_started;

static void log_line(const char *format, ...) {
    int fd = open(LOG_PATH, O_WRONLY | O_APPEND | O_CLOEXEC);
    if (fd < 0) return;
    char line[512];
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    int used = snprintf(line, sizeof(line), "[%lld.%03lld] ",
                        (long long)now.tv_sec,
                        (long long)(now.tv_nsec / 1000000));
    if (used < 0 || (size_t)used >= sizeof(line)) {
        close(fd);
        return;
    }
    va_list args;
    va_start(args, format);
    int added = vsnprintf(line + used, sizeof(line) - (size_t)used, format, args);
    va_end(args);
    if (added > 0) {
        size_t length = strnlen(line, sizeof(line) - 2);
        line[length++] = '\n';
        (void)write(fd, line, length);
    }
    close(fd);
}

static int compare_map_address(const void *left, const void *right) {
    const CameraMap *a = (const CameraMap *)left;
    const CameraMap *b = (const CameraMap *)right;
    return a->address < b->address ? -1 : a->address > b->address;
}

static int discover_camera_maps(void) {
    FILE *maps = fopen("/proc/self/maps", "r");
    if (!maps) return 0;
    char line[1024];
    while (fgets(line, sizeof(line), maps)) {
        unsigned long long start = 0, end = 0, offset = 0, inode = 0;
        char permissions[5] = {0};
        char device[24] = {0};
        int path_offset = 0;
        int fields = sscanf(line, "%llx-%llx %4s %llx %23s %llu %n",
                            &start, &end, permissions, &offset, device, &inode,
                            &path_offset);
        (void)offset;
        (void)device;
        (void)inode;
        if (fields != 6 || permissions[0] != 'r' || end <= start ||
            (size_t)(end - start) != CAMERA_MAP_BYTES)
            continue;
        char *path = line + path_offset;
        while (*path == ' ' || *path == '\t') ++path;
        if (!strstr(path, "/dmabuf:dmabuf")) continue;
        if (g_map_count >= MAX_CAMERA_MAPS) {
            fclose(maps);
            return 0;
        }
        g_maps[g_map_count++].address = (uint8_t *)(uintptr_t)start;
    }
    fclose(maps);
    qsort(g_maps, g_map_count, sizeof(g_maps[0]), compare_map_address);
    return g_map_count == 9;
}

static uint32_t frame_counter(const CameraMap *map) {
    uint32_t value = 0;
    memcpy(&value, map->address + FRAME_COUNTER_OFFSET, sizeof(value));
    return value;
}

static int counter_is_newer(uint32_t candidate, uint32_t reference) {
    return (int32_t)(candidate - reference) > 0;
}

static int contains_face_views(const uint8_t *pixels) {
    unsigned nonzero = 0;
    unsigned samples = 0;
    for (unsigned y = 23; y < SENSOR_HEIGHT; y += 47) {
        const uint8_t *row = pixels + (size_t)y * SENSOR_WIDTH;
        for (unsigned x = FACE_X + 19; x < SENSOR_WIDTH; x += 89) {
            nonzero += row[x] != 0;
            ++samples;
        }
    }
    return samples != 0 && nonzero * 4 > samples * 3;
}

static uint64_t monotonic_nanoseconds(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return (uint64_t)value.tv_sec * UINT64_C(1000000000) +
           (uint64_t)value.tv_nsec;
}

static uint8_t *open_shared_output(void) {
    for (;;) {
        int fd = open(SHARED_PATH, O_RDWR | O_CLOEXEC);
        if (fd >= 0) {
            struct stat status;
            if (fstat(fd, &status) == 0 && (size_t)status.st_size >= SHARED_BYTES) {
                void *mapping = mmap(NULL, SHARED_BYTES, PROT_READ | PROT_WRITE,
                                     MAP_SHARED, fd, 0);
                close(fd);
                if (mapping != MAP_FAILED) return (uint8_t *)mapping;
            } else {
                close(fd);
            }
        }
        log_line("WAITING_FOR_SHARED_OUTPUT error=%s", strerror(errno));
        sleep(1);
    }
}

static void publish_frame(uint8_t *shared, uint64_t sequence,
                          const uint8_t *frame) {
    uint32_t *generation = (uint32_t *)(void *)(shared + 8);
    uint32_t value = __atomic_load_n(generation, __ATOMIC_RELAXED);
    if (value & 1u) ++value;
    __atomic_store_n(generation, value + 1u, __ATOMIC_RELEASE);
    memcpy(shared + 16, &sequence, sizeof(sequence));
    uint64_t timestamp = monotonic_nanoseconds();
    memcpy(shared + 24, &timestamp, sizeof(timestamp));
    memcpy(shared + SHARED_HEADER_BYTES, frame, SENSOR_BYTES);
    __atomic_store_n(generation, value + 2u, __ATOMIC_RELEASE);
}

static void publish_torn_count(uint8_t *shared, uint64_t count) {
    uint64_t *value = (uint64_t *)(void *)(shared + SHARED_TORN_COUNT_OFFSET);
    __atomic_store_n(value, count, __ATOMIC_RELEASE);
}

static int capture_is_requested(const uint8_t *shared) {
    const uint64_t *active_until = (const uint64_t *)(const void *)(
        shared + SHARED_ACTIVE_UNTIL_OFFSET);
    return __atomic_load_n(active_until, __ATOMIC_ACQUIRE) >
           monotonic_nanoseconds();
}

static uint32_t requested_max_fps(const uint8_t *shared) {
    const uint32_t *value = (const uint32_t *)(const void *)(
        shared + SHARED_REQUESTED_MAX_FPS_OFFSET);
    return __atomic_load_n(value, __ATOMIC_ACQUIRE);
}

/*
 * DMA memory can change during memcpy. Two complete copies separated by a
 * short interval ensure that every published scan is internally consistent.
 * A sparse before/after hash alone can miss a changed horizontal row.
 */
static int copy_stable_frame(const CameraMap *map, uint32_t expected_counter,
                             uint8_t *first, uint8_t *second) {
    uint32_t before = frame_counter(map);
    if (before != expected_counter) return 0;
    memcpy(first, map->address, SENSOR_BYTES);
    usleep(250);
    memcpy(second, map->address, SENSOR_BYTES);
    uint32_t after = frame_counter(map);
    return before == after && memcmp(first, second, SENSOR_BYTES) == 0;
}

/*
 * The provider stores a global monotonic frame counter in every DMA slot's
 * metadata. Reading nine four-byte counters at an output deadline is enough to
 * identify the newest full face frame. Unlike v7's continuous image hashes,
 * this does not repeatedly touch the camera surfaces between deadlines and
 * cannot mistake a static image for a frozen producer.
 */
static uint32_t newest_counter(void) {
    uint32_t newest = 0;
    int have_newest = 0;
    for (size_t i = 0; i < g_map_count; ++i) {
        uint32_t current = frame_counter(&g_maps[i]);
        if (!have_newest || counter_is_newer(current, newest)) {
            newest = current;
            have_newest = 1;
        }
    }
    return newest;
}

static CameraMap *newest_face_slot(uint32_t after_counter,
                                   uint32_t *selected_counter) {
    CameraMap *newest = NULL;
    uint32_t newest_value = after_counter;
    for (size_t i = 0; i < g_map_count; ++i) {
        CameraMap *map = &g_maps[i];
        uint32_t current = frame_counter(map);
        if (!counter_is_newer(current, after_counter) ||
            (newest && !counter_is_newer(current, newest_value)) ||
            !contains_face_views(map->address))
            continue;
        newest = map;
        newest_value = current;
    }
    if (newest) *selected_counter = newest_value;
    return newest;
}

static void *stream_worker(void *unused) {
    (void)unused;
    while (!discover_camera_maps()) {
        log_line("WAITING_FOR_CAMERA_MAPS count=%zu", g_map_count);
        g_map_count = 0;
        sleep(1);
    }
    log_line("MAP_DISCOVERY_OK count=%zu", g_map_count);
    uint8_t *first = malloc(SENSOR_BYTES);
    uint8_t *second = malloc(SENSOR_BYTES);
    if (!first || !second) {
        free(first);
        free(second);
        log_line("FRAME_ALLOCATION_FAILED");
        return NULL;
    }
    uint8_t *shared = open_shared_output();
    publish_torn_count(shared, 0);
    log_line("SHARED_OUTPUT_READY bytes=%zu", SHARED_BYTES);

    uint64_t sequence = 0;
    uint64_t rejected_torn = 0;
    uint64_t next_capture_at = 0;
    uint32_t last_published_counter = 0;
    int was_active = 0;
    for (;;) {
        int active = capture_is_requested(shared);
        if (active != was_active) {
            log_line(active ? "CAPTURE_ACTIVE" : "CAPTURE_IDLE");
            if (active) {
                /* Discard frames accumulated while idle. The first output
                 * must carry a hardware counter newer than this baseline. */
                last_published_counter = newest_counter();
            }
            was_active = active;
        }
        if (!active) {
            next_capture_at = 0;
            usleep(20000);
            continue;
        }
        uint32_t max_fps = requested_max_fps(shared);
        if (max_fps) {
            uint64_t now = monotonic_nanoseconds();
            uint64_t interval = UINT64_C(1000000000) / max_fps;
            if (next_capture_at && now < next_capture_at) {
                uint64_t remaining_us = (next_capture_at - now) / 1000;
                usleep((useconds_t)(remaining_us > 2000 ? 2000 : remaining_us));
                continue;
            }
            next_capture_at = now + interval;
        }
        uint32_t selected_counter = last_published_counter;
        CameraMap *map = newest_face_slot(
            last_published_counter, &selected_counter);
        if (!map) {
            usleep(1000);
            continue;
        }
        if (!copy_stable_frame(map, selected_counter, first, second)) {
            ++rejected_torn;
            publish_torn_count(shared, rejected_torn);
            if ((rejected_torn & 255u) == 1u)
                log_line("TORN_FRAME_REJECTED total=%llu",
                         (unsigned long long)rejected_torn);
            continue;
        }
        if (!contains_face_views(second)) {
            last_published_counter = selected_counter;
            continue;
        }
        last_published_counter = selected_counter;
        ++sequence;
        publish_frame(shared, sequence, second);
        usleep(1000);
    }
}

__attribute__((constructor)) static void start_streamer(void) {
    if (__atomic_exchange_n(&g_started, 1, __ATOMIC_ACQ_REL)) return;
    int result = pthread_create(&g_worker, NULL, stream_worker, NULL);
    if (result != 0) {
        log_line("THREAD_CREATE_FAILED error=%s", strerror(result));
        return;
    }
    pthread_detach(g_worker);
    log_line("STREAMER_STARTED version=8.0 cameras=all stability=counter-guarded-double-copy provider-cap=relay-max-fps ring-order=hardware-counter lifecycle=client-lease diagnostics=torn-count");
}
