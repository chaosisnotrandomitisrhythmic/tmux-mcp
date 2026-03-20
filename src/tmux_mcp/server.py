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


def _run_tmux(*args: str, timeout: int = 10) -> str:
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed or not in PATH")
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"tmux error: {stderr}")
    return result.stdout.strip()


def _resolve_dir(directory: Optional[str], root: Optional[str] = None) -> Optional[str]:
    """Expand ~ and resolve relative paths against root."""
    if not directory:
        return None
    directory = os.path.expanduser(directory)
    if not os.path.isabs(directory) and root:
        directory = os.path.join(os.path.expanduser(root), directory)
    return directory


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
    """Send raw tmux key names to a pane (e.g. C-c, Escape, Up, Enter, C-l). Target format: session:window.pane."""
    _run_tmux("send-keys", "-t", target, keys)
    return f"Keys sent to {target}."


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
