"""ns-bind-mount 的 pytest 测试套件。

用例分三类：
  * 纯参数校验（TestUsage）：不涉及命名空间副作用，直接调用被测二进制
    断言退出码与报错文本（报错文案检查统一用 LC_ALL=C.UTF-8 固定 locale）。
  * 功能用例：参数化为 [bwrap] / [unshare] 两种目标构造方式。bwrap 是
    重点——向运行中的 bwrap 沙盒注入挂载本来就是程序的设计场景——
    unshare 作对照。两种目标都建在独立 userns 里（conftest 的 target
    fixture 做前提断言），所以这些用例本身覆盖了跨 userns 注入。
  * 干净源用例（TestCleanSource）：挂载选项断言必须用干净的 tmpfs 源
    才可判定（见 clean_source_case.py 的模块说明），经 unshare 子进程
    执行。

观察目标命名空间视图的方式统一为 FIFO 请求-响应（见 sandbox.py 的
Target 与 target.py）：注入的挂载只存在于目标 mntns 里，宿主侧看不见，
必须让目标进程自证。
"""

import os
import subprocess

import pytest
import sandbox

# ===========================================================================
# 小工具
# ===========================================================================


def run_bin(bin_path, *args):
    """运行被测二进制（sandbox.ns_bind_mount 之外的调用形式）。

    固定 LC_ALL=C.UTF-8，报错文案可断言。
    """
    env = dict(os.environ, LC_ALL="C.UTF-8")
    return subprocess.run(
        [str(bin_path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
        check=False,
    )


# ===========================================================================
# 纯参数校验（不涉及命名空间）
# ===========================================================================


class TestUsage:
    def test_help_prints_usage(self, bin_path):
        r = run_bin(bin_path, "--help")
        assert r.returncode == 0
        assert "Usage:" in r.stdout
        for mode in ("bind", "ro-bind", "dev-bind"):
            assert mode in r.stdout

    def test_h_is_short_help(self, bin_path):
        r = run_bin(bin_path, "-h")
        assert r.returncode == 0
        assert "Usage:" in r.stdout

    def test_no_arguments_exits_nonzero(self, bin_path):
        r = run_bin(bin_path)
        assert r.returncode == 1
        assert "Usage:" in r.stderr

    def test_too_few_arguments_exits_nonzero(self, bin_path):
        r = run_bin(bin_path, "bind")
        assert r.returncode == 1

    def test_too_many_arguments_exits_nonzero(self, bin_path):
        r = run_bin(bin_path, "bind", "1", "/a", "/b", "extra")
        assert r.returncode == 1

    def test_unknown_mode_is_reported(self, bin_path):
        r = run_bin(bin_path, "frobnicate", "1", "/tmp", "/tmp")
        assert r.returncode == 1
        assert "Unknown mode" in r.stderr

    @pytest.mark.parametrize(
        "bad_pid",
        [
            pytest.param("notanumber", id="non-numeric"),
            pytest.param("0", id="zero"),
            pytest.param("-5", id="negative"),
            pytest.param("12abc", id="trailing-garbage"),
        ],
    )
    def test_bad_pid_is_reported(self, bin_path, bad_pid):
        r = run_bin(bin_path, "bind", bad_pid, "/tmp", "/tmp")
        assert r.returncode == 1
        assert "Invalid pid" in r.stderr

    def test_nonexistent_pid_reports_open_failure(self, bin_path):
        # pid_max + 1 在任何系统上都不可能存在。
        gone = sandbox.pid_max() + 1
        r = run_bin(bin_path, "bind", str(gone), "/tmp", "/tmp")
        assert r.returncode == 1
        assert "open(/proc" in r.stderr


# ===========================================================================
# 功能用例：[bwrap] / [unshare] 双模式参数化
# ===========================================================================


class TestBind:
    def test_visible_and_writable(self, bin_path, workdir, target, pid):
        src, dst = workdir / "src", workdir / "dst"
        (src / "file.txt").write_text("payload-bind\n", encoding="utf-8")

        r = sandbox.ns_bind_mount(bin_path, "bind", pid, src, dst)
        assert r.returncode == 0, r.stderr

        out = target.read_file(dst / "file.txt")
        assert "payload-bind" in out

        target.touch(dst / "created")
        # 写入应穿透回源目录
        assert (src / "created").exists()


class TestRoBind:
    def test_readonly_semantics(self, bin_path, workdir, target, pid):
        src, dst = workdir / "src", workdir / "dst"
        (src / "file.txt").write_text("payload-ro\n", encoding="utf-8")

        r = sandbox.ns_bind_mount(bin_path, "ro-bind", pid, src, dst)
        assert r.returncode == 0, r.stderr

        out = target.read_file(dst / "file.txt")
        assert "payload-ro" in out

        # 写入被拒绝，且错误正是"只读文件系统"（errno 名由目标回传，
        # 不受 locale 影响）。
        ok, err = target.try_write_file(dst / "denied", "x\n")
        assert not ok
        assert err == "EROFS"
        # 只读挂载不得穿透写源目录
        assert not (src / "denied").exists()


class TestDevBind:
    def test_visible_and_writable(self, bin_path, workdir, target, pid):
        src, dst = workdir / "src", workdir / "dst"
        (src / "file.txt").write_text("payload-dev\n", encoding="utf-8")

        r = sandbox.ns_bind_mount(bin_path, "dev-bind", pid, src, dst)
        assert r.returncode == 0, r.stderr

        out = target.read_file(dst / "file.txt")
        assert "payload-dev" in out

        target.touch(dst / "created")
        assert (src / "created").exists()


class TestDevBindAccessible:
    """用真实设备文件验证 dev-bind 的核心语义。

    nodev 会阻止打开设备节点；不带 nodev 的 dev-bind 应能正常读写。
    源用 /dev/null：自行 mknod 需要特权，而环境里现成的这个设备可用。
    """

    def test_device_accessibility(self, bin_path, workdir, target, pid):
        try:
            is_chardev = (os.stat("/dev/null").st_mode & 0o170000) == 0o020000
        except OSError:
            is_chardev = False
        if not is_chardev:
            pytest.skip("/dev/null is not a character device here")

        bnull, dnull = workdir / "bdev" / "null", workdir / "ddev" / "null"
        bnull.touch()
        dnull.touch()

        rb = sandbox.ns_bind_mount(bin_path, "bind", pid, "/dev/null", bnull)
        assert rb.returncode == 0, rb.stderr
        rd = sandbox.ns_bind_mount(bin_path, "dev-bind", pid, "/dev/null", dnull)
        assert rd.returncode == 0, rd.stderr

        # bind（带 nodev）应拒绝打开设备；dev-bind（无 nodev）应允许写入。
        ok, err = target.try_write_file(bnull, "A")
        assert not ok
        assert err == "EACCES"
        ok, err = target.try_write_file(dnull, "B")
        assert ok, f"dev-bind device write failed: {err}"

        # 选项层面再确认一次。两个都是文件级挂载，mountinfo 里同一路径
        # 会出现多层（mount_opts 取最内层的那条）。
        b_opts = target.mount_opts(bnull)
        d_opts = target.mount_opts(dnull)
        assert sandbox.mount_has_opt(b_opts, "nodev"), f"got [{b_opts}]"
        assert not sandbox.mount_has_opt(d_opts, "nodev"), f"got [{d_opts}]"


class TestRepeatTarget:
    """往同一个目标点重复注入：后挂的应叠加在最上层，盖住旧内容。

    注意历史上这里曾测成"两个不同挂载点"，那只能说明互不干扰，
    验证不了"同一 dst 重复注入"这一档语义，所以现在两个源都挂到同一 dst。
    """

    def test_stacking_on_same_dst(self, bin_path, workdir, target, pid):
        s1, s2, dst = workdir / "s1", workdir / "s2", workdir / "dst"
        (s1 / "f").write_text("first\n", encoding="utf-8")
        (s2 / "f").write_text("second\n", encoding="utf-8")

        r1 = sandbox.ns_bind_mount(bin_path, "bind", pid, s1, dst)
        assert r1.returncode == 0, r1.stderr
        assert "first" in target.read_file(dst / "f")

        # 同一 dst 再挂一次：新挂载叠在最上层，旧内容应被盖住
        r2 = sandbox.ns_bind_mount(bin_path, "bind", pid, s2, dst)
        assert r2.returncode == 0, r2.stderr
        assert "second" in target.read_file(dst / "f")

        # 再挂回第一个源：覆盖关系应再次翻转，证明叠层可继续演进
        r3 = sandbox.ns_bind_mount(bin_path, "bind", pid, s1, dst)
        assert r3.returncode == 0, r3.stderr
        assert "first" in target.read_file(dst / "f")


class TestBindFile:
    def test_file_level_bind(self, bin_path, workdir, target, pid):
        src = workdir / "srcfile"
        dst = workdir / "dst" / "targetfile"
        src.write_text("single-file\n", encoding="utf-8")
        dst.touch()

        r = sandbox.ns_bind_mount(bin_path, "bind", pid, src, dst)
        assert r.returncode == 0, r.stderr

        out = target.read_file(dst)
        assert "single-file" in out


class TestErrorPaths:
    def test_missing_dst_fails_at_move_mount(self, bin_path, workdir, target, pid):
        r = sandbox.ns_bind_mount(
            bin_path, "bind", pid, workdir / "src", workdir / "does-not-exist"
        )
        assert r.returncode == 1
        assert "move_mount" in r.stderr

    def test_missing_src_fails_at_open_tree(self, bin_path, workdir, target, pid):
        r = sandbox.ns_bind_mount(
            bin_path, "bind", pid, workdir / "no-such-src", workdir / "dst"
        )
        assert r.returncode == 1
        assert "open_tree" in r.stderr

    def test_stderr_empty_on_success(self, bin_path, workdir, target, pid):
        src, dst = workdir / "src", workdir / "dst"
        r = sandbox.ns_bind_mount(bin_path, "bind", pid, src, dst)
        assert r.returncode == 0
        assert r.stderr == ""


class TestHostIsolation:
    """宿主侧视图：注入的挂载不应出现在宿主 mount namespace 里。"""

    def test_host_is_not_polluted(self, bin_path, workdir, target, pid):
        src, dst = workdir / "src", workdir / "dst"
        (src / "f").write_text("isolated\n", encoding="utf-8")

        r = sandbox.ns_bind_mount(bin_path, "bind", pid, src, dst)
        assert r.returncode == 0, r.stderr

        # 注意必须精确匹配挂载点：findmnt --target 会回退到最近的祖先
        # 挂载（如 /tmp），导致"目录根本不是挂载点"时也返回非空。
        assert sandbox.mountinfo_opts(str(dst)) == ""
        assert not (dst / "f").exists()
        # 目标侧仍然可见
        assert "isolated" in target.read_file(dst / "f")


# ===========================================================================
# 干净源用例：挂载选项断言必须在干净 tmpfs 上做（见 clean_source_case.py）
# ===========================================================================


class TestCleanSource:
    """挂载参数与递归语义，用干净的 tmpfs 源测。

    断言"程序设置了 nodev/nosuid"只有在源本身不带这些标志时才有意义
    （mount_setattr 只能设置标志，无法清除源已继承的）。干净 tmpfs 必须
    挂在用例执行环境的 mntns 里，而普通环境里挂 tmpfs 只有 unshare -Urm
    能提供，所以这些用例经 unshare 子进程执行（sandbox.run_in_unshare）。
    目标在子进程内按 bwrap/unshare 两种方式各跑一遍——与其它功能用例
    相比只是"多了一层宿主 unshare"这一件事，用例体本身同样用标准
    assert 编写（clean_source_case.py）。

    用例体（clean_source_case.py 里的函数名去掉 test_ 前缀）作为 pytest
    参数化项：每个用例名一条测试项，报告里逐项可见。
    """

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param("bind_mount_options", id="bind"),
            pytest.param("ro_bind_mount_options", id="ro-bind"),
            pytest.param("dev_bind_mount_options", id="dev-bind"),
            pytest.param("recursive", id="recursive"),
        ],
    )
    def test_clean_source(self, bin_path, workdir, case, unshare_userns_available):
        """按参数化选择的用例名，在 unshare 内跑干净源断言。"""
        r = sandbox.run_in_unshare(
            "clean_source_case", [bin_path, workdir], selected=(case,)
        )
        print(r.stdout)
        assert r.returncode == 0, r.stderr
