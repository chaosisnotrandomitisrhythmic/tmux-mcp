"""Live integration tests against a real tmux server.

These drive an actual scratch tmux session (a plain shell) to prove the
key-passing fix end-to-end. Skipped automatically if tmux is unavailable.
"""

import shutil
import time

import pytest

import tmux_mcp.server as server

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

SESSION = "tmuxmcp_pytest"
TARGET = f"{SESSION}:0"


@pytest.fixture()
def scratch_session():
    server._run_tmux("kill-session", "-t", SESSION, timeout=5) if _exists() else None
    server._run_tmux("new-session", "-d", "-s", SESSION, "-x", "120", "-y", "30")
    time.sleep(0.4)
    server._pane_snapshots.pop(TARGET, None)
    try:
        yield
    finally:
        try:
            server._run_tmux("kill-session", "-t", SESSION)
        except RuntimeError:
            pass


def _exists() -> bool:
    try:
        server._run_tmux("has-session", "-t", SESSION)
        return True
    except RuntimeError:
        return False


def _prompt_line() -> str:
    lines = server._capture_lines(TARGET, full_history=False)
    return lines[-1] if lines else ""


def _wait_for(predicate, timeout: float = 4.0, interval: float = 0.1) -> bool:
    """Poll until predicate() is truthy or timeout — keeps the live tests from
    racing fixed sleeps under parallel tmux-server load."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_named_keys_clear_a_typed_line(scratch_session):
    # Type literal text onto the shell input line...
    server._send_text(TARGET, "echo MARKER_TEXT", bracketed=False)
    assert _wait_for(lambda: "MARKER_TEXT" in _prompt_line())

    # ...then C-a C-k as SEPARATE keys must clear it (the regression scenario).
    server._send_keys(TARGET, "C-a C-k")
    assert _wait_for(lambda: "MARKER_TEXT" not in _prompt_line())


def test_buggy_single_arg_would_type_literally(scratch_session):
    # Demonstrate the OLD behaviour: a multi-token string as a single argv item
    # is typed literally by tmux. This is what _coerce_keys now prevents.
    server._run_tmux("send-keys", "-t", TARGET, "C-a C-k")
    assert _wait_for(lambda: "C-a C-k" in _prompt_line())
    # Clean up the line via the correct path.
    server._send_keys(TARGET, ["C-a", "C-k"])


def test_read_pane_delta_returns_only_new_output(scratch_session):
    server._read_pane_delta(TARGET)  # baseline
    server._send_text(TARGET, "echo DELTA_ONE", bracketed=False)
    server._send_keys(TARGET, ["Enter"])
    assert _wait_for(lambda: "DELTA_ONE" in "\n".join(server._capture_lines(TARGET)))
    delta = server._read_pane_delta(TARGET)
    assert "DELTA_ONE" in delta

    # A second read should not re-report the first command's output.
    server._send_text(TARGET, "echo DELTA_TWO", bracketed=False)
    server._send_keys(TARGET, ["Enter"])
    assert _wait_for(lambda: "DELTA_TWO" in "\n".join(server._capture_lines(TARGET)))
    delta2 = server._read_pane_delta(TARGET)
    assert "DELTA_TWO" in delta2
    assert "DELTA_ONE" not in delta2


def test_send_text_multiline_does_not_submit_early(scratch_session):
    # Start a `cat` so we can see the raw bytes that arrive (it echoes input).
    server._send_text(TARGET, "cat", bracketed=False)
    server._send_keys(TARGET, ["Enter"])
    time.sleep(0.3)
    server._read_pane_delta(TARGET)
    # Bracketed paste should deliver both lines as one paste, not run them.
    server._send_text(TARGET, "alpha\nbeta", bracketed=True)
    assert _wait_for(
        lambda: all(w in "\n".join(server._capture_lines(TARGET)) for w in ("alpha", "beta"))
    )
    # End cat.
    server._send_keys(TARGET, ["C-c"])
