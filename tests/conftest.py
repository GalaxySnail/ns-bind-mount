"""pytest 配置：命令行选项、环境探测 fixtures 与目标沙盒 fixture。

测试主体在 test_ns_bind_mount.py；命名空间沙盒与 FIFO 通信的实现在
sandbox.py（Target）；观测端脚本在 target.py。

用例名字约定：依赖目标沙盒的用例参数化为 [bwrap] / [unshare] 两种
构造方式，bwrap 是重点（程序的设计场景），unshare 作对照。两种目标
都建在独立的 user namespace 里，因此这些用例本身就是在测跨 userns
注入（程序走 setns(CLONE_NEWUSER) + unshare(CLONE_NEWNS) 分支），
由 target fixture 统一做一次前提断言，不再需要专门用例。
"""

import os
import sys
from pathlib import Path

import pytest

# sandbox.py 不是包的一部分，把 tests/ 加进 sys.path 后直接导入。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sandbox


def pytest_addoption(parser):
    parser.addoption(
        "--bin",
        action="store",
        default=os.environ.get("BIN", ""),
        help="path to the ns-bind-mount binary under test",
    )


# ---------------------------------------------------------------------------
# 被测二进制与环境探测
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def bin_path(pytestconfig):
    """被测二进制路径；不存在时整个会话直接报错。"""
    path = pytestconfig.getoption("--bin") or str(sandbox.ROOT / "ns-bind-mount")
    if not os.access(path, os.X_OK):
        pytest.exit(f"cannot find executable {path}, run make first", 2)
    return path


@pytest.fixture(scope="session")
def bwrap_available():
    """bwrap 可用性；缺失时依赖它的用例显式 skip。"""
    if not sandbox.have_bwrap():
        pytest.skip("bwrap is not installed")


@pytest.fixture(scope="session")
def unshare_userns_available():
    """非特权 user namespace 可用性；缺失时依赖它的用例显式 skip。"""
    if not sandbox.have_unshare_userns():
        pytest.skip("unprivileged user namespaces unavailable")


# ---------------------------------------------------------------------------
# 目标沙盒
# ---------------------------------------------------------------------------

TARGET_MODES = ["bwrap", "unshare"]


@pytest.fixture(params=TARGET_MODES)
def target_mode(request):
    """功能用例的目标构造方式：[bwrap] 为重点，[unshare] 作对照。"""
    if request.param == "bwrap":
        request.getfixturevalue("bwrap_available")
    else:
        request.getfixturevalue("unshare_userns_available")
    return request.param


@pytest.fixture
def workdir(tmp_path):
    """每个用例独立的 workdir，含用例常用的目录骨架。"""
    w = tmp_path / "work"
    for d in ("src", "dst", "bdev", "ddev", "s1", "s2"):
        (w / d).mkdir(parents=True, exist_ok=True)
    return w


@pytest.fixture
def target(workdir, target_mode):
    """已启动的目标沙盒；用例结束时自动停止。

    前提断言：目标必须处于与调用者不同的 user namespace——这正是
    程序的设计场景（跨 userns 注入走 setns(CLONE_NEWUSER) 分支），
    不满足说明测试环境不符合预期，继续跑没有意义。
    """
    with sandbox.Target(workdir, target_mode) as t:
        inner = sandbox.ns_id(f"/proc/{t.pid}/ns/user")
        outer = sandbox.ns_id("/proc/self/ns/user")
        assert inner and inner != outer, (
            f"target shares the current user namespace (inner={inner})"
        )
        yield t


@pytest.fixture
def pid(target):
    """目标进程的外部 pid（注入挂载的锚点）。"""
    return target.pid
