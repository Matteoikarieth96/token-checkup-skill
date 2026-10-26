"""What every session loads before the first answer, and what grew.

Checks the files you control (CLAUDE.md files, the memory index, skill
descriptions, hooks, compaction settings) and compares what transcripts record
before the first answer (tool lists, server instructions, skill listing, hook
text) between this window and the one before, to catch silent growth.

Usage:
  audit_start.py --project <folder> [--days 7] [--json]
  audit_start.py --project=KEY [--days 7] [--json]   # keys start with "-", so pass them with "="

--project is the project folder (default: the current folder); its CLAUDE.md,
.claude/CLAUDE.md and CLAUDE.local.md are read. A transcript folder key works too,
written as --project=KEY, but then the project's own CLAUDE.md files are not read.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_HELP, claude_dir, clean, config, read_json, read_text, resolve_project,  # noqa: E402
                    session_stats, transcripts)

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


def hook_commands(groups):
    """Command strings of one hook event, skipping entries with an unexpected shape."""
    out = []
    for g in groups if isinstance(groups, list) else []:
        for h in (g.get("hooks") if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []):
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                out.append(h["command"])
    return out


def settings_summary():
    try:
        s = read_json(os.path.join(claude_dir(), "settings.json"))
    except (OSError, ValueError):
        return {"error": "settings.json not readable"}
    if not isinstance(s, dict):
        return {"error": "settings.json is not a JSON object"}
    all_hooks = s.get("hooks") if isinstance(s.get("hooks"), dict) else {}
    hooks = {event: len(hook_commands(groups)) for event, groups in all_hooks.items()}
    guard = any("context_guard.py" in c for c in hook_commands(all_hooks.get("UserPromptSubmit")))
    senv = s.get("env") if isinstance(s.get("env"), dict) else {}
    env = {key: senv[key] for key in COMPACT_ENV if senv.get(key)}
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
    p.add_argument("--project", help=PROJECT_HELP)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    if a.days < 1:
        p.error("--days must be 1 or more")
    cfg = config()
    pdir, folder = resolve_project(a.project)
    if not a.json:
        print(f"project: {os.path.basename(pdir)}")
    now = time.time()
    span = a.days * 86400
    cur, n_cur = start_profile(transcripts(pdir, now - span, now), cfg)
    prev, n_prev = start_profile(transcripts(pdir, now - 2 * span, now - span), cfg)
    files = {"~/.claude/CLAUDE.md": size(os.path.join(claude_dir(), "CLAUDE.md"))}
    if folder:
        for rel in ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"), "CLAUDE.local.md"):
            files["<project>/" + rel] = size(os.path.join(folder, rel))
    files["memory/MEMORY.md"] = size(os.path.join(pdir, "memory", "MEMORY.md"))
    skills = skill_descriptions()
    settings = settings_summary()
    growth = sorted(((key, prev.get(key, 0), cur.get(key, 0)) for key in set(cur) | set(prev)),
                    key=lambda x: -(x[2] - x[1]))
    result = {"project": os.path.basename(pdir), "project_folder": folder, "files": files, "skills": skills,
              "settings": settings,
              "start_profile": {"sessions_now": n_cur, "sessions_before": n_prev, "rows": growth}}
    if a.json:
        print(json.dumps(result, indent=1))
        return
    print(f"Start-of-session audit ({os.path.basename(pdir)})")
    print("Files loaded into every session (bytes, ~tokens = bytes/4):")
    if folder:
        print(f"  <project> = {folder}")
    else:
        print("  the project's CLAUDE.md files were not read: pass the project folder as --project to include them")
    for name, b in files.items():
        if b is not None:
            print(f"  {name:30s} {b:7d}  ~{b // 4} tokens")
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
        print(f"  {clean(key, 40):40s} {b0:8.0f} -> {b1:8.0f} bytes  ({b1 - b0:+.0f} bytes, about {(b1 - b0) / 4:+.0f} tokens)")
    print("Note: the system prompt and tool definitions are not in transcripts; the first-call context by "
          "Claude Code version in measure.py shows growth that comes from app updates.")


if __name__ == "__main__":
    main()
