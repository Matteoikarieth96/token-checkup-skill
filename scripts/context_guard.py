"""Context guard: a UserPromptSubmit hook that speaks up when a session gets big.

Every tool call re-reads the whole session, so a session at 600k tokens costs
far more per call than a fresh one. The hook reads the current context size
from the transcript and, the first time a session crosses each band
(config "guard_bands", default 250k/400k/550k/700k/850k), asks Claude to open
its reply with one line suggesting a new session or /compact.

It never blocks a prompt: any error means no output and exit 0. State files
(one per session, under <data dir>/guard/) older than 30 days are deleted
whenever it saves state.

Usage:
  context_guard.py hook            # hook mode, reads the hook JSON on stdin
  context_guard.py check FILE      # print the current context of a transcript
"""
import json
import os
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATE_MAX_AGE_DAYS = 30  # state files of sessions idle longer than this are deleted


def band_of(tokens, bands):
    passed = [b for b in bands if tokens >= b]
    return passed[-1] if passed else 0


def state_file(session):
    from common import data_dir
    return os.path.join(data_dir(), "guard", session + ".json")


def load_band(session):
    try:
        with open(state_file(session)) as f:
            band = json.load(f).get("band", 0)
    except (OSError, ValueError, AttributeError):
        return 0
    return band if isinstance(band, int) and not isinstance(band, bool) else 0


def prune_state(folder, keep, max_age_days=STATE_MAX_AGE_DAYS):
    """Delete guard state files older than max_age_days. Best effort: never raises."""
    try:
        cutoff = time.time() - max_age_days * 86400
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not name.endswith(".json") or path == keep:
                continue
            try:
                st = os.lstat(path)
                if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                    os.remove(path)
            except OSError:
                continue
    except Exception:
        pass


def save_band(session, band):
    path = state_file(session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"band": band}, f)
    prune_state(os.path.dirname(path), path)


def message(tokens):
    return (
        f"[context-guard] This session's context is ~{round(tokens / 1000)}k tokens, and every tool call "
        "re-reads all of it. Open your reply with ONE short line to the user, in their language, giving that "
        "size and suggesting: if the task in progress is finished, start a new session for the next one "
        "(save what matters to memory first); if it is not, keep heavy reading (big files, screenshots, web "
        "pages) in subagents, or run /compact at a natural break. Then answer normally. Say it once; this hook "
        "speaks again only at the next band."
    )


def hook():
    from common import config, last_context
    data = json.load(sys.stdin)
    session = "".join(c for c in str(data.get("session_id", "")) if c.isalnum() or c in "-_")
    path = data.get("transcript_path")
    if not session or not path or not os.path.isfile(path):
        return
    tokens = last_context(path)
    if tokens is None:
        return
    band, warned = band_of(tokens, config()["guard_bands"]), load_band(session)
    if band < warned:  # compacted or cleared: count again from here
        save_band(session, band)
        return
    if band == 0 or band == warned:
        return
    save_band(session, band)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": message(tokens)}}))


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "check":
        from common import config, last_context
        tokens = last_context(sys.argv[2])
        print("unknown" if tokens is None else f"{tokens} tokens (band {band_of(tokens, config()['guard_bands'])})")
        return
    try:
        hook()
    except Exception:
        pass


if __name__ == "__main__":
    main()
