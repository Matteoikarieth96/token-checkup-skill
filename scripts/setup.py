"""Install, check or remove the token-checkup pieces in ~/.claude/settings.json.

  install    add the context-guard UserPromptSubmit hook (replacing any older
             context_guard.py hook) and set "autoCompactWindow" (default 300000).
             The value is capped by Claude Code at each model's window, so only
             1M-window models change. Settings are backed up first, and the
             previous autoCompactWindow is recorded so uninstall can restore it.
  uninstall  remove the hook and restore the previous autoCompactWindow.
  check      print what is installed.

Usage:
  setup.py check
  setup.py install [--compact-window 300000] [--no-compact]
  setup.py uninstall
"""
import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import claude_dir, config, data_dir, read_json  # noqa: E402

TAG = "# token-checkup-guard"
MISSING = "__missing__"


def settings_path():
    return os.path.join(claude_dir(), "settings.json")


def load():
    try:
        with open(settings_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(s):
    path = settings_path()
    if os.path.exists(path):
        os.makedirs(os.path.join(data_dir(), "backups"), exist_ok=True)
        shutil.copy2(path, os.path.join(data_dir(), "backups",
                                        "settings-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def guard_command():
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "context_guard.py")
    return f"{shlex.quote(sys.executable)} {shlex.quote(script)} hook 2>/dev/null || true {TAG}"


def strip_guard(s):
    """Remove every hook entry that runs a context_guard.py; drop empty groups."""
    groups = (s.get("hooks") or {}).get("UserPromptSubmit") or []
    kept = []
    for g in groups:
        hooks = [h for h in g.get("hooks", []) if "context_guard.py" not in h.get("command", "")]
        if hooks:
            kept.append({**g, "hooks": hooks})
    if "hooks" in s:
        if kept:
            s["hooks"]["UserPromptSubmit"] = kept
        else:
            s["hooks"].pop("UserPromptSubmit", None)
    return s


def install(window, compact):
    s = strip_guard(load())
    entry = {"type": "command", "timeout": 5, "command": guard_command()}
    groups = s.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if groups:
        groups[0].setdefault("hooks", []).append(entry)
    else:
        groups.append({"hooks": [entry]})
    state_path = os.path.join(data_dir(), "install.json")
    if compact:
        if not os.path.exists(state_path):  # remember the value from before the first install only
            os.makedirs(data_dir(), exist_ok=True)
            with open(state_path, "w") as f:
                json.dump({"previous_autoCompactWindow": s.get("autoCompactWindow", MISSING)}, f)
        s["autoCompactWindow"] = window
    save(s)
    print(f"installed: guard hook -> {entry['command']}")
    if compact:
        print(f"autoCompactWindow = {window} (active in sessions started from now on; check with /autocompact)")


def uninstall():
    s = strip_guard(load())
    state_path = os.path.join(data_dir(), "install.json")
    try:
        prev = read_json(state_path).get("previous_autoCompactWindow", MISSING)
    except (OSError, ValueError):
        prev = None
    if prev == MISSING:
        s.pop("autoCompactWindow", None)
    elif prev is not None:
        s["autoCompactWindow"] = prev
    save(s)
    if prev is not None:
        os.remove(state_path)
    print("removed the guard hook" + ("" if prev is None else f"; autoCompactWindow restored to "
                                       f"{'unset' if prev == MISSING else prev}"))


def check():
    s = load()
    cmds = [h.get("command", "") for g in (s.get("hooks") or {}).get("UserPromptSubmit", [])
            for h in g.get("hooks", [])]
    guard = [c for c in cmds if "context_guard.py" in c]
    print(f"settings: {settings_path()}")
    print(f"guard hook: {'installed' if guard else 'not installed'}" + (f" ({guard[0][:120]})" if guard else ""))
    print(f"autoCompactWindow: {s.get('autoCompactWindow', 'not set')}")
    print(f"data dir: {data_dir()} ({'exists' if os.path.isdir(data_dir()) else 'not created yet'})")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["install", "uninstall", "check"])
    p.add_argument("--compact-window", type=int, default=config()["compact_window"])
    p.add_argument("--no-compact", action="store_true")
    a = p.parse_args()
    if a.action == "install":
        if not 100_000 <= a.compact_window <= 1_000_000:
            raise SystemExit("--compact-window must be between 100000 and 1000000")
        install(a.compact_window, not a.no_compact)
    elif a.action == "uninstall":
        uninstall()
    else:
        check()


if __name__ == "__main__":
    main()
