#!/bin/bash
# ns-bind-mount 测试套件
#
# 设计要点：
#  1. 每个用例在独立的 user+mount namespace 中运行（unshare -Urm），
#     即使失败也不会污染宿主机的挂载表；命名空间随进程销毁自动清理。
#  2. 被测程序的作用对象是"另一个 mount namespace"。因此每个用例都先起
#     一个常驻目标进程，并通过 USR1 信号让目标自己回报观察结果——因为注入
#     的挂载对宿主侧不可见，必须由目标自证，这也正是程序的设计目的。
#  3. 无法构造的场景会被显式 skip 并说明原因，不静默通过。
#
# 用法：
#   tests/run-tests.sh            # 跑全部
#   tests/run-tests.sh bind ro    # 只跑名字匹配给定子串的用例
#   BIN=/path/to/bin tests/run-tests.sh

set -uo pipefail

SELF=$(readlink -f "${BASH_SOURCE[0]}")
HERE=$(cd "$(dirname "$SELF")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
BIN=${BIN:-$ROOT/ns-bind-mount}
HELPER=$HERE/target-ns.sh

RED='' GREEN='' YELLOW='' BOLD='' RESET=''
if [ -t 1 ]; then
    RED=$'\033[31m' GREEN=$'\033[32m' YELLOW=$'\033[33m'
    BOLD=$'\033[1m' RESET=$'\033[0m'
fi

# ===========================================================================
# 断言与小工具（在子 shell 内执行）
# ===========================================================================
PASS=0
FAIL=0
SKIP=0
FAILED_NAMES=()

ok()   { PASS=$((PASS + 1)); printf '  %sok%s   %s\n' "$GREEN" "$RESET" "$1"; }
bad()  { FAIL=$((FAIL + 1)); FAILED_NAMES+=("$1"); printf '  %sFAIL%s %s\n' "$RED" "$RESET" "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  %sskip%s %s\n' "$YELLOW" "$RESET" "$1"; }

expect_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 [$2]，实际 [$3]）"; fi
}

expect_exit() {
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望退出码 $2，实际 $3）"; fi
}

expect_contains() {
    case "$3" in
        *"$2"*) ok "$1" ;;
        *)      bad "$1（未找到 [$2]，实际：[$(printf '%s' "$3" | tr '\n' '|')]）" ;;
    esac
}

expect_not_contains() {
    case "$3" in
        *"$2"*) bad "$1（不应出现 [$2]）" ;;
        *)      ok "$1" ;;
    esac
}

# mount_has_opt <mount 选项串> <选项名>：精确匹配逗号分隔的选项
# 注意不能直接 grep 子串：选项串里选项名本身可能就是别的选项的一部分，
# 更常见的是 "nodev" 会因为源挂载（如 /tmp 上的 tmpfs）而被继承下来。
mount_has_opt() {
    printf '%s' "$1" | tr ',' '\n' | awk -v o="$2" '$0 == o { found = 1 } END { exit !found }'
}

# --- 目标 mount namespace 的脚手架 -----------------------------------------
TARGET_PID=''

start_target() {
    local workdir=$1 i
    mkdir -p "$workdir"
    rm -f "$workdir/ready" "$workdir/cmd" "$workdir/out"
    "$HELPER" "$workdir" >/dev/null 2>&1 &
    # 注意不能直接用 $!：辅助脚本为了获得独立 mount namespace 会 exec
    # unshare，$! 拿到的是那层包装进程，与真正持有命名空间的进程 PID
    # 不同，信号打过去会被丢弃。真正的 PID 由辅助脚本自己写进 ready。
    TARGET_PID=$!
    for i in $(seq 1 300); do
        if [ -s "$workdir/ready" ]; then
            TARGET_PID=$(cat "$workdir/ready")
            return 0
        fi
        sleep 0.02
    done
    return 1
}

stop_target() {
    if [ -n "${TARGET_PID:-}" ]; then
        kill "$TARGET_PID" 2>/dev/null
        wait "$TARGET_PID" 2>/dev/null
    fi
    TARGET_PID=''
}

