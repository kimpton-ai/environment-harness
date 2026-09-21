"""Windows file-lock compatibility coverage without requiring a Windows runner."""

import errno
import sys
import types

import pytest

from environment_harness import motor as motor_module
from environment_harness.motor import MotorExecutor
from environment_harness.motor_contracts import MotorProfile


class Adapter:
    implementation = "motor.v1"


def _executor(tmp_path, monkeypatch, msvcrt):
    monkeypatch.setattr(motor_module, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)
    return MotorExecutor(Adapter(), MotorProfile(adapter="motor.v1"), journal=tmp_path / "motor.sqlite")


def test_windows_constructor_backend_acquires_and_releases_offset_zero(tmp_path, monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        LK_NBLCK=1,
        LK_UNLCK=2,
        locking=lambda fd, mode, size: calls.append((fd, mode, size)),
    )
    executor = _executor(tmp_path, monkeypatch, fake)
    lock_path = tmp_path / "lock"
    with lock_path.open("a+") as lock:
        executor._acquire_file_lock(lock)
        executor._release_file_lock(lock)
    assert calls[0][1:] == (fake.LK_NBLCK, 1)
    assert calls[1][1:] == (fake.LK_UNLCK, 1)
    assert lock_path.read_text() == "0"


@pytest.mark.parametrize("code", [errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36])
def test_windows_contention_errors_become_blocking(tmp_path, monkeypatch, code):
    def locking(_fd, _mode, _size):
        raise OSError(code, "contended")

    executor = _executor(
        tmp_path, monkeypatch, types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    )
    with (tmp_path / "lock").open("a+") as lock:
        with pytest.raises(BlockingIOError):
            executor._acquire_file_lock(lock)


def test_windows_non_contention_error_propagates(tmp_path, monkeypatch):
    def locking(_fd, _mode, _size):
        raise OSError(errno.EIO, "disk failure")

    executor = _executor(
        tmp_path, monkeypatch, types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    )
    with (tmp_path / "lock").open("a+") as lock:
        with pytest.raises(OSError) as error:
            executor._acquire_file_lock(lock)
    assert error.value.errno == errno.EIO
