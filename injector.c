#define _GNU_SOURCE

#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

#define PROVIDER_NAME "vendor.oculus.hardware.sensors@1.0-service"
#define MAX_THREADS 256
#define SCRATCH_BYTES 2048
#define NT_PRSTATUS_VALUE 1
#define AARCH64_NR_OPENAT 56
#define AARCH64_NR_CLOSE 57
#define AARCH64_NR_LSEEK 62
#define AARCH64_NR_SENDFILE 71
#define AARCH64_NR_MMAP 222
#define AARCH64_NR_MPROTECT 226
#define AARCH64_NR_MEMFD_CREATE 279
#define ANDROID_DLEXT_USE_LIBRARY_FD UINT64_C(0x10)
#define MFD_CLOEXEC_VALUE 0x0001u
#define REMOTE_CODE_BYTES ((size_t)16384)
#define REMOTE_ALLOCATION_BYTES ((size_t)65536)

typedef struct {
    uint64_t regs[31];
    uint64_t sp;
    uint64_t pc;
    uint64_t pstate;
} Aarch64Regs;

typedef struct {
    uint64_t flags;
    uint64_t reserved_addr;
    uint64_t reserved_size;
    int32_t relro_fd;
    int32_t library_fd;
    int64_t library_fd_offset;
    uint64_t library_namespace;
} AndroidDlextInfo64;

typedef struct {
    pid_t tid;
    Aarch64Regs original_regs;
    uintptr_t patched_address;
    uintptr_t execution_address;
    uintptr_t scratch_address;
    uint64_t original_code;
    uint8_t original_scratch[SCRATCH_BYTES];
    int prepared;
    int original_code_patched;
} RemoteContext;

static int is_decimal_name(const char *text) {
    if (!text || !*text) return 0;
    for (const char *p = text; *p; ++p)
        if (*p < '0' || *p > '9') return 0;
    return 1;
}

static pid_t find_provider_pid(void) {
    DIR *directory = opendir("/proc");
    if (!directory) return -1;
    pid_t found = -1;
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (!is_decimal_name(entry->d_name)) continue;
        pid_t candidate = (pid_t)strtol(entry->d_name, NULL, 10);
        char path[64];
        snprintf(path, sizeof(path), "/proc/%d/cmdline", candidate);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) continue;
        char command[512];
        ssize_t count = read(fd, command, sizeof(command) - 1);
        close(fd);
        if (count <= 0) continue;
        command[count] = '\0';
        if (strstr(command, PROVIDER_NAME)) {
            found = candidate;
            break;
        }
    }
    closedir(directory);
    return found;
}

static int contains_tid(const pid_t *tids, size_t count, pid_t tid) {
    for (size_t i = 0; i < count; ++i)
        if (tids[i] == tid) return 1;
    return 0;
}

static int list_threads(pid_t pid, pid_t *tids, size_t capacity) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/task", pid);
    DIR *directory = opendir(path);
    if (!directory) return -1;
    size_t count = 0;
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (!is_decimal_name(entry->d_name)) continue;
        if (count >= capacity) {
            closedir(directory);
            return -1;
        }
        tids[count++] = (pid_t)strtol(entry->d_name, NULL, 10);
    }
    closedir(directory);
    return (int)count;
}

static int attach_one(pid_t tid) {
    if (ptrace(PTRACE_ATTACH, tid, NULL, NULL) != 0) return 0;
    int status = 0;
    if (waitpid(tid, &status, __WALL) != tid || !WIFSTOPPED(status)) return 0;
    return 1;
}

static int attach_all_threads(pid_t pid, pid_t *attached, size_t *count) {
    *count = 0;
    if (!attach_one(pid)) return 0;
    attached[(*count)++] = pid;
    for (int pass = 0; pass < 3; ++pass) {
        pid_t current[MAX_THREADS];
        int found = list_threads(pid, current, MAX_THREADS);
        if (found < 0) return 0;
        int added = 0;
        for (int i = 0; i < found; ++i) {
            if (contains_tid(attached, *count, current[i])) continue;
            if (*count >= MAX_THREADS || !attach_one(current[i])) return 0;
            attached[(*count)++] = current[i];
            added = 1;
        }
        if (!added) break;
    }
    return 1;
}