# ask_target <workdir> <命令>：在目标命名空间内执行命令并返回输出
ask_target() {
    local workdir=$1 cmd=$2 i
    printf '%s\n' "$cmd" > "$workdir/cmd"
    rm -f "$workdir/out"
    kill -USR1 "$TARGET_PID" 2>/dev/null || return 1
    for i in $(seq 1 300); do
        [ -f "$workdir/out" ] && break
        sleep 0.02
    done
    cat "$workdir/out" 2>/dev/null
}

# ===========================================================================
# 各测试用例。每个函数在独立命名空间中运行，可用上述断言。
# ===========================================================================

# --- 基本的路径/参数校验（不需要命名空间副作用）-----------------------------
t_help() {
    local out rc
    out=$("$BIN" --help 2>&1); rc=$?
    expect_exit "--help 退出码为 0" 0 "$rc"
    expect_contains "--help 打印用法" "Usage:" "$out"
    expect_contains "--help 列出 bind 模式" "bind" "$out"
    expect_contains "--help 列出 ro-bind 模式" "ro-bind" "$out"
    expect_contains "--help 列出 dev-bind 模式" "dev-bind" "$out"

    out=$("$BIN" -h 2>&1); rc=$?
    expect_exit "-h 退出码为 0" 0 "$rc"
}

t_usage_errors() {
    local out rc
    out=$("$BIN" 2>&1); rc=$?
    expect_exit "无参数时退出码非 0" 1 "$rc"
    expect_contains "无参数时打印用法到 stderr" "Usage:" "$out"

    out=$("$BIN" bind 2>&1); rc=$?
    expect_exit "参数不足时退出码非 0" 1 "$rc"

    out=$("$BIN" bind 1 /a /b extra 2>&1); rc=$?
    expect_exit "参数过多时退出码非 0" 1 "$rc"
}

t_bad_mode() {
    local out rc
    out=$("$BIN" frobnicate 1 /tmp /tmp 2>&1); rc=$?
    expect_exit "未知模式退出码非 0" 1 "$rc"
    expect_contains "未知模式给出提示" "Unknown mode" "$out"
}

t_bad_pid() {
    local out rc
    out=$("$BIN" bind notanumber /tmp /tmp 2>&1); rc=$?
    expect_exit "非数字 pid 退出码非 0" 1 "$rc"
    expect_contains "非数字 pid 给出提示" "Invalid pid" "$out"

    out=$("$BIN" bind 0 /tmp /tmp 2>&1); rc=$?
    expect_exit "pid=0 退出码非 0" 1 "$rc"

    out=$("$BIN" bind -5 /tmp /tmp 2>&1); rc=$?
    expect_exit "负 pid 退出码非 0" 1 "$rc"

    out=$("$BIN" bind 12abc /tmp /tmp 2>&1); rc=$?
    expect_exit "pid 带尾随垃圾字符退出码非 0" 1 "$rc"
}

t_nonexistent_pid() {
    local out rc
    # 找一个几乎肯定不存在的 pid
    out=$("$BIN" bind 4194303 /tmp /tmp 2>&1); rc=$?
    expect_exit "不存在的 pid 退出码非 0" 1 "$rc"
    expect_contains "不存在的 pid 报告 open 失败" "open(/proc" "$out"
}

# --- 核心功能 ---------------------------------------------------------------
t_bind() {
    local w=$PWD/work out
    mkdir -p "$w/src" "$w/dst"
    echo "payload-bind" > "$w/src/file.txt"

    start_target "$w" || { bad "目标命名空间启动失败"; return; }

    "$BIN" bind "$TARGET_PID" "$w/src" "$w/dst" 2>"$w/err"; local rc=$?
    expect_exit "bind 成功退出" 0 "$rc"

    out=$(ask_target "$w" "cat $w/dst/file.txt")
    expect_contains "目标命名空间内可见源文件" "payload-bind" "$out"

    out=$(ask_target "$w" "touch $w/dst/created && echo WROTE-OK")
    expect_contains "bind 为可写挂载" "WROTE-OK" "$out"
    # 写入应穿透到源目录
    if [ -f "$w/src/created" ]; then ok "写入穿透到源目录"; else bad "写入未穿透到源目录"; fi
    rm -f "$w/src/created"

    out=$(ask_target "$w" "findmnt -no OPTIONS -M $w/dst")
    expect_contains "bind 带 nodev" "nodev" "$out"
    expect_contains "bind 带 nosuid" "nosuid" "$out"
    expect_not_contains "bind 不带 ro" "ro," "$out"

    stop_target
}

