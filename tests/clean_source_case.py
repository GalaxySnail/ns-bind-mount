"""干净源用例体：在 unshare -Urm 内部执行（见 test_ns_bind_mount.py 调用方）。

外层（sandbox.run_in_unshare）以
  unshare -Urm python -m clean_source_case <bin> <workdir> [测试名...]
启动本模块。本模块先在 unshare 的 mntns 里挂出干净的 tmpfs 源，再对
按 mode 构造的目标沙盒做注入与断言。

为什么整个用例要搬进 unshare：干净源必须挂在"用例执行环境"的 mntns
里才能被程序看见——unshare 进程退出挂载就消失；而普通环境里挂 tmpfs
只有 unshare -Urm 能提供。目标仍按 mode 由 bwrap/unshare 构造，
与这层 unshare 是替代关系而非叠加。

用例名可通过 argv 过滤（不传则全跑）；每个测试与 pytest 版本同构，
用标准 assert 编写。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sandbox

BIN = sys.argv[1]
WORKDIR = Path(sys.argv[2])
SELECTED = set(sys.argv[3:])


def mount_clean_source(inner=False):
    """挂出干净的 tmpfs 源；inner=True 时再在源下挂一个子挂载。

    挂载点目录不存在时先建（mount 只认已有目录）。
    挂载失败直接以异常终止：没有干净源，后面的断言没有意义。
    """
    clean = WORKDIR / "clean"
    clean.mkdir(exist_ok=True)
    # dst 是注入目标点，move_mount 只认已有路径
    (WORKDIR / "dst").mkdir(exist_ok=True)
    r = sandbox.mount_tmpfs(clean, opts=("-o", "rw"))
    assert r.returncode == 0, f"mount tmpfs at {clean} failed: {r.stderr!r}"
    if inner:
        inner_mnt = clean / "inner-mnt"
        inner_mnt.mkdir(exist_ok=True)
        r = sandbox.mount_tmpfs(inner_mnt)
        assert r.returncode == 0, f"mount tmpfs at {inner_mnt} failed: {r.stderr!r}"
        (inner_mnt / "g").write_text("inner-mount\n", encoding="utf-8")
    return clean


# ---------------------------------------------------------------------------
# 各测试：与 pytest 版本同构，src 一律用干净的 tmpfs（workdir/clean）
# ---------------------------------------------------------------------------


def test_bind_mount_options(mode):
    """干净源上 bind 的挂载参数：主动加 nodev/nosuid，且非只读。

    源干净时，挂载结果里出现的每个标志都只能来自程序自身。
    """
    clean = mount_clean_source()
    dst = WORKDIR / "dst"
    (clean / "f").write_text("x\n", encoding="utf-8")

    with sandbox.Target(WORKDIR, mode) as t:
        r = sandbox.ns_bind_mount(BIN, "bind", t.pid, clean, dst)
        assert r.returncode == 0, r.stderr

        opts = t.mount_opts(dst)
        assert sandbox.mount_has_opt(opts, "nodev"), f"got [{opts}]"
        assert sandbox.mount_has_opt(opts, "nosuid"), f"got [{opts}]"
        assert not sandbox.mount_has_opt(opts, "ro"), f"got [{opts}]"
    print("ok   bind sets nodev/nosuid and stays rw on a clean source")


def test_ro_bind_mount_options(mode):
    """干净源上 ro-bind 的挂载参数：ro + nodev + nosuid。"""
    clean = mount_clean_source()
    dst = WORKDIR / "dst"
    (clean / "f").write_text("x\n", encoding="utf-8")

    with sandbox.Target(WORKDIR, mode) as t:
        r = sandbox.ns_bind_mount(BIN, "ro-bind", t.pid, clean, dst)
        assert r.returncode == 0, r.stderr

        opts = t.mount_opts(dst)
        assert sandbox.mount_has_opt(opts, "ro"), f"got [{opts}]"
        assert sandbox.mount_has_opt(opts, "nodev"), f"got [{opts}]"
        assert sandbox.mount_has_opt(opts, "nosuid"), f"got [{opts}]"
    print("ok   ro-bind sets ro/nodev/nosuid on a clean source")


def test_dev_bind_mount_options(mode):
    """干净源上 dev-bind 的挂载参数：加 nosuid，不加 nodev，非只读。

    这是"dev-bind 不设 nodev"的可判定证明。
    """
    clean = mount_clean_source()
    dst = WORKDIR / "dst"
    (clean / "f").write_text("x\n", encoding="utf-8")

    with sandbox.Target(WORKDIR, mode) as t:
        r = sandbox.ns_bind_mount(BIN, "dev-bind", t.pid, clean, dst)
        assert r.returncode == 0, r.stderr

        opts = t.mount_opts(dst)
        assert sandbox.mount_has_opt(opts, "nosuid"), f"got [{opts}]"
        assert not sandbox.mount_has_opt(opts, "nodev"), f"got [{opts}]"
        assert not sandbox.mount_has_opt(opts, "ro"), f"got [{opts}]"
    print("ok   dev-bind sets nosuid only (no nodev) on a clean source")


def test_recursive(mode):
    """源内已有子挂载时，AT_RECURSIVE 应一并带过去。

    子挂载必须对 open_tree 的调用者（注入进程，即本进程）可见才能被
    克隆：内核在调用者的 mount namespace 里解析 srcpath。注意不能从
    目标沙盒内探——bwrap 的 --bind 不递归，子挂载在沙盒里本来就看不见，
    克隆却照样会把它带上（这正是要测的）。
    """
    clean = mount_clean_source(inner=True)
    dst = WORKDIR / "dst"

    with sandbox.Target(WORKDIR, mode) as t:
        r = sandbox.ns_bind_mount(BIN, "bind", t.pid, clean, dst)
        assert r.returncode == 0, r.stderr

        out = t.read_file(dst / "inner-mnt" / "g")
        assert "inner-mount" in out, f"got {out!r}"
        print("ok   nested mount is copied recursively")

        # 子挂载也会应用同样的属性（AT_RECURSIVE 的 mount_setattr）
        opts = t.mount_opts(dst / "inner-mnt")
        assert sandbox.mount_has_opt(opts, "nosuid"), f"got [{opts}]"
        print("ok   nested mount also gets nosuid")


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------

CLEAN_SOURCE_TESTS = [
    test_bind_mount_options,
    test_ro_bind_mount_options,
    test_dev_bind_mount_options,
    test_recursive,
]


def main():
    for mode in ("bwrap", "unshare"):
        if mode == "bwrap" and not sandbox.have_bwrap():
            print("skip bwrap mode: bwrap is not installed")
            continue
        for func in CLEAN_SOURCE_TESTS:
            if SELECTED and func.__name__.removeprefix("test_") not in SELECTED:
                continue
            func(mode)
    print("all clean-source checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