static void detach_all_threads(const pid_t *attached, size_t count) {
    for (size_t i = count; i > 0; --i) {
        if (ptrace(PTRACE_DETACH, attached[i - 1], NULL, NULL) != 0)
            fprintf(stderr, "DETACH_WARNING tid=%d error=%s\n",
                    attached[i - 1], strerror(errno));
    }
}

static void resume_secondary_threads(const pid_t *attached, size_t *count) {
    for (size_t i = *count; i > 1; --i) {
        if (ptrace(PTRACE_DETACH, attached[i - 1], NULL, NULL) != 0)
            fprintf(stderr, "DETACH_WARNING tid=%d error=%s\n",
                    attached[i - 1], strerror(errno));
    }
    *count = *count ? 1 : 0;
}

static int get_regs(pid_t tid, Aarch64Regs *regs) {
    struct iovec io = {.iov_base = regs, .iov_len = sizeof(*regs)};
    return ptrace(PTRACE_GETREGSET, tid, (void *)(uintptr_t)NT_PRSTATUS_VALUE,
                  &io) == 0 && io.iov_len >= sizeof(*regs);
}

static int set_regs(pid_t tid, const Aarch64Regs *regs) {
    struct iovec io = {.iov_base = (void *)regs, .iov_len = sizeof(*regs)};
    return ptrace(PTRACE_SETREGSET, tid, (void *)(uintptr_t)NT_PRSTATUS_VALUE,
                  &io) == 0;
}

static int peek_word(pid_t tid, uintptr_t address, uint64_t *value) {
    errno = 0;
    long result = ptrace(PTRACE_PEEKDATA, tid, (void *)address, NULL);
    if (result == -1 && errno) return 0;
    *value = (uint64_t)(unsigned long)result;
    return 1;
}

static int poke_word(pid_t tid, uintptr_t address, uint64_t value) {
    return ptrace(PTRACE_POKEDATA, tid, (void *)address,
                  (void *)(uintptr_t)value) == 0;
}

static int read_memory(pid_t tid, uintptr_t address, void *output, size_t size) {
    uint8_t *bytes = (uint8_t *)output;
    for (size_t offset = 0; offset < size; offset += sizeof(uint64_t)) {
        uint64_t word = 0;
        if (!peek_word(tid, address + offset, &word)) return 0;
        size_t amount = size - offset < sizeof(word) ? size - offset : sizeof(word);
        memcpy(bytes + offset, &word, amount);
    }
    return 1;
}

static int write_memory(pid_t tid, uintptr_t address, const void *input,
                        size_t size) {
    const uint8_t *bytes = (const uint8_t *)input;
    for (size_t offset = 0; offset < size; offset += sizeof(uint64_t)) {
        uint64_t word = 0;
        size_t amount = size - offset < sizeof(word) ? size - offset : sizeof(word);
        if (amount != sizeof(word) && !peek_word(tid, address + offset, &word))
            return 0;
        memcpy(&word, bytes + offset, amount);
        if (!poke_word(tid, address + offset, word)) return 0;
    }
    return 1;
}

static int prepare_remote(RemoteContext *remote, pid_t tid) {
    memset(remote, 0, sizeof(*remote));
    remote->tid = tid;
    if (!get_regs(tid, &remote->original_regs)) return 0;
    remote->patched_address = (uintptr_t)remote->original_regs.pc;
    remote->execution_address = remote->patched_address;
    remote->scratch_address = ((uintptr_t)remote->original_regs.sp -
                               SCRATCH_BYTES - 64) & ~(uintptr_t)15;
    if (!peek_word(tid, remote->patched_address, &remote->original_code) ||
        !read_memory(tid, remote->scratch_address, remote->original_scratch,
                     SCRATCH_BYTES))
        return 0;
    // svc #0 followed by brk #0. Remote function calls return to the brk.
    if (!poke_word(tid, remote->patched_address,
                   UINT64_C(0xd4200000d4000001)))
        return 0;
    remote->original_code_patched = 1;
    remote->prepared = 1;
    return 1;
}