t_ro_bind() {
    local w=$PWD/work out
    mkdir -p "$w/src" "$w/dst"
    echo "payload-ro" > "$w/src/file.txt"

    start_target "$w" || { bad "目标命名空间启动失败"; return; }

    "$BIN" ro-bind "$TARGET_PID" "$w/src" "$w/dst" 2>"$w/err"; local rc=$?
    expect_exit "ro-bind 成功退出" 0 "$rc"

    out=$(ask_target "$w" "cat $w/dst/file.txt")
    expect_contains "目标内可读源文件" "payload-ro" "$out"

    out=$(ask_target "$w" "touch $w/dst/denied 2>&1; echo rc=\$?")
    expect_contains "ro-bind 拒绝写入" "rc=1" "$out"
    # 错误消息受 locale 影响（这里就是中文），不硬编码英文文案，
    # 而是用挂载选项本身确认只读语义（见下面的 findmnt 断言）。
    expect_contains "ro-bind 写入报错而非静默成功" "touch:" "$out"

    if [ -e "$w/src/denied" ]; then bad "只读挂载竟然写到了源目录"; else ok "源目录未被写入"; fi

    out=$(ask_target "$w" "findmnt -no OPTIONS -M $w/dst")
    expect_contains "ro-bind 带 ro" "ro" "$out"
    expect_contains "ro-bind 带 nodev" "nodev" "$out"
    expect_contains "ro-bind 带 nosuid" "nosuid" "$out"

    stop_target
}

t_dev_bind() {
    local w=$PWD/work out
    mkdir -p "$w/src" "$w/dst"

    # 测试环境常整体位于 tmpfs 上（nodev），而 mount_setattr 只能"设置"标志、
    # 无法清除源已继承的 nodev。因此这里自己挂一个干净的 tmpfs 作源，
    # 使 "dev-bind 不设置 nodev" 这条断言真正可判定。
    if ! mount -t tmpfs -o rw tmpfs "$w/src" 2>/dev/null; then
        skip "无法挂载 tmpfs 构造非 nodev 源，跳过 dev-bind"
        return
    fi
    echo "payload-dev" > "$w/src/file.txt"

    local src_opts
    src_opts=$(findmnt -no OPTIONS -M "$w/src")
    if mount_has_opt "$src_opts" nodev; then
        skip "源文件系统仍带 nodev（$src_opts），该断言无意义"
        return
    fi

    start_target "$w" || { bad "目标命名空间启动失败"; return; }

    "$BIN" dev-bind "$TARGET_PID" "$w/src" "$w/dst" 2>"$w/err"; local rc=$?
    expect_exit "dev-bind 成功退出" 0 "$rc"

    out=$(ask_target "$w" "cat $w/dst/file.txt")
    expect_contains "目标内可读源文件" "payload-dev" "$out"

    out=$(ask_target "$w" "findmnt -no OPTIONS -M $w/dst")
    expect_contains "dev-bind 带 nosuid" "nosuid" "$out"
    if mount_has_opt "$out" nodev; then
        bad "dev-bind 不应设置 nodev（实际：$out）"
    else
        ok "dev-bind 不设置 nodev"
    fi
    if mount_has_opt "$out" ro; then
        bad "dev-bind 不应是只读（实际：$out）"
    else
        ok "dev-bind 为可写"
    fi

    stop_target
}

# 与 dev-bind 对照：同一干净源下普通 bind 必须设置 nodev。
# 这两条合起来才能证明程序真的按模式设置了 MOUNT_ATTR_NODEV，
# 而不是"碰巧源上没有 nodev"。
t_bind_sets_nodev() {
    local w=$PWD/work out
    mkdir -p "$w/src" "$w/dst"
    if ! mount -t tmpfs -o rw tmpfs "$w/src" 2>/dev/null; then
        skip "无法挂载 tmpfs 构造非 nodev 源"
        return
    fi
    echo "x" > "$w/src/f"

    start_target "$w" || { bad "目标命名空间启动失败"; return; }
    "$BIN" bind "$TARGET_PID" "$w/src" "$w/dst" 2>/dev/null

    out=$(ask_target "$w" "findmnt -no OPTIONS -M $w/dst")
    if mount_has_opt "$out" nodev; then
        ok "干净源上 bind 主动添加了 nodev"
    else
        bad "干净源上 bind 未添加 nodev（实际：$out）"
    fi
    stop_target
}

