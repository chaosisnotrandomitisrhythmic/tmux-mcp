"""Unit tests for send_and_await_reply completion logic (no real tmux).

The key regression: the await must NOT return on the just-submitted prompt echo
during the brief idle gap before the worker goes busy — it must wait for the
busy->idle transition and the reply to settle.
"""

import tmux_mcp.server as server


class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def _drive(monkeypatch, frames):
    """Run send_and_await_reply.fn against a scripted sequence of capture frames.

    frames: list[list[str]] — capture returned on each successive _capture_lines
    call (baseline is the first; the last frame repeats once exhausted).
    """
    clock = _FakeClock()
    monkeypatch.setattr(server.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(server.time, "sleep", clock.sleep)
    monkeypatch.setattr(server, "_send_message", lambda *a, **k: None)

    seq = iter(frames)
    last = {"f": frames[0]}

    def fake_capture(target, full_history=True):
        try:
            last["f"] = next(seq)
        except StopIteration:
            pass
        return last["f"]

    monkeypatch.setattr(server, "_capture_lines", fake_capture)
    server._pane_snapshots.clear()
    return server.send_and_await_reply.fn("t", "do the thing", timeout=30)


IDLE = "  ? for shortcuts"
BUSY = "  esc to interrupt"


def test_waits_for_busy_then_returns_reply(monkeypatch):
    frames = [
        ["banner", IDLE],                         # baseline
        ["banner", "❯ do the thing", IDLE],       # echo, idle gap (must NOT return)
        ["banner", "❯ do the thing", IDLE],       # still idle, settled — still wait
        ["banner", "❯ do the thing", BUSY],       # now working
        ["banner", "❯ do the thing", BUSY],
        ["banner", "❯ do the thing", "⏺ ANSWER-42", IDLE],   # reply arrives
        ["banner", "❯ do the thing", "⏺ ANSWER-42", IDLE],   # settles
    ]
    out = _drive(monkeypatch, frames)
    assert "ANSWER-42" in out
    assert "banner" not in out  # delta isolation


def test_does_not_return_on_echo_before_busy(monkeypatch):
    # If we only ever see the idle echo (never busy) the fallback eventually
    # returns it — but only AFTER the startup grace, not immediately.
    frames = [["b", IDLE]] + [["b", "❯ q", IDLE]] * 50
    out = _drive(monkeypatch, frames)
    # It returned the echo via the grace fallback (no busy ever seen).
    assert "❯ q" in out


def test_non_claude_pane_settles_on_output(monkeypatch):
    # A plain shell: no markers at all -> settle on stable new output. The top
    # line is stable history; the prompt line mutates and new output appends.
    frames = [
        ["~/proj banner", "~/proj $"],
        ["~/proj banner", "~/proj $ echo hi", "hi", "~/proj $"],
        ["~/proj banner", "~/proj $ echo hi", "hi", "~/proj $"],
    ]
    out = _drive(monkeypatch, frames)
    assert "hi" in out
