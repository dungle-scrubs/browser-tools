"""Process inspection preserves argument boundaries on the host platform."""

import subprocess
import sys

from browser_tools.process_utils import pid_holds_user_data_dir


def test_live_process_holds_a_directory_containing_spaces(tmp_path):
    directory = tmp_path / "profile with spaces"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={directory}"]
    )
    try:
        assert pid_holds_user_data_dir(child.pid, directory)
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_recorded_linux_argv_preserves_spaces_and_empty_arguments(monkeypatch):
    from pathlib import Path

    from browser_tools import process_utils

    monkeypatch.setattr(process_utils.sys, "platform", "linux")
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: b"chrome\0--user-data-dir=/profiles/with spaces\0\0"
    )
    assert process_utils.read_process_args(123) == [
        "chrome",
        "--user-data-dir=/profiles/with spaces",
        "",
    ]


def test_recorded_darwin_argv_excludes_environment(monkeypatch):
    import ctypes

    from browser_tools import process_utils

    args = [b"chrome", b"--user-data-dir=/profiles/with spaces", b""]
    raw = (
        len(args).to_bytes(4, sys.byteorder, signed=True)
        + b"/Applications/Chrome\0\0\0"
        + b"\0".join(args)
        + b"\0SYNTHETIC_TEST_VALUE=unused\0"
    )

    class Libc:
        def sysctl(self, mib, count, buffer, size, new, new_size):
            assert list(mib) == [1, 49, 123]
            ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = len(raw)
            if buffer is not None:
                ctypes.memmove(buffer, raw, len(raw))
            return 0

    monkeypatch.setattr(process_utils.sys, "platform", "darwin")
    monkeypatch.setattr(process_utils.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    assert process_utils.read_process_args(123) == [value.decode() for value in args]
