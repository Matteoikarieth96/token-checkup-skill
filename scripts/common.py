"""Shared helpers for token-checkup: paths, config, transcript parsing.

Standard library only. Everything is read locally; nothing is sent anywhere.
"""
import datetime as dt
import json
import math
import os
import re
import stat
import unicodedata

DEFAULTS = {
    "compact_window": 300_000,      # autoCompactWindow that setup.py writes
    "guard_bands": [250_000, 400_000, 550_000, 700_000, 850_000],
    "reverify_days": 28,            # memories verified longer ago get re-checked
    "archive_idle_days": 30,        # project memories not mentioned for this long are archive candidates
    "split_chars": 10_000,          # memories bigger than this are split candidates
    "index_line_max": 150,          # MEMORY.md line length limit
    "image_tokens": 1600,           # rough cost of one image in a tool result
    "auto_fix_memory": False,       # true only if the user said yes at setup: fact fixes are applied, not proposed
}

INT_KEYS = ("compact_window", "reverify_days", "archive_idle_days", "split_chars", "index_line_max", "image_tokens")

PROJECT_HELP = ("project folder (a path), or a transcript folder key given as --project=KEY "
                "(keys start with '-'). Default: the current folder.")


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


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def config():
    """DEFAULTS, overridden by the keys of config.json that have the right type."""
    cfg = {key: (list(v) if isinstance(v, list) else v) for key, v in DEFAULTS.items()}
    try:
        with open(os.path.join(data_dir(), "config.json")) as f:
            user = json.load(f)
    except (OSError, ValueError):
        return cfg
    if not isinstance(user, dict):
        return cfg
    for key in INT_KEYS:
        if is_int(user.get(key)) and user[key] >= 0:
            cfg[key] = user[key]
    bands = user.get("guard_bands")
    if isinstance(bands, list) and bands and all(is_int(b) and b > 0 for b in bands):
        cfg["guard_bands"] = sorted(bands)
    if isinstance(user.get("auto_fix_memory"), bool):
        cfg["auto_fix_memory"] = user["auto_fix_memory"]
    return cfg


