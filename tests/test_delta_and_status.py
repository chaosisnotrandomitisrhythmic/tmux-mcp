"""Tests for read_pane_delta diffing and pane_status classification."""

import tmux_mcp.server as server

# ── _diff_new_lines ────────────────────────────────────────────────


def test_diff_first_capture_returns_all():
    assert server._diff_new_lines(None, ["a", "b"]) == "a\nb"


def test_diff_appended_lines():
    prev = ["a", "b", "c", "d", "e"]
    new = ["a", "b", "c", "d", "e", "NEW1", "NEW2"]
    assert server._diff_new_lines(prev, new) == "NEW1\nNEW2"


def test_diff_no_change():
    prev = ["a", "b", "c"]
    assert server._diff_new_lines(prev, ["a", "b", "c"]) == ""


def test_diff_scrolled_top_off():
    # Oldest line scrolled out of view; anchor on prev tail still locates the seam.
    prev = ["x", "b", "c", "d", "e"]
    new = ["b", "c", "d", "e", "NEW"]
    assert server._diff_new_lines(prev, new) == "NEW"


def test_diff_screen_reset_returns_all():
    prev = ["old1", "old2", "old3", "old4", "old5"]
    new = ["totally", "different"]
    assert server._diff_new_lines(prev, new) == "totally\ndifferent"


def test_diff_empty_new():
    assert server._diff_new_lines(["a"], []) == ""


# ── _read_pane_delta cursor behaviour (capture mocked) ─────────────


def test_read_pane_delta_tracks_cursor(monkeypatch):
    server._pane_snapshots.clear()
    frames = iter([
        ["line1", "line2"],
        ["line1", "line2", "line3"],
        ["line1", "line2", "line3"],
    ])
    monkeypatch.setattr(server, "_capture_lines", lambda *a, **k: next(frames))

    assert server._read_pane_delta("t") == "line1\nline2"  # baseline
    assert server._read_pane_delta("t") == "line3"          # only the new line
    assert server._read_pane_delta("t") == ""               # nothing new


# ── _pane_status ───────────────────────────────────────────────────


def _mock_capture(monkeypatch, lines):
    monkeypatch.setattr(server, "_capture_lines", lambda *a, **k: lines)


def test_pane_status_busy(monkeypatch):
    _mock_capture(monkeypatch, ["Thinking…", "  (esc to interrupt)"])
    assert server._pane_status("t") == "busy"


def test_pane_status_idle(monkeypatch):
    _mock_capture(monkeypatch, ["│ > ", "  ? for shortcuts"])
    assert server._pane_status("t") == "idle"


def test_pane_status_unknown_shell(monkeypatch):
    _mock_capture(monkeypatch, ["~/dev/tmux-mcp $ ", ""])
    assert server._pane_status("t") == "unknown"


def test_pane_status_busy_wins_over_idle(monkeypatch):
    # If both markers somehow render, busy takes precedence (still generating).
    _mock_capture(monkeypatch, ["? for shortcuts", "esc to interrupt"])
    assert server._pane_status("t") == "busy"
