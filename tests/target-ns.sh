#!/bin/bash
# 测试辅助：在一个独立 mount namespace 中常驻，并通过 USR1 信号回报自己的视图。
#
# 用法：target-ns <workdir>
#   workdir/ready   目标就绪后写入（内容为 pid）
#   workdir/cmd     host 写入一条 shell 命令；收到 USR1 后在目标 ns 内执行
#   workdir/out     目标内执行该命令的 stdout+stderr
#
# 之所以要信号握手，是因为注入的挂载只存在于目标 mount namespace 中，
# 宿主侧 ls 看不到；必须让目标进程自己观察才算真验证。

set -u

workdir=${1:?workdir required}

# 把自己放进独立的 mount namespace：这样它才真正模拟"另一个 mount namespace"
# 里的进程，注入的挂载不会泄漏到调用方视图，隔离性才能被验证。
# 在已持有 CAP_SYS_ADMIN 的 userns 内这是一步，无需额外权限。
if [ "${NSBM_NO_UNSHARE:-0}" != 1 ] && [ -z "${NSBM_UNSHARED:-}" ]; then
    export NSBM_UNSHARED=1
    exec unshare -m --propagation private "$0" "$@"
fi

echo $$ > "$workdir/ready"
run_cmd() {
    local cmd
    cmd=$(cat "$workdir/cmd" 2>/dev/null)
    # 先写临时文件再原子改名，避免请求方读到写了一半的结果。
    {
        echo "--- uid=$(id -u) mntns=$(readlink /proc/self/ns/mnt)"
        eval "$cmd" 2>&1
        echo "--- exit=$?"
    } > "$workdir/out.tmp" 2>&1
    mv -f "$workdir/out.tmp" "$workdir/out"
}

trap run_cmd USR1

# 忙等而非 sleep，避免 sleep 子进程成为 mount 目标路径解析的干扰项。
while :; do
    sleep 0.05
done