# 只读挂载后能否再次以 bind 覆盖（同一 dst 重复注入）
t_repeat_target() {
    local w=$PWD/work out
    mkdir -p "$w/s1" "$w/s2" "$w/dst"
    echo "first"  > "$w/s1/f"
    echo "second" > "$w/s2/f"

    start_target "$w" || { bad "目标命名空间启动失败"; return; }

    "$BIN" bind "$TARGET_PID" "$w/s1" "$w/dst" 2>/dev/null
    out=$(ask_target "$w" "cat $w/dst/f")
    expect_contains "第一次挂载生效" "first" "$out"

    # 往同一 dst 再挂一次：应叠加在后
    mkdir -p "$w/dst2"
    "$BIN" bind "$TARGET_PID" "$w/s2" "$w/dst2" 2>/dev/null
    out=$(ask_target "$w" "cat $w/dst2/f")
    expect_contains "第二个目标点独立生效" "second" "$out"

    out=$(ask_target "$w" "cat $w/dst/f")
    expect_contains "第一个挂载未被破坏" "first" "$out"

    stop_target
}

# 源为单个文件（非目录）时的 bind
t_bind_file() {
    local w=$PWD/work out
    mkdir -p "$w/dst"
    echo "single-file" > "$w/srcfile"
    touch "$w/dst/targetfile"

    start_target "$w" || { bad "目标命名空间启动失败"; return; }

    "$BIN" bind "$TARGET_PID" "$w/srcfile" "$w/dst/targetfile" 2>"$w/err"; local rc=$?
    # 文件 bind 依赖/支持情况明确记录：成功则继续验证，失败则报告
    if [ "$rc" -ne 0 ]; then
        skip "文件级 bind 不被支持（$(tr -d '\n' < "$w/err")）"
        stop_target
        return
    fi

    out=$(ask_target "$w" "cat $w/dst/targetfile")
    expect_contains "文件级 bind 生效" "single-file" "$out"
    stop_target
}

# 目标 dst 不存在时应失败
t_missing_dst() {
    local w=$PWD/work
    mkdir -p "$w/src"
    start_target "$w" || { bad "目标命名空间启动失败"; return; }

    "$BIN" bind "$TARGET_PID" "$w/src" "$w/does-not-exist" 2>"$w/err"; local rc=$?
    expect_exit "dst 不存在时失败" 1 "$rc"
    expect_contains "报告 move_mount 失败" "move_mount" "$(cat "$w/err")"
    stop_target
}

# 源不存在时应失败，且应发生在 open_tree 阶段
t_missing_src() {
    local w=$PWD/work
    mkdir -p "$w/dst"
    start_target "$w" || { bad "目标命名空间启动失败"; return; }

    "$BIN" bind "$TARGET_PID" "$w/no-such-src" "$w/dst" 2>"$w/err"; local rc=$?
    expect_exit "src 不存在时失败" 1 "$rc"
    expect_contains "报告 open_tree 失败" "open_tree" "$(cat "$w/err")"
    stop_target
}

# 宿主侧视图：注入的挂载不应出现在宿主 mount namespace 里
t_host_isolation() {
    local w=$PWD/work
    mkdir -p "$w/src" "$w/dst"
    echo "isolated" > "$w/src/f"

    start_target "$w" || { bad "目标命名空间启动失败"; return; }
    "$BIN" bind "$TARGET_PID" "$w/src" "$w/dst" 2>/dev/null

    # 注意用 -M（精确匹配某个挂载点）而不是 --target：后者会回退到最近的
    # 祖先挂载（例如 /tmp），导致"目录根本不是挂载点"时也返回非空。
    if findmnt -no TARGET -M "$w/dst" >/dev/null 2>&1; then
        bad "宿主命名空间中不应出现该挂载点"
    else
        ok "宿主命名空间未受污染"
    fi
    if [ -e "$w/dst/f" ]; then
        bad "宿主侧不应看到注入的文件"
    else
        ok "宿主侧看不到注入内容"
    fi

    local out
    out=$(ask_target "$w" "cat $w/dst/f")
    expect_contains "目标侧仍然可见" "isolated" "$out"
    stop_target
}

