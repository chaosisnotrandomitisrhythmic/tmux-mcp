"""Tests for the key-passing layer and cross-pane communication primitives.

The regression these guard against: a multi-token key string ("C-a C-k") was
sent to `tmux send-keys` as ONE argv item, so tmux saw a single unknown key name
and typed it literally instead of sending Ctrl-A then Ctrl-K.
"""

import tmux_mcp.server as server

# ── _coerce_keys: literal-vs-named key splitting ───────────────────


def test_coerce_single_named_key():
    assert server._coerce_keys("Enter") == ["Enter"]


def test_coerce_multi_token_string_splits():
    # The core bug: must split into separate key names, not one literal token.
    assert server._coerce_keys("C-a C-k") == ["C-a", "C-k"]


def test_coerce_repeated_keys():
    assert server._coerce_keys("BSpace BSpace BSpace") == ["BSpace", "BSpace", "BSpace"]


def test_coerce_accepts_list():
    assert server._coerce_keys(["C-a", "C-k"]) == ["C-a", "C-k"]


def test_coerce_drops_empty_tokens():
    assert server._coerce_keys("  C-u   ") == ["C-u"]
    assert server._coerce_keys(["C-u", ""]) == ["C-u"]


def test_coerce_empty():
    assert server._coerce_keys("") == []


# ── argv assertions: named keys vs literal text ────────────────────


class _Recorder:
    """Drop-in for server._run_tmux that records argv (and stdin) calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, timeout=10, input_data=None):
        self.calls.append({"args": list(args), "input": input_data})
        return ""


def _patch_run_tmux(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(server, "_run_tmux", rec)
    return rec


def test_send_keys_passes_separate_argv_no_literal_flag(monkeypatch):
    rec = _patch_run_tmux(monkeypatch)
    server._send_keys("dev:0.1", "C-a C-k")
    assert len(rec.calls) == 1
    args = rec.calls[0]["args"]
    # Each key is its own argv item; crucially there is NO -l (literal) flag.
    assert args == ["send-keys", "-t", "dev:0.1", "C-a", "C-k"]
    assert "-l" not in args


def test_send_keys_single_key(monkeypatch):
    rec = _patch_run_tmux(monkeypatch)
    server._send_keys("dev:0.1", ["Enter"])
    assert rec.calls[0]["args"] == ["send-keys", "-t", "dev:0.1", "Enter"]


def test_send_keys_noop_on_empty(monkeypatch):
    rec = _patch_run_tmux(monkeypatch)
    server._send_keys("dev:0.1", "")
    assert rec.calls == []


def _split_chain(argv):
    """Split a single chained tmux argv (commands joined by ';') into sub-commands."""
    cmds, cur = [], []
    for tok in argv:
        if tok == ";":
            cmds.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        cmds.append(cur)
    return cmds


def test_send_text_uses_literal_paste_path(monkeypatch):
    rec = _patch_run_tmux(monkeypatch)
    server._send_text("dev:0.1", "echo hi\nsecond line")
    # Single chained tmux call: load-buffer (stdin) then paste-buffer. Literal
    # text never goes through send-keys key-name interpretation.
    assert len(rec.calls) == 1
    assert rec.calls[0]["input"] == "echo hi\nsecond line"
    cmds = _split_chain(rec.calls[0]["args"])
    assert [c[0] for c in cmds] == ["load-buffer", "paste-buffer"]
    assert "-p" in cmds[1]  # bracketed paste so newlines don't submit early


def test_clear_input_methods(monkeypatch):
    rec = _patch_run_tmux(monkeypatch)
    server._clear_input("dev:0.1", method="ctrl-u")
    server._clear_input("dev:0.1", method="kill-line")
    server._clear_input("dev:0.1", method="ctrl-c")
    sent = [c["args"][3:] for c in rec.calls]
    assert sent == [["C-u"], ["C-a", "C-k"], ["C-c"]]


def test_send_message_is_one_chained_call(monkeypatch):
    rec = _patch_run_tmux(monkeypatch)
    server._send_message("dev:0.1", "hello", submit=True, clear_first=True)
    # The entire send is a SINGLE tmux invocation (speed: one subprocess, no
    # inter-step sleeps). Ordering within the chain: clear -> paste -> Enter.
    assert len(rec.calls) == 1
    cmds = _split_chain(rec.calls[0]["args"])
    assert [c[0] for c in cmds] == ["send-keys", "load-buffer", "paste-buffer", "send-keys"]
    assert cmds[0][3:] == ["C-c"]      # clear
    assert cmds[-1][3:] == ["Enter"]   # submit
    assert rec.calls[0]["input"] == "hello"


def test_send_message_no_submit_no_clear(monkeypatch):
    rec = _patch_run_tmux(monkeypatch)
    server._send_message("dev:0.1", "hello", submit=False, clear_first=False)
    assert len(rec.calls) == 1
    cmds = _split_chain(rec.calls[0]["args"])
    assert [c[0] for c in cmds] == ["load-buffer", "paste-buffer"]
