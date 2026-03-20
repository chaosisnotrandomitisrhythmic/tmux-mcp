# tmux-mcp

MCP server that gives AI agents direct access to tmux.

Born out of frustration with flashy IDE integrations that make you dizzy. This is the opposite — a terminal-first, anti-distraction workflow where the AI works alongside you in tmux panes instead of taking over your screen.

Inspired by [ThePrimeagen's per-project `.tmux` files](https://frontendmasters.com/courses/developer-productivity-v2/). Same idea, but as YAML layouts that an AI agent can execute in one shot.

## What it does

13 tools. ~250 lines of Python. That's the whole thing.

**Session/pane basics:** create sessions, split panes, send commands, read output.

**Window management:** create, list, select, rename windows.

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

## Intentionally scrappy

This is a working tool, not a polished product. No tests, no CI, no docs site. It does what I need it to do.

If you also think the right AI coding UI is a terminal with less noise — PRs welcome. Fork it, break it, make it yours.

## License

MIT
