"""What every session loads before the first answer, and what grew.

Checks the files you control (CLAUDE.md files, the memory index, skill
descriptions, hooks, compaction settings) and compares what transcripts record
before the first answer (tool lists, server instructions, skill listing, hook
text) between this window and the one before, to catch silent growth.

Usage:
  audit_start.py [--project <path or key>] [--days 7] [--json]
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import claude_dir, config, k, project_dir, read_json, read_text, session_stats, transcripts  # noqa: E402

COMPACT_ENV = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "DISABLE_AUTO_COMPACT")


def size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def skill_descriptions():
    out = []
    for f in sorted(os.listdir(os.path.join(claude_dir(), "skills")) if os.path.isdir(os.path.join(claude_dir(), "skills")) else []):
        p = os.path.join(claude_dir(), "skills", f, "SKILL.md")
        try:
            text = read_text(p)
        except OSError:
            continue
        fm = text.split("---", 2)[1] if text.startswith("---") else ""
        m = re.search(r"^description:(.*(?:\n[ \t].*)*)", fm, re.M)
        out.append((f, len(m.group(1).strip()) if m else 0))
    return sorted(out, key=lambda x: -x[1])


def settings_summary():
    try:
        s = read_json(os.path.join(claude_dir(), "settings.json"))
    except (OSError, ValueError):
        return {"error": "settings.json not readable"}
    hooks = {}
    for event, groups in (s.get("hooks") or {}).items():
        cmds = [h.get("command", "") for g in groups for h in g.get("hooks", [])]
        hooks[event] = len(cmds)
    guard = any("context_guard.py" in h.get("command", "")
                for g in (s.get("hooks") or {}).get("UserPromptSubmit", []) for h in g.get("hooks", []))
    env = {key: (s.get("env") or {}).get(key) for key in COMPACT_ENV if (s.get("env") or {}).get(key)}
    env.update({key: os.environ[key] for key in COMPACT_ENV if os.environ.get(key)})
    return {"autoCompactWindow": s.get("autoCompactWindow"), "guard_hook": guard, "hooks": hooks, "compact_env": env}


def start_profile(files, cfg):
    """Mean bytes per session recorded before the first answer, by entry type."""
    totals, n = {}, 0
    for f in files:
        r = session_stats(f, cfg["image_tokens"])
        if not r:
            continue
        n += 1
        for key, b in r["start_bytes"].items():
            if key.startswith(("api-request", "user:", "assistant")):
                continue
            totals[key] = totals.get(key, 0) + b
    return {key: v / n for key, v in totals.items()} if n else {}, n


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    cfg = config()
    pdir = project_dir(a.project)
    now = time.time()
    span = a.days * 86400
    cur, n_cur = start_profile(transcripts(pdir, now - span, now), cfg)
    prev, n_prev = start_profile(transcripts(pdir, now - 2 * span, now - span), cfg)
    files = {
        "~/.claude/CLAUDE.md": size(os.path.join(claude_dir(), "CLAUDE.md")),
        "./CLAUDE.md": size("CLAUDE.md"),
        "./.claude/CLAUDE.md": size(os.path.join(".claude", "CLAUDE.md")),
        "./CLAUDE.local.md": size("CLAUDE.local.md"),
        "memory/MEMORY.md": size(os.path.join(pdir, "memory", "MEMORY.md")),
    }
    skills = skill_descriptions()
    settings = settings_summary()
    growth = sorted(((key, prev.get(key, 0), cur.get(key, 0)) for key in set(cur) | set(prev)),
                    key=lambda x: -(x[2] - x[1]))
    result = {"project": os.path.basename(pdir), "files": files, "skills": skills, "settings": settings,
              "start_profile": {"sessions_now": n_cur, "sessions_before": n_prev, "rows": growth}}
    if a.json:
        print(json.dumps(result, indent=1))
        return
    print(f"Start-of-session audit ({os.path.basename(pdir)})")
    print("Files loaded into every session (bytes, ~tokens = bytes/4):")
    for name, b in files.items():
        if b is not None:
            print(f"  {name:24s} {b:7d}  ~{b // 4} tokens")
    total = sum(n for _, n in skills)
    print(f"Skill descriptions: {len(skills)} skills, {total} chars (~{total // 4} tokens); longest: "
          + ", ".join(f"{s} {n}" for s, n in skills[:4]))
    st = settings
    if "error" in st:
        print("Settings: " + st["error"])
    else:
        win = st["autoCompactWindow"]
        print(f"Compaction: autoCompactWindow={win if win else 'not set (1M models compact near ~967k)'}"
              + (f"; env overrides {st['compact_env']}" if st["compact_env"] else ""))
        print(f"Context guard hook: {'installed' if st['guard_hook'] else 'not installed'}; hooks per event: {st['hooks']}")
    print(f"Recorded before the first answer, mean bytes per session (last {a.days} days n={n_cur} "
          f"vs the {a.days} before n={n_prev}); biggest growth first:")
    for key, b0, b1 in growth[:8]:
        print(f"  {key:40s} {b0:8.0f} -> {b1:8.0f}  ({k((b1 - b0) / 4)} tokens)")
    print("Note: the system prompt and tool definitions are not in transcripts; the first-call context by "
          "Claude Code version in measure.py shows growth that comes from app updates.")


if __name__ == "__main__":
    main()