def install_record():
    """install.json written by setup.py, or {} when missing or unreadable."""
    try:
        rec = read_json(os.path.join(data_dir(), "install.json"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def project_key(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.path.expanduser(path)))


def resolve_project(arg=None):
    """(transcript folder, project folder or None) for --project.

    arg is a path (mapped to projects/<project_key(path)>) or a bare key: a name with
    no "/" that exists as a folder directly under projects/. No arg: the cwd. The
    argument itself is never used as the transcript folder.
    """
    base = os.path.normpath(os.path.join(claude_dir(), "projects"))
    folder = None
    if arg and "/" not in arg and arg not in (".", "..") and "\0" not in arg \
            and os.path.isdir(os.path.join(base, arg)):
        cand = os.path.join(base, arg)
    else:
        folder = os.path.abspath(os.path.expanduser(arg)) if arg else _cwd(base)
        cand = os.path.join(base, project_key(folder))
    cand = os.path.normpath(cand)
    if os.path.dirname(cand) != base or not os.path.isdir(cand):
        if arg:
            raise SystemExit(f"no Claude Code transcripts for {arg!r} (looked for {cand}). Pass the project "
                             "folder, or a key from that folder as --project=KEY.")
        raise SystemExit(f"no Claude Code transcripts for the current folder {folder} (looked for {cand}). "
                         "Pass --project <project folder>.")
    return cand, folder


def _cwd(base):
    """The current folder as the shell names it ($PWD, if it is the same folder), else os.getcwd()."""
    cwd, pwd = os.getcwd(), os.environ.get("PWD")
    try:
        if pwd and os.path.isabs(pwd) and pwd != cwd and os.path.samefile(pwd, cwd) \
                and os.path.isdir(os.path.join(base, project_key(pwd))):
            return pwd
    except OSError:
        pass
    return cwd


def project_dir(arg=None):
    """Transcript folder of a project (see resolve_project)."""
    return resolve_project(arg)[0]


def regular_files(folder, suffix):
    """[(path, lstat)] of regular files ending in suffix directly in folder; never symlinks or folders."""
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not name.endswith(suffix):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            out.append((path, st))
    return out


def transcripts(pdir, lo=None, hi=None):
    """Main-session transcripts whose last activity falls in [lo, hi)."""
    out = [(st.st_mtime, f) for f, st in regular_files(pdir, ".jsonl")
           if (lo is None or st.st_mtime >= lo) and (hi is None or st.st_mtime < hi)]
    return [f for _, f in sorted(out)]


def num(v):
    """A token count: int for finite non-negative int/float, 0 for anything else."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v if v >= 0 else 0
    if isinstance(v, float) and math.isfinite(v) and v >= 0:
        return int(v)
    return 0


def as_dict(v):
    return v if isinstance(v, dict) else {}


def as_str(v):
    return v if isinstance(v, str) else None


def context_of(usage):
    usage = as_dict(usage)
    return (num(usage.get("input_tokens")) + num(usage.get("cache_read_input_tokens"))
            + num(usage.get("cache_creation_input_tokens")))


def session_stats(path, image_tokens=DEFAULTS["image_tokens"]):
    """Per-session numbers from one transcript, or None if it has no model calls."""
    st = {"seen": set(), "ctxs": [], "tools": {}, "names": {}, "start_bytes": {}, "compactions": [],
          "first_answer": False, "version": None, "images": 0, "cache_read": 0, "out": 0,
          "pending_compact": None}
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if not isinstance(x, dict):
                continue
            try:
                _session_line(st, x, line, image_tokens)
            except Exception:  # one odd record never aborts a run
                continue
    ctxs = st["ctxs"]
    if not ctxs:
        return None
    born = os.stat(path)
    return {
        "id": os.path.basename(path)[:8],
        "born": getattr(born, "st_birthtime", born.st_mtime),
        "version": st["version"],
        "first": ctxs[0],
        "peak": max(ctxs),
        "series": ctxs,
        "calls": len(ctxs),
        "cache_read": st["cache_read"],
        "out": st["out"],
        "compactions": st["compactions"],
        "tools": st["tools"],
        "images": st["images"],
        "start_bytes": st["start_bytes"],
    }


def _session_line(st, x, line, image_tokens):
    st["version"] = st["version"] or as_str(x.get("version"))
    kind = as_str(x.get("type"))
    if kind == "system" and x.get("subtype") == "compact_boundary":
        st["pending_compact"] = st["ctxs"][-1] if st["ctxs"] else 0
        return
    if not st["first_answer"] and kind != "assistant":
        sub = as_str(as_dict(x.get("attachment")).get("type")) or as_str(x.get("subtype")) or ""
        key = (kind or "?") + ":" + sub
        st["start_bytes"][key] = st["start_bytes"].get(key, 0) + len(line)
    msg = as_dict(x.get("message"))
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                n = as_str(b.get("name")) or "?"
                tid = as_str(b.get("id"))
                if tid is not None:
                    st["names"][tid] = n.split("__")[-1] if n.startswith("mcp__") else n
            elif b.get("type") == "tool_result":
                tid = as_str(b.get("tool_use_id"))
                n, c = st["names"].get(tid, "?") if tid is not None else "?", b.get("content")
                add = 0
                if isinstance(c, str):
                    add = len(c) / 4
                elif isinstance(c, list):
                    for s in c:
                        if not isinstance(s, dict):
                            continue
                        if s.get("type") == "image":
                            st["images"] += 1
                            add += image_tokens
                        else:
                            add += len(as_str(s.get("text")) or "") / 4
                st["tools"][n] = st["tools"].get(n, 0) + add
    if kind != "assistant" or x.get("isSidechain"):
        return
    st["first_answer"] = True
    usage, mid = msg.get("usage"), as_str(msg.get("id"))
    if not isinstance(usage, dict) or not usage:
        return
    if mid is not None:
        if mid in st["seen"]:
            return
        st["seen"].add(mid)
    ctx = context_of(usage)
    if not ctx:
        return
    if st["pending_compact"] is not None:
        st["compactions"].append((st["pending_compact"], ctx))
        st["pending_compact"] = None
    st["ctxs"].append(ctx)
    st["cache_read"] += num(usage.get("cache_read_input_tokens"))
    st["out"] += num(usage.get("output_tokens"))


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
        if not isinstance(x, dict):
            continue
        try:
            if x.get("type") == "system" and x.get("subtype") == "compact_boundary":
                return 0
            if x.get("type") != "assistant" or x.get("isSidechain"):
                continue
            usage = as_dict(as_dict(x.get("message")).get("usage"))
            ctx = context_of(usage)
            if ctx:
                return ctx + num(usage.get("output_tokens"))
        except Exception:  # one odd record never aborts a run
            continue
    return None


def parse_iso(s):
    """Timestamp of an ISO date or date-time string, or None."""
    if not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def clean(text, limit=120):
    """Text safe to print: no control or format characters, at most `limit` characters."""
    s = "".join(c for c in str(text) if unicodedata.category(c) not in ("Cc", "Cf"))
    return s if len(s) <= limit else s[:limit - 3] + "..."


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
