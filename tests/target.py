#!/usr/bin/env python3
"""沙盒目标进程：运行在目标命名空间里，代调用方观测该命名空间的视图。

本脚本由 sandbox.py（或 bwrap/unshare 包装）直接启动，**不需要也不会
exec 自己**——创建命名空间是外层（sandbox.py）的事：
  bwrap  模式外层用 bwrap 建沙盒（userns/pidns/mntns），本脚本就是沙盒 init；
  unshare 模式外层用 unshare -Urm 建，本脚本运行其中；
  none   模式不建任何命名空间（进程就在调用者的 mnt ns 里）。

与调用方的通信走两条 FIFO（workdir/req、workdir/resp），内容为 JSON Lines：
  请求行 {"op": ...}   见下方 ops 表，参数都是结构化值（argv 列表、路径），
                       不经 shell 拼接，也就不存在引号转义问题
  响应行 {"ok": true, "out": "..."} / {"ok": false, "error": "..."}

ops:
  run           {"argv": [...]}   以 argv 执行外部命令，回传 stdout+stderr
  read_file     {"path": ...}      读文件内容
  write_file    {"path": ..., "data": ...}  写文件；失败时 error 带错误名
  touch         {"path": ...}      创建空文件（已存在则更新时间戳）
  exists        {"path": ...}      路径是否存在
  mount_opts    {"path": ...}      该路径所在挂载的选项串
  quit          {}                 目标退出

打开顺序（关键）：内外层脚本都以**一致的顺序、阻塞式**打开两条 FIFO
（先 req 后 resp，本脚本先打开、调用方后打开），因此不会死锁，也不再
需要"pid 文件就绪标志"——调用方按同一顺序阻塞 open，等它拿到 resp 的
读端时，本脚本的读写两端必然都已就绪。
"""

import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

WORKDIR = Path(sys.argv[1])


def log(msg):
    """目标侧诊断输出：写到 stderr（不是 resp FIFO），便于失败时排查。"""
    print(f"[target] {msg}", file=sys.stderr, flush=True)


def mount_opts_at(path):
    """读取 path 所在挂载的选项串（取最内层那一条）。

    mountinfo 字段位置固定：第 5 字段是挂载点，第 6 字段是挂载选项。
    同一路径可能出现多层（后出现的更深层），取最后一条，也就是真正
    承载该路径的那个挂载。空字符串表示该路径不是挂载点。
    """
    opts = ""
    with open("/proc/self/mountinfo", encoding="utf-8") as f:
        for line in f:
            fields = line.split()
            if len(fields) > 5 and fields[4] == path:
                opts = fields[5]
    return opts


def handle(req):
    """执行一条请求，返回响应 dict。

    各 op 都用 python 直接实现（argv 命令除外）：不经 shell，也就没有
    引号转义和 locale 差异问题；失败以 error 字段回传 errno 名。
    """
    op = req.get("op")
    if op == "quit":
        return {"op": "quit"}
    if op == "run":
        # stderr 重定向进 stdout（标准做法：stderr=subprocess.STDOUT），
        # 回传的 out 语义与 shell 的 2>&1 一致。
        p = subprocess.run(
            req["argv"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        return {"ok": p.returncode == 0, "out": p.stdout}
    if op == "read_file":
        try:
            out = Path(req["path"]).read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": errno.errorcode.get(exc.errno, "?")}
        return {"ok": True, "out": out}
    if op == "write_file":
        try:
            Path(req["path"]).write_text(req["data"], encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": errno.errorcode.get(exc.errno, "?")}
        return {"ok": True, "out": ""}
    if op == "touch":
        try:
            # O_CREAT 而不截断：已存在的文件保持内容不变
            fd = os.open(req["path"], os.O_CREAT | os.O_WRONLY, 0o644)
            os.close(fd)
        except OSError as exc:
            return {"ok": False, "error": errno.errorcode.get(exc.errno, "?")}
        return {"ok": True, "out": ""}
    if op == "exists":
        return {"ok": True, "out": os.path.exists(req["path"])}
    if op == "mount_opts":
        return {"ok": True, "out": mount_opts_at(req["path"])}
    return {"ok": False, "error": f"unknown op: {op!r}"}


def main():
    req_path = WORKDIR / "req"
    resp_path = WORKDIR / "resp"
    for p in (req_path, resp_path):
        if not stat.S_ISFIFO(os.stat(p).st_mode):
            log(f"missing fifo: {p}")
            return 2

    # 与调用方（sandbox.py 的 Target.start）相同顺序、阻塞式打开。
    # 内置 open 对 FIFO 与 os.open 同样阻塞（open(2) 语义）：读端等写者、
    # 写端等读者。with 保证半关闭时另一端也被关闭；读端的逐行迭代
    # 在调用方关闭写端后自然结束。
    with (
        open(req_path, encoding="utf-8") as reqf,
        open(resp_path, "w", encoding="utf-8") as respf,
    ):
        log("fifos connected, serving requests")
        for line in reqf:
            line = line.strip()
            if not line:
                continue
            try:
                answer = handle(json.loads(line))
            except Exception as exc:  # noqa: BLE001 - 协议兜底
                answer = {"ok": False, "error": f"bad request: {exc!r}"}
            if answer.get("op") == "quit":
                break
            respf.write(json.dumps(answer, ensure_ascii=False) + "\n")
            respf.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
