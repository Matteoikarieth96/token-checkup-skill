"""Mechanical checks of a project's Claude Code memory, cheap enough to run weekly.

For the memory folder (~/.claude/projects/<key>/memory) it reports:
  - index problems in MEMORY.md: long lines, files missing from the index, links to files that
    do not exist (%xx decoded first), links that point outside the memory folder. A line is to
    drop only when every link on it is broken; a line with a broken link and another link is
    "mixed" and only a proposal
  - per memory: size, type, last "Verified/Verificato YYYY-MM-DD" date (invalid and future dates
    are ignored), changes since then, paths quoted in backticks that no longer exist (as written
    and with "~" expanded), path text that looks unusual (reported, never checked), [[links]] to
    memories that are gone
  - which memories need a fact check by an agent (never verified, verified long ago, or changed
    since), and which wait for the user (a line starting with "**Needs user")
  - archive candidates: project memories no session mentioned for N days (never rules or user notes)
  - split candidates: memories over N characters
Only regular *.md files count: symlinks and folders are skipped. It changes nothing unless
--backup is given, which first copies the regular *.md files of the folder (subfolders too,
symlinks never followed) to <data dir>/backups/memory/<project key>/<timestamp> and keeps the
last four copies of that project. Flat backups/memory-* folders of earlier versions are left alone.

Usage:
  memory_check.py --project <folder> [--json] [--backup]
  memory_check.py --project=KEY [--json] [--backup]   # keys start with "-", so pass them with "="
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PROJECT_HELP, clean, config, data_dir, project_dir, read_text, regular_files, transcripts  # noqa: E402

VERIFIED = re.compile(r"(?:Verificato|Verified)[*\s:]{0,8}(\d{4}-\d{2}-\d{2})")
NEEDS_USER = re.compile(r"^\*\*Needs user", re.M)
LINK = re.compile(r"\]\(([^)\s]{1,200}\.md)\)")
WIKI = re.compile(r"\[\[([^\]\n]{1,200})\]\]")
PATH = re.compile(r"`((?:~|/Users|/home|/root|/private|/opt|/Library|/var|/tmp|/mnt|/srv)/[^`\n*?]{0,290})`")
TYPE = re.compile(r"^[ \t]*type:[ \t]*[\"']?([A-Za-z_-]+)", re.M)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
UNUSUAL = set("$`;&|()<>")
BACKUP_NAME = re.compile(r"\d{8}-\d{6}-\d{6}(?:-\d+)?")
KEEP_TYPES = {"user", "feedback", "reference"}
KEEP_BACKUPS = 4


def frontmatter_type(text):
    if not text.startswith("---"):
        return None
    m = TYPE.search(text.split("---", 2)[1])
    return m.group(1) if m else None


def verified_date(text, today):
    """Latest valid Verified/Verificato date that is not in the future, or None."""
    best = None
    for s in VERIFIED.findall(text):
        try:
            d = dt.date.fromisoformat(s)
        except ValueError:
            continue
        if d <= today and (best is None or d > best):
            best = d
    return best


def unusual(path_text):
    return any(c in UNUSUAL or unicodedata.category(c) == "Cc" for c in path_text)


def resolve_link(mem, link):
    """(normalized path, inside the memory folder?) of an index link: %xx decoded, relative to the memory folder."""
    p = os.path.normpath(os.path.join(mem, unquote(link)))
    return p, p.startswith(mem + os.sep)


def index_links(mem, index):
    """Index links checked line by line.

    A line is to drop only when every link on it points to a missing file inside the
    memory folder. A line with a broken link and any other link (a working one, a URL or
    one outside the memory folder) is "mixed": a proposal, never dropped.
    """
    r = {"indexed": set(), "broken": set(), "outside": set(), "drop": [], "mixed": [], "count": 0}
    for n, line in enumerate(index.splitlines(), 1):
        bad, other = [], []
        for link in LINK.findall(line):
            r["count"] += 1
            if SCHEME.match(link):  # a web or other URL, not a memory file
                other.append(link)
                continue
            p, inside = resolve_link(mem, link)
            if not inside:
                r["outside"].add(link)
                other.append(link)
                continue
            r["indexed"].add(p)
            (other if os.path.exists(p) else bad).append(link)
        if bad:
            r["broken"].update(bad)
            if other:
                r["mixed"].append({"line": n, "broken": bad, "other": other})
            else:
                r["drop"].append({"line": n, "broken": bad})
    return r


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
    mem = os.path.normpath(os.path.join(pdir, "memory"))
    if not os.path.isdir(mem):
        raise SystemExit(f"no memory folder at {mem}: this project has no auto-memory, so skip the memory step")
    index_path = os.path.join(mem, "MEMORY.md")
    index_files = {os.path.basename(p) for p, _ in regular_files(mem, ".md")}
    index = read_text(index_path) if "MEMORY.md" in index_files else ""
    entries = {os.path.basename(p): st for p, st in regular_files(mem, ".md") if os.path.basename(p) != "MEMORY.md"}
    files = sorted(entries)
    links = index_links(mem, index)
    archive_names = set()
    arch = os.path.join(os.path.dirname(mem), "memory-archive")
    for _, _, fs in os.walk(arch):
        archive_names.update(f[:-3] for f in fs if f.endswith(".md"))
    long_lines = [ln[:60] for ln in index.splitlines() if ln.startswith("- [") and len(ln) > cfg["index_line_max"]]
    today = dt.date.today()
    used = mentioned_names(pdir, files, cfg["archive_idle_days"])
    rows, unreadable = [], []
    for f in files:
        path = os.path.join(mem, f)
        try:
            text = read_text(path)
        except OSError:
            unreadable.append(f)
            continue
        mtime = dt.date.fromtimestamp(entries[f].st_mtime)
        waiting = bool(NEEDS_USER.search(text))
        vdate = None if waiting else verified_date(text, today)
        found = list(dict.fromkeys(PATH.findall(text)))
        odd_paths = [p for p in found if unusual(p)]
        dead_paths = []
        for p in found:
            full = os.path.expanduser(p.rstrip("/.,;:"))
            if p not in odd_paths and not os.path.exists(full):
                dead_paths.append({"written": p, "expanded": full})
        dead_links = [w for w in dict.fromkeys(WIKI.findall(text))
                      if not os.path.exists(os.path.join(mem, w + ".md")) and w not in archive_names]
        mtype = frontmatter_type(text)
        reasons = []
        if waiting:
            reasons.append("waiting for the user")
        elif not vdate:
            reasons.append("never verified")
        elif (today - vdate).days > cfg["reverify_days"]:
            reasons.append(f"verified {(today - vdate).days} days ago")
        elif mtime > vdate:
            reasons.append("changed since verified")
        if dead_paths:
            reasons.append(f"{len(dead_paths)} missing paths")
        rows.append({
            "file": f, "type": mtype, "chars": len(text), "modified": str(mtime),
            "verified": str(vdate) if vdate else None, "waiting_for_user": waiting,
            "needs_check": reasons, "dead_paths": dead_paths[:5], "unusual_paths": odd_paths[:5],
            "dead_links": dead_links, "in_index": path in links["indexed"],
            "archive_candidate": mtype not in KEEP_TYPES and f not in used and f != "memory-archive.md",
            "split_candidate": len(text) > cfg["split_chars"],
        })
    return {
        "memory_dir": mem,
        "auto_fix_memory": cfg["auto_fix_memory"],
        "index_chars": len(index),
        "index_entries": links["count"],
        "index_long_lines": long_lines,
        "orphans": [r["file"] for r in rows if not r["in_index"]],
        "broken_index_links": sorted(links["broken"]),
        "index_lines_to_drop": links["drop"],
        "mixed_index_lines": links["mixed"],
        "outside_index_links": sorted(links["outside"]),
        "unreadable": unreadable,
        "memories": rows,
    }


def backup(mem, key=None):
    """Copy the regular *.md files of the memory folder (subfolders too) to
    <data dir>/backups/memory/<key>/<timestamp>.

    key is the project key (default: the name of the folder that holds the memory folder).
    Symlinks are never followed. Errors are collected, not raised. Keeps the last
    KEEP_BACKUPS timestamp folders of this project only; other projects, other names and
    the flat backups/memory-* folders of earlier versions are never deleted.
    """
    key = key or os.path.basename(os.path.dirname(os.path.normpath(mem)))
    if key in ("", ".", "..") or os.sep in key or "\0" in key:
        raise ValueError(f"not a project key: {key!r}")
    root = os.path.join(data_dir(), "backups")
    os.makedirs(root, mode=0o700, exist_ok=True)
    for part in ("memory", key):
        root = os.path.join(root, part)
        os.makedirs(root, mode=0o700, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest, n = os.path.join(root, stamp), 0
    while True:
        try:
            os.mkdir(dest, 0o700)
            break
        except FileExistsError:
            n += 1
            dest = os.path.join(root, f"{stamp}-{n}")
    copied, errors = 0, []

    def walk_error(e):
        errors.append(f"{clean(os.path.relpath(e.filename or mem, mem))}: {e.strerror or e}")

    for dirpath, dirnames, filenames in os.walk(mem, onerror=walk_error):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, mem)
            try:
                if not stat.S_ISREG(os.lstat(src).st_mode):
                    continue
                out = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(out), mode=0o700, exist_ok=True)
                shutil.copy2(src, out, follow_symlinks=False)
                copied += 1
            except OSError as e:
                errors.append(f"{clean(rel)}: {e.strerror or e}")
    try:
        old = sorted(d for d in os.listdir(root) if BACKUP_NAME.fullmatch(d)
                     and os.path.isdir(os.path.join(root, d)) and not os.path.islink(os.path.join(root, d)))
        for d in old[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    except OSError as e:
        errors.append(f"rotation: {e.strerror or e}")
    return {"path": dest, "files": copied, "errors": errors}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", help=PROJECT_HELP)
    p.add_argument("--json", action="store_true")
    p.add_argument("--backup", action="store_true")
    a = p.parse_args()
    cfg = config()
    pdir = project_dir(a.project)
    if not a.json:
        print(f"project: {os.path.basename(pdir)}")
    r = check(pdir, cfg)
    r["project"] = os.path.basename(pdir)
    if a.backup:
        b = backup(r["memory_dir"], os.path.basename(pdir))
        r["backup"], r["backup_files"], r["backup_errors"] = b["path"], b["files"], b["errors"]
    if a.json:
        print(json.dumps(r, indent=1))
        return
    m = r["memories"]

    def names(items):
        return ", ".join(clean(x) for x in items) if items else "none"

    print(f"Memory: {len(m)} files, {sum(x['chars'] for x in m)} chars; MEMORY.md {r['index_chars']} chars, "
          f"{r['index_entries']} entries")
    print("Fact fixes: " + ("applied by the fact-check agents (auto_fix_memory true)" if r["auto_fix_memory"]
                            else "proposals only (auto_fix_memory false)"))
    if a.backup:
        print(f"Backup: {r['backup']} ({r['backup_files']} files)")
        if r["backup_errors"]:
            print("Backup errors: " + "; ".join(clean(e) for e in r["backup_errors"][:10]))
    def lines(items):
        shown = [f"line {x['line']} ({names(x['broken'])})" for x in items[:20]]
        return "; ".join(shown) + (f"; and {len(items) - 20} more (see --json)" if len(items) > 20 else "")

    print(f"Index: {len(r['index_long_lines'])} lines over {cfg['index_line_max']} chars; "
          f"orphans {names(r['orphans'])}; broken links {names(r['broken_index_links'])}")
    print("Index lines to drop, every link on them broken: " + (lines(r["index_lines_to_drop"]) or "none"))
    if r["mixed_index_lines"]:
        print("Mixed index lines, some links broken and some not (a proposal, not dropped): "
              + lines(r["mixed_index_lines"]))
    if r["outside_index_links"]:
        print("Index links outside the memory folder (left alone): " + names(r["outside_index_links"]))
    if r["unreadable"]:
        print("Unreadable memory files: " + names(r["unreadable"]))
    need = [x for x in m if x["needs_check"] and not x["waiting_for_user"]]
    print(f"Need a fact check ({len(need)}):")
    for x in need:
        print(f"  {clean(x['file'])}: {', '.join(x['needs_check'])}"
              + (f"  e.g. {clean(x['dead_paths'][0]['expanded'], 400)}" if x["dead_paths"] else ""))
    waiting = [x["file"] for x in m if x["waiting_for_user"]]
    print(f"Waiting for the user ({len(waiting)}): {names(waiting)}")
    odd = [f"{clean(x['file'])} -> {names(x['unusual_paths'])}" for x in m if x["unusual_paths"]]
    if odd:
        print("Unusual path text, not checked: " + "; ".join(odd))
    arch = [x["file"] for x in m if x["archive_candidate"]]
    print(f"Archive candidates, not mentioned in {cfg['archive_idle_days']} days ({len(arch)}): {names(arch)}")
    split = [f"{clean(x['file'])} ({x['chars']})" for x in m if x["split_candidate"]]
    print(f"Split candidates over {cfg['split_chars']} chars: {', '.join(split) or 'none'}")
    links = [f"{clean(x['file'])} -> {names(x['dead_links'])}" for x in m if x["dead_links"]]
    if links:
        print("Links to memories that no longer exist: " + "; ".join(links))


if __name__ == "__main__":
    main()