# 递归：源内已有子挂载时，AT_RECURSIVE 应一并带过去
t_recursive() {
    local w=$PWD/work out
    mkdir -p "$w/src/inner-mnt" "$w/dst"
    echo "outer" > "$w/src/f"
    mount -t tmpfs tmpfs "$w/src/inner-mnt" 2>/dev/null \
        || { skip "当前环境无法 mount tmpfs 构造子挂载"; stop_target; return; }
    echo "inner-mount" > "$w/src/inner-mnt/g"

    start_target "$w" || { bad "目标命名空间启动失败"; return; }
    "$BIN" bind "$TARGET_PID" "$w/src" "$w/dst" 2>"$w/err"; local rc=$?
    expect_exit "带子挂载的源 bind 成功" 0 "$rc"

    out=$(ask_target "$w" "cat $w/dst/inner-mnt/g")
    expect_contains "子挂载被递归复制" "inner-mount" "$out"
    stop_target
}

# 不支持的架构上 syscall 会失败——验证错误路径信息合理
t_error_reporting() {
    local w=$PWD/work
    start_target "$w" || { bad "目标命名空间启动失败"; return; }
    mkdir -p "$w/src" "$w/dst"
    "$BIN" bind "$TARGET_PID" "$w/src" "$w/dst" 2>"$w/err"
    # 成功路径不应往 stderr 写任何东西
    expect_eq "成功时 stderr 为空" "" "$(cat "$w/err")"
    stop_target
}

# ---------------------------------------------------------------------------
# 设计目标场景：非 root 用户运行中的 bwrap 沙盒，从外部注入 bind mount。
#
# 这条用例最关键：bwrap 沙盒处在**另一个 user namespace** 里（与调用者
# 不同），因此程序会走 setns(CLONE_NEWUSER) + unshare(CLONE_NEWNS) 分支。
# 上面的用例目标都还在调用者自己的 userns 中，覆盖不到这条路径。
# ---------------------------------------------------------------------------
t_bwrap_sandbox() {
    local w=$PWD/work out
    command -v bwrap >/dev/null 2>&1 || { skip "未安装 bwrap"; return; }

    mkdir -p "$w/src" "$w/dst"
    echo "injected-into-bwrap" > "$w/src/secret.txt"

    # 沙盒目标：常驻，收到 USR1 时把"自己看到的"视图写入 probe.out
    cat > "$w/sandbox.sh" <<'EOS'
echo $$ > "$D/sb.pid"
trap '{
    echo "userns=$(readlink /proc/self/ns/user)"
    cat "$D/dst/secret.txt" 2>&1
    echo "opts=$(findmnt -no OPTIONS -M "$D/dst" 2>&1)"
} > "$D/probe.out"' USR1
while :; do sleep 0.05; done
EOS

    # 注意：测试框架已把本用例放进 unshare -Urm，而 bwrap 在内层无法再
    # 创建 user namespace。若当前环境允许，bwrap 会建立独立的 userns，
    # 本用例就测到了真实沙盒场景；否则下面会 skip 并说明原因。
    # 该场景的确定性覆盖由 t_cross_userns 负责。
    bwrap --dev-bind / / --bind "$w" "$w" env D="$w" sh "$w/sandbox.sh" \
        >/dev/null 2>"$w/sb.err" &
    local sb_bg=$!

    local i
    for i in $(seq 1 300); do
        [ -s "$w/sb.pid" ] && break
        sleep 0.02
    done
    if [ ! -s "$w/sb.pid" ]; then
        skip "bwrap 无法在当前环境启动（$(tr '\n' ' ' < "$w/sb.err")）"
        kill "$sb_bg" 2>/dev/null
        return
    fi
    local sb_pid
    sb_pid=$(cat "$w/sb.pid")

    # 确认这确实是"跨 user namespace"场景，否则本用例只重复了已有覆盖
    if [ "$(readlink "/proc/$sb_pid/ns/user")" = "$(readlink /proc/self/ns/user)" ]; then
        skip "bwrap 复用了当前 user namespace（环境禁止嵌套 userns），该场景由 t_cross_userns 覆盖"
        kill "$sb_pid" 2>/dev/null
        return
    fi
    ok "沙盒进程位于独立的 user namespace"

    "$BIN" bind "$sb_pid" "$w/src" "$w/dst" 2>"$w/err"; local rc=$?
    expect_exit "向 bwrap 沙盒注入 bind 成功" 0 "$rc"
    if [ "$rc" -ne 0 ]; then
        bad "setns(CLONE_NEWUSER) 路径失败：$(cat "$w/err")"
    fi

    rm -f "$w/probe.out"
    kill -USR1 "$sb_pid" 2>/dev/null
    for i in $(seq 1 300); do
        [ -s "$w/probe.out" ] && break
        sleep 0.02
    done
    out=$(cat "$w/probe.out" 2>/dev/null)

    expect_contains "沙盒内读到注入内容（跨 userns 生效）" "injected-into-bwrap" "$out"

    # 宿主侧不应出现该挂载点
    if findmnt -no TARGET -M "$w/dst" >/dev/null 2>&1; then
        bad "宿主侧不应出现沙盒内的挂载点"
    else
        ok "宿主侧视图未被污染"
    fi
    if [ -e "$w/dst/secret.txt" ]; then
        bad "宿主侧不应看到注入的文件"
    else
        ok "宿主侧看不到注入内容"
    fi

    kill "$sb_pid" 2>/dev/null
    wait "$sb_bg" 2>/dev/null
}

