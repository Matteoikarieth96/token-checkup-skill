"""Token use of past Claude Code sessions, with week-over-week comparison.

Reads the main-session transcripts of one project (subagents excluded) and
reports what every session pays at the start, how big sessions grow, whether
compaction fires where it should, and which tools fill the context.

Usage:
  measure.py --project <folder>                       # last 7 days
  measure.py --project <folder> --days 14 --compare   # also the 14 days before, and the first saved run
  measure.py --project <folder> --since 2026-10-12 --until 2026-10-26
  measure.py --project <folder> --save                # append the result to the history file
  measure.py --project <folder> --json                # machine-readable output
  measure.py --project <folder> --days 14 --simulate 300000   # what compaction at 300k would have saved
  measure.py --project=KEY ...     # a transcript folder key: keys start with "-", so pass it with "="

--project is the project folder (default: the current folder), or the key of its
transcript folder under ~/.claude/projects/ written as --project=KEY.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_HELP, config, data_dir, install_record, k, median, parse_iso,  # noqa: E402
                    project_dir, session_stats, transcripts)

HISTORY = "history.jsonl"
HISTORY_VERSION = 2  # 2: context_per_call is the full context (input + cache read + cache creation) per call
# Windows can differ in length, so only medians and per-call values are compared with the baseline.
BASELINE_KEYS = {"first_median", "peak_median", "peak_p90", "context_per_call", "cache_read_session_median"}


def summarize(rows, cfg, rec=None):
    if not rows:
        return {"sessions": 0}
    window = cfg["compact_window"]
    rec = install_record() if rec is None else rec
    since = parse_iso(rec.get("installed_at"))
    peaks = sorted(r["peak"] for r in rows)
    cr = [r["cache_read"] for r in rows]
    calls = sum(r["calls"] for r in rows)
    by_version = {}
    for r in rows:
        by_version.setdefault(r["version"] or "?", []).append(r["first"])
    tools = {}
    for r in rows:
        for n, t in r["tools"].items():
            tools[n] = tools.get(n, 0) + t
    comp = [c for r in rows for c in r["compactions"]]
    context_total = sum(sum(r["series"]) for r in rows)
    return {
        "sessions": len(rows),
        "first_median": median([r["first"] for r in rows]),
        "first_by_version": {v: [median(x), len(x)] for v, x in sorted(by_version.items())},
        "peak_median": median(peaks),
        "peak_p90": peaks[int(len(peaks) * 0.9)] if len(peaks) > 1 else peaks[0],
        "peak_max": peaks[-1],
        "over_200k": sum(p > 200_000 for p in peaks),
        "over_400k": sum(p > 400_000 for p in peaks),
        "over_window": sum(p > window * 1.1 for p in peaks),
        # born after setup, over the window x1.1, never compacted: compaction did not fire
        "missed_compaction": sum(1 for r in rows if (since is None or r["born"] >= since)
                                 and r["peak"] > window * 1.1 and not r["compactions"]),
        "missed_since": rec.get("installed_at") if since is not None else None,
        "compactions": len(comp),
        "compaction_at_median": median([b for b, _ in comp]),
        "after_compaction_median": median([a for _, a in comp]),
        "cache_read_total": sum(cr),
        "cache_read_session_median": median(cr),
        "calls": calls,
        "context_total": context_total,
        "context_per_call": context_total / max(calls, 1),
        "output_total": sum(r["out"] for r in rows),
        "images": sum(r["images"] for r in rows),
        "top_tools": sorted(tools.items(), key=lambda kv: -kv[1])[:8],
    }


def simulate(rows, window, restart=None):
    """Replay each session's per-call context with compaction at `window`.

    After a simulated compaction the context restarts at `restart` (default: the
    median seen after real compactions, else 1.4x the median first-call context)
    and then grows by the same amounts as in the real session. An estimate: it
    assumes the work after compaction would have been the same.
    """
    if not rows:
        return None
    if restart is None:
        after = [a for r in rows for _, a in r["compactions"]]
        restart = median(after) if after else 1.4 * median([r["first"] for r in rows])
    real = sim = 0
    affected = 0
    for r in rows:
        s = r["series"]
        prev = cur = s[0]
        real += s[0]
        sim += s[0]
        hit = False
        for c in s[1:]:
            d = c - prev
            prev = c
            cur = c if d < 0 else cur + d  # real drops (compaction, /clear) stay as they were
            if cur > window:
                cur = restart + max(d, 0)
                hit = True
            real += c
            sim += cur
        affected += hit
    return {"window": window, "restart": restart, "sessions": len(rows), "sessions_affected": affected,
            "context_read_real": real, "context_read_sim": sim, "saving_pct": 100 * (1 - sim / real)}


def load_rows(pdir, lo, hi, cfg):
    files = transcripts(pdir, lo, hi)
    return [r for r in (session_stats(f, cfg["image_tokens"]) for f in files) if r]


def run(pdir, lo, hi, cfg):
    return summarize(load_rows(pdir, lo, hi, cfg), cfg)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def delta(now, before):
    if not is_num(before) or not before or not is_num(now):
        return ""
    return f"  ({k(before) if before >= 1000 else before} -> {k(now) if now >= 1000 else now}, {100 * (now - before) / before:+.0f}%)"


def report(m, label, prev=None, base=None, window=None):
    if not m.get("sessions"):
        print(f"{label}: no sessions")
        return
    p = prev if prev and prev.get("sessions") else None
    b = base if base and base.get("sessions") else None
    missed = ("sessions that should have compacted but did not "
              + ("(since setup)" if m.get("missed_since") else "(no setup record: all sessions)"))
    lines = [
        ("sessions", "sessions", False),
        ("first-call context (median)", "first_median", True),
        ("peak context (median)", "peak_median", True),
        ("peak context (p90)", "peak_p90", True),
        ("sessions over 400k", "over_400k", False),
        ("sessions over the compact window x1.1 (any)", "over_window", False),
        (missed, "missed_compaction", False),
        ("compactions", "compactions", False),
        ("mean context per model call", "context_per_call", True),
        ("cache reads per session (median)", "cache_read_session_median", True),
        ("cache reads total", "cache_read_total", True),
        ("images in tool results", "images", False),
    ]
    print(label)
    for name, key, tok in lines:
        val = m[key]
        shown = k(val) if tok else val
        extra = delta(val, p.get(key)) if p else ""
        if b and key in BASELINE_KEYS:
            extra += "  vs baseline" + delta(val, b.get(key))
        print(f"  {name:48s} {shown}{extra}")
    if window:
        print(f"  compact window used above: {k(window)} (config compact_window)")
    if m["compactions"]:
        print(f"  compactions fired at about {k(m['compaction_at_median'])}, sessions restarted at about "
              f"{k(m['after_compaction_median'])}")
    print("  first-call context by Claude Code version: " + ", ".join(
        f"{v} {k(x[0])} (n={x[1]})" for v, x in m["first_by_version"].items()))
    print("  tools that put the most into context: " + ", ".join(f"{n} {t / 1e6:.2f}M" for n, t in m["top_tools"]))


def day_ts(s):
    return dt.datetime.fromisoformat(s).timestamp()


def parse_day(p, flag, s):
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        p.error(f"{flag} must be a date written YYYY-MM-DD, got {s!r}")


def baseline(key):
    """Metrics of the first saved run for this project, or None."""
    try:
        with open(os.path.join(data_dir(), HISTORY)) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in lines:
        try:
            h = json.loads(line)
        except ValueError:
            continue
        if isinstance(h, dict) and h.get("project") == key and isinstance(h.get("metrics"), dict):
            base = dict(h["metrics"])
            if h.get("v") != HISTORY_VERSION:  # older runs measured context_per_call differently
                base.pop("context_per_call", None)
            return base
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--since", help="first day included, YYYY-MM-DD")
    p.add_argument("--until", help="last day included, YYYY-MM-DD")
    p.add_argument("--project", help=PROJECT_HELP)
    p.add_argument("--compare", action="store_true")
    p.add_argument("--save", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--simulate", type=int, metavar="WINDOW",
                   help="estimate the context re-read if sessions had compacted at WINDOW tokens")
    a = p.parse_args()
    if a.days < 1:
        p.error("--days must be 1 or more")
    today = dt.date.today()
    until = parse_day(p, "--until", a.until) if a.until else today
    since = parse_day(p, "--since", a.since) if a.since else until - dt.timedelta(days=a.days - 1)
    if since > until:
        p.error("--since is after --until")
    cfg = config()
    pdir = project_dir(a.project)
    key = os.path.basename(pdir)
    if not a.json:
        print(f"project: {key}")
    span = (until - since).days + 1
    lo, hi = day_ts(str(since)), day_ts(str(until + dt.timedelta(days=1)))
    rows = load_rows(pdir, lo, hi, cfg)
    now = summarize(rows, cfg)
    sim = simulate(rows, a.simulate) if a.simulate else None
    prev = base = None
    if a.compare:
        prev = run(pdir, lo - span * 86400, lo, cfg)
        base = baseline(key)
    if a.save:
        os.makedirs(data_dir(), exist_ok=True)
        with open(os.path.join(data_dir(), HISTORY), "a") as f:
            f.write(json.dumps({"v": HISTORY_VERSION, "date": str(today), "since": str(since), "until": str(until),
                                "project": key, "metrics": now}) + "\n")
    if a.json:
        print(json.dumps({"project": key, "since": str(since), "until": str(until),
                          "now": now, "previous": prev, "baseline": base, "simulation": sim}, indent=1))
        return
    report(now, f"{since} .. {until}  ({key}, subagents excluded)", prev, base, cfg["compact_window"])
    if a.compare and ((prev and prev.get("sessions")) or base):
        print(f"(deltas: the {span} days before; 'vs baseline': first saved run, medians and per-call values only)")
    if sim:
        print(f"simulated compaction at {k(sim['window'])} (restart at about {k(sim['restart'])}): "
              f"{sim['sessions_affected']} of {sim['sessions']} sessions would compact; context re-read "
              f"{k(sim['context_read_real'])} -> {k(sim['context_read_sim'])} ({sim['saving_pct']:.0f}% less). "
              "Estimate: assumes the same work after compaction.")


if __name__ == "__main__":
    main()
