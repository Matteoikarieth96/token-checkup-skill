"""Shared helpers for token-checkup: paths, config, transcript parsing.

Standard library only. Everything is read locally; nothing is sent anywhere.
"""
import glob
import json
import os
import re

DEFAULTS = {
    "compact_window": 300_000,      # autoCompactWindow that setup.py writes
    "guard_bands": [250_000, 400_000, 550_000, 700_000, 850_000],
    "reverify_days": 28,            # memories verified longer ago get re-checked
    "archive_idle_days": 30,        # project memories not mentioned for this long are archive candidates
    "split_chars": 10_000,          # memories bigger than this are split candidates
    "index_line_max": 150,          # MEMORY.md line length limit
    "image_tokens": 1600,           # rough cost of one image in a tool result
    "auto_fix_memory": True,        # weekly runs may correct proved-wrong facts in memory files
}


def read_text(path):
    with open(path, errors="ignore") as f:
        return f.read()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def claude_dir():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))


def data_dir():
    return os.path.join(claude_dir(), "token-checkup")


def config():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(data_dir(), "config.json")) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def project_key(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.path.expanduser(path)))


def project_dir(arg=None):
    """Transcript folder of a project: a key, a path, the cwd, or the most recent one."""
    base = os.path.join(claude_dir(), "projects")
    for cand in ([arg, project_key(arg)] if arg else [project_key(os.getcwd())]):
        if cand and os.path.isdir(os.path.join(base, cand)):
            return os.path.join(base, cand)
    if arg:
        raise SystemExit(f"no Claude Code project found for {arg!r} under {base}")
    dirs = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)]
    if not dirs:
        raise SystemExit(f"no Claude Code projects under {base}")
    return max(dirs, key=os.path.getmtime)


def transcripts(pdir, lo=None, hi=None):
    """Main-session transcripts whose last activity falls in [lo, hi)."""
    out = []
    for f in glob.glob(os.path.join(pdir, "*.jsonl")):
        t = os.path.getmtime(f)
        if (lo is None or t >= lo) and (hi is None or t < hi):
            out.append(f)
    return sorted(out, key=os.path.getmtime)


def context_of(usage):
    return (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


def session_stats(path, image_tokens=DEFAULTS["image_tokens"]):
    """Per-session numbers from one transcript, or None if it has no model calls."""
    seen, ctxs, tool_tok = set(), [], {}
    names, start_bytes, compactions = {}, {}, []
    first_answer, version, images, cache_read, out = False, None, 0, 0, 0
    pending_compact = None
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                x = json.loads(line)
            except ValueError:
                continue
            version = version or x.get("version")
            kind = x.get("type")
            if kind == "system" and x.get("subtype") == "compact_boundary":
                pending_compact = ctxs[-1] if ctxs else 0
                continue
            if not first_answer and kind != "assistant":
                key = kind or "?"
                sub = (x.get("attachment") or {}).get("type") if isinstance(x.get("attachment"), dict) else None
                key += ":" + str(sub or x.get("subtype") or "")
                start_bytes[key] = start_bytes.get(key, 0) + len(line)
            msg = x.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        n = b.get("name", "?")
                        names[b.get("id")] = n.split("__")[-1] if n.startswith("mcp__") else n
                    elif b.get("type") == "tool_result":
                        n, c = names.get(b.get("tool_use_id"), "?"), b.get("content")
                        add = 0
                        if isinstance(c, str):
                            add = len(c) / 4
                        elif isinstance(c, list):
                            for s in c:
                                if not isinstance(s, dict):
                                    continue
                                if s.get("type") == "image":
                                    images += 1
                                    add += image_tokens
                                else:
                                    add += len(s.get("text", "")) / 4
                        tool_tok[n] = tool_tok.get(n, 0) + add
            if kind != "assistant" or x.get("isSidechain"):
                continue
            first_answer = True
            usage, mid = msg.get("usage"), msg.get("id")
            if not usage or mid in seen:
                continue
            seen.add(mid)
            ctx = context_of(usage)
            if not ctx:
                continue
            if pending_compact is not None:
                compactions.append((pending_compact, ctx))
                pending_compact = None
            ctxs.append(ctx)
            cache_read += usage.get("cache_read_input_tokens", 0)
            out += usage.get("output_tokens", 0)
    if not ctxs:
        return None
    born = os.stat(path)
    return {
        "id": os.path.basename(path)[:8],
        "born": getattr(born, "st_birthtime", born.st_mtime),
        "version": version,
        "first": ctxs[0],
        "peak": max(ctxs),
        "series": ctxs,
        "calls": len(ctxs),
        "cache_read": cache_read,
        "out": out,
        "compactions": compactions,
        "tools": tool_tok,
        "images": images,
        "start_bytes": start_bytes,
    }


def last_context(path, tail_bytes=4_000_000):
    """Context size at the last main-thread call; 0 right after a compaction; None if unknown."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - tail_bytes))
        lines = f.read().splitlines()
    for raw in reversed(lines):
        if b"compact_boundary" not in raw and b'"usage"' not in raw:
            continue
        try:
            x = json.loads(raw)
        except ValueError:
            continue
        if x.get("type") == "system" and x.get("subtype") == "compact_boundary":
            return 0
        if x.get("type") != "assistant" or x.get("isSidechain"):
            continue
        usage = (x.get("message") or {}).get("usage") or {}
        if usage:
            return context_of(usage) + usage.get("output_tokens", 0)
    return None


def median(values):
    v = sorted(values)
    if not v:
        return 0
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def k(n):
    """Short token count: 85k, 7.0M, 1.76B."""
    a = abs(n)
    if a >= 1e9:
        return f"{n / 1e9:.2f}B"
    if a >= 1e6:
        return f"{n / 1e6:.1f}M"
    return f"{n / 1000:.0f}k"