static void restore_remote(RemoteContext *remote) {
    if (!remote->prepared) return;
    if (remote->original_code_patched) {
        if (!poke_word(remote->tid, remote->patched_address,
                       remote->original_code))
            fprintf(stderr, "CODE_RESTORE_WARNING error=%s\n", strerror(errno));
        else
            remote->original_code_patched = 0;
    }
    if (!write_memory(remote->tid, remote->scratch_address,
                      remote->original_scratch, SCRATCH_BYTES))
        fprintf(stderr, "STACK_RESTORE_WARNING error=%s\n", strerror(errno));
    if (!set_regs(remote->tid, &remote->original_regs))
        fprintf(stderr, "REGS_RESTORE_WARNING error=%s\n", strerror(errno));
    remote->prepared = 0;
}

static int remote_syscall(RemoteContext *remote, uint64_t number,
                          const uint64_t arguments[6], int64_t *result) {
    Aarch64Regs regs = remote->original_regs;
    for (int i = 0; i < 6; ++i) regs.regs[i] = arguments[i];
    regs.regs[8] = number;
    regs.pc = remote->execution_address;
    if (!set_regs(remote->tid, &regs) ||
        ptrace(PTRACE_CONT, remote->tid, NULL, NULL) != 0)
        return 0;
    int status = 0;
    if (waitpid(remote->tid, &status, __WALL) != remote->tid ||
        !WIFSTOPPED(status) || WSTOPSIG(status) != SIGTRAP)
        return 0;
    if (!get_regs(remote->tid, &regs)) return 0;
    *result = (int64_t)regs.regs[0];
    return 1;
}

static int remote_call(RemoteContext *remote, uintptr_t function,
                       const uint64_t arguments[3], uintptr_t stack_pointer,
                       uint64_t *result) {
    Aarch64Regs regs = remote->original_regs;
    regs.regs[0] = arguments[0];
    regs.regs[1] = arguments[1];
    regs.regs[2] = arguments[2];
    regs.regs[30] = remote->execution_address + 4;
    regs.sp = stack_pointer;
    regs.pc = function;
    if (!set_regs(remote->tid, &regs) ||
        ptrace(PTRACE_CONT, remote->tid, NULL, NULL) != 0)
        return 0;
    int status = 0;
    if (waitpid(remote->tid, &status, __WALL) != remote->tid ||
        !WIFSTOPPED(status) || WSTOPSIG(status) != SIGTRAP)
        return 0;
    if (!get_regs(remote->tid, &regs)) return 0;
    *result = regs.regs[0];
    return 1;
}

static int remote_sendfile_all(RemoteContext *remote, int output_fd,
                               int input_fd, size_t size) {
    size_t copied = 0;
    while (copied < size) {
        uint64_t arguments[6] = {
            (uint64_t)output_fd, (uint64_t)input_fd, 0,
            (uint64_t)(size - copied), 0, 0
        };
        int64_t result = 0;
        if (!remote_syscall(remote, AARCH64_NR_SENDFILE, arguments, &result) ||
            result <= 0) {
            fprintf(stderr, "REMOTE_SENDFILE_FAILED copied=%zu result=%" PRId64 "\n",
                    copied, result);
            return 0;
        }
        copied += (size_t)result;
    }
    return 1;
}

