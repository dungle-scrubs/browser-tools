"""The private credential relay, against fake opchain binaries only.

No test here touches 1Password, a real opchain, or a real credential. Every
child is a synthetic script that implements just enough of the relay grammar
to exercise one seam: write bytes to the inherited fd, exit codes, stderr
identifiers, descendants, and timeouts. The sentinel values are obviously
fake (``SENTINEL...``) so a leak into output is visible by string match.

The guarantees under test come from the opchain caller recipe: private
inherited pipe, own process group, SIGKILL on cancellation and again after
completion before the EOF wait, bounded bytes/duration, strict UTF-8, and
byte-for-byte preservation including newlines.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from browser_tools import credential_relay as relay
from browser_tools.credential_relay import (
    RelayError,
    RelayMissingError,
    RelayUnsupportedError,
    Secret,
    ensure_relay_supported,
    find_opchain,
    read_reference,
    relay_failure_identifier,
)

VAULT = "dqss6l4yprxhntxymzjaltchwe"
ITEM = "bbbbbbbbbbbbbbbbbbbbbbbbbb"
IDENTITY = "browser"

SENTINEL_USER = "SENTINEL-user\n\u00e9"
SENTINEL_PASS = "SENTINEL-pass\n\nwith blank line"


def write_fake(bin_dir: Path, name: str, body: str) -> Path:
    """Write one executable fake; returns its path."""
    path = bin_dir / name
    path.write_text(textwrap.dedent(body.lstrip("\n")).replace("SYSEXEC", sys.executable))
    path.chmod(0o755)
    return path


def good_relay_body(values: dict[str, str] | None = None) -> str:
    """A relay-capable fake: answers the probe, writes the field's value."""
    values = values or {
        "username": SENTINEL_USER,
        "password": SENTINEL_PASS,
    }
    return textwrap.dedent(
        f"""
        #!/usr/bin/env python3
        import os, sys
        argv = sys.argv
        fd_text = argv[argv.index("--value-fd") + 1]
        if fd_text == "0":
            sys.stderr.write("opchain: relay: descriptor-standard\\n")
            sys.exit(1)
        ref = argv[argv.index("op") + 2]
        field = ref.rsplit("/", 1)[-1]
        values = {values!r}
        data = values.get(field, "SENTINEL-default").encode("utf-8")
        os.write(1, data)
        os.write(2, data)
        os.write(int(fd_text), data)
        sys.exit(0)
        """
    ).lstrip("\n")


@pytest.fixture
def bin_dir(tmp_path) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    return path


class TestReferenceBoundary:
    @pytest.mark.parametrize("field", ["otp", "password/other", "SYNTHETIC-field"])
    def test_only_username_and_password_are_read(self, bin_dir, field):
        binary = write_fake(bin_dir, "opchain", good_relay_body())
        with pytest.raises(RelayError, match="username and password") as error:
            read_reference(
                binary=str(binary), identity=IDENTITY, vault=VAULT, item=ITEM, field=field,
            )
        assert field not in str(error.value)


