"""Tests for the keep_awake power-management tool.

These guard the safety contract: 'off' must ALWAYS reset pmset disablesleep to 0
(so the Mac can't get stuck in no-sleep mode), 'on' is idempotent and only pops
the admin dialog when lid_closed=True, and status reflects the live state. All
subprocess/osascript calls are mocked so no real dialogs or caffeinate spawn.
"""

import tmux_mcp.server as server


class _TmuxRecorder:
    """Drop-in for server._run_tmux. Records argv; raises for has-session unless present."""

    def __init__(self, has_keepawake=False):
        self.calls = []
        self.has_keepawake = has_keepawake

    def __call__(self, *args, timeout=10, input_data=None):
        self.calls.append(list(args))
        if args[:1] == ("has-session",):
            if not self.has_keepawake:
                raise RuntimeError("tmux error: can't find session")
            return ""
        return ""


class _CmdRecorder:
    """Drop-in for server._run_cmd. Records argv and returns scripted (code, out, err)."""

    def __init__(self, responses=None):
        self.calls = []
        # map of argv[0] -> (returncode, stdout, stderr)
        self.responses = responses or {}

    def __call__(self, *args, timeout=30):
        self.calls.append(list(args))
        return self.responses.get(args[0], (0, "", ""))

    def argv0s(self):
        return [c[0] for c in self.calls]


def _patch(monkeypatch, tmux, cmd):
    monkeypatch.setattr(server, "_run_tmux", tmux)
    monkeypatch.setattr(server, "_run_cmd", cmd)


# ── on (caffeinate-only, default) ──────────────────────────────────


def test_on_starts_caffeinate_session_no_admin(monkeypatch):
    tmux = _TmuxRecorder(has_keepawake=False)
    cmd = _CmdRecorder({"pgrep": (1, "", "")})  # no caffeinate running
    _patch(monkeypatch, tmux, cmd)

    out = server._keep_awake("on")

    # New tmux session created with caffeinate command.
    new_sess = [c for c in tmux.calls if c[:1] == ["new-session"]]
    assert new_sess and new_sess[0] == [
        "new-session", "-d", "-s", "keepawake", "caffeinate -dimsu",
    ]
    # No osascript (admin dialog) for the default path.
    assert "osascript" not in cmd.argv0s()
    assert "Started caffeinate" in out


def test_on_is_idempotent_when_already_running(monkeypatch):
    tmux = _TmuxRecorder(has_keepawake=True)
    cmd = _CmdRecorder()
    _patch(monkeypatch, tmux, cmd)

    out = server._keep_awake("on")

    # Must NOT create a second session.
    assert not [c for c in tmux.calls if c[:1] == ["new-session"]]
    assert "already running" in out


def test_on_lid_closed_invokes_osascript_disablesleep_1(monkeypatch):
    tmux = _TmuxRecorder(has_keepawake=False)
    cmd = _CmdRecorder({"pgrep": (1, "", "")})
    _patch(monkeypatch, tmux, cmd)

    out = server._keep_awake("on", lid_closed=True)

    osa = [c for c in cmd.calls if c[0] == "osascript"]
    assert len(osa) == 1
    assert "disablesleep 1" in osa[0][2]
    assert "with administrator privileges" in osa[0][2]
    assert "Lid-closed mode ON" in out


# ── off (safety net) ───────────────────────────────────────────────


def test_off_always_resets_disablesleep_0(monkeypatch):
    tmux = _TmuxRecorder(has_keepawake=True)
    cmd = _CmdRecorder()
    _patch(monkeypatch, tmux, cmd)

    out = server._keep_awake("off")

    # keepawake session killed.
    assert ["kill-session", "-t", "keepawake"] in tmux.calls
    # disablesleep ALWAYS reset to 0, even though we never set it to 1 here.
    osa = [c for c in cmd.calls if c[0] == "osascript"]
    assert len(osa) == 1
    assert "disablesleep 0" in osa[0][2]
    assert "pkill" in cmd.argv0s()  # stray caffeinate cleanup
    assert "disablesleep 0" in out


def test_off_tolerates_missing_session(monkeypatch):
    # kill-session raises when there's no session; off must not blow up.
    class _Tmux(_TmuxRecorder):
        def __call__(self, *args, timeout=10, input_data=None):
            self.calls.append(list(args))
            if args[:1] == ("kill-session",):
                raise RuntimeError("tmux error: can't find session")
            return ""

    tmux = _Tmux()
    cmd = _CmdRecorder()
    _patch(monkeypatch, tmux, cmd)

    out = server._keep_awake("off")
    assert "disablesleep 0" in out  # still reset despite missing session


# ── status ─────────────────────────────────────────────────────────


def test_status_reports_running_and_sleepdisabled(monkeypatch):
    tmux = _TmuxRecorder(has_keepawake=True)
    cmd = _CmdRecorder({"pmset": (0, " SleepDisabled\t\t1", "")})
    _patch(monkeypatch, tmux, cmd)

    out = server._keep_awake("status")
    assert "running: yes" in out
    assert "SleepDisabled: 1" in out


def test_status_not_running_and_no_sleepdisabled(monkeypatch):
    tmux = _TmuxRecorder(has_keepawake=False)
    cmd = _CmdRecorder({"pgrep": (1, "", ""), "pmset": (0, " SleepDisabled\t\t0", "")})
    _patch(monkeypatch, tmux, cmd)

    out = server._keep_awake("status")
    assert "running: no" in out
    assert "SleepDisabled: 0" in out


def test_disablesleep_value_parsing(monkeypatch):
    cmd = _CmdRecorder({"pmset": (0, "System-wide power settings:\n SleepDisabled\t\t1", "")})
    monkeypatch.setattr(server, "_run_cmd", cmd)
    assert server._disablesleep_value() == "1"


def test_disablesleep_value_absent(monkeypatch):
    cmd = _CmdRecorder({"pmset": (0, "Active Profiles:\n Battery Power\t-1", "")})
    monkeypatch.setattr(server, "_run_cmd", cmd)
    assert server._disablesleep_value() is None


# ── unknown action ─────────────────────────────────────────────────


def test_unknown_action(monkeypatch):
    _patch(monkeypatch, _TmuxRecorder(), _CmdRecorder())
    out = server._keep_awake("frobnicate")
    assert "Unknown action" in out
