#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/mount.h>
#include <linux/nsfs.h>
#include <sched.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef __NR_open_tree
#define __NR_open_tree 428
#endif

#ifndef __NR_move_mount
#define __NR_move_mount 429
#endif

#ifndef __NR_mount_setattr
#define __NR_mount_setattr 442
#endif

#ifndef OPEN_TREE_CLONE
#define OPEN_TREE_CLONE 1
#endif

#ifndef OPEN_TREE_CLOEXEC
#define OPEN_TREE_CLOEXEC O_CLOEXEC
#endif

#ifndef AT_EMPTY_PATH
#define AT_EMPTY_PATH 0x1000
#endif

#ifndef AT_RECURSIVE
#define AT_RECURSIVE 0x8000
#endif

#ifndef MOVE_MOUNT_F_EMPTY_PATH
#define MOVE_MOUNT_F_EMPTY_PATH 0x00000004
#endif

#ifndef NS_GET_USERNS
#define NS_GET_USERNS _IO(0xb7, 0x1)
#endif

static inline int sys_open_tree(int dfd, const char *filename, unsigned int flags)
{
    return (int)syscall(__NR_open_tree, dfd, filename, flags);
}

static inline int sys_mount_setattr(int dfd, const char *path, unsigned int flags,
                                    struct mount_attr *attr, size_t size)
{
    return (int)syscall(__NR_mount_setattr, dfd, path, flags, attr, size);
}

static inline int sys_move_mount(int from_dfd, const char *from_pathname,
                                 int to_dfd, const char *to_pathname,
                                 unsigned int flags)
{
    return (int)syscall(__NR_move_mount, from_dfd, from_pathname,
                        to_dfd, to_pathname, flags);
}

static int same_ns(int fd_a, const char *path_b)
{
    struct stat st_a, st_b;
    if (fstat(fd_a, &st_a) < 0)
        return -1;
    if (stat(path_b, &st_b) < 0)
        return -1;
    return (st_a.st_dev == st_b.st_dev && st_a.st_ino == st_b.st_ino);
}

static int open_ns_fd(pid_t pid, const char *ns_name)
{
    char path[PATH_MAX];
    int n = snprintf(path, sizeof(path), "/proc/%d/ns/%s", pid, ns_name);
    if (n < 0 || (size_t)n >= sizeof(path)) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return open(path, O_RDONLY | O_CLOEXEC);
}

static void usage(const char *prog, FILE *out)
{
    fprintf(out,
            "Usage: %s <bind|ro-bind|dev-bind> <pid> <srcpath> <dstpath>\n\n"
            "Modes:\n"
            "  bind      Bind mount srcpath on dstpath (nosuid, nodev, recursive)\n"
            "  ro-bind   Read-only bind mount srcpath on dstpath (ro, nosuid, nodev, recursive)\n"
            "  dev-bind  Bind mount srcpath on dstpath allowing devices (nosuid, recursive)\n",
            prog);
}

int main(int argc, char *argv[])
{
    if (argc == 2 && (strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0)) {
        usage(argv[0], stdout);
        return EXIT_SUCCESS;
    }

    if (argc != 5) {
        usage(argv[0], stderr);
        return EXIT_FAILURE;
    }

    const char *mode = argv[1];
    bool readonly = false;
    bool devices = false;

    if (strcmp(mode, "bind") == 0) {
        /* readonly = false, devices = false */
    } else if (strcmp(mode, "ro-bind") == 0) {
        readonly = true;
    } else if (strcmp(mode, "dev-bind") == 0) {
        devices = true;
    } else {
        fprintf(stderr, "Unknown mode: %s\n", mode);
        usage(argv[0], stderr);
        return EXIT_FAILURE;
    }

    char *endptr = NULL;
    errno = 0;
    long pid_val = strtol(argv[2], &endptr, 10);
    if (errno != 0 || endptr == argv[2] || *endptr != '\0' || pid_val <= 0) {
        fprintf(stderr, "Invalid pid: %s\n", argv[2]);
        return EXIT_FAILURE;
    }
    pid_t pid = (pid_t)pid_val;
    const char *srcpath = argv[3];
    const char *dstpath = argv[4];

    int ret = EXIT_FAILURE;
    int fd_mntns = -1;
    int fd_owner_userns = -1;
    int fd_mnt = -1;

    fd_mntns = open_ns_fd(pid, "mnt");
    if (fd_mntns < 0) {
        perror("open(/proc/<pid>/ns/mnt)");
        goto out;
    }

    /*
     * 1. 通过 NS_GET_USERNS 获取拥有目标 mount namespace 的 user namespace
     *    （即便沙盒嵌套了多层 userns，也能直接定位到负责挂载的那一层）。
     */
    fd_owner_userns = ioctl(fd_mntns, NS_GET_USERNS);
    if (fd_owner_userns < 0) {
        perror("ioctl(NS_GET_USERNS)");
        goto out;
    }

    int is_same_userns = same_ns(fd_owner_userns, "/proc/self/ns/user");
    if (is_same_userns < 0) {
        perror("stat userns");
        goto out;
    }

    /*
     * 2. 若目标 mountns 属于另一个 userns：
     *    - 先切入该 userns（同 UID 创建者进入后自动获得其中的全套 capabilities）；
     *    - 再 unshare(CLONE_NEWNS) 创建一个归属于该 userns 的临时 mountns，
     *      以便在保留宿主机路径视图的同时合法调用 open_tree(OPEN_TREE_CLONE)。
     */
    if (!is_same_userns) {
        if (setns(fd_owner_userns, CLONE_NEWUSER) < 0) {
            perror("setns(CLONE_NEWUSER)");
            goto out;
        }
        if (unshare(CLONE_NEWNS) < 0) {
            perror("unshare(CLONE_NEWNS)");
            goto out;
        }
    }

    /*
     * 3. 克隆 srcpath 得到游离挂载（detached mount）。
     *    加上 AT_RECURSIVE 以兼容非特权 mountns 中带 MNT_LOCKED 锁的子挂载点。
     */
    fd_mnt = sys_open_tree(AT_FDCWD, srcpath,
                           OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC | AT_RECURSIVE);
    if (fd_mnt < 0) {
        perror("open_tree(OPEN_TREE_CLONE)");
        goto out;
    }

    struct mount_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.attr_set = MOUNT_ATTR_NOSUID |
                    (devices ? 0 : MOUNT_ATTR_NODEV) |
                    (readonly ? MOUNT_ATTR_RDONLY : 0);

    if (sys_mount_setattr(fd_mnt, "", AT_EMPTY_PATH | AT_RECURSIVE,
                          &attr, sizeof(attr)) < 0) {
        perror("mount_setattr");
        goto out;
    }

    /*
     * 4. 切入目标进程的 mount namespace。
     */
    if (setns(fd_mntns, CLONE_NEWNS) < 0) {
        perror("setns(CLONE_NEWNS)");
        goto out;
    }

    /*
     * 5. 将游离挂载附加到目标 mount namespace 中的 dstpath。
     */
    if (sys_move_mount(fd_mnt, "", AT_FDCWD, dstpath, MOVE_MOUNT_F_EMPTY_PATH) < 0) {
        perror("move_mount");
        goto out;
    }

    ret = EXIT_SUCCESS;

out:
    if (fd_mnt >= 0)
        close(fd_mnt);
    if (fd_owner_userns >= 0)
        close(fd_owner_userns);
    if (fd_mntns >= 0)
        close(fd_mntns);
    return ret;
}
