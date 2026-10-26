"""Mechanical checks of a project's Claude Code memory, cheap enough to run weekly.

For the memory folder (<project transcripts>/memory) it reports:
  - index problems in MEMORY.md: long lines, files missing from the index, links to missing files
  - per memory: size, type, last "Verified/Verificato YYYY-MM-DD" date, changes since then,
    paths quoted in backticks that no longer exist, [[links]] to memories that are gone
  - which memories need a fact check by an agent (never verified, verified long ago, or changed since)
  - archive candidates: project memories no session mentioned for N days (never rules or user notes)
  - split candidates: memories over N characters
It changes nothing unless --backup is given, which copies the folder to the data dir first.

Usage:
  memory_check.py [--project <path or key>] [--json] [--backup]
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import config, data_dir, project_dir, read_text, transcripts  # noqa: E402

VERIFIED = re.compile(r"(?:Verificato|Verified)\**\s*:?\**\s*(\d{4}-\d{2}-\d{2})")
LINK = re.compile(r"\]\(([^)\s]+\.md)\)")
WIKI = re.compile(r"\[\[([^\]]+)\]\]")
PATH = re.compile(r"`((?:~|/Users|/home|/private|/opt|/Library)/[^`*?<>|]+)`")
KEEP_TYPES = {"user", "feedback", "reference"}


def frontmatter_type(text):
    if not text.startswith("---"):
        return None
    fm = text.split("---", 2)[1]
    m = re.search(r"^\s*type:\s*([A-Za-z_-]+)", fm, re.M)
    return m.group(1) if m else None


def mentioned_names(pdir, names, days):
    """Memory files read or written (by path, '/memory/<file>') in transcripts of the last `days` days.

    Matching the bare file name would count every session, because the index that
    lists all names is loaded into each one.
    """
    if not names:
        return set()
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pat = re.compile(("/memory/(" + alt + ")").encode())
    found = set()
    for f in transcripts(pdir, time.time() - days * 86400):
        with open(f, "rb") as fh:
            for m in pat.finditer(fh.read()):
                found.add(m.group(1).decode())
        if found >= set(names):
            break
    return found


def check(pdir, cfg):
    mem = os.path.join(pdir, "memory")
    if not os.path.isdir(mem):
        raise SystemExit(f"no memory folder at {mem}")
    index_path = os.path.join(mem, "MEMORY.md")
    index = read_text(index_path) if os.path.exists(index_path) else ""
    files = sorted(f for f in os.listdir(mem) if f.endswith(".md") and f != "MEMORY.md")
    linked = LINK.findall(index)
    archive_names = set()
    arch = os.path.join(os.path.dirname(mem), "memory-archive")
    for root, _, fs in os.walk(arch):
        archive_names.update(f[:-3] for f in fs if f.endswith(".md"))
    long_lines = [ln[:60] for ln in index.splitlines() if ln.startswith("- [") and len(ln) > cfg["index_line_max"]]
    today = dt.date.today()
    used = mentioned_names(pdir, files, cfg["archive_idle_days"])
    rows = []
    for f in files:
        path = os.path.join(mem, f)
        text = read_text(path)
        mtime = dt.date.fromtimestamp(os.path.getmtime(path))
        dates = VERIFIED.findall(text)
        verified = max(dates) if dates else None
        vdate = dt.date.fromisoformat(verified) if verified else None
        dead_paths = [p for p in dict.fromkeys(PATH.findall(text))
                      if not os.path.exists(os.path.expanduser(p.rstrip("/.,;:")))]
        dead_links = [w for w in dict.fromkeys(WIKI.findall(text))
                      if not os.path.exists(os.path.join(mem, w + ".md")) and w not in archive_names]
        mtype = frontmatter_type(text)
        reasons = []
        if not vdate:
            reasons.append("never verified")
        elif (today - vdate).days > cfg["reverify_days"]:
            reasons.append(f"verified {(today - vdate).days} days ago")
        elif mtime > vdate:
            reasons.append("changed since verified")
        if dead_paths:
            reasons.append(f"{len(dead_paths)} missing paths")
        rows.append({
            "file": f, "type": mtype, "chars": len(text), "modified": str(mtime), "verified": verified,
            "needs_check": reasons, "dead_paths": dead_paths[:5], "dead_links": dead_links,
            "in_index": f in linked,
            "archive_candidate": mtype not in KEEP_TYPES and f not in used and f != "memory-archive.md",
            "split_candidate": len(text) > cfg["split_chars"],
        })
    return {
        "memory_dir": mem,
        "index_chars": len(index),
        "index_entries": len(linked),
        "index_long_lines": long_lines,
        "orphans": [r["file"] for r in rows if not r["in_index"]],
        "broken_index_links": sorted(set(linked) - set(files)),
        "memories": rows,
    }


def backup(mem):
    dest_root = os.path.join(data_dir(), "backups")
    os.makedirs(dest_root, exist_ok=True)
    dest = os.path.join(dest_root, "memory-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copytree(mem, dest)
    old = sorted(d for d in os.listdir(dest_root) if d.startswith("memory-"))
    for d in old[:-4]:  # keep the last four
        shutil.rmtree(os.path.join(dest_root, d), ignore_errors=True)
    return dest


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project")
    p.add_argument("--json", action="store_true")
    p.add_argument("--backup", action="store_true")
    a = p.parse_args()
    cfg = config()
    r = check(project_dir(a.project), cfg)
    if a.backup:
        r["backup"] = backup(r["memory_dir"])
    if a.json:
        print(json.dumps(r, indent=1))
        return
    m = r["memories"]
    print(f"Memory: {len(m)} files, {sum(x['chars'] for x in m)} chars; MEMORY.md {r['index_chars']} chars, "
          f"{r['index_entries']} entries")
    if r.get("backup"):
        print(f"Backup: {r['backup']}")
    print(f"Index: {len(r['index_long_lines'])} lines over {cfg['index_line_max']} chars; "
          f"orphans {r['orphans'] or 'none'}; broken links {r['broken_index_links'] or 'none'}")
    need = [x for x in m if x["needs_check"]]
    print(f"Need a fact check ({len(need)}):")
    for x in need:
        print(f"  {x['file']}: {', '.join(x['needs_check'])}" + (f"  e.g. {x['dead_paths'][0]}" if x["dead_paths"] else ""))
    arch = [x["file"] for x in m if x["archive_candidate"]]
    print(f"Archive candidates, not mentioned in {cfg['archive_idle_days']} days ({len(arch)}): {', '.join(arch) or 'none'}")
    split = [f"{x['file']} ({x['chars']})" for x in m if x["split_candidate"]]
    print(f"Split candidates over {cfg['split_chars']} chars: {', '.join(split) or 'none'}")
    links = [f"{x['file']} -> {', '.join(x['dead_links'])}" for x in m if x["dead_links"]]
    if links:
        print("Links to memories that no longer exist: " + "; ".join(links))


if __name__ == "__main__":
    main()
