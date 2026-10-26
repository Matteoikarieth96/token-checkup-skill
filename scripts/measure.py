"""Token use of past Claude Code sessions, with week-over-week comparison.

Reads the main-session transcripts of one project (subagents excluded) and
reports what every session pays at the start, how big sessions grow, whether
compaction fires where it should, and which tools fill the context.

Usage:
  measure.py                       # last 7 days, current project
  measure.py --days 14 --compare   # also the 14 days before, and the first saved run
  measure.py --since 2026-10-12 --until 2026-10-26 --project <path or key>
  measure.py --save                # append the result to the history file
  measure.py --json                # machine-readable output
  measure.py --days 14 --simulate 300000   # what compaction at 300k would have saved
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import config, data_dir, k, median, project_dir, session_stats, transcripts  # noqa: E402

HISTORY = "history.jsonl"


def summarize(rows, cfg):
    if not rows:
        return {"sessions": 0}
    window = cfg["compact_window"]
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
        "compactions": len(comp),
        "compaction_at_median": median([b for b, _ in comp]),
        "after_compaction_median": median([a for _, a in comp]),
        "cache_read_total": sum(cr),
        "cache_read_session_median": median(cr),
        "calls": calls,
        "context_per_call": sum(cr) / max(calls, 1),
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


def delta(now, before):
    if not before:
        return ""
    return f"  ({k(before) if before >= 1000 else before} -> {k(now) if now >= 1000 else now}, {100 * (now - before) / before:+.0f}%)"


def report(m, label, prev=None, base=None, window=None):
    if not m.get("sessions"):
        print(f"{label}: no sessions")
        return
    p = prev if prev and prev.get("sessions") else None
    b = base if base and base.get("sessions") else None
    lines = [
        ("sessions", "sessions", False),
        ("first-call context (median)", "first_median", True),
        ("peak context (median)", "peak_median", True),
        ("peak context (p90)", "peak_p90", True),
        ("sessions over 400k", "over_400k", False),
        (f"sessions over the compact window x1.1 ({k(window)})", "over_window", False),
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
        extra += ("  vs baseline" + delta(val, b.get(key))) if b else ""
        print(f"  {name:48s} {shown}{extra}")
    if m["compactions"]:
        print(f"  compactions fired at about {k(m['compaction_at_median'])}, sessions restarted at about "
              f"{k(m['after_compaction_median'])}")
    print("  first-call context by Claude Code version: " + ", ".join(
        f"{v} {k(x[0])} (n={x[1]})" for v, x in m["first_by_version"].items()))
    print("  tools that put the most into context: " + ", ".join(f"{n} {t / 1e6:.2f}M" for n, t in m["top_tools"]))


def day_ts(s):
    return dt.datetime.fromisoformat(s).timestamp()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--since")
    p.add_argument("--until", help="last day included")
    p.add_argument("--project")
    p.add_argument("--compare", action="store_true")
    p.add_argument("--save", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--simulate", type=int, metavar="WINDOW",
                   help="estimate the context re-read if sessions had compacted at WINDOW tokens")
    a = p.parse_args()
    cfg = config()
    pdir = project_dir(a.project)
    today = dt.date.today()
    until = dt.date.fromisoformat(a.until) if a.until else today
    since = dt.date.fromisoformat(a.since) if a.since else until - dt.timedelta(days=a.days - 1)
    span = (until - since).days + 1
    lo, hi = day_ts(str(since)), day_ts(str(until + dt.timedelta(days=1)))
    rows = load_rows(pdir, lo, hi, cfg)
    now = summarize(rows, cfg)
    sim = simulate(rows, a.simulate) if a.simulate else None
    prev = base = None
    if a.compare:
        prev = run(pdir, lo - span * 86400, lo, cfg)
        try:
            with open(os.path.join(data_dir(), HISTORY)) as f:
                first = [json.loads(x) for x in f if x.strip()]
            first = [h for h in first if h.get("project") == os.path.basename(pdir)]
            base = first[0]["metrics"] if first else None
        except (OSError, ValueError):
            base = None
    if a.save:
        os.makedirs(data_dir(), exist_ok=True)
        with open(os.path.join(data_dir(), HISTORY), "a") as f:
            f.write(json.dumps({"date": str(today), "since": str(since), "until": str(until),
                                "project": os.path.basename(pdir), "metrics": now}) + "\n")
    if a.json:
        print(json.dumps({"project": os.path.basename(pdir), "since": str(since), "until": str(until),
                          "now": now, "previous": prev, "baseline": base, "simulation": sim}, indent=1))
        return
    report(now, f"{since} .. {until}  ({os.path.basename(pdir)}, subagents excluded)", prev, base,
           cfg["compact_window"])
    if a.compare and prev and prev.get("sessions"):
        print(f"(deltas: the {span} days before; 'vs baseline': first saved run)")
    if sim:
        print(f"simulated compaction at {k(sim['window'])} (restart at about {k(sim['restart'])}): "
              f"{sim['sessions_affected']} of {sim['sessions']} sessions would compact; context re-read "
              f"{k(sim['context_read_real'])} -> {k(sim['context_read_sim'])} ({sim['saving_pct']:.0f}% less). "
              "Estimate: assumes the same work after compaction.")


if __name__ == "__main__":
    main()
