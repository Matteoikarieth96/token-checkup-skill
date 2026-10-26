"""Mechanical checks of a project's Claude Code memory, cheap enough to run weekly.

For the memory folder (~/.claude/projects/<key>/memory) it reports:
  - index problems in MEMORY.md: long lines, files missing from the index, links to files that
    do not exist (a link works if its raw text or its %xx-decoded text names a file inside the
    memory folder), links that point outside the memory folder. A line with a broken link is a
    "drop candidate" only when it is a plain "- [title](file.md) text" line with that one link
    and no other link, [[link]], URL or backtick; any other line with a broken link is "mixed".
    Every index change is a proposal: this script never edits MEMORY.md
  - per memory: size, type, last "Verified/Verificato YYYY-MM-DD" date (invalid and future dates
    are ignored), changes since then, paths quoted in backticks that no longer exist (as written
    and with "~" expanded), path text that looks unusual (reported, never checked: shell or
    control characters, a ".." component, or a path under /net, /Network, /Volumes or /mnt, and
    on macOS /home, so a memory cannot trigger an automount or a network lookup), [[links]] to
    memories that are gone
  - which memories need a fact check by an agent (never verified, verified long ago, or changed
    since), and which wait for the user (a line starting with "**Needs user")
  - archive candidates: project memories no session mentioned for N days (never rules or user notes)
  - split candidates: memories over N characters
Only regular *.md files count: symlinks and folders are skipped. It changes nothing unless
--backup is given, which copies the regular *.md files of the folder (subfolders too, symlinks
never followed) to <data dir>/backups/memory/<project key>/<timestamp> and keeps the last four
copies of that project, by modification time (a time in the future counts as the oldest), never
deleting the new one. It refuses to back up when backups, backups/memory or the project's backup
folder is a symlink. Any copy or folder-walk error fails the backup: it is printed as
"Backup FAILED" with every error counted, --json gives "backup_failed": true, and the fact-check
mode is "propose". Flat backups/memory-* folders of earlier versions are left alone.

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
NETWORK_ROOTS = ("/net", "/network", "/volumes", "/mnt")  # compared case-insensitively
MACOS_ROOTS = ("/home", "/system/volumes/data/home")  # auto_home mounts on macOS (lower case: matched case-insensitively)
PLAIN_LINE = re.compile(r"- \[[^\[\]`\n]{1,300}\]\([^()\s`\[\]]{1,200}\.md\)[^\[\]`\n]{0,2000}")
URLISH = re.compile(r"://|www\.", re.I)
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


def check_target(path_text):
    """The path that is checked for a path quoted in a memory: "~" expanded, trailing punctuation cut."""
    return os.path.expanduser(path_text.rstrip("/.,;:"))


def network_roots():
    """Roots that can automount or reach the network: /home too on macOS (auto_home)."""
    return NETWORK_ROOTS + (MACOS_ROOTS if sys.platform == "darwin" else ())


def unusual(path_text):
    """True for path text that is reported and never checked.

    That is text with shell or control characters, a ".." component once "~" is expanded,
    or a path under /net, /Network, /Volumes or /mnt, and on macOS /home, so that a memory
    cannot make the check trigger an automount or a network lookup.
    """
    if any(c in UNUSUAL or unicodedata.category(c) == "Cc" for c in path_text):
        return True
    if ".." in os.path.expanduser(path_text).split("/"):
        return True
    norm = os.path.normpath(check_target(path_text)).casefold()
    if norm.startswith("/"):
        norm = "/" + norm.lstrip("/")
    return any(norm == root or norm.startswith(root + "/") for root in network_roots())


def resolve_link(mem, link):
    """[(normalized path, inside the memory folder?)] of an index link: its raw text first, then
    its %xx-decoded text when that differs, each relative to the memory folder."""
    out = []
    for text in dict.fromkeys((link, unquote(link))):
        p = os.path.normpath(os.path.join(mem, text))
        out.append((p, p.startswith(mem + os.sep)))
    return out


def index_links(mem, index):
    """Index links checked line by line. Nothing here changes the index.

    A link works when its raw or its %xx-decoded text names an existing file inside the
    memory folder. A link that is not inside it is reported as outside and never broken.
    A line with a broken link is a drop candidate only when it is a plain
    "- [title](file.md) text" line with that one link and no other link, [[link]], URL or
    backtick. Any other line with a broken link is "mixed". Both are proposals.
    """
    r = {"indexed": set(), "broken": set(), "outside": set(), "drop": [], "mixed": [], "count": 0}
    for n, line in enumerate(index.splitlines(), 1):
        bad, other = [], []
        for link in LINK.findall(line):
            r["count"] += 1
            if SCHEME.match(link):  # a web or other URL, not a memory file
                other.append(link)
                continue
            cands = resolve_link(mem, link)
            hit = next((p for p, inside in cands if inside and os.path.exists(p)), None)
            if hit:
                r["indexed"].add(hit)
                other.append(link)
            elif not all(inside for _, inside in cands):
                r["outside"].add(link)
                other.append(link)
            else:
                bad.append(link)
        if bad:
            r["broken"].update(bad)
            plain = not other and PLAIN_LINE.fullmatch(line.rstrip()) and not URLISH.search(line)
            if plain:
                r["drop"].append({"line": n, "broken": bad})
            else:
                r["mixed"].append({"line": n, "broken": bad, "other": other})
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
            full = check_target(p)
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
    Symlinks are never followed. If backups, backups/memory or backups/memory/<key> is a
    symlink, nothing is created and the backup fails. Errors are collected, not raised.
    Rotation keeps the last KEEP_BACKUPS copies of this project: the new copy plus the
    newest older ones by modification time, where a time in the future counts as the
    oldest, so a future-dated folder rotates out instead of staying forever. It never
    deletes the new copy, and deletes only timestamp-named folders of this project; other
    projects, other names and the flat backups/memory-* folders of earlier versions are
    never touched. "failed" is true when any copy or folder-walk error happened, or when
    there is no new copy left at the end. A rotation error goes to "notes" and does not
    fail the backup: the new copy is complete.
    """
    key = key or os.path.basename(os.path.dirname(os.path.normpath(mem)))
    if key in ("", ".", "..") or os.sep in key or "\0" in key:
        raise ValueError(f"not a project key: {key!r}")
    levels = [os.path.join(data_dir(), "backups")]
    levels += [os.path.join(levels[0], "memory"), os.path.join(levels[0], "memory", key)]

    def failed(msg):
        return {"path": None, "files": 0, "errors": [msg], "notes": [], "failed": True}

    links = [p for p in levels if os.path.islink(p)]
    if links:
        return failed(f"refused: {clean(links[0], 400)} is a symlink, so nothing was backed up")
    root = levels[-1]
    try:
        for p in levels:
            os.makedirs(p, mode=0o700, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest, n = os.path.join(root, stamp), 0
        while True:
            try:
                os.mkdir(dest, 0o700)
                break
            except FileExistsError:
                n += 1
                dest = os.path.join(root, f"{stamp}-{n}")
    except OSError as e:
        return failed(f"cannot create the backup folder: {e.strerror or e}")
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
    new, notes, now = os.path.basename(dest), [], time.time_ns()
    try:
        old = []
        for d in os.listdir(root):
            if d == new or not BACKUP_NAME.fullmatch(d):
                continue
            try:
                st = os.lstat(os.path.join(root, d))
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):  # a time in the future counts as the oldest
                old.append((st.st_mtime_ns if st.st_mtime_ns <= now else 0, d))
        old.sort()
        for _, d in ([] if errors else old[:max(0, len(old) - (KEEP_BACKUPS - 1))]):
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    except OSError as e:
        notes.append(f"rotation: {e.strerror or e}; older copies were kept")
    if errors:
        notes.append("rotation skipped: this copy failed, so every older copy was kept")
        if not os.path.islink(dest):
            shutil.rmtree(dest, ignore_errors=True)  # a partial copy must never count as a backup
        notes.append("the partial copy was removed")
        return {"path": None, "files": copied, "errors": errors, "notes": notes, "failed": True}
    if os.path.islink(dest) or not os.path.isdir(dest):
        errors.append("the new backup is missing after rotation")
    return {"path": dest, "files": copied, "errors": errors, "notes": notes, "failed": bool(errors)}


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
        r["backup_notes"], r["backup_failed"] = b["notes"], b["failed"]
    # agents may edit memory files only after a backup in this run that had no error
    backed_up = a.backup and not r["backup_failed"]
    r["fact_check_mode"] = "fix" if r["auto_fix_memory"] and backed_up else "propose"
    if a.json:
        print(json.dumps(r, indent=1))
        return
    m = r["memories"]

    def names(items):
        return ", ".join(clean(x) for x in items) if items else "none"

    print(f"Memory: {len(m)} files, {sum(x['chars'] for x in m)} chars; MEMORY.md {r['index_chars']} chars, "
          f"{r['index_entries']} entries")
    if not r["auto_fix_memory"]:
        why = "auto_fix_memory false: proposals only"
    elif not a.backup:
        why = "no backup in this run: run with --backup to allow fixes"
    elif r["backup_failed"]:
        why = "the backup failed: proposals only"
    else:
        why = "auto_fix_memory true and the backup worked"
    print(f"Fact fixes: MODE {r['fact_check_mode']} ({why})")
    if a.backup and r["backup_failed"]:
        errs = r["backup_errors"]
        more = f"; and {len(errs) - 10} more" if len(errs) > 10 else ""
        print(f"Backup FAILED ({len(errs)} error{'s' if len(errs) != 1 else ''}): "
              + "; ".join(clean(e, 400) for e in errs[:10]) + more
              + ". Do not edit any memory file in this run.")
    elif a.backup:
        print(f"Backup: {r['backup']} ({r['backup_files']} files)")
    if a.backup and r["backup_notes"]:
        print("Backup rotation: " + "; ".join(clean(e, 400) for e in r["backup_notes"]))

    def lines(items):
        shown = [f"line {x['line']} ({names(x['broken'])})" for x in items[:20]]
        return "; ".join(shown) + (f"; and {len(items) - 20} more (see --json)" if len(items) > 20 else "")

    print(f"Index: {len(r['index_long_lines'])} lines over {cfg['index_line_max']} chars; "
          f"orphans {names(r['orphans'])}; broken links {names(r['broken_index_links'])}")
    print("Index changes are proposals: nothing in MEMORY.md changes without the user's yes.")
    print("Index drop candidates, one plain link and it is broken (a proposal): "
          + (lines(r["index_lines_to_drop"]) or "none"))
    if r["mixed_index_lines"]:
        print("Mixed index lines, a broken link next to other text or links (a proposal: remove only the "
              "broken link): " + lines(r["mixed_index_lines"]))
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
