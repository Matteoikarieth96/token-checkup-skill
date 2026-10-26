"""Install, check or remove the token-checkup pieces in the user-level ~/.claude/settings.json.

  install    add the context-guard UserPromptSubmit hook (replacing an older one that
             carries the "# token-checkup-guard" tag) and set "autoCompactWindow"
             (default 300000). Both are user-level settings, so they apply to every
             project. Claude Code caps the value at each model's window, so only
             models with a larger window (1M models in practice) change. Settings are
             backed up first, and the autoCompactWindow from before the first install
             is recorded so uninstall can restore it. Untagged hooks that look like a
             context guard are listed and left alone, unless --replace-legacy is given.
  uninstall  remove the tagged hook and restore the previous autoCompactWindow, if the
             value is still the one install wrote.
  check      print what is installed, and whether the hook's interpreter and script exist.

Usage:
  setup.py check
  setup.py install [--compact-window 300000] [--no-compact] [--replace-legacy]
  setup.py uninstall
"""
import argparse
import copy
import datetime as dt
import json
import os
import re
import shlex
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import claude_dir, clean, config, data_dir, install_record, is_int  # noqa: E402

TAG = "# token-checkup-guard"
MISSING = "missing"
WINDOW_MIN, WINDOW_MAX = 100_000, 1_000_000
KEEP_BACKUPS = 5
FIRST_BACKUP = "settings-first.json"
BACKUP_NAME = re.compile(r"settings-\d{8}-\d{6}-\d{6}(?:-\d+)?\.json")


def settings_path():
    return os.path.join(claude_dir(), "settings.json")


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def load():
    """settings.json as a dict ({} if missing). Exits with a message, writing nothing, if it is unusable."""
    path = settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read {path} ({e}). Nothing was written.")
    try:
        s = json.loads(text, parse_constant=_reject_constant)
    except ValueError as e:
        raise SystemExit(f"{path} is not valid JSON ({e}). Fix it first; nothing was written.")
    if not isinstance(s, dict):
        raise SystemExit(f"{path} does not hold a JSON object at the top level. Nothing was written.")
    if "hooks" in s:
        hooks = s["hooks"]
        if not isinstance(hooks, dict):
            raise SystemExit(f'"hooks" in {path} is not an object. Nothing was written.')
        if "UserPromptSubmit" in hooks:
            groups = hooks["UserPromptSubmit"]
            ok = isinstance(groups, list) and all(
                isinstance(g, dict) and isinstance(g.get("hooks", []), list)
                and all(isinstance(h, dict) for h in g.get("hooks", [])) for g in groups)
            if not ok:
                raise SystemExit(f'"hooks.UserPromptSubmit" in {path} does not have the expected shape '
                                 "(a list of groups, each with a list of hook objects). Nothing was written.")
    return s


def _exclusive_write(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def backup_settings(real):
    """Copy settings.json to a new, uniquely named backup (mode 0600).

    The very first backup ever made is also kept as settings-first.json; of the
    timestamped ones, the last KEEP_BACKUPS are kept.
    """
    with open(real, "rb") as f:
        data = f.read()
    folder = os.path.join(data_dir(), "backups")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    try:
        _exclusive_write(os.path.join(folder, FIRST_BACKUP), data)
    except FileExistsError:
        pass
    stamp = "settings-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    n = 0
    while True:
        dest = os.path.join(folder, stamp + (f"-{n}" if n else "") + ".json")
        try:
            _exclusive_write(dest, data)
            break
        except FileExistsError:
            n += 1
    old = sorted(d for d in os.listdir(folder) if BACKUP_NAME.fullmatch(d))
    for d in old[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(folder, d))
        except OSError:
            pass
    return dest


def save(s):
    """Write settings atomically to the real file (a symlinked settings.json keeps its link)."""
    path = os.path.realpath(settings_path())
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        backup_settings(path)
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_record(rec):
    os.makedirs(data_dir(), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".install-", suffix=".tmp", dir=data_dir())
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    os.replace(tmp, os.path.join(data_dir(), "install.json"))


def guard_command():
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "context_guard.py")
    return f"{shlex.quote(sys.executable)} {shlex.quote(script)} hook 2>/dev/null || true {TAG}"


