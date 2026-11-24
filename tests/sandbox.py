#!/usr/bin/env python3
"""ns-bind-mount 测试的公共脚手架（沙盒外的驱动侧）。

沙盒外的测试驱动工作都集中在本文件：
  * ns_bind_mount        运行被测二进制注入一个挂载（参数顺序同命令行）；
  * Target               创建并驱动一个目标沙盒（bwrap/unshare/none 三种
                         构造方式），与 target.py 通过 FIFO 通信；
  * run_in_unshare       把"源必须由调用者构造"的用例体放进 unshare 执行；
  * mount_has_opt / mountinfo_opts  挂载选项解析（断言辅助）；
  * mount_tmpfs / 环境探测 / ns_id / pid_max  各种小工具。
沙盒内的观测端是 target.py，那里只做请求分发，不关心自己是怎么被放进
沙盒的。

设计要点：
  1. 大部分功能用例都以 bwrap 建立的沙盒为目标命名空间（程序的设计场景
     就是向运行中的 bwrap 沙盒注入挂载），另以 unshare 建的目标作对照。
     创建命名空间的逻辑就在本文件的 Target._launch：bwrap 与 unshare 是
     替代关系，谁也不套谁；target.py 只负责运行在命名空间里做观测，
     不 exec、不关心自己是怎么被放进沙盒的。
  2. 被测程序作用于"另一个 mount namespace"，注入的挂载对宿主侧不可见，
     必须让目标进程自证。与目标的通信走两条 FIFO 上的 JSON Lines 请求-
     响应：双方只需打开同一个路径，结构化的请求也不存在转义问题。
  3. 打开顺序（关键）：内外层以一致顺序、阻塞式打开两条 FIFO——先
     req（目标读端/调用方写端）再 resp（目标写端/调用方读端）。互补的
     两端交叉配对，因此不会死锁；也不再需要 pid 文件之类的就绪标志：
     调用方 open 返回即意味着两侧都已连好。内置 open 对 FIFO 的阻塞
     语义与 open(2) 一致：读端逐行迭代、写端写后 flush，代码不手工
     管理缓冲。
  4. 注入挂载需要目标的外部 pid（程序打开 /proc/<pid>/ns/mnt）。
     bwrap 模式取 --info-fd 报告的 child-pid：它是沙盒内的常驻进程，
     与沙盒内其它进程共享同一个 mount namespace，可直接作注入锚点；
     unshare / none 模式没有独立 pidns，包装进程的 pid 就是外部 pid。

协议（JSON Lines，详见 tests/target.py）：请求带 argv 列表或专用 op，
不经 shell 拼接，因此不存在引号转义问题。
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SELF_DIR = Path(__file__).resolve().parent
ROOT = SELF_DIR.parent
BIN = os.environ.get("BIN", str(ROOT / "ns-bind-mount"))
TARGET_SCRIPT = SELF_DIR / "target.py"


class TargetError(Exception):
    """目标沙盒启动或通信失败。"""


def ns_bind_mount(bin_path, mode, pid, src, dst):
    """运行被测二进制注入一个挂载。

    参数顺序与命令行一致：<mode> <pid> <srcpath> <dstpath>。
    固定 LC_ALL=C.UTF-8，报错文案可断言。
    """
    env = dict(os.environ, LC_ALL="C.UTF-8")
    return subprocess.run(
        [str(bin_path), mode, str(pid), str(src), str(dst)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
        check=False,
    )


def have_bwrap() -> bool:
    return shutil.which("bwrap") is not None


def have_unshare_userns() -> bool:
    return (
        subprocess.run(
            ["unshare", "-Urm", "true"], check=False, capture_output=True
        ).returncode
        == 0
    )


def mountinfo_opts(path, text=None):
    """读取 path 所在挂载的选项串（取最内层那一条），宿主侧视角。

    mountinfo 字段位置固定：第 5 字段是挂载点、第 6 字段是挂载选项。
    同一路径可能出现多层（后出现的更深层），取最后一条，也就是真正
    承载该路径的那个挂载。空字符串表示该路径不是挂载点。
    """
    if text is None:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    opts = ""
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[4] == path:
            opts = fields[5]
    return opts


def mount_has_opt(opts, name):
    """精确匹配逗号分隔的选项名。

    不能做子串查找：选项名可能恰好是别的选项的一部分，而 "nodev" 之类
    更可能因为源挂载被继承下来。
    """
    return name in (opts or "").split(",")


def ns_id(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def pid_max():
    """当前系统的 pid 上限（/proc/sys/kernel/pid_max）。"""
    return int(Path("/proc/sys/kernel/pid_max").read_text(encoding="ascii"))


def mount_tmpfs(path, opts=()):
    """在当前 mntns 挂一个 tmpfs（需要 CAP_SYS_ADMIN，unshare -Urm 内可用）。

    以 argv 形式调用 mount 命令，不经 shell。
    """
    return subprocess.run(
        ["mount", "-t", "tmpfs", *opts, "tmpfs", str(path)],
        check=False,
        capture_output=True,
    )


class Target:
    """一个常驻在独立 mount namespace 里的目标沙盒。

    mode:
      bwrap   外层用 bwrap 自建 user+pid+mount namespace（主要模式）
      unshare 外层用 unshare -Urm 建（对照模式）
      none    不建任何命名空间（进程就在调用者的 mnt ns 里）

    用法（配套 pytest fixture 见 conftest.py）：
        t = Target(workdir, "bwrap")
        t.start()                  # 或 with Target(...) as t:
        t.run(["cat", "/path"])    # 在目标内以 argv 执行命令
        t.read_file(path)          # 目标内读文件
        t.stop()                   # 或等 with 块结束
    """

    REQ = "req"
    RESP = "resp"

    def __init__(self, workdir, mode="bwrap"):
        if mode not in ("bwrap", "unshare", "none"):
            raise ValueError(f"unknown target mode: {mode}")
        self.workdir = Path(workdir)
        self.mode = mode
        self.pid = None  # 目标进程的外部 pid，注入挂载时使用
        self._proc = None  # 外层包装进程（bwrap / unshare / python）
        # FIFO 两端登记在栈上：Target 本身是上下文管理器（stop / __exit__
        # 关栈），文件的生命周期就是 Target 的生命周期。
        self._fifos = contextlib.ExitStack()

    # -- 启动 ---------------------------------------------------------------

    def _make_fifos(self):
        w = self.workdir
        w.mkdir(parents=True, exist_ok=True)
        for name in (self.REQ, self.RESP):
            fifo = w / name
            if not fifo.exists():
                os.mkfifo(fifo)

    def _target_argv(self):
        """target.py 的启动命令（运行在目标命名空间里）。"""
        return [sys.executable, str(TARGET_SCRIPT), str(self.workdir)]

    def _launch(self):
        """按 mode 启动包装进程；bwrap 模式经 --info-fd 报告 child-pid。"""
        if self.mode == "bwrap":
            # 只要 child-pid，用 --info-fd（一行 JSON）就够了，
            # 不需要 --json-status-fd 的多文档流。
            info_r, info_w = os.pipe()
            try:
                self._proc = subprocess.Popen(
                    [
                        "bwrap",
                        "--unshare-user",
                        "--unshare-pid",
                        "--dev-bind",
                        "/",
                        "/",
                        "--bind",
                        str(self.workdir),
                        str(self.workdir),
                        "--info-fd",
                        str(info_w),
                        *self._target_argv(),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(info_w,),
                )
            finally:
                os.close(info_w)
            with os.fdopen(info_r, encoding="utf-8") as f:
                info = json.loads(f.read())
            self.pid = int(info["child-pid"])
        else:
            argv = (
                ["unshare", "-Urm", *self._target_argv()]
                if self.mode == "unshare"
                else self._target_argv()
            )
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # unshare / none 模式没有独立 pidns，包装进程就是目标进程。
            self.pid = self._proc.pid

    def start(self):
        """启动目标沙盒并完成 FIFO 握手，返回 self。"""
        self._make_fifos()
        self._launch()
        # FIFO 握手：与 target.py 相同顺序、阻塞式打开（先 req 后 resp）。
        # 互补的两端交叉配对，open 返回即意味着两侧都已连好。
        # 内置 open 对 FIFO 与 os.open 同样阻塞（open(2) 语义），
        # 读端逐行迭代、写端写后 flush。两端登记在 ExitStack 上，
        # 由 stop() 统一关闭——它们的生命周期就是 Target 的生命周期。
        self._reqf = self._fifos.enter_context(self._open_fifo(self.REQ, "w"))
        self._respf = self._fifos.enter_context(self._open_fifo(self.RESP))
        return self

    def _open_fifo(self, name, mode="r"):
        """阻塞式打开一条 FIFO 并包成文本文件对象。"""
        return open(self.workdir / name, mode, encoding="utf-8")

    # -- 通信 ---------------------------------------------------------------

    def _roundtrip(self, req):
        """发一条请求并等对应的响应；目标死亡时抛 TargetError。

        不设超时：阻塞读会随目标退出（写端关闭得到 EOF）自然解除，
        挂死即真异常，不需要计时兜底。
        """
        if self._reqf is None:
            raise TargetError("target is not running")
        try:
            self._reqf.write(json.dumps(req) + "\n")
            self._reqf.flush()
        except BrokenPipeError:
            # 目标已死、其 req 读端关闭：等包装进程收尾，把原因带进异常。
            rc = self._wait_exit()
            raise TargetError(
                f"{self.mode} target exited before answering (rc={rc})"
            ) from None
        line = self._respf.readline()
        if not line:
            # EOF：目标已退出。等包装进程收尾，把原因带进异常里。
            rc = self._wait_exit()
            raise TargetError(f"{self.mode} target exited before answering (rc={rc})")
        return json.loads(line)

    def _wait_exit(self, timeout=5):
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            return self._proc.wait(timeout=5)

    # -- 请求封装：每个 op 对应一个方法，调用方无需拼写协议 -----------------

    def run(self, argv):
        """在目标命名空间内以 argv 执行命令，返回 stdout+stderr。"""
        answer = self._roundtrip({"op": "run", "argv": [str(x) for x in argv]})
        if not answer.get("ok"):
            raise TargetError(f"target command failed: {answer.get('out')}")
        return answer.get("out", "")

    def read_file(self, path):
        """在目标命名空间内读取文件内容。"""
        answer = self._roundtrip({"op": "read_file", "path": str(path)})
        if not answer.get("ok"):
            raise TargetError(f"read_file({path}) failed: {answer.get('error')}")
        return answer.get("out", "")

    def write_file(self, path, data):
        """在目标命名空间内写文件；失败抛 TargetError。"""
        answer = self._roundtrip({"op": "write_file", "path": str(path), "data": data})
        if not answer.get("ok"):
            raise TargetError(f"write_file({path}) failed: {answer.get('error')}")

    def try_write_file(self, path, data):
        """同 write_file，但失败不抛异常：返回 (ok, errno 名)。

        写入被拒绝（EROFS/EACCES 等）是部分用例的预期结果，不应当作
        通信错误；errno 名也不受 locale 影响。
        """
        answer = self._roundtrip({"op": "write_file", "path": str(path), "data": data})
        return bool(answer.get("ok")), answer.get("error", "")

    def touch(self, path):
        """在目标命名空间内创建空文件（已存在则保持内容不变）。"""
        answer = self._roundtrip({"op": "touch", "path": str(path)})
        if not answer.get("ok"):
            raise TargetError(f"touch({path}) failed: {answer.get('error')}")

    def exists(self, path):
        """目标命名空间内该路径是否存在。"""
        answer = self._roundtrip({"op": "exists", "path": str(path)})
        if not answer.get("ok"):
            raise TargetError(f"exists({path}) failed: {answer.get('error')}")
        return bool(answer.get("out"))

    def mount_opts(self, path):
        """在目标命名空间内读取 path 所在挂载的选项串。"""
        answer = self._roundtrip({"op": "mount_opts", "path": str(path)})
        if not answer.get("ok"):
            raise TargetError(f"mount_opts({path}) failed: {answer.get('error')}")
        return answer.get("out", "").strip()

    # -- 收尾 ---------------------------------------------------------------

    def stop(self):
        """请求目标退出并等待；超时才强杀。

        直接 kill 包装进程会让沙盒里的子进程变孤儿继续空转，
        所以先走 quit 协议，把强杀留作兜底。FIFO 两端由 ExitStack
        统一关闭。
        """
        if self._reqf is not None:
            try:
                self._reqf.write('{"op": "quit"}\n')
                self._reqf.flush()
            except OSError:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        # 关闭写端可能触发缓冲区 flush，对端已死时会抛 BrokenPipeError；
        # 收尾阶段不关心这个错误，但仍由 ExitStack 保证两端都关掉。
        with contextlib.suppress(OSError):
            self._fifos.close()
        self._reqf = None
        self._respf = None
        self.pid = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def run_in_unshare(module, args, selected=(), timeout=180):
    """把一个"源必须由调用者构造"的用例体放进 unshare -Urm 里执行。

      unshare -Urm python -m <module> <args...> [选中的用例名...]

    用例体模块（clean_source_case.py 等）在 unshare 的 mntns 里自行完成
    源构造（mount_tmpfs 已提供 argv 形式的挂载），再做注入与断言；返回
    非零表示有断言失败。stdout 原样透传（pytest 失败时的输出里可见每条
    ok/FAIL 行）。

    selected 传给用例体做用例过滤（如 ("recursive",)），不传则全跑。

    为什么需要这层 unshare：干净源/子挂载必须挂在"用例执行环境"的
    mntns 里才对 open_tree 的调用者可见——unshare 进程退出挂载就消失；
    而普通环境里挂 tmpfs 只有 unshare -Urm 能提供。目标仍按 mode 由
    bwrap/unshare 构造，与这层 unshare 是替代关系而非叠加。
    """
    argv = [
        "unshare",
        "-Urm",
        sys.executable,
        "-m",
        module,
        *map(str, args),
        *selected,
    ]
    env = dict(os.environ, PYTHONPATH=str(SELF_DIR))
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        check=False,
    )