class TestSuccessfulReads:
    def test_value_arrives_byte_for_byte(self, bin_dir):
        binary = write_fake(bin_dir, "opchain", good_relay_body())
        secret = read_reference(
            binary=binary,
            identity=IDENTITY,
            vault=VAULT,
            item=ITEM,
            field="username",
        )
        assert isinstance(secret, Secret)
        assert secret.data == SENTINEL_USER.encode("utf-8")

    def test_newlines_and_unicode_survive_both_fields(self, bin_dir):
        binary = write_fake(bin_dir, "opchain", good_relay_body())
        user = read_reference(
            binary=binary, identity=IDENTITY, vault=VAULT, item=ITEM, field="username"
        )
        password = read_reference(
            binary=binary, identity=IDENTITY, vault=VAULT, item=ITEM, field="password"
        )
        assert user.text() == SENTINEL_USER
        assert password.text() == SENTINEL_PASS
        assert password.data.endswith(b"\nwith blank line")

    def test_a_zero_byte_value_is_a_value_not_an_error(self, bin_dir):
        binary = write_fake(bin_dir, "opchain", good_relay_body(values={"username": ""}))
        secret = read_reference(
            binary=binary, identity=IDENTITY, vault=VAULT, item=ITEM, field="username"
        )
        assert secret.data == b""

    def test_the_reference_is_built_from_policy_parts(self, bin_dir):
        body = good_relay_body().replace(
            "os.write(int(fd_text), data)",
            "open(os.environ['FAKE_LOG'],'w').write(sys.argv[-1]); "
            "os.write(int(fd_text), data)",
        )
        binary = write_fake(bin_dir, "opchain", body)
        log = bin_dir / "argv.log"
        script = textwrap.dedent(
            f"""
            from browser_tools.credential_relay import read_reference
            s = read_reference(binary={str(binary)!r}, identity={IDENTITY!r},
                               vault={VAULT!r}, item={ITEM!r}, field="username")
            assert s.data
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=dict(os.environ, FAKE_LOG=str(log)),
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        assert log.read_text().strip() == f"op://{VAULT}/{ITEM}/username"


class TestFailedReads:
    def test_nonzero_exit_discards_the_partial_value(self, bin_dir, capfd):
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import os, sys
            argv = sys.argv
            fd_text = argv[argv.index("--value-fd") + 1]
            if fd_text == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            os.write(int(fd_text), b"SENTINEL-partial")
            sys.stderr.write("opchain: relay: child-failed:denied\\n")
            sys.exit(1)
            """,
        )
        with pytest.raises(RelayError, match="refused or failed"):
            read_reference(
                binary=binary, identity=IDENTITY, vault=VAULT, item=ITEM, field="username"
            )
        captured = capfd.readouterr()
        assert "SENTINEL" not in captured.out + captured.err

    def test_a_nonzero_child_that_prints_the_value_cannot_leak_it(self, bin_dir, capfd):
        """The child's own diagnostics never reach bt's output."""
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import os, sys
            argv = sys.argv
            fd_text = argv[argv.index("--value-fd") + 1]
            if fd_text == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            os.write(int(fd_text), b"SENTINEL-secret")
            sys.stdout.write("wrote SENTINEL-secret\\n")
            sys.stderr.write("failed writing SENTINEL-secret\\n")
            sys.exit(3)
            """,
        )
        with pytest.raises(RelayError) as excinfo:
            read_reference(
                binary=binary, identity=IDENTITY, vault=VAULT, item=ITEM, field="username"
            )
        assert "SENTINEL" not in str(excinfo.value)
        captured = capfd.readouterr()
        assert "SENTINEL" not in captured.out + captured.err

    def test_a_silent_nonzero_child_gets_the_plain_fixed_message(self, bin_dir):
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import sys
            argv = sys.argv
            if argv[argv.index("--value-fd") + 1] == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            sys.exit(1)
            """,
        )
        with pytest.raises(RelayError, match="refused or failed"):
            read_reference(
                binary=binary, identity=IDENTITY, vault=VAULT, item=ITEM, field="username"
            )

    def test_overflow_discards_the_value(self, bin_dir, capfd):
        # The child blocks writing more than the bound; the concurrent
        # bounded reader observes the overflow and the group is killed.
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import os, sys
            argv = sys.argv
            fd_text = argv[argv.index("--value-fd") + 1]
            if fd_text == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            blob = b"S" * (64 * 1024 + 1)
            try:
                os.write(int(fd_text), blob)
            except BrokenPipeError:
                pass
            sys.exit(0)
            """,
        )
        start = time.monotonic()
        with pytest.raises(RelayError, match="exceeded"):
            read_reference(
                binary=binary,
                identity=IDENTITY,
                vault=VAULT,
                item=ITEM,
                field="username",
                command_timeout=10.0,
            )
        assert time.monotonic() - start < 9
        assert "SSSS" not in capfd.readouterr().out

    def test_timeout_kills_the_child(self, bin_dir):
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import sys, time
            argv = sys.argv
            if argv[argv.index("--value-fd") + 1] == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            time.sleep(30)
            """,
        )
        start = time.monotonic()
        with pytest.raises(RelayError, match="deadline"):
            read_reference(
                binary=binary,
                identity=IDENTITY,
                vault=VAULT,
                item=ITEM,
                field="username",
                command_timeout=0.5,
                read_timeout=1.0,
            )
        assert time.monotonic() - start < 10

    def test_pipe_that_never_reaches_eof_times_out(self, bin_dir):
        # Parent exits; a descendant that escaped the group by setsid holds
        # the write end open, so EOF never arrives and the read must time out.
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import os, sys, time, subprocess
            if os.environ.get("FAKE_DESCENDANT") == "1":
                time.sleep(30)
                sys.exit(0)
            argv = sys.argv
            fd_text = argv[argv.index("--value-fd") + 1]
            if fd_text == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            wfd = int(fd_text)
            os.write(wfd, b"SENTINEL-value")
            env = dict(os.environ, FAKE_DESCENDANT="1")
            subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                             pass_fds=(wfd,), start_new_session=True, env=env,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.exit(0)
            """,
        )
        start = time.monotonic()
        with pytest.raises(RelayError, match="did not close in time"):
            read_reference(
                binary=binary,
                identity=IDENTITY,
                vault=VAULT,
                item=ITEM,
                field="username",
                command_timeout=5.0,
                read_timeout=1.0,
            )
        assert time.monotonic() - start < 20

    def test_descendant_in_the_group_dies_and_read_completes(self, bin_dir):
        # The recipe case: a normal descendant holds the pipe open; the
        # post-wait group kill makes EOF arrive.
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import os, sys, subprocess
            argv = sys.argv
            fd_text = argv[argv.index("--value-fd") + 1]
            if fd_text == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            wfd = int(fd_text)
            os.write(wfd, "SENTINEL-group".encode())
            subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(30)"],
                             pass_fds=(wfd,),
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.exit(0)
            """,
        )
        start = time.monotonic()
        secret = read_reference(
            binary=binary,
            identity=IDENTITY,
            vault=VAULT,
            item=ITEM,
            field="username",
            command_timeout=5.0,
            read_timeout=3.0,
        )
        assert secret.data == b"SENTINEL-group"
        assert time.monotonic() - start < 15

    def test_cancellation_kills_the_group_and_closes_descriptors(self, bin_dir, monkeypatch):
        binary = write_fake(bin_dir, "opchain", good_relay_body())
        spawned: list[int] = []
        real_popen = subprocess.Popen

        class RecordingPopen(real_popen):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                spawned.append(self.pid)

        monkeypatch.setattr(relay.subprocess, "Popen", RecordingPopen)

        def _interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(relay._PipeReader, "finish", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            read_reference(
                binary=binary,
                identity=IDENTITY,
                vault=VAULT,
                item=ITEM,
                field="username",
            )
        # The child had completed (finish runs after wait), then the interrupt
        # hit: its group is dead either way.
        time.sleep(0.3)
        for pid in spawned:
            with pytest.raises(OSError):
                os.kill(pid, 0)  # process is gone

    def test_invalid_utf8_refuses_rather_than_mangle(self, bin_dir):
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import os, sys
            argv = sys.argv
            fd_text = argv[argv.index("--value-fd") + 1]
            if fd_text == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            os.write(int(fd_text), b"\\xff\\xfe-broken")
            sys.exit(0)
            """,
        )
        secret = read_reference(
            binary=binary, identity=IDENTITY, vault=VAULT, item=ITEM, field="username"
        )
        with pytest.raises(RelayError, match="UTF-8") as error:
            secret.text()
        assert error.value.__cause__ is None
        assert error.value.__suppress_context__ is True

    def test_secret_repr_is_fixed_text(self, bin_dir):
        binary = write_fake(bin_dir, "opchain", good_relay_body())
        secret = read_reference(
            binary=binary, identity=IDENTITY, vault=VAULT, item=ITEM, field="username"
        )
        assert "SENTINEL" not in repr(secret)
        assert repr(secret) == "<redacted relay value>"


class TestMissingAndUnsupportedBinaries:
    def test_missing_binary_has_its_own_fixed_diagnostic(self, tmp_path):
        with pytest.raises(RelayMissingError, match="not installed"):
            read_reference(
                binary=str(tmp_path / "no-such-opchain"),
                identity=IDENTITY,
                vault=VAULT,
                item=ITEM,
                field="username",
            )

    def test_missing_binary_in_the_probe_too(self, tmp_path):
        with pytest.raises(RelayMissingError, match="not installed"):
            ensure_relay_supported(str(tmp_path / "no-such-opchain"), IDENTITY)

    def test_old_binary_without_the_relay_is_refused(self, bin_dir):
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import sys
            sys.stderr.write("Unknown option: --value-fd\\n")
            sys.exit(1)
            """,
        )
        with pytest.raises(RelayUnsupportedError, match="--value-fd"):
            ensure_relay_supported(str(binary), IDENTITY)

    def test_old_binary_read_also_fails_closed(self, bin_dir):
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import sys
            sys.stderr.write("Unknown option: --value-fd\\n")
            sys.exit(1)
            """,
        )
        with pytest.raises(RelayError):
            read_reference(
                binary=str(binary),
                identity=IDENTITY,
                vault=VAULT,
                item=ITEM,
                field="username",
            )

    def test_supporting_binary_passes_the_probe(self, bin_dir):
        binary = write_fake(bin_dir, "opchain", good_relay_body())
        ensure_relay_supported(str(binary), IDENTITY, timeout=10.0)

    def test_a_different_relay_line_is_not_support(self, bin_dir):
        # Only the exact descriptor-standard refusal means the binary has
        # the relay; any other anchored line (here: a token-resolution
        # failure) must read as unsupported, not as support.
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import sys
            sys.stderr.write("opchain: relay: token\\n")
            sys.exit(1)
            """,
        )
        with pytest.raises(RelayUnsupportedError):
            ensure_relay_supported(str(binary), IDENTITY, timeout=10.0)

    def test_a_zero_exit_is_not_support_either(self, bin_dir):
        binary = write_fake(
            bin_dir,
            "opchain",
            """
            #!/usr/bin/env python3
            import sys
            sys.stderr.write("opchain: relay: descriptor-standard\\n")
            sys.exit(0)
            """,
        )
        with pytest.raises(RelayUnsupportedError):
            ensure_relay_supported(str(binary), IDENTITY, timeout=10.0)

    def test_interrupting_a_live_probe_reaps_its_group(self, bin_dir, monkeypatch):
        marker = bin_dir / "probe-pids"
        binary = write_fake(
            bin_dir, "opchain",
            f"""
            #!/usr/bin/env python3
            import os, time
            from pathlib import Path
            child = os.fork()
            if child == 0:
                time.sleep(30)
                os._exit(0)
            Path({str(marker)!r}).write_text(f"{{os.getpid()}} {{child}}")
            time.sleep(30)
            """,
        )
        original_wait = subprocess.Popen.wait
        processes = []

        def interrupt_once(proc, timeout=None):
            if not processes:
                processes.append(proc)
                ready_by = time.monotonic() + 5
                while time.monotonic() < ready_by:
                    if marker.exists() and len(marker.read_text().split()) == 2:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("synthetic probe did not start its descendant")
                assert proc.poll() is None
                raise KeyboardInterrupt
            return original_wait(proc, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", interrupt_once)
        try:
            with pytest.raises(KeyboardInterrupt):
                ensure_relay_supported(str(binary), IDENTITY, timeout=10.0)
            assert processes[0].poll() is not None, "interrupted probe was not reaped"
            gone_by = time.monotonic() + 5
            while time.monotonic() < gone_by:
                try:
                    os.killpg(processes[0].pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("interrupted probe left its process group alive")
        finally:
            for proc in processes:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                original_wait(proc, timeout=5)

    def test_a_hung_probe_is_bounded_and_its_group_torn_down(self, bin_dir):
        marker = bin_dir / "probe-pid"
        binary = write_fake(
            bin_dir,
            "opchain",
            f"""
            #!/usr/bin/env python3
            import os, sys, time
            with open({str(marker)!r}, "w") as fh:
                fh.write(str(os.getpid()))
            time.sleep(30)
            """,
        )
        start = time.monotonic()
        with pytest.raises(RelayError, match="did not answer in time"):
            ensure_relay_supported(str(binary), IDENTITY, timeout=0.5)
        assert time.monotonic() - start < 10
        pid = int(marker.read_text())
        gone = False
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except OSError:
                gone = True
                break
            time.sleep(0.05)
        assert gone, "the probe's process group survived its timeout"

    def test_probe_never_touches_a_credential(self, bin_dir):
        """The probe's reference names nothing and resolves no token."""
        body = good_relay_body().replace(
            'sys.stderr.write("opchain: relay: descriptor-standard\\n")',
            "open(os.environ['FAKE_LOG'],'a').write(' '.join(sys.argv)); "
            'sys.stderr.write("opchain: relay: descriptor-standard\\n")',
        )
        binary = write_fake(bin_dir, "opchain", body)
        log = bin_dir / "probe.log"
        os.environ["FAKE_LOG"] = str(log)
        try:
            ensure_relay_supported(str(binary), IDENTITY, timeout=10.0)
        finally:
            os.environ.pop("FAKE_LOG", None)
        assert "op://relay-support-probe" in log.read_text()


class TestIdentifierExtraction:
    def test_only_the_anchored_identifier_line_counts(self):
        text = "noise\nopchain: relay: token\nmore noise with opchain: relay: fake extra"
        assert relay_failure_identifier(text) == "token"

    def test_arbitrary_stderr_yields_none(self):
        assert relay_failure_identifier("child said: denied SECRET") is None
        assert relay_failure_identifier("") is None

    def test_debug_lines_do_not_match(self):
        assert relay_failure_identifier("opchain debug: relay: fd=3") is None


class TestCooperativeCancellation:
    """The threading.Event cancellation signal, at the provider level.

    The asyncio-side regression (cancel the awaiting coroutine, prove the
    group dies promptly) lives in tests/test_login_cancellation.py.
    """

    def test_a_read_cancelled_before_spawn_never_starts_a_child(self, bin_dir):
        binary = write_fake(bin_dir, "opchain", good_relay_body())
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(RelayError, match="cancelled"):
            read_reference(
                binary=str(binary), identity=IDENTITY, vault=VAULT, item=ITEM,
                field="username", cancel=cancel,
            )

    def test_cancellation_mid_read_kills_the_group_promptly(self, bin_dir):
        marker = bin_dir / "pid"
        binary = write_fake(
            bin_dir,
            "opchain",
            f"""
            #!/usr/bin/env python3
            import os, sys, time
            argv = sys.argv
            if argv[argv.index("--value-fd") + 1] == "0":
                sys.stderr.write("opchain: relay: descriptor-standard\\n")
                sys.exit(1)
            with open({str(marker)!r}, "w") as fh:
                fh.write(str(os.getpid()))
            time.sleep(30)
            """,
        )
        cancel = threading.Event()
        result: list = []

        def canceller() -> None:
            for _ in range(500):
                if marker.exists():
                    cancel.set()
                    return
                time.sleep(0.02)

        thread = threading.Thread(target=canceller)
        thread.start()
        start = time.monotonic()
        with pytest.raises(RelayError, match="cancelled"):
            result.append(read_reference(
                binary=str(binary), identity=IDENTITY, vault=VAULT, item=ITEM,
                field="username", cancel=cancel,
            ))
        elapsed = time.monotonic() - start
        thread.join()
        assert elapsed < 6, elapsed
        assert result == []
        pid = int(marker.read_text())
        gone = False
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except OSError:
                gone = True
                break
            time.sleep(0.05)
        assert gone, "the cancelled read's process group survived"


class TestPathLookup:
    def test_find_opchain_reads_path(self, bin_dir, monkeypatch):
        write_fake(bin_dir, "opchain", good_relay_body())
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        assert find_opchain() is not None and find_opchain().endswith("opchain")

    def test_find_opchain_missing(self, bin_dir, monkeypatch):
        monkeypatch.setenv("PATH", str(bin_dir))
        assert find_opchain() is None