def _command(h):
    return h.get("command") if isinstance(h, dict) and isinstance(h.get("command"), str) else ""


def is_tagged(h):
    return TAG in _command(h)


def is_legacy(h):
    """An untagged hook that runs a context_guard.py: maybe ours from an old install, maybe not."""
    return "context_guard.py" in _command(h) and not is_tagged(h)


def strip_guard(s, replace_legacy=False):
    """Remove the tagged hook entries (and untagged context guards if replace_legacy).

    Groups that held no removed entry are left exactly as they were. A group left
    empty by the removal is dropped; so is UserPromptSubmit, and then "hooks", if
    the removal emptied them. Returns (settings, entries removed, legacy commands seen).
    """
    hooks = s.get("hooks")
    groups = hooks.get("UserPromptSubmit") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return s, 0, []
    removed, legacy, kept = 0, [], []
    for g in groups:
        entries = g.get("hooks") if isinstance(g, dict) else None
        if not isinstance(entries, list):
            kept.append(g)
            continue
        legacy += [_command(h) for h in entries if is_legacy(h)]
        drop = [i for i, h in enumerate(entries) if is_tagged(h) or (replace_legacy and is_legacy(h))]
        if not drop:
            kept.append(g)
            continue
        removed += len(drop)
        rest = [h for i, h in enumerate(entries) if i not in drop]
        if rest:
            kept.append(dict(g, hooks=rest))
    if removed:
        if kept:
            hooks["UserPromptSubmit"] = kept
        else:
            del hooks["UserPromptSubmit"]
            if not hooks:
                del s["hooks"]
    return s, removed, legacy


def valid_window(v):
    return is_int(v) and WINDOW_MIN <= v <= WINDOW_MAX