static const char *base_name(const char *path) {
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

static uintptr_t find_remote_module_base(pid_t pid, const char *local_path) {
    char maps_path[64];
    snprintf(maps_path, sizeof(maps_path), "/proc/%d/maps", pid);
    FILE *maps = fopen(maps_path, "r");
    if (!maps) return 0;
    const char *wanted_name = base_name(local_path);
    uintptr_t result = 0;
    char line[1024];
    while (fgets(line, sizeof(line), maps)) {
        unsigned long long start = 0, end = 0, offset = 0, inode = 0;
        char permissions[5] = {0};
        char device[24] = {0};
        int path_offset = 0;
        if (sscanf(line, "%llx-%llx %4s %llx %23s %llu %n", &start, &end,
                   permissions, &offset, device, &inode, &path_offset) != 6 ||
            offset != 0)
            continue;
        (void)end;
        (void)permissions;
        (void)device;
        (void)inode;
        char *path = line + path_offset;
        while (*path == ' ' || *path == '\t') ++path;
        path[strcspn(path, "\r\n")] = '\0';
        if (!strcmp(path, local_path) || !strcmp(base_name(path), wanted_name)) {
            result = (uintptr_t)start;
            break;
        }
    }
    fclose(maps);
    return result;
}

static uintptr_t resolve_remote_symbol(pid_t pid, const char *name) {
    void *symbol = dlsym(RTLD_DEFAULT, name);
    if (!symbol) {
        void *libdl = dlopen("libdl.so", RTLD_NOW | RTLD_LOCAL);
        if (libdl) symbol = dlsym(libdl, name);
    }
    if (!symbol) {
        fprintf(stderr, "LOCAL_SYMBOL_NOT_RESOLVED name=%s error=%s\n",
                name, dlerror());
        return 0;
    }
    Dl_info information;
    if (!dladdr(symbol, &information) || !information.dli_fbase ||
        !information.dli_fname) {
        fprintf(stderr, "LOCAL_SYMBOL_MODULE_UNKNOWN name=%s\n", name);
        return 0;
    }
    uintptr_t remote_base = find_remote_module_base(pid, information.dli_fname);
    if (!remote_base) {
        fprintf(stderr, "REMOTE_MODULE_NOT_FOUND name=%s local_module=%s\n",
                name, information.dli_fname);
        return 0;
    }
    uintptr_t offset = (uintptr_t)symbol - (uintptr_t)information.dli_fbase;
    printf("SYMBOL %s local_module=%s offset=0x%" PRIxPTR "\n",
           name, information.dli_fname, offset);
    return remote_base + offset;
}

static int read_remote_string(pid_t tid, uintptr_t address, char *output,
                              size_t capacity) {
    if (!address || capacity == 0) return 0;
    for (size_t i = 0; i + 1 < capacity; ++i) {
        uint64_t word = 0;
        uintptr_t aligned = (address + i) & ~(uintptr_t)7;
        if (!peek_word(tid, aligned, &word)) return 0;
        char value = (char)((word >> (((address + i) & 7) * 8)) & 0xff);
        output[i] = value;
        if (!value) return 1;
    }
    output[capacity - 1] = '\0';
    return 1;
}

static int prepare_log_file(void) {
    int fd = open("/data/local/tmp/questpro-live-v3.log",
                  O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC, 0666);
    if (fd < 0) return 0;
    close(fd);
    /* The launcher pre-creates this as Android Shell with mode 0666. Some
       Magisk builds grant uid 0 without CAP_FOWNER; fchmod would therefore
       reject the shell-owned file even though opening/truncating it works. */
    return 1;
}

static int library_is_loaded(pid_t pid, const char *library_path) {
    char maps_path[64];
    snprintf(maps_path, sizeof(maps_path), "/proc/%d/maps", pid);
    FILE *maps = fopen(maps_path, "r");
    if (!maps) return 0;
    const char *name = base_name(library_path);
    char line[1024];
    int found = 0;
    while (fgets(line, sizeof(line), maps)) {
        if (strstr(line, name)) {
            found = 1;
            break;
        }
    }
    fclose(maps);
    return found;
}

int main(int argc, char **argv) {
    const char *library_path = argc > 1
        ? argv[1] : "/data/local/tmp/libquestpro-camera-streamer.so";
    pid_t pid = find_provider_pid();
    if (pid <= 0) {
        fprintf(stderr, "PROVIDER_NOT_FOUND\n");
        return 3;
    }
    printf("PROVIDER_CANDIDATE_PID %d\n", pid);
    struct stat library_stat;
    if (stat(library_path, &library_stat) != 0 || library_stat.st_size <= 0 ||
        (uint64_t)library_stat.st_size > UINT64_C(64) * 1024 * 1024) {
        fprintf(stderr, "LIBRARY_STAT_FAILED path=%s error=%s\n",
                library_path, strerror(errno));
        return 3;
    }
    if (library_is_loaded(pid, library_path)) {
        printf("PROVIDER_PID %d\nINJECTION_ALREADY_ACTIVE transport=shared-memory\n", pid);
        return 0;
    }
    if (!prepare_log_file()) {
        fprintf(stderr, "LOG_PREPARE_FAILED error=%s\n", strerror(errno));
        return 2;
    }
    uintptr_t remote_dlopen = resolve_remote_symbol(pid, "android_dlopen_ext");
    uintptr_t remote_dlerror = resolve_remote_symbol(pid, "dlerror");
    if (!remote_dlopen) {
        fprintf(stderr, "ANDROID_DLOPEN_EXT_NOT_RESOLVED\n");
        return 4;
    }
    printf("PROVIDER_PID %d\nREMOTE_ANDROID_DLOPEN_EXT 0x%" PRIxPTR "\n",
           pid, remote_dlopen);

    pid_t attached[MAX_THREADS];
    size_t attached_count = 0;
    if (!attach_all_threads(pid, attached, &attached_count)) {
        fprintf(stderr, "ATTACH_FAILED error=%s\n", strerror(errno));
        detach_all_threads(attached, attached_count);
        return 5;
    }
    printf("THREADS_PAUSED %zu\n", attached_count);

    RemoteContext remote;
    int success = 0;
    int remote_source_fd = -1;
    int remote_memfd = -1;
    uintptr_t remote_allocation = 0;
    if (!prepare_remote(&remote, pid)) {
        fprintf(stderr, "REMOTE_PREPARE_FAILED error=%s\n", strerror(errno));
        goto cleanup;
    }

    uint64_t mmap_args[6] = {
        0, REMOTE_ALLOCATION_BYTES, PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANONYMOUS, (uint64_t)(int64_t)-1, 0
    };
    int64_t syscall_result = 0;
    if (!remote_syscall(&remote, AARCH64_NR_MMAP, mmap_args, &syscall_result) ||
        syscall_result <= 0) {
        fprintf(stderr, "REMOTE_MMAP_FAILED result=%" PRId64 "\n",
                syscall_result);
        goto cleanup;
    }
    remote_allocation = (uintptr_t)syscall_result;
    const uint64_t svc_then_break = UINT64_C(0xd4200000d4000001);
    if (!write_memory(pid, remote_allocation, &svc_then_break,
                      sizeof(svc_then_break))) {
        fprintf(stderr, "REMOTE_CODE_WRITE_FAILED\n");
        goto cleanup;
    }
    uint64_t protect_args[6] = {
        remote_allocation, REMOTE_CODE_BYTES, PROT_READ | PROT_EXEC, 0, 0, 0
    };
    if (!remote_syscall(&remote, AARCH64_NR_MPROTECT, protect_args,
                        &syscall_result) || syscall_result != 0) {
        fprintf(stderr, "REMOTE_MPROTECT_FAILED result=%" PRId64 "\n",
                syscall_result);
        goto cleanup;
    }
    if (!poke_word(pid, remote.patched_address, remote.original_code)) {
        fprintf(stderr, "ORIGINAL_CODE_EARLY_RESTORE_FAILED\n");
        goto cleanup;
    }
    remote.original_code_patched = 0;
    remote.execution_address = remote_allocation;
    resume_secondary_threads(attached, &attached_count);
    printf("SECONDARY_THREADS_RESUMED\n");

    uintptr_t remote_stack = remote_allocation + REMOTE_ALLOCATION_BYTES - 16;
    size_t path_length = strlen(library_path) + 1;
    uintptr_t path_address = remote_allocation + REMOTE_CODE_BYTES + 256;
    const char *library_name = base_name(library_path);
    size_t name_length = strlen(library_name) + 1;
    uintptr_t name_address = remote_allocation + REMOTE_CODE_BYTES + 1024;
    uintptr_t info_address = remote_allocation + REMOTE_CODE_BYTES + 2048;
    if (path_length > 400 ||
        name_length > 400 ||
        !write_memory(pid, path_address, library_path, path_length) ||
        !write_memory(pid, name_address, library_name, name_length)) {
        fprintf(stderr, "REMOTE_PATH_WRITE_FAILED\n");
        goto cleanup;
    }
    uint64_t open_args[6] = {
        (uint64_t)(int64_t)-100, path_address, O_RDONLY | O_CLOEXEC, 0, 0, 0
    };
    if (!remote_syscall(&remote, AARCH64_NR_OPENAT, open_args,
                        &syscall_result) || syscall_result < 0) {
        fprintf(stderr, "REMOTE_OPEN_FAILED result=%" PRId64 "\n",
                syscall_result);
        goto cleanup;
    }
    remote_source_fd = (int)syscall_result;

    uint64_t memfd_args[6] = {
        name_address, MFD_CLOEXEC_VALUE, 0, 0, 0, 0
    };
    if (!remote_syscall(&remote, AARCH64_NR_MEMFD_CREATE, memfd_args,
                        &syscall_result) || syscall_result < 0) {
        fprintf(stderr, "REMOTE_MEMFD_CREATE_FAILED result=%" PRId64 "\n",
                syscall_result);
        goto cleanup;
    }
    remote_memfd = (int)syscall_result;
    if (!remote_sendfile_all(&remote, remote_memfd, remote_source_fd,
                             (size_t)library_stat.st_size))
        goto cleanup;
    uint64_t seek_args[6] = {(uint64_t)remote_memfd, 0, SEEK_SET, 0, 0, 0};
    if (!remote_syscall(&remote, AARCH64_NR_LSEEK, seek_args, &syscall_result) ||
        syscall_result != 0) {
        fprintf(stderr, "REMOTE_MEMFD_REWIND_FAILED result=%" PRId64 "\n",
                syscall_result);
        goto cleanup;
    }

    AndroidDlextInfo64 info;
    memset(&info, 0, sizeof(info));
    info.flags = ANDROID_DLEXT_USE_LIBRARY_FD;
    info.relro_fd = -1;
    info.library_fd = remote_memfd;
    if (!write_memory(pid, info_address, &info, sizeof(info))) {
        fprintf(stderr, "REMOTE_INFO_WRITE_FAILED\n");
        goto cleanup;
    }
    uint64_t call_args[3] = {name_address, RTLD_NOW | RTLD_LOCAL, info_address};
    uint64_t handle = 0;
    if (!remote_call(&remote, remote_dlopen, call_args, remote_stack, &handle)) {
        fprintf(stderr, "REMOTE_DLOPEN_CALL_FAILED error=%s\n", strerror(errno));
        goto cleanup;
    }
    if (!handle) {
        fprintf(stderr, "REMOTE_DLOPEN_RETURNED_NULL\n");
        if (remote_dlerror) {
            const uint64_t no_args[3] = {0, 0, 0};
            uint64_t error_address = 0;
            if (remote_call(&remote, remote_dlerror, no_args, remote_stack,
                            &error_address) &&
                error_address) {
                char error_text[512];
                if (read_remote_string(pid, error_address, error_text,
                                       sizeof(error_text)))
                    fprintf(stderr, "REMOTE_DLERROR %s\n", error_text);
            }
        }
        goto cleanup;
    }
    printf("LIBRARY_HANDLE 0x%" PRIx64 "\n", handle);
    success = 1;

cleanup:
    if (remote.prepared && remote_memfd >= 0) {
        uint64_t close_args[6] = {(uint64_t)remote_memfd, 0, 0, 0, 0, 0};
        int64_t ignored = 0;
        (void)remote_syscall(&remote, AARCH64_NR_CLOSE, close_args, &ignored);
    }
    if (remote.prepared && remote_source_fd >= 0) {
        uint64_t close_args[6] = {(uint64_t)remote_source_fd, 0, 0, 0, 0, 0};
        int64_t ignored = 0;
        (void)remote_syscall(&remote, AARCH64_NR_CLOSE, close_args, &ignored);
    }
    restore_remote(&remote);
    detach_all_threads(attached, attached_count);
    printf("THREADS_RESTORED %zu\n", attached_count);
    if (!success) return 6;
    printf("INJECTION_OK transport=shared-memory\n");
    return 0;
}
