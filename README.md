# tmux-mcp

MCP server that gives AI agents direct access to tmux.

Born out of frustration with flashy IDE integrations that make you dizzy. This is the opposite — a terminal-first, anti-distraction workflow where the AI works alongside you in tmux panes instead of taking over your screen.

Inspired by [ThePrimeagen's per-project `.tmux` files](https://frontendmasters.com/courses/developer-productivity-v2/). Same idea, but as YAML layouts that an AI agent can execute in one shot.

## What it does

20 tools. One file of Python. That's the whole thing.

**Session/pane basics:** create sessions, split panes, send commands, read output.

**Window management:** create, list, select, rename windows.

**Drive another TUI (e.g. a worker Claude session in another pane):** clear a
half-typed input line, send a (multi-line) message safely, detect whether the
target is busy or idle, read just the new output, or do the whole round-trip in
one call. See below.

**macOS keep-awake (`keep_awake`):** keep the Mac from sleeping so a remote/headless tmux session survives — handy when you close the lid and drive it over SSH. See below.

**Layout engine:** Define your project setup in a `.tmux-layout.yaml`, call `run_layout`, get your entire workspace in one shot:

```yaml
session: myproject
root: ~/dev/myproject
windows:
  - name: server
    command: make dev
  - name: frontend
    panes:
      - command: npm run dev
      - direction: horizontal
        command: npm run test -- --watch
  - name: shell
focus: server
```

One tool call. Four windows. Split panes. All running. Done.

## Driving another Claude session from a Claude session

A two-pane setup — an *operator* Claude on the left, a *worker* Claude on the
right — needs reliable cross-pane messaging. A raw `send-keys` into a TUI is
brittle: multi-token key strings get typed literally, new text concatenates onto
whatever was half-typed, and you can only read the whole screen back. These tools
fix that:

| Tool | What it does |
|------|--------------|
| `send_keys` | Send named keys. `"C-a C-k"` sends Ctrl-A then Ctrl-K (each token is a separate tmux key), never the literal text. |
| `send_text` | Type **literal** text via bracketed paste (multi-line safe — newlines don't submit early). |
| `clear_input` | Empty a half-typed input line (`ctrl-c` clears the Claude prompt; `ctrl-u` / `kill-line` for shells). |
| `send_message` | The safe way to message a worker TUI: clears existing input, pastes the (multi-line) text, presses Enter — all in **one** tmux call. |
| `pane_status` | Is the target Claude pane `busy` (generating) or `idle` (ready)? |
| `read_pane_delta` | Return only output **added since the last read** for that pane — not the whole screen. |
| `send_and_await_reply` | The seamless round-trip: send a message, wait for the worker to go busy→idle, return just the reply. |

**Built for speed.** A send is a single chained tmux invocation with no fixed
sleeps (~8 ms of overhead), and `send_and_await_reply` detects completion by
watching the pane rather than waiting out fixed grace windows — so a round-trip
costs roughly *model generation time + a ~0.25 s settle*, nothing more.

```
operator> send_and_await_reply("dev:0.1", "Run the tests and report failures")
worker    (goes busy… then idle)
operator< "3 tests failed in test_auth.py: ..."   # just the new reply
```

## Keeping the Mac awake (macOS)

When you want a tmux session to keep running after you walk away — or close the
lid and drive the machine over SSH — `keep_awake` toggles macOS power management
so the Mac doesn't sleep out from under your session.

| Action | What it does |
|--------|--------------|
| `keep_awake("on")` | Starts `caffeinate -dimsu` inside a dedicated tmux session named `keepawake`. Lid must stay **open**; no admin needed. Idempotent — won't double-start. |
| `keep_awake("on", lid_closed=True)` | Also runs `pmset disablesleep 1` (via an `osascript` admin prompt) so the Mac stays awake with the lid **closed**. |
| `keep_awake("off")` | Kills the `keepawake` session + its `caffeinate`, and always resets `pmset disablesleep 0` (safety net — the Mac can never get stuck no-sleep). |
| `keep_awake("status")` | Reports whether *this tool's* keep-awake is active and the current `pmset SleepDisabled` value. |

Detection is **scoped to the tool's own resources** — the `keepawake` tmux
session and the specific `caffeinate -dimsu` it launches. An unrelated
`caffeinate` running on the machine (e.g. one your terminal or another app
started) won't register as keep-awake-on, and `off` won't kill it.

> ⚠️ **Battery:** `lid_closed=True` (`pmset disablesleep`) keeps the Mac awake on
> battery too and will drain it. Use it only when plugged in, and run
> `keep_awake("off")` to revert. The default (caffeinate-only) is the safe option.

## Install

```bash
uv pip install .
```

Add to your Claude Code MCP config or any MCP-compatible client:

```json
{
  "tmux-mcp": {
    "command": "uv",
    "args": ["--directory", "/path/to/tmux-mcp", "run", "tmux-mcp"]
  }
}
```

## Tests

```bash
uv sync --extra dev
uv run pytest        # unit tests + live tmux integration (skips if tmux absent)
uv run ruff check src/ tests/
```

## Intentionally scrappy

This is a working tool, not a polished product. It does what I need it to do.

If you also think the right AI coding UI is a terminal with less noise — PRs welcome. Fork it, break it, make it yours.

## License

MIT