def install(window, compact, replace_legacy=False):
    s, removed, legacy = strip_guard(load(), replace_legacy)
    entry = {"type": "command", "timeout": 5, "command": guard_command()}
    s.setdefault("hooks", {}).setdefault("UserPromptSubmit", []).append({"hooks": [entry]})
    rec = install_record()
    if compact:
        if "previous_autoCompactWindow" not in rec:  # the value from before the first install only
            rec["previous_autoCompactWindow"] = s.get("autoCompactWindow", MISSING)
        rec["installed_value"] = window
        rec["installed_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        s["autoCompactWindow"] = window
    save(s)
    if compact:
        write_record(rec)
    print(f"installed: guard hook -> {entry['command']}")
    if compact:
        print(f"autoCompactWindow = {window} (user-level: every project; active in sessions started from now on; "
              "check with /autocompact)")
    if legacy and replace_legacy:
        print("removed other hooks that looked like a context guard: " + "; ".join(clean(c) for c in legacy))
    elif legacy:
        print("other hooks that look like a context guard (left untouched): " + "; ".join(clean(c) for c in legacy))
        print("  run install with --replace-legacy to remove them too")
    if sys.prefix != sys.base_prefix:
        print(f"warning: this Python runs in a virtualenv ({sys.prefix}). The hook will use this interpreter "
              f"({sys.executable}) and stop working if the venv is removed. Re-run install with a system python3 "
              "to avoid that.")


def uninstall():
    before = load()
    s, removed, legacy = strip_guard(copy.deepcopy(before))
    rec = install_record()
    notes = []
    if "previous_autoCompactWindow" in rec:
        prev, inst = rec["previous_autoCompactWindow"], rec.get("installed_value")
        cur = s.get("autoCompactWindow", MISSING)
        if cur == MISSING:
            notes.append("autoCompactWindow is not set; nothing to restore")
        elif not (is_int(cur) and is_int(inst) and cur == inst):
            notes.append(f"autoCompactWindow left at {clean(cur)}: it is not the value install wrote ({clean(inst)}), "
                         "so it was changed after install")
        elif prev == MISSING:
            del s["autoCompactWindow"]
            notes.append("autoCompactWindow removed (it was not set before install)")
        elif valid_window(prev):
            s["autoCompactWindow"] = prev
            notes.append(f"autoCompactWindow restored to {prev}")
        else:
            notes.append(f"autoCompactWindow left at {cur}: the recorded previous value ({clean(prev)}) is not "
                         f"a number between {WINDOW_MIN} and {WINDOW_MAX}")
    elif "autoCompactWindow" in s:
        notes.append("autoCompactWindow left as it is: there is no install record")
    if legacy:
        notes.append("other hooks that look like a context guard (left untouched): "
                     + "; ".join(clean(c) for c in legacy))
    if s == before:
        print("nothing to remove" + "".join("\n" + n for n in notes))
    else:
        save(s)
        print(("removed the guard hook" if removed else "no guard hook found")
              + "".join("\n" + n for n in notes))
    if rec:
        try:
            os.remove(os.path.join(data_dir(), "install.json"))
        except OSError:
            pass


def hook_paths_ok(cmd):
    """(ok, missing paths) for the interpreter and script in a guard hook command."""
    try:
        parts = shlex.split(cmd.rsplit(" 2>", 1)[0])
    except ValueError:
        return False, ["unparsable command"]
    if len(parts) < 2:
        return False, ["unparsable command"]
    missing = [p for p in parts[:2] if not os.path.isfile(p)]
    return not missing, missing


def check():
    s = load()
    groups = (s.get("hooks") or {}).get("UserPromptSubmit") or []
    entries = [h for g in groups for h in g.get("hooks", [])]
    guard = [_command(h) for h in entries if is_tagged(h)]
    legacy = [_command(h) for h in entries if is_legacy(h)]
    real = os.path.realpath(settings_path())
    print(f"settings: {settings_path()}" + (f" -> {real}" if real != settings_path() else "")
          + " (user-level: applies to every project)")
    print(f"guard hook: {'installed' if guard else 'not installed'}" + (f" ({clean(guard[0])})" if guard else ""))
    for cmd in guard:
        ok, missing = hook_paths_ok(cmd)
        print("hook command: OK (interpreter and script exist)" if ok
              else "hook command: BROKEN (missing: " + ", ".join(clean(m) for m in missing) + ")")
    if legacy:
        print("other hooks that look like a context guard (left untouched): " + "; ".join(clean(c) for c in legacy))
    print(f"autoCompactWindow: {clean(s.get('autoCompactWindow', 'not set'))}")
    rec = install_record()
    if rec:
        print(f"install record: previous autoCompactWindow {clean(rec.get('previous_autoCompactWindow'))}, "
              f"installed {clean(rec.get('installed_value'))} at {clean(rec.get('installed_at'))}")
    env = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if env:
        print(f"note: CLAUDE_CODE_AUTO_COMPACT_WINDOW={clean(env)} is set and overrides autoCompactWindow")
    print(f"data dir: {data_dir()} ({'exists' if os.path.isdir(data_dir()) else 'not created yet'})")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["install", "uninstall", "check"])
    p.add_argument("--compact-window", type=int, default=config()["compact_window"])
    p.add_argument("--no-compact", action="store_true")
    p.add_argument("--replace-legacy", action="store_true",
                   help="install: also remove untagged hooks that run a context_guard.py")
    a = p.parse_args()
    if a.action == "install":
        if not WINDOW_MIN <= a.compact_window <= WINDOW_MAX:
            raise SystemExit(f"--compact-window must be between {WINDOW_MIN} and {WINDOW_MAX}")
        install(a.compact_window, not a.no_compact, a.replace_legacy)
    elif a.action == "uninstall":
        uninstall()
    else:
        check()


if __name__ == "__main__":
    main()