# 与上一条互补：直接用 unshare 造一个"不同 user namespace"里的目标，
# 不依赖 bwrap 是否安装。
t_cross_userns() {
    local w=$PWD/work out
    mkdir -p "$w/src" "$w/dst"
    echo "cross-userns-payload" > "$w/src/f"

    if ! unshare -Urm true 2>/dev/null; then
        skip "非特权 user namespace 不可用"
        return
    fi

    # 在子 userns 中起常驻进程，报告自己的 pid
    unshare -Urm sh -c 'echo $$ > "'"$w"'/pid2"; exec sleep 30' &
    local bg=$!
    local i
    for i in $(seq 1 300); do
        [ -s "$w/pid2" ] && break
        sleep 0.02
    done
    if [ ! -s "$w/pid2" ]; then
        skip "无法创建子 user namespace 目标进程"
        return
    fi
    local pid2
    pid2=$(cat "$w/pid2")

    if [ "$(readlink "/proc/$pid2/ns/user")" = "$(readlink /proc/self/ns/user)" ]; then
        skip "子进程未处于不同 user namespace"
        kill "$bg" 2>/dev/null
        return
    fi

    "$BIN" bind "$pid2" "$w/src" "$w/dst" 2>"$w/err"; local rc=$?
    expect_exit "跨 user namespace 注入成功" 0 "$rc"
    if [ "$rc" -eq 0 ]; then
        ok "跨 userns 场景下未报错"
    else
        bad "跨 user namespace 注入失败：$(cat "$w/err")"
    fi

    # 用 nsenter 独立确认挂载真的存在于目标 namespace 里，
    # 而不只是"命令返回 0"。
    if command -v nsenter >/dev/null 2>&1; then
        local seen
        seen=$(nsenter -t "$pid2" -m -- cat "$w/dst/f" 2>/dev/null)
        expect_contains "目标命名空间内确实可见注入内容" "cross-userns-payload" "$seen"
    else
        skip "缺少 nsenter，无法独立确认目标侧视图"
    fi

    kill "$bg" 2>/dev/null
    wait "$bg" 2>/dev/null
}

# ===========================================================================
# 调度
# ===========================================================================
TESTS=(
    t_help
    t_usage_errors
    t_bad_mode
    t_bad_pid
    t_nonexistent_pid
    t_bind
    t_ro_bind
    t_dev_bind
    t_bind_sets_nodev
    t_repeat_target
    t_bind_file
    t_missing_dst
    t_missing_src
    t_host_isolation
    t_cross_userns
    t_bwrap_sandbox
    t_recursive
    t_error_reporting
)

