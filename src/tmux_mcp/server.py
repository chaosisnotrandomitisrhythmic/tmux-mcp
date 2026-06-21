import os
import shutil
import subprocess
import time
from typing import Optional

import yaml
from fastmcp import FastMCP

mcp = FastMCP(
    name="tmux-mcp",
    instructions="Manage tmux sessions, windows, and panes. Pane targeting uses tmux native format: session:window.pane (e.g. dev:0.1).",
)


def _run_tmux(*args: str, timeout: int = 10, input_data: Optional[str] = None) -> str:
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed or not in PATH")
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_data,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"tmux error: {stderr}")
    return result.stdout.strip()


def _run_cmd(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a non-tmux command, returning (returncode, stdout, stderr).

    Unlike _run_tmux this does NOT raise on a non-zero exit — the power-management
    helpers need to inspect the code (e.g. pgrep returns 1 when there is no match,
    which is a normal "not running" answer, not an error).
    """
    result = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _resolve_dir(directory: Optional[str], root: Optional[str] = None) -> Optional[str]:
    """Expand ~ and resolve relative paths against root."""
    if not directory:
        return None
    directory = os.path.expanduser(directory)
    if not os.path.isabs(directory) and root:
        directory = os.path.join(os.path.expanduser(root), directory)
    return directory


# ── Cross-pane communication primitives ────────────────────────────
#
# These back the public tools below. They are plain functions (not @mcp.tool
# wrapped) so they can call each other internally — a FastMCP-decorated symbol
# is a Tool object, not a directly callable function.

# Per-target snapshot of the captured scrollback, used by read_pane_delta to
# return only output added since the previous read. Keyed by the target string
# exactly as the caller passes it.
_pane_snapshots: dict[str, list[str]] = {}

# Markers used to classify a Claude Code TUI pane as BUSY vs IDLE.
_CLAUDE_BUSY_MARKERS = ("esc to interrupt", "esc to cancel")
_CLAUDE_IDLE_MARKERS = ("? for shortcuts", "? for help")

# How many lines from the bottom of a capture to scan for status markers. Kept
# small so a stale footer left higher in the scrollback can't read as idle.
_STATUS_TAIL = 20


def _run_tmux_chain(commands: list[list[str]], input_data: Optional[str] = None) -> str:
    """Run several tmux commands in ONE `tmux` invocation, separated by `;`.

    tmux executes the queued commands in order within a single process, so this
    both removes per-command subprocess spawn latency and guarantees ordering
    (clear -> paste -> Enter) without needing sleeps between them. The bytes are
    written to the pane's PTY in sequence, so the target app reads them in order.
    """
    argv: list[str] = []
    for i, cmd in enumerate(commands):
        if i:
            argv.append(";")
        argv.extend(cmd)
    return _run_tmux(*argv, input_data=input_data)


def _coerce_keys(keys) -> list[str]:
    """Normalise a keys argument into a list of individual tmux key names.

    Accepts either a list/tuple of key names (passed through as-is) or a single
    string, which is split on whitespace so that each token (e.g. "C-a", "C-k")
    becomes a separate argv item for `tmux send-keys`. This is the crux of the
    key-passing fix: tmux interprets each argv item as one key name, so a
    multi-token string MUST be split — otherwise tmux sees one unknown key name
    and types it literally.
    """
    if isinstance(keys, (list, tuple)):
        return [str(k) for k in keys if str(k) != ""]
    return [tok for tok in str(keys).split() if tok]


def _send_keys(target: str, keys) -> None:
    """Send one or more named tmux keys, each as a separate argv item (no -l)."""
    key_list = _coerce_keys(keys)
    if not key_list:
        return
    _run_tmux("send-keys", "-t", target, *key_list)


def _paste_cmds(target: str, text: str, bracketed: bool) -> list[list[str]]:
    """Build the load-buffer + paste-buffer command pair for literal text."""
    buf = f"tmuxmcp-{os.getpid()}"
    paste = ["paste-buffer", "-t", target, "-b", buf, "-d"]
    if bracketed:
        paste.append("-p")
    # load-buffer from stdin avoids any leading-dash-in-data argv ambiguity.
    return [["load-buffer", "-b", buf, "-"], paste]


def _send_text(target: str, text: str, bracketed: bool = True) -> None:
    """Type literal text into a pane without submitting.

    Uses a tmux paste buffer + `paste-buffer` so that embedded newlines are
    delivered safely. With bracketed=True the paste is wrapped in bracketed-paste
    escapes (paste-buffer -p), which a TUI like Claude Code recognises as a single
    paste event — so multi-line text does NOT submit early on each newline. The
    load + paste run in a single tmux invocation (one subprocess).
    """
    if text == "":
        return
    _run_tmux_chain(_paste_cmds(target, text, bracketed), input_data=text)


def _clear_keys(method: str) -> list[str]:
    """Return the key name(s) that clear an input line for the given method."""
    if method == "ctrl-u":
        return ["C-u"]
    if method == "kill-line":
        return ["C-a", "C-k"]
    return ["C-c"]  # default — reliably empties the Claude Code TUI prompt


def _clear_input(target: str, method: str = "ctrl-c") -> None:
    """Empty the current input line of the target pane.

    Methods:
      - "ctrl-c":    send C-c. Clears the Claude Code TUI prompt reliably. On a
                     plain shell this sends SIGINT (abandons the current line),
                     which also leaves an empty prompt.
      - "ctrl-u":    send C-u (readline kill-to-start). Good for shells.
      - "kill-line": send C-a then C-k (move to start, kill to end). Good for
                     shells / readline-style editors.
    """
    _send_keys(target, _clear_keys(method))


def _capture_lines(target: str, full_history: bool = True) -> list[str]:
    """Capture a pane as a list of lines, with trailing blank lines trimmed."""
    start = "-" if full_history else "-50"
    out = _run_tmux("capture-pane", "-t", target, "-p", "-S", start)
    lines = out.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    return lines


def _diff_new_lines(prev: Optional[list[str]], new: list[str]) -> str:
    """Return the lines of `new` that were added after `prev` was captured.

    Primary strategy is a top-anchored common prefix: terminal scrollback only
    grows at the bottom and the historical (upper) lines are stable, so the first
    point where `new` diverges from `prev` is where the new output begins. This
    is robust against the live bottom line mutating (a shell prompt being typed
    into, or a Claude input box) — which a naive bottom-anchored search latches
    onto incorrectly because the empty prompt recurs identically.

    Fallbacks: if there is no common prefix at all (the top scrolled out of the
    captured window, or the screen was cleared) resync on the tail of `prev`, and
    finally return the whole capture.
    """
    if not new:
        return ""
    if not prev:
        return "\n".join(new)

    # Common prefix from the top — the stable region.
    common = 0
    for a, b in zip(prev, new):
        if a != b:
            break
        common += 1

    if common > 0:
        return "\n".join(new[common:])

    # No top overlap: the oldest lines scrolled out of the window. Resync by
    # locating prev's tail block inside new and returning everything after it.
    anchor = prev[-5:]
    span = len(anchor)
    for start in range(len(new) - span, -1, -1):
        if new[start:start + span] == anchor:
            return "\n".join(new[start + span:])

    last_prev = next((line for line in reversed(prev) if line.strip()), None)
    if last_prev is not None:
        for i in range(len(new) - 1, -1, -1):
            if new[i] == last_prev:
                return "\n".join(new[i + 1:])

    return "\n".join(new)


def _read_pane_delta(target: str) -> str:
    """Return output added since the previous delta read for this target."""
    new = _capture_lines(target, full_history=True)
    prev = _pane_snapshots.get(target)
    _pane_snapshots[target] = new
    return _diff_new_lines(prev, new)


def _classify(lines: list[str]) -> str:
    """Classify a Claude TUI from already-captured lines.

    Only the bottom `_STATUS_TAIL` lines are scanned so a stale footer left
    higher in the scrollback (e.g. the welcome screen's "? for shortcuts")
    cannot be misread as the current status.
    """
    text = "\n".join(lines[-_STATUS_TAIL:]).lower()
    if any(marker in text for marker in _CLAUDE_BUSY_MARKERS):
        return "busy"
    if any(marker in text for marker in _CLAUDE_IDLE_MARKERS):
        return "idle"
    return "unknown"


def _pane_status(target: str) -> str:
    """Classify a Claude Code TUI pane as 'busy', 'idle', or 'unknown'.

    busy    -> the working footer ("esc to interrupt") is showing
    idle    -> the prompt footer ("? for shortcuts") is showing, not busy
    unknown -> neither marker found (e.g. the pane is a plain shell)
    """
    return _classify(_capture_lines(target, full_history=False))


def _send_message(
    target: str,
    text: str,
    submit: bool = True,
    clear_first: bool = True,
    clear_method: str = "ctrl-c",
) -> None:
    """Clear the input, paste text, and optionally submit — in ONE tmux call.

    Everything (clear keys -> load-buffer -> bracketed paste -> Enter) is queued
    into a single tmux invocation. tmux runs the queue in order and writes to the
    pane PTY sequentially, so no inter-step sleeps are needed: the target reads
    the SIGINT/clear, then the bracketed paste, then the carriage return in
    order. This keeps a send at ~one subprocess of latency.
    """
    cmds: list[list[str]] = []
    if clear_first:
        cmds.append(["send-keys", "-t", target, *_clear_keys(clear_method)])
    if text != "":
        cmds.extend(_paste_cmds(target, text, bracketed=True))
    if submit:
        cmds.append(["send-keys", "-t", target, "Enter"])
    if not cmds:
        return
    _run_tmux_chain(cmds, input_data=text if text != "" else None)


def _setup_window_panes(
    session: str,
    window_index: str,
    panes: list[dict],
    root: Optional[str] = None,
) -> None:
    """Set up panes within a window. First pane entry configures the existing pane, rest are splits."""
    target_base = f"{session}:{window_index}"

    for i, pane in enumerate(panes):
        if i == 0:
            # First pane already exists — just send command if any
            pane_dir = _resolve_dir(pane.get("directory"), root)
            if pane_dir:
                _run_tmux("send-keys", "-t", target_base, f"cd {pane_dir}", "Enter")
                time.sleep(0.1)
            if pane.get("command"):
                _run_tmux("send-keys", "-t", target_base, pane["command"], "Enter")
        else:
            # Split from the existing pane
            direction = pane.get("direction", "vertical")
            split_args = ["split-window"]
            if direction == "horizontal":
                split_args.append("-h")
            else:
                split_args.append("-v")
            split_args.extend(["-t", target_base])

            pane_dir = _resolve_dir(pane.get("directory"), root)
            if pane_dir:
                split_args.extend(["-c", pane_dir])

            _run_tmux(*split_args)
            # After split, tmux auto-selects the new pane — send command to it
            if pane.get("command"):
                _run_tmux("send-keys", "-t", target_base, pane["command"], "Enter")


@mcp.tool(annotations={"readOnlyHint": True})
def list_sessions() -> str:
    """List all tmux sessions with window count and attached status."""
    try:
        return _run_tmux(
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_windows} windows\t#{?session_attached,attached,detached}",
        )
    except RuntimeError as e:
        if "no server running" in str(e) or "no sessions" in str(e):
            return "No tmux sessions running."
        raise


@mcp.tool()
def create_session(name: str, directory: Optional[str] = None) -> str:
    """Create a new detached tmux session with the given name and optional starting directory."""
    args = ["new-session", "-d", "-s", name]
    if directory:
        args.extend(["-c", directory])
    _run_tmux(*args)
    return f"Session '{name}' created."


@mcp.tool()
def send_command(target: str, command: str) -> str:
    """Send a command to a tmux pane (fire-and-forget). Target format: session:window.pane (e.g. dev:0.1)."""
    _run_tmux("send-keys", "-t", target, "-l", command)
    _run_tmux("send-keys", "-t", target, "Enter")
    return f"Command sent to {target}."


@mcp.tool()
def send_keys(target: str, keys: str) -> str:
    """Send one or more raw tmux key NAMES to a pane (e.g. C-c, Escape, Up, Enter, C-l).

    A multi-key sequence is whitespace-separated and each token is sent as a
    distinct key, so "C-a C-k" sends Ctrl-A then Ctrl-K (it is NOT typed as the
    literal text "C-a C-k"). Use this only for named keys; to type literal text
    use send_text or send_message. Target format: session:window.pane.
    """
    key_list = _coerce_keys(keys)
    if not key_list:
        return f"No keys to send to {target}."
    _send_keys(target, key_list)
    return f"Sent {len(key_list)} key(s) to {target}: {' '.join(key_list)}"


@mcp.tool()
def send_text(target: str, text: str, submit: bool = False) -> str:
    """Type LITERAL text into a pane (not interpreted as key names).

    Newlines are delivered via bracketed paste so multi-line text does not submit
    on each line. Set submit=True to press Enter afterwards. Target format:
    session:window.pane. For driving a Claude TUI prefer send_message (it clears
    any half-typed input first).
    """
    # Route through _send_message (no clear) so the paste + Enter go out in a
    # single chained tmux call — no inter-step sleep needed.
    _send_message(target, text, submit=submit, clear_first=False)
    return f"Text sent to {target}{' and submitted' if submit else ''}."


@mcp.tool()
def clear_input(target: str, method: str = "ctrl-c") -> str:
    """Clear the current (possibly half-typed) input line of a pane.

    method: "ctrl-c" (default, reliably empties the Claude Code TUI prompt;
    sends SIGINT in a plain shell), "ctrl-u" (readline kill-to-start), or
    "kill-line" (C-a then C-k). Target format: session:window.pane.
    """
    if method not in ("ctrl-c", "ctrl-u", "kill-line"):
        raise ValueError("method must be one of: ctrl-c, ctrl-u, kill-line")
    _clear_input(target, method=method)
    return f"Cleared input of {target} via {method}."


@mcp.tool()
def send_message(
    target: str,
    text: str,
    submit: bool = True,
    clear_first: bool = True,
    clear_method: str = "ctrl-c",
) -> str:
    """Safely send a message to a pane running a TUI (e.g. another Claude Code session).

    Unlike send_command (which appends to whatever is already in the input
    buffer), this first clears any half-typed input, then types the text via
    bracketed paste (multi-line safe — newlines won't submit early), then
    optionally presses Enter. This is the recommended way to drive a worker
    Claude session. Target format: session:window.pane.

    submit: press Enter after typing (default True).
    clear_first: clear existing input before typing (default True).
    clear_method: how to clear — "ctrl-c" (default), "ctrl-u", or "kill-line".
    """
    if clear_method not in ("ctrl-c", "ctrl-u", "kill-line"):
        raise ValueError("clear_method must be one of: ctrl-c, ctrl-u, kill-line")
    _send_message(
        target,
        text,
        submit=submit,
        clear_first=clear_first,
        clear_method=clear_method,
    )
    return f"Message sent to {target}{' and submitted' if submit else ''}."


@mcp.tool(annotations={"readOnlyHint": True})
def pane_status(target: str) -> str:
    """Report whether a Claude Code TUI pane is BUSY or IDLE.

    Returns "busy" (the worker is generating — "esc to interrupt" visible),
    "idle" (prompt ready — "? for shortcuts" visible), or "unknown" (no Claude
    markers found, e.g. a plain shell pane). Target format: session:window.pane.
    """
    return _pane_status(target)


@mcp.tool(annotations={"readOnlyHint": True})
def read_pane_delta(target: str) -> str:
    """Return only the output ADDED to a pane since the last read_pane_delta call.

    Tracks a per-target snapshot of the scrollback, so repeated calls return just
    the new lines (the worker's latest reply) rather than the whole screen. The
    first call for a target establishes the baseline and returns the current
    scrollback. Target format: session:window.pane.
    """
    delta = _read_pane_delta(target)
    return delta if delta else "(no new output since last read)"


@mcp.tool()
def send_and_await_reply(
    target: str,
    text: str,
    timeout: int = 120,
    submit: bool = True,
    poll_interval: float = 0.1,
    stable_period: float = 0.25,
) -> str:
    """Send a message to a worker Claude pane and return its new output once idle.

    Optimised for low latency: the message is sent in a single tmux call (no
    inter-key sleeps) and completion is detected by *watching the pane*, not by
    waiting out fixed grace windows. Each poll takes one capture, classifies the
    footer (busy/idle), and tracks output changes; the call returns as soon as
    the pane is IDLE and its output has been stable for `stable_period` seconds —
    so a fast reply returns in roughly (model time + stable_period), with no
    extra fixed delay. Returns only the newly added output (the reply).

    Works on non-Claude panes too (e.g. a shell): with no busy/idle markers it
    returns once new output has settled. Target format: session:window.pane.

    timeout: max seconds to wait for the reply to complete (default 120).
    poll_interval: seconds between captures while waiting (default 0.1).
    stable_period: seconds of unchanged output required before returning (0.25).
    """
    timeout = max(1, min(timeout, 1800))
    poll_interval = max(0.02, poll_interval)
    stable_period = max(0.0, stable_period)
    # On a Claude pane, submitting reliably flips it BUSY within a fraction of a
    # second. We must observe that BUSY transition before accepting IDLE as
    # "done" — otherwise the brief idle gap between submit and generation (with
    # the just-submitted prompt already echoed) reads as a finished reply. The
    # startup grace bounds the wait when nothing ever happens (no-op / error).
    startup_grace = max(stable_period, 1.5)

    # Baseline BEFORE sending so the reply is isolated from prior screen content.
    baseline = _capture_lines(target, full_history=True)
    _pane_snapshots[target] = baseline
    _send_message(target, text, submit=submit)

    start = time.monotonic()
    deadline = start + timeout
    prev = baseline
    last_change = start
    saw_busy = False
    claude_seen = False
    timed_out = True

    while time.monotonic() < deadline:
        cur = _capture_lines(target, full_history=True)
        if cur != prev:
            prev = cur
            last_change = time.monotonic()
        status = _classify(cur)
        if status in ("busy", "idle"):
            claude_seen = True
        if status == "busy":
            saw_busy = True

        now = time.monotonic()
        has_new = bool(_diff_new_lines(baseline, cur))
        settled = (now - last_change) >= stable_period

        done = False
        if status == "busy":
            done = False
        elif status == "idle":
            # Normal completion: we saw it work, now it's idle and output settled.
            if saw_busy and settled:
                done = True
            # Fallback: it never went busy within the grace — nothing to wait for.
            elif (now - start) >= startup_grace and has_new and settled:
                done = True
        else:  # unknown — a non-Claude pane (e.g. a shell) has no markers
            if not claude_seen and has_new and settled and (now - start) >= stable_period:
                done = True

        if done:
            timed_out = False
            break
        time.sleep(poll_interval)

    final = _capture_lines(target, full_history=True)
    _pane_snapshots[target] = final
    delta = _diff_new_lines(baseline, final)
    if timed_out:
        return f"[timeout after {timeout}s — partial output below]\n{delta}".rstrip()
    return delta if delta else "(no new output captured)"


@mcp.tool(annotations={"readOnlyHint": True})
def read_pane(target: str, lines: int = 50) -> str:
    """Capture visible output from a tmux pane. Target format: session:window.pane. Lines: number of history lines (default 50, max 5000)."""
    lines = max(1, min(lines, 5000))
    return _run_tmux("capture-pane", "-t", target, "-p", "-S", f"-{lines}")


@mcp.tool()
def split_pane(target: str, horizontal: bool = False, directory: Optional[str] = None) -> str:
    """Split a tmux pane. Default vertical (top/bottom), set horizontal=True for left/right. Target format: session:window.pane."""
    if not directory:
        # Inherit cwd from source pane
        directory = _run_tmux("display-message", "-t", target, "-p", "#{pane_current_path}")
    args = ["split-window"]
    if horizontal:
        args.append("-h")
    else:
        args.append("-v")
    args.extend(["-t", target])
    if directory:
        args.extend(["-c", directory])
    _run_tmux(*args)
    return f"Pane split at {target}."


@mcp.tool(annotations={"readOnlyHint": True})
def list_panes(session: str) -> str:
    """List all panes across all windows in a session with their IDs, running commands, directories, and dimensions."""
    return _run_tmux(
        "list-panes",
        "-t",
        session,
        "-s",
        "-F",
        "#{window_index}.#{pane_index}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_width}x#{pane_height}",
    )


@mcp.tool()
def kill_session(name: str) -> str:
    """Kill a tmux session by name."""
    _run_tmux("kill-session", "-t", name)
    return f"Session '{name}' killed."


# ── macOS keep-awake (power management) ────────────────────────────
#
# Two awake levels:
#   (a) caffeinate -dimsu  — lid OPEN, no admin. Run in a dedicated, visible,
#       survivable tmux session so it's easy to find and kill (vs a bare
#       subprocess that gets orphaned and stuck — which is how we ended up
#       no-sleep for days). Detected by tmux session / pgrep, nothing fragile.
#   (b) pmset disablesleep 1 — lid CLOSED, needs admin (osascript GUI auth so it
#       works without a tty). Drains battery; opt-in only via lid_closed=True.

_KEEPAWAKE_SESSION = "keepawake"


def _keepawake_running() -> bool:
    """True if the keepawake tmux session exists, or our own `caffeinate -dimsu`
    process is up.

    The dedicated tmux session is the canonical signal. We also match the
    SPECIFIC `caffeinate -dimsu` command (our own signature) as a belt-and-suspenders
    for a stray process whose session was killed out-of-band — but NEVER bare
    `caffeinate`, so an unrelated caffeinate (e.g. the harness's `caffeinate -i -t 300`)
    does not register as keep-awake-on.
    """
    try:
        _run_tmux("has-session", "-t", _KEEPAWAKE_SESSION)
        return True
    except RuntimeError:
        pass
    code, _, _ = _run_cmd("pgrep", "-f", "caffeinate -dimsu")
    return code == 0


def _disablesleep_value() -> Optional[str]:
    """Return the SleepDisabled value from `pmset -g` ('0'/'1'), or None if absent."""
    code, out, _ = _run_cmd("pmset", "-g")
    if code != 0:
        return None
    for line in out.splitlines():
        if "sleepdisabled" in line.lower():
            return line.split()[-1]
    return None


def _set_disablesleep(value: int) -> None:
    """Set pmset disablesleep via an osascript admin prompt (GUI auth, no tty needed)."""
    script = (
        f'do shell script "/usr/bin/pmset -a disablesleep {value}" '
        "with administrator privileges"
    )
    code, _, err = _run_cmd("osascript", "-e", script)
    if code != 0:
        raise RuntimeError(f"pmset disablesleep {value} failed: {err}")


def _start_keepawake() -> bool:
    """Start caffeinate in the keepawake tmux session. True if newly started, False if already up."""
    if _keepawake_running():
        return False
    _run_tmux("new-session", "-d", "-s", _KEEPAWAKE_SESSION, "caffeinate -dimsu")
    return True


def _stop_keepawake() -> None:
    """Kill the keepawake session and any stray `caffeinate -dimsu` we started."""
    try:
        _run_tmux("kill-session", "-t", _KEEPAWAKE_SESSION)
    except RuntimeError:
        pass  # session may not exist — fine
    # Only our own `caffeinate -dimsu` — never bare `caffeinate`, so we don't kill
    # unrelated caffeinate processes (e.g. the harness's `caffeinate -i -t 300`).
    _run_cmd("pkill", "-f", "caffeinate -dimsu")


def _keep_awake(action: str, lid_closed: bool = False) -> str:
    action = action.lower().strip()
    if action == "on":
        started = _start_keepawake()
        lines = [
            "Started caffeinate in tmux session 'keepawake'."
            if started
            else "caffeinate already running (keepawake) — left as is."
        ]
        if lid_closed:
            _set_disablesleep(1)
            lines.append(
                "Lid-closed mode ON: pmset disablesleep 1 "
                "(⚠️ drains battery on AC/battery — run keep_awake('off') to revert)."
            )
        else:
            lines.append(
                "Caffeinate-only (lid must stay OPEN). Pass lid_closed=True to allow the lid closed."
            )
        return "\n".join(lines)
    if action == "off":
        _stop_keepawake()
        _set_disablesleep(0)
        return "Stopped keepawake/caffeinate and reset pmset disablesleep 0 (sleep restored)."
    if action == "status":
        running = _keepawake_running()
        sleepdisabled = _disablesleep_value()
        sd = sleepdisabled if sleepdisabled is not None else "unknown"
        return (
            f"caffeinate/keepawake running: {'yes' if running else 'no'}\n"
            f"pmset SleepDisabled: {sd}"
        )
    return f"Unknown action '{action}'. Use one of: on, off, status."


@mcp.tool()
def keep_awake(action: str, lid_closed: bool = False) -> str:
    """Toggle macOS keep-awake for lid-closed remote tmux management.

    action="on": start `caffeinate -dimsu` in a dedicated tmux session named
    'keepawake' (idempotent — won't double-start). Lid must stay OPEN; no admin
    needed. Pass lid_closed=True to ALSO run `pmset disablesleep 1` (via an
    osascript admin prompt) so the Mac stays awake with the lid CLOSED.

    action="off": kill the keepawake session/caffeinate AND always reset
    `pmset disablesleep 0` (safety net — reverts lid-closed mode even if it was
    enabled out-of-band, so the Mac can never get stuck in no-sleep).

    action="status": report whether caffeinate/keepawake is running and the
    current pmset SleepDisabled value.

    ⚠️ SAFETY: lid_closed / disablesleep keeps the Mac awake on BATTERY too and
    will drain it. The default (caffeinate-only) is the safe option — use
    lid_closed only when plugged in, and run keep_awake('off') to revert.
    """
    return _keep_awake(action, lid_closed)


# ── Window management ──────────────────────────────────────────────


@mcp.tool()
def new_window(session: str, name: Optional[str] = None, directory: Optional[str] = None) -> str:
    """Create a new window in an existing session. Returns window index."""
    args = ["new-window", "-t", session]
    if name:
        args.extend(["-n", name])
    if directory:
        args.extend(["-c", os.path.expanduser(directory)])
    args.extend(["-P", "-F", "#{window_index}"])
    index = _run_tmux(*args)
    label = f"'{name}'" if name else index
    return f"Window {label} created in session '{session}' at index {index}."


@mcp.tool(annotations={"readOnlyHint": True})
def list_windows(session: str) -> str:
    """List all windows in a session with index, name, and pane count."""
    return _run_tmux(
        "list-windows",
        "-t",
        session,
        "-F",
        "#{window_index}\t#{window_name}\t#{window_panes} panes\t#{?window_active,active,}",
    )


@mcp.tool()
def select_window(target: str) -> str:
    """Focus a window. Target format: session:window (e.g. dev:2 or dev:logs)."""
    _run_tmux("select-window", "-t", target)
    return f"Selected window {target}."


@mcp.tool()
def rename_window(target: str, name: str) -> str:
    """Rename a window. Target format: session:window (e.g. dev:0)."""
    _run_tmux("rename-window", "-t", target, name)
    return f"Window {target} renamed to '{name}'."


# ── Layout engine ──────────────────────────────────────────────────


@mcp.tool()
def run_layout(
    file_path: Optional[str] = None,
    layout: Optional[dict] = None,
    replace: bool = True,
) -> str:
    """Set up an entire tmux project layout from a YAML file or inline dict.

    Provide either file_path (path to .tmux-layout.yaml) or layout (inline dict).
    If replace=True (default), kills any existing session with the same name first.

    YAML schema:
      session: name (required)
      root: base directory (optional, ~ expanded)
      on_create: list of shell commands to run before window setup (optional)
      windows: list of window specs (required)
        - name: window name (required)
          directory: override root (optional)
          command: command for single/first pane (optional)
          panes: list of pane specs for multi-pane windows (optional)
            - direction: horizontal or vertical (default, only on 2nd+ pane)
              directory: pane directory (optional)
              command: pane command (optional)
      focus: window name to select after setup (optional)
    """
    if file_path and layout:
        raise ValueError("Provide either file_path or layout, not both.")
    if not file_path and not layout:
        raise ValueError("Provide either file_path or layout.")

    if file_path:
        file_path = os.path.expanduser(file_path)
        with open(file_path) as f:
            layout = yaml.safe_load(f)

    session = layout["session"]
    root = _resolve_dir(layout.get("root"))
    windows = layout.get("windows", [])
    on_create = layout.get("on_create", [])
    focus = layout.get("focus")

    if not windows:
        raise ValueError("Layout must have at least one window.")

    # Kill existing session if replacing
    if replace:
        try:
            _run_tmux("kill-session", "-t", session)
        except RuntimeError:
            pass  # Session didn't exist

    # Create session with first window
    first_window = windows[0]
    first_dir = _resolve_dir(first_window.get("directory"), root) or root
    create_args = ["new-session", "-d", "-s", session, "-n", first_window["name"]]
    if first_dir:
        create_args.extend(["-c", first_dir])
    _run_tmux(*create_args)

    # Run on_create commands in the first pane
    if on_create:
        for cmd in on_create:
            _run_tmux("send-keys", "-t", f"{session}:0", cmd, "Enter")
        time.sleep(1)

    # Set up first window
    if first_window.get("panes"):
        _setup_window_panes(session, "0", first_window["panes"], root)
    elif first_window.get("command"):
        _run_tmux("send-keys", "-t", f"{session}:0", first_window["command"], "Enter")

    # Create remaining windows
    for win in windows[1:]:
        win_dir = _resolve_dir(win.get("directory"), root) or root
        new_args = ["new-window", "-t", session, "-n", win["name"]]
        if win_dir:
            new_args.extend(["-c", win_dir])
        _run_tmux(*new_args)

        # Get the index of the window we just created
        win_index = _run_tmux(
            "display-message", "-t", f"{session}:{win['name']}", "-p", "#{window_index}"
        )

        if win.get("panes"):
            _setup_window_panes(session, win_index, win["panes"], root)
        elif win.get("command"):
            _run_tmux("send-keys", "-t", f"{session}:{win_index}", win["command"], "Enter")

    # Focus requested window
    if focus:
        _run_tmux("select-window", "-t", f"{session}:{focus}")

    # Build summary
    summary_lines = [f"Session '{session}' created with {len(windows)} windows:"]
    for i, win in enumerate(windows):
        pane_count = len(win.get("panes", [])) or 1
        marker = " (focused)" if focus and win["name"] == focus else ""
        summary_lines.append(f"  {i}. {win['name']} ({pane_count} pane{'s' if pane_count > 1 else ''}){marker}")
    return "\n".join(summary_lines)