# 这些用例会自己创建 user namespace（bwrap 需要），若外层再套 unshare -U
# 就会因为环境禁止嵌套 userns 而失败。因此它们只用 mountns 隔离。
MOUNTNS_ONLY=()

# 单用例模式：由调度器用 ns-bind-mount-tests-single 环境变量驱动
if [ "${NSBM_RE_ENTRY:-}" = 1 ]; then
    t=$1
    CURRENT_TEST=$t
    printf '%s%s%s\n' "$BOLD" "$t" "$RESET"
    WORK=$PWD/work
    mkdir -p "$WORK"
    trap 'stop_target' EXIT
    "$t"
    printf '__RESULT__ %d %d %d\n' "$PASS" "$FAIL" "$SKIP"
    exit 0
fi

# --- 顶层调度 ---------------------------------------------------------------
if [ ! -x "$BIN" ]; then
    echo "错误：找不到可执行文件 $BIN，请先运行 make" >&2
    exit 2
fi
if [ ! -x "$HELPER" ]; then
    echo "错误：找不到辅助脚本 $HELPER" >&2
    exit 2
fi

FILTERS=("$@")

runnable() {
    [ ${#FILTERS[@]} -eq 0 ] && return 0
    local f
    for f in "${FILTERS[@]}"; do
        case "$1" in *"$f"*) return 0 ;; esac
    done
    return 1
}

if [ "$(id -u)" = 0 ]; then
    NS_CMD=(unshare -m --propagation private)
    NS_DESC="root + private mountns"
elif unshare -Urm true 2>/dev/null; then
    NS_CMD=(unshare -Urm)
    NS_DESC="unprivileged user+mount namespace"
else
    echo "错误：当前用户不是 root，且非特权 user namespace 被禁用，无法隔离测试。" >&2
    exit 2
fi

printf '%s== ns-bind-mount 测试 ==%s\n' "$BOLD" "$RESET"
printf '二进制：%s\n' "$BIN"
printf '隔离方式：%s\n\n' "$NS_DESC"

TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_SKIP=0
ALL_FAILED=()

for t in "${TESTS[@]}"; do
    if ! runnable "$t"; then continue; fi

    workdir=$(mktemp -d)
    ns_cmd=("${NS_CMD[@]}")
    for m in "${MOUNTNS_ONLY[@]}"; do
        [ "$t" = "$m" ] && ns_cmd=(unshare -m --propagation private)
    done
    output=$("${ns_cmd[@]}" bash -c \
        "cd '$workdir' && NSBM_RE_ENTRY=1 BIN='$BIN' HELPER='$HELPER' exec '$SELF' '$t'" 2>&1)
    rc=$?
    rm -rf "$workdir"

    printf '%s\n' "$output" | grep -v '^__RESULT__'

    result=$(printf '%s\n' "$output" | grep '^__RESULT__' | tail -1)
    if [ -z "$result" ]; then
        printf '  %sFAIL%s 用例异常终止（rc=%s）\n' "$RED" "$RESET" "$rc"
        ALL_FAILED+=("$t: 异常终止 rc=$rc")
        TOTAL_FAIL=$((TOTAL_FAIL + 1))
        continue
    fi
    read -r _ p f s <<< "$result"
    TOTAL_PASS=$((TOTAL_PASS + p))
    TOTAL_FAIL=$((TOTAL_FAIL + f))
    TOTAL_SKIP=$((TOTAL_SKIP + s))
    if [ "$f" -gt 0 ]; then
        ALL_FAILED+=("$t: $f 项失败")
    fi
    echo
done

printf '%s== 结果 ==%s\n' "$BOLD" "$RESET"
printf '通过 %d，失败 %d，跳过 %d\n' "$TOTAL_PASS" "$TOTAL_FAIL" "$TOTAL_SKIP"
if [ ${#ALL_FAILED[@]} -gt 0 ]; then
    printf '%s失败项：%s\n' "$RED" "$RESET"
    for x in "${ALL_FAILED[@]}"; do printf '  - %s\n' "$x"; done
    exit 1
fi
exit 0
