"""Tests on synthetic transcripts and memory folders in a temp CLAUDE_CONFIG_DIR.

Run: python3 -m unittest discover -s tests
"""
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import common  # noqa: E402
import context_guard  # noqa: E402
import measure  # noqa: E402
import memory_check  # noqa: E402
import setup as tc_setup  # noqa: E402

STAMPED_SETTINGS = re.compile(r"settings-\d{8}-\d{6}-\d{6}(?:-\d+)?\.json")
STAMPED_MEMORY = re.compile(r"\d{8}-\d{6}-\d{6}(?:-\d+)?")


def assistant(mid, ctx, tool=None):
    content = [{"type": "tool_use", "id": "t-" + mid, "name": tool, "input": {}}] if tool else []
    return {"type": "assistant", "version": "9.9.9", "message": {
        "id": mid, "content": content,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": ctx - 10,
                  "cache_creation_input_tokens": 0, "output_tokens": 5}}}


def tool_result(mid, text):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t-" + mid, "content": text}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp
        self.pdir = os.path.join(self.tmp, "projects", "-tmp-demo")
        os.makedirs(self.pdir)

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        for root, dirs, _ in os.walk(self.tmp):
            for d in dirs:
                p = os.path.join(root, d)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        shutil.rmtree(self.tmp)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def transcript(self, name, entries):
        path = os.path.join(self.pdir, name + ".jsonl")
        with open(path, "w") as f:
            for e in entries:
                f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")
        return path

    def mem(self):
        mem = os.path.join(self.pdir, "memory")
        os.makedirs(mem, exist_ok=True)
        return mem

    def run_script(self, name, *args, cwd=None):
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, name)] + list(args),
                              capture_output=True, text=True, cwd=cwd or self.tmp)


class TranscriptTests(Base):
    def test_session_stats_and_compaction(self):
        path = self.transcript("s1", [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "attachment", "attachment": {"type": "skill_listing"}},
            assistant("m1", 100_000, "Read"),
            tool_result("m1", "x" * 4000),
            assistant("m2", 320_000),
            {"type": "system", "subtype": "compact_boundary"},
            assistant("m3", 90_000),
        ])
        r = common.session_stats(path)
        self.assertEqual(r["first"], 100_000)
        self.assertEqual(r["peak"], 320_000)
        self.assertEqual(r["compactions"], [(320_000, 90_000)])
        self.assertEqual(round(r["tools"]["Read"]), 1000)
        self.assertIn("attachment:skill_listing", r["start_bytes"])
        self.assertEqual(common.last_context(path), 90_005)

    def test_last_context_after_boundary_is_zero(self):
        path = self.transcript("s2", [assistant("m1", 500_000), {"type": "system", "subtype": "compact_boundary"}])
        self.assertEqual(common.last_context(path), 0)

    def test_measure_summary(self):
        self.transcript("s3", [assistant("a", 80_000), assistant("b", 450_000)])
        m = measure.run(self.pdir, None, None, common.config())
        self.assertEqual(m["sessions"], 1)
        self.assertEqual(m["over_400k"], 1)
        self.assertEqual(m["over_window"], 1)
        self.assertEqual(m["first_by_version"]["9.9.9"][0], 80_000)

    # item 2
    def test_transcripts_skip_symlinks_and_dirs(self):
        real = self.transcript("real", [assistant("a", 1000)])
        outside = os.path.join(self.tmp, "outside.jsonl")
        self.write(outside, json.dumps(assistant("b", 2000)) + "\n")
        os.symlink(outside, os.path.join(self.pdir, "link.jsonl"))
        os.makedirs(os.path.join(self.pdir, "dir.jsonl"))
        self.assertEqual(common.transcripts(self.pdir), [real])

    # item 3
    def test_parsing_survives_odd_records(self):
        path = self.transcript("odd", [
            "[]", '"text"', "42", "null", "not json",
            {"type": "assistant", "message": "nope"},
            {"type": "assistant", "message": {"id": ["x"], "usage": {"input_tokens": "100"}}},
            {"type": "assistant", "message": {"id": "u", "usage": "bad"}},
            {"type": ["assistant"], "version": 5},
            {"type": "attachment", "attachment": ["x"]},
            {"type": "assistant", "message": {"id": "t", "content": [
                {"type": "tool_use", "id": ["a"], "name": 5}, {"type": "tool_use", "id": "ok", "name": ["n"]}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": {"a": 1}, "content": [{"type": "text", "text": 7}]}]}},
            '{"type": "assistant", "message": {"id": "n", "usage": {"input_tokens": NaN, '
            '"cache_read_input_tokens": Infinity, "cache_creation_input_tokens": -5, "output_tokens": true}}}',
            {"type": "assistant", "message": {"id": "f", "usage": {
                "input_tokens": 2500.0, "cache_read_input_tokens": True, "cache_creation_input_tokens": None}}},
            assistant("m1", 100_000),
        ])
        r = common.session_stats(path)
        self.assertEqual(r["series"], [2500, 100_000])
        self.assertEqual(r["version"], "9.9.9")
        self.assertEqual(r["cache_read"], 99_990)

    def test_last_context_survives_odd_records(self):
        path = self.transcript("odd2", [
            assistant("m1", 100_000), '["usage"]', '{"type": "assistant", "message": {"usage": ["x"]}}',
            '{"type": "assistant", "message": "usage"}',
            '{"type": "assistant", "message": {"usage": {"input_tokens": "9", "output_tokens": NaN}}}'])
        self.assertEqual(common.last_context(path), 100_005)

    # item 7
    def test_mean_context_per_call_is_full_context(self):
        first = {"type": "assistant", "message": {"id": "c1", "usage": {
            "input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 99_000,
            "output_tokens": 1}}}
        self.transcript("full", [first, assistant("c2", 200_000)])
        m = measure.run(self.pdir, None, None, common.config())
        self.assertEqual(m["context_per_call"], 150_000)
        self.assertEqual(m["cache_read_total"], 199_990)

    def test_baseline_only_for_medians_and_per_call(self):
        self.transcript("b1", [assistant("a", 80_000), assistant("b", 450_000)])
        m = measure.run(self.pdir, None, None, common.config())
        out = io.StringIO()
        with redirect_stdout(out):
            measure.report(m, "label", None, m, 300_000)
        lines = {ln[2:50].rstrip(): ln for ln in out.getvalue().splitlines() if ln.startswith("  ")}
        for name in ("first-call context (median)", "peak context (median)", "peak context (p90)",
                     "mean context per model call", "cache reads per session (median)"):
            self.assertIn("vs baseline", lines[name], name)
        for name in ("sessions", "sessions over 400k", "compactions", "cache reads total", "images in tool results"):
            self.assertNotIn("vs baseline", lines[name], name)

    def test_missed_compaction_since_setup(self):
        self.transcript("big", [assistant("a", 80_000), assistant("b", 450_000)])
        self.transcript("small", [assistant("c", 80_000)])
        os.makedirs(common.data_dir())
        rec = os.path.join(common.data_dir(), "install.json")
        for when, missed in (("2000-01-01T00:00:00+00:00", 1), ("2100-01-01T00:00:00+00:00", 0)):
            self.write(rec, json.dumps({"previous_autoCompactWindow": "missing", "installed_value": 300_000,
                                        "installed_at": when}))
            m = measure.run(self.pdir, None, None, common.config())
            self.assertEqual(m["missed_compaction"], missed)
            self.assertEqual(m["over_window"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            measure.report(m, "label", None, None, 300_000)
        self.assertIn("sessions that should have compacted but did not (since setup)", out.getvalue())
        self.assertIn("sessions over the compact window x1.1 (any)", out.getvalue())


class ProjectTests(Base):
    # item 1
    def test_project_path_maps_to_transcript_folder(self):
        work = os.path.join(self.tmp, "work", "my proj")
        os.makedirs(work)
        pdir = os.path.join(self.tmp, "projects", common.project_key(work))
        os.makedirs(pdir)
        self.assertEqual(common.project_dir(work), pdir)
        other = os.path.join(self.tmp, "work", "no transcripts")
        os.makedirs(other)
        with self.assertRaises(SystemExit):
            common.project_dir(other)

    def test_project_bare_key_and_outside_candidates(self):
        self.assertEqual(common.project_dir("-tmp-demo"), self.pdir)
        self.write(os.path.join(self.tmp, "projects", "-a-file"), "x")
        for bad in ("..", ".", "../projects/-tmp-demo", "-a-file", "-not-there"):
            with self.assertRaises(SystemExit, msg=bad):
                common.project_dir(bad)

    def test_no_project_and_unknown_cwd_is_a_clear_error(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        proc = self.run_script("measure.py", cwd=elsewhere)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--project", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        old = os.getcwd()
        os.chdir(elsewhere)
        try:
            pdir = os.path.join(self.tmp, "projects", common.project_key(os.getcwd()))
            os.makedirs(pdir)
            self.assertEqual(common.project_dir(), pdir)
        finally:
            os.chdir(old)

    def test_scripts_print_project_key_first(self):
        self.transcript("k", [assistant("a", 1000)])
        self.mem()
        for script in ("measure.py", "audit_start.py", "memory_check.py"):
            proc = self.run_script(script, "--project=-tmp-demo")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.splitlines()[0], "project: -tmp-demo", script)
            self.assertIn("--project=KEY", self.run_script(script, "--help").stdout, script)

    # item 3
    def test_config_ignores_bad_file_and_wrong_types(self):
        os.makedirs(common.data_dir())
        path = os.path.join(common.data_dir(), "config.json")
        self.write(path, "[1, 2]")
        self.assertEqual(common.config(), common.DEFAULTS)
        self.write(path, json.dumps({"compact_window": "300000", "guard_bands": [250_000, "x"],
                                     "auto_fix_memory": "yes", "reverify_days": True, "split_chars": 5000,
                                     "index_line_max": 1.5}))
        cfg = common.config()
        self.assertEqual(cfg["split_chars"], 5000)
        for key in ("compact_window", "guard_bands", "auto_fix_memory", "reverify_days", "index_line_max"):
            self.assertEqual(cfg[key], common.DEFAULTS[key], key)

    # item 10
    def test_auto_fix_memory_defaults_off(self):
        self.assertIs(common.DEFAULTS["auto_fix_memory"], False)
        self.assertIs(common.config()["auto_fix_memory"], False)
        os.makedirs(common.data_dir())
        self.write(os.path.join(common.data_dir(), "config.json"), json.dumps({"auto_fix_memory": True}))
        self.assertIs(common.config()["auto_fix_memory"], True)

    def test_bad_dates_are_a_clear_error(self):
        for args in (["--since", "2026-13-45"], ["--until", "yesterday"]):
            proc = self.run_script("measure.py", "--project=-tmp-demo", *args)
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn(args[0], proc.stderr)

    # item 8
    def test_audit_reads_project_folder_claude_md(self):
        work = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(work, ".claude"))
        self.write(os.path.join(work, "CLAUDE.md"), "w" * 1234)
        self.write(os.path.join(work, ".claude", "CLAUDE.md"), "w" * 10)
        self.write(os.path.join(work, "CLAUDE.local.md"), "w" * 7)
        os.makedirs(os.path.join(self.tmp, "projects", common.project_key(work)))
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.write(os.path.join(elsewhere, "CLAUDE.md"), "e" * 99)
        proc = self.run_script("audit_start.py", "--project", work, "--json", cwd=elsewhere)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = json.loads(proc.stdout)
        self.assertEqual(r["project_folder"], work)
        self.assertEqual(r["files"]["<project>/CLAUDE.md"], 1234)
        self.assertEqual(r["files"]["<project>/.claude/CLAUDE.md"], 10)
        self.assertEqual(r["files"]["<project>/CLAUDE.local.md"], 7)

    def test_audit_growth_shows_small_values(self):
        now = time.time()
        old = self.transcript("old", [{"type": "attachment", "attachment": {"type": "hook_additional_context",
                                                                            "content": "x" * 10}},
                                      assistant("a", 1000)])
        os.utime(old, (now - 10 * 86400, now - 10 * 86400))
        self.transcript("new", [{"type": "attachment", "attachment": {"type": "hook_additional_context",
                                                                      "content": "x" * 410}},
                                assistant("b", 1000)])
        proc = self.run_script("audit_start.py", "--project=-tmp-demo", "--days", "7")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [ln for ln in proc.stdout.splitlines() if "attachment:hook_additional_context" in ln][0]
        self.assertNotIn("0k", line)
        self.assertIn("+400 bytes", line)
        self.assertIn("+100 tokens", line)


class SimulateTests(Base):
    def test_simulated_compaction_cuts_reads(self):
        self.transcript("sim", [assistant("a", 80_000), assistant("b", 200_000),
                                assistant("c", 350_000), assistant("d", 400_000)])
        rows = measure.load_rows(self.pdir, None, None, common.config())
        sim = measure.simulate(rows, 300_000, restart=90_000)
        self.assertEqual(sim["sessions_affected"], 1)
        self.assertEqual(sim["context_read_real"], 1_030_000)
        self.assertEqual(sim["context_read_sim"], 80_000 + 200_000 + 240_000 + 290_000)


class GuardTests(Base):
    def run_hook(self, path, session="sess-1"):
        out = io.StringIO()
        sys.stdin = io.StringIO(json.dumps({"session_id": session, "transcript_path": path}))
        try:
            with redirect_stdout(out):
                context_guard.hook()
        finally:
            sys.stdin = sys.__stdin__
        return out.getvalue()

    def test_warns_once_per_band_and_resets(self):
        path = self.transcript("g", [assistant("m1", 260_000)])
        first = self.run_hook(path)
        self.assertIn("context-guard", first)
        self.assertEqual(json.loads(first)["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(self.run_hook(path), "")
        path = self.transcript("g", [assistant("m1", 260_000), {"type": "system", "subtype": "compact_boundary"}])
        self.assertEqual(self.run_hook(path), "")
        self.assertEqual(context_guard.load_band("sess-1"), 0)

    def test_silent_below_first_band_and_on_bad_input(self):
        path = self.transcript("g2", [assistant("m1", 100_000)])
        self.assertEqual(self.run_hook(path), "")
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "context_guard.py"), "hook"],
                              input="not json", capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    # item 5
    def test_old_guard_state_files_are_pruned(self):
        gdir = os.path.join(common.data_dir(), "guard")
        os.makedirs(gdir)
        old, recent = os.path.join(gdir, "old-session.json"), os.path.join(gdir, "recent.json")
        self.write(old, '{"band": 250000}')
        self.write(recent, '{"band": 250000}')
        t = time.time() - 40 * 86400
        os.utime(old, (t, t))
        self.assertIn("context-guard", self.run_hook(self.transcript("g3", [assistant("m1", 260_000)])))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))


class MemoryTests(Base):
    def test_index_and_memory_checks(self):
        mem = self.mem()
        with open(os.path.join(mem, "MEMORY.md"), "w") as f:
            f.write("- [A](a.md): short\n- [Gone](gone.md): " + "x" * 200 + "\n")
        with open(os.path.join(mem, "a.md"), "w") as f:
            f.write("---\nname: a\nmetadata:\n  type: feedback\n---\nrule\n**Verificato 2026-10-26:** ok\n")
        with open(os.path.join(mem, "b.md"), "w") as f:
            f.write("---\nname: b\nmetadata:\n  type: project\n---\nsee `~/definitely/not/here.txt` and [[nowhere]]\n"
                    + "y" * 11_000)
        r = memory_check.check(self.pdir, common.config())
        self.assertEqual(r["orphans"], ["b.md"])
        self.assertEqual(r["broken_index_links"], ["gone.md"])
        self.assertEqual(len(r["index_long_lines"]), 1)
        rows = {x["file"]: x for x in r["memories"]}
        self.assertFalse(rows["a.md"]["archive_candidate"])  # feedback rules are never archive candidates
        self.assertTrue(rows["b.md"]["archive_candidate"])
        self.assertTrue(rows["b.md"]["split_candidate"])
        self.assertIn("never verified", rows["b.md"]["needs_check"])
        self.assertEqual(rows["b.md"]["dead_links"], ["nowhere"])
        self.assertEqual([p["written"] for p in rows["b.md"]["dead_paths"]], ["~/definitely/not/here.txt"])

    def test_used_memory_is_not_archive_candidate(self):
        mem = self.mem()
        self.write(os.path.join(mem, "MEMORY.md"), "- [P](p.md): x\n")
        self.write(os.path.join(mem, "p.md"), "---\nmetadata:\n  type: project\n---\nbody\n")
        self.transcript("u", [{"type": "user", "message": {"content": f"read {mem}/p.md"}}, assistant("m", 1000)])
        rows = {x["file"]: x for x in memory_check.check(self.pdir, common.config())["memories"]}
        self.assertFalse(rows["p.md"]["archive_candidate"])

    def rows(self):
        return {x["file"]: x for x in memory_check.check(self.pdir, common.config())["memories"]}

    # item 2
    def test_memory_files_skip_symlinks_and_dirs(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a.md"), "---\ntype: project\n---\nbody\n")
        outside = os.path.join(self.tmp, "outside.md")
        self.write(outside, "secret\n")
        os.symlink(outside, os.path.join(mem, "link.md"))
        os.makedirs(os.path.join(mem, "sub.md"))
        self.assertEqual(list(self.rows()), ["a.md"])

    # item 6
    def test_verified_regex_is_linear(self):
        text = ("Verified" + "*" * 3000) * 40
        start = time.monotonic()
        memory_check.VERIFIED.findall(text)
        self.assertLess(time.monotonic() - start, 0.5)
        for ok in ("**Verified 2026-10-20:** ok", "Verificato: 2026-10-20", "**Verificato** 2026-10-20",
                   "**Verified:** 2026-10-20"):
            self.assertEqual(memory_check.VERIFIED.findall(ok), ["2026-10-20"], ok)

    def test_verified_dates_invalid_and_future_ignored(self):
        mem = self.mem()
        self.write(os.path.join(mem, "bad.md"), "**Verified 2026-13-45:** x\n")
        self.write(os.path.join(mem, "future.md"), "**Verified 2999-01-01:** x\n")
        self.write(os.path.join(mem, "mixed.md"),
                   "Verified 2026-10-01\n**Verificato:** 2026-10-20 ok\nVerified 2999-12-31\n")
        rows = self.rows()
        self.assertIsNone(rows["bad.md"]["verified"])
        self.assertIn("never verified", rows["bad.md"]["needs_check"])
        self.assertIsNone(rows["future.md"]["verified"])
        self.assertEqual(rows["mixed.md"]["verified"], "2026-10-20")

    def test_needs_user_is_waiting_not_verified(self):
        mem = self.mem()
        self.write(os.path.join(mem, "w.md"), "---\ntype: project\n---\nbody\n**Verified 2026-10-20:** ok\n"
                                              "**Needs user 2026-10-21:** is the deploy done?\n")
        row = self.rows()["w.md"]
        self.assertIsNone(row["verified"])
        self.assertIn("waiting for the user", row["needs_check"])

    def test_quoted_frontmatter_type(self):
        mem = self.mem()
        self.write(os.path.join(mem, "q.md"), '---\nname: q\ntype: "feedback"\n---\nrule\n')
        self.write(os.path.join(mem, "q2.md"), "---\nmetadata:\n  type: 'user'\n---\nnote\n")
        rows = self.rows()
        self.assertEqual((rows["q.md"]["type"], rows["q2.md"]["type"]), ("feedback", "user"))
        self.assertFalse(rows["q.md"]["archive_candidate"])

    def test_wiki_and_path_patterns(self):
        mem = self.mem()
        self.write(os.path.join(mem, "p.md"),
                   "---\ntype: project\n---\n[[multi\nline]] [[" + "w" * 250 + "]]\n"
                   "`~/a\nb` `/var/definitely-missing-xyz` `/tmp/$(touch pwned)` `/srv/a;b` `/tmp/"
                   + "p" * 400 + "`\n")
        row = self.rows()["p.md"]
        self.assertEqual(row["dead_links"], [])
        self.assertEqual(row["dead_paths"], [{"written": "/var/definitely-missing-xyz",
                                              "expanded": "/var/definitely-missing-xyz"}])
        self.assertEqual(row["unusual_paths"], ["/tmp/$(touch pwned)", "/srv/a;b"])

    def test_printed_memory_text_is_sanitized(self):
        mem = self.mem()
        self.write(os.path.join(mem, "MEMORY.md"), "- [E](e.md): x\n")
        self.write(os.path.join(mem, "e.md"), "---\ntype: project\n---\n[[evil\x1b[2Jname]] [[" + "z" * 190 + "]]\n")
        proc = self.run_script("memory_check.py", "--project=-tmp-demo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("evil", proc.stdout)
        self.assertNotIn("\x1b", proc.stdout)
        self.assertNotIn("z" * 121, proc.stdout)

    def test_index_links_resolved_inside_memory_folder(self):
        mem = self.mem()
        os.makedirs(os.path.join(mem, "sub"))
        self.write(os.path.join(mem, "MEMORY.md"), "- [A](./a.md): x\n- [S](sub/s.md): x\n- [O](../outside.md): x\n"
                                                   "- [G](gone.md): x\n- [N](sub/../a.md): x\n")
        self.write(os.path.join(mem, "a.md"), "a\n")
        self.write(os.path.join(mem, "sub", "s.md"), "s\n")
        self.write(os.path.join(self.pdir, "outside.md"), "o\n")
        r = memory_check.check(self.pdir, common.config())
        self.assertEqual(r["broken_index_links"], ["gone.md"])
        self.assertEqual(r["orphans"], [])
        self.assertEqual(r["outside_index_links"], ["../outside.md"])

    def test_backup_copies_only_regular_md(self):
        mem = self.mem()
        os.makedirs(os.path.join(mem, "sub"))
        self.write(os.path.join(mem, "a.md"), "a\n")
        self.write(os.path.join(mem, "sub", "b.md"), "b\n")
        self.write(os.path.join(mem, "notes.txt"), "n\n")
        secret = os.path.join(self.tmp, "secret.md")
        self.write(secret, "secret\n")
        os.symlink(secret, os.path.join(mem, "link.md"))
        outdir = os.path.join(self.tmp, "outdir")
        os.makedirs(outdir)
        self.write(os.path.join(outdir, "c.md"), "c\n")
        os.symlink(outdir, os.path.join(mem, "linkdir"))
        res = memory_check.backup(mem)
        got = sorted(os.path.relpath(os.path.join(r, f), res["path"])
                     for r, _, fs in os.walk(res["path"]) for f in fs)
        self.assertEqual(got, ["a.md", os.path.join("sub", "b.md")])
        self.assertEqual((res["files"], res["errors"]), (2, []))
        if os.geteuid() != 0:
            locked = os.path.join(mem, "locked.md")
            self.write(locked, "l\n")
            os.chmod(locked, 0)
            try:
                res = memory_check.backup(mem)
            finally:
                os.chmod(locked, 0o600)
            self.assertEqual(len(res["errors"]), 1)
            self.assertEqual(res["files"], 2)

    def test_backup_unique_names_and_safe_rotation(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a.md"), "a\n")
        backups = os.path.join(common.data_dir(), "backups")
        root = os.path.join(backups, "memory", "-tmp-demo")
        os.makedirs(os.path.join(root, "keepme"))
        flat = os.path.join(backups, "memory-20200101-000000-000000")  # a flat folder from an earlier version
        os.makedirs(flat)
        paths = [memory_check.backup(mem)["path"] for _ in range(6)]
        self.assertEqual(len(set(paths)), 6)
        names = sorted(os.listdir(root))
        stamped = [n for n in names if STAMPED_MEMORY.fullmatch(n)]
        self.assertEqual(stamped, [os.path.basename(p) for p in paths[-4:]])
        self.assertIn("keepme", names)
        self.assertTrue(os.path.isdir(flat))

    # round 2, item 3
    def test_backups_rotate_per_project(self):
        mem_a = self.mem()
        self.write(os.path.join(mem_a, "a.md"), "a\n")
        mem_b = os.path.join(self.tmp, "projects", "-tmp-other", "memory")
        os.makedirs(mem_b)
        self.write(os.path.join(mem_b, "b.md"), "b\n")
        first = memory_check.backup(mem_a)["path"]
        for _ in range(4):
            memory_check.backup(mem_b)
        self.assertTrue(os.path.isfile(os.path.join(first, "a.md")))
        root = os.path.join(common.data_dir(), "backups", "memory")
        self.assertEqual(os.path.dirname(first), os.path.join(root, "-tmp-demo"))
        self.assertEqual(len(os.listdir(os.path.join(root, "-tmp-other"))), 4)

    # round 3, item 3
    def test_rotation_never_deletes_the_new_backup(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a.md"), "a\n")
        root = os.path.join(common.data_dir(), "backups", "memory", "-tmp-demo")
        now, future = time.time(), []
        for i in range(4):
            d = os.path.join(root, f"2999010{i + 1}-000000-000000")
            os.makedirs(d)
            os.utime(d, (now - 100 + i, now - 100 + i))
            future.append(d)
        res = memory_check.backup(mem)
        self.assertTrue(os.path.isfile(os.path.join(res["path"], "a.md")))
        self.assertIs(res.get("failed"), False)
        self.assertEqual(len([n for n in os.listdir(root) if STAMPED_MEMORY.fullmatch(n)]), 4)
        self.assertFalse(os.path.exists(future[0]))  # the oldest by modification time goes
        self.assertTrue(all(os.path.isdir(d) for d in future[1:]))

    def test_symlinked_backup_folders_are_refused(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a.md"), "a\n")
        backups = os.path.join(common.data_dir(), "backups")
        levels = (os.path.join(backups, "memory", "-tmp-demo"), os.path.join(backups, "memory"), backups)
        for i, level in enumerate(levels):
            shutil.rmtree(common.data_dir(), ignore_errors=True)
            os.makedirs(os.path.dirname(level))
            elsewhere = os.path.join(self.tmp, f"elsewhere-{i}")
            os.makedirs(elsewhere)
            os.symlink(elsewhere, level)
            res = memory_check.backup(mem)
            self.assertIs(res.get("failed"), True, level)
            self.assertTrue(res["errors"], level)
            self.assertEqual(os.listdir(elsewhere), [], level)
        proc = self.run_script("memory_check.py", "--project=-tmp-demo", "--backup")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Backup FAILED", proc.stdout)
        self.assertIn("Do not edit any memory file in this run", proc.stdout)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "elsewhere-2")), [])

    def test_backup_gone_after_rotation_is_a_failure(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a.md"), "a\n")
        root = os.path.join(common.data_dir(), "backups", "memory", "-tmp-demo")
        for i in range(4):
            os.makedirs(os.path.join(root, f"2000010{i + 1}-000000-000000"))
        real = shutil.rmtree

        def wipe(path, *args, **kw):  # a rotation that removes everything, the new copy too
            for name in os.listdir(os.path.dirname(path)):
                real(os.path.join(os.path.dirname(path), name), ignore_errors=True)

        with mock.patch.object(memory_check.shutil, "rmtree", side_effect=wipe):
            res = memory_check.backup(mem)
        self.assertFalse(os.path.exists(res["path"]))
        self.assertIs(res.get("failed"), True)
        self.assertTrue(res["errors"])

    # round 2, item 4
    def test_tilde_dead_paths_are_reported_expanded(self):
        mem = self.mem()
        self.write(os.path.join(mem, "MEMORY.md"), "- [T](t.md): x\n")
        self.write(os.path.join(mem, "t.md"), "---\ntype: project\n---\nsee `~/definitely/not/here.txt`\n")
        expanded = os.path.join(self.tmp, "definitely", "not", "here.txt")
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            row = self.rows()["t.md"]
            proc = self.run_script("memory_check.py", "--project=-tmp-demo")
        self.assertEqual(row["dead_paths"], [{"written": "~/definitely/not/here.txt", "expanded": expanded}])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(expanded, proc.stdout)
        self.assertNotIn("e.g. ~/", proc.stdout)

    # round 2, item 5
    def test_index_links_are_percent_decoded(self):
        mem = self.mem()
        self.write(os.path.join(mem, "my note.md"), "n\n")
        self.write(os.path.join(mem, "MEMORY.md"), "- [N](my%20note.md): x\n")
        r = memory_check.check(self.pdir, common.config())
        self.assertEqual(r["broken_index_links"], [])
        self.assertEqual(r["orphans"], [])

    def test_mixed_index_line_is_a_proposal_not_a_drop(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a.md"), "a\n")
        self.write(os.path.join(mem, "MEMORY.md"),
                   "- [A](a.md): see also [G](gone.md)\n- [X](gone2.md): x [Y](gone3.md)\n"
                   "- [Z](gone4.md): a plain hook\n")
        r = memory_check.check(self.pdir, common.config())
        self.assertEqual([x["line"] for x in r.get("mixed_index_lines", [])], [1, 2])
        self.assertEqual([x["line"] for x in r.get("index_lines_to_drop", [])], [3])
        self.assertEqual(r["broken_index_links"], ["gone.md", "gone2.md", "gone3.md", "gone4.md"])
        before = self.read(os.path.join(mem, "MEMORY.md"))
        proc = self.run_script("memory_check.py", "--project=-tmp-demo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Mixed index lines", proc.stdout)
        self.assertIn("Index drop candidates, one plain link and it is broken (a proposal): line 3 (", proc.stdout)
        self.assertIn("Index changes are proposals", proc.stdout)
        self.assertEqual(self.read(os.path.join(mem, "MEMORY.md")), before)

    # round 3, item 2
    def test_literal_percent_file_name_is_not_broken(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a%20b.md"), "n\n")
        self.write(os.path.join(mem, "MEMORY.md"), "- [N](a%20b.md): x\n")
        r = memory_check.check(self.pdir, common.config())
        self.assertEqual(r["broken_index_links"], [])
        self.assertEqual(r["index_lines_to_drop"], [])
        self.assertEqual(r["orphans"], [])

    def test_broken_link_next_to_anything_else_is_mixed(self):
        mem = self.mem()
        self.write(os.path.join(mem, "ok.md"), "ok\n")
        self.write(os.path.join(mem, "MEMORY.md"),
                   "- [G](gone1.md): see https://example.com/page\n"
                   "- [G](gone2.md): see [[ok]]\n"
                   "- **Rule for deploys.** Details: [r](gone3.md)\n"
                   "- [C](gone4.md): run `make`\n"
                   "- [P](gone5.md): a plain hook\n")
        r = memory_check.check(self.pdir, common.config())
        self.assertEqual([x["line"] for x in r["mixed_index_lines"]], [1, 2, 3, 4])
        self.assertEqual([x["line"] for x in r["index_lines_to_drop"]], [5])

    # round 3, item 1 and item 4
    def test_docs_run_no_git_and_scope_read(self):
        root = os.path.dirname(HERE)
        git_cmd = re.compile(r"\bgit\s+(?:-C|-c|--no-pager|--git-dir|log|status|show|rev-parse|branch|diff|config|"
                             r"fetch|remote|describe|ls-files|cat-file|for-each-ref)\b")
        for doc in ("SKILL.md", "SECURITY.md", "README.md", os.path.join("references", "verify-agent.md")):
            self.assertIsNone(git_cmd.search(self.read(os.path.join(root, doc))), doc)
        skill = self.read(os.path.join(root, "SKILL.md"))
        self.assertNotIn("'Read'", skill)
        for rule in ("'Read(~/.claude/projects/<key>/**)'", "'Read(~/.claude/token-checkup/**)'",
                     "'Read(<skill dir>/**)'"):
            self.assertIn(rule, skill)

    def docs(self):
        root = os.path.dirname(HERE)
        names = ("SKILL.md", "SECURITY.md", "README.md", os.path.join("references", "verify-agent.md"),
                 os.path.join("references", "report.md"))
        return {n: " ".join(self.read(os.path.join(root, n)).split()) for n in names}

    def assertInDoc(self, part, text, name):
        self.assertTrue(part in text, f"{name}: missing {part!r}")

    # round 4, item 1
    def test_docs_never_read_inside_a_repo(self):
        inside_git = re.compile(r"/\.git\b|\.git/")  # a path that names a .git folder or a file in one
        read_git = re.compile(r"\b(?:read|open|cat|list|look|inspect)\w*\b[^.,;:\n]{0,80}\.git\b", re.I)
        for name, text in self.docs().items():
            m = inside_git.search(text)
            self.assertIsNone(m, f"{name}: {m and text[max(0, m.start() - 60):m.end() + 20]!r}")
            for m in read_git.finditer(text):  # allowed only as a prohibition
                before = text[max(0, m.start() - 20):m.start()]
                self.assertRegex(before, r"(?i)\b(?:never|not|no)\b", f"{name}: {m.group(0)!r}")
        name = os.path.join("references", "verify-agent.md")
        verify = self.docs()[name]
        for part in ("test -d '<dir>'", 'its branch, commits, uncommitted work and anything inside it are "not checked"',
                     "gh repo view OWNER/NAME --json visibility,isArchived,pushedAt"):
            self.assertInDoc(part, verify, name)

    # round 4, item 2
    def test_docs_fix_mode_needs_a_working_backup(self):
        docs = self.docs()
        name = os.path.join("references", "verify-agent.md")
        verify = docs[name]
        self.assertInDoc("{BACKUP}", verify, name)
        self.assertInDoc("none: propose only", verify, name)
        self.assertFalse("a backup was made" in verify, name)
        for doc in (name, "SKILL.md"):
            self.assertInDoc("backup_failed: false", docs[doc], doc)

    # round 4, item 4
    def test_scheduled_run_starts_in_the_data_folder(self):
        self.assertInDoc('cd "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/token-checkup" && claude -p',
                         self.docs()["SKILL.md"], "SKILL.md")

    # round 4, item 2
    def test_failed_copy_never_rotates_older_copies(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a.md"), "x\n")
        good = [memory_check.backup(mem)["path"] for _ in range(4)]
        self.assertTrue(all(os.path.isdir(g) for g in good))

        def copy(src, dst, **kw):
            raise PermissionError(13, "Permission denied", src)

        with mock.patch.object(memory_check.shutil, "copy2", side_effect=copy):
            res = memory_check.backup(mem)
        self.assertIs(res["failed"], True)
        self.assertTrue(all(os.path.isdir(g) for g in good))
        self.assertTrue(any("rotation skipped" in n for n in res["notes"]))

    def test_any_copy_or_walk_error_fails_the_backup(self):
        mem = self.mem()
        for i in range(13):
            self.write(os.path.join(mem, f"m{i:02}.md"), "x\n")
        real_copy, real_walk = shutil.copy2, os.walk

        def copy(src, dst, **kw):  # every file but the first cannot be copied
            if os.path.basename(src) != "m00.md":
                raise PermissionError(13, "Permission denied", src)
            return real_copy(src, dst, **kw)

        def walk(top, onerror=None, **kw):  # a folder that cannot be listed
            onerror(PermissionError(13, "Permission denied", os.path.join(top, "sub")))
            yield from real_walk(top, onerror=onerror, **kw)

        with mock.patch.object(memory_check.shutil, "copy2", side_effect=copy):
            res = memory_check.backup(mem)
        self.assertEqual((res["files"], len(res["errors"])), (1, 12))
        self.assertTrue(os.path.isdir(res["path"]))
        self.assertIs(res["failed"], True)
        with mock.patch.object(memory_check.os, "walk", side_effect=walk):
            res = memory_check.backup(mem)
        self.assertEqual((res["files"], len(res["errors"])), (13, 1))
        self.assertIs(res["failed"], True)
        os.makedirs(common.data_dir(), exist_ok=True)
        self.write(os.path.join(common.data_dir(), "config.json"), json.dumps({"auto_fix_memory": True}))

        def run(*args, fail=True):
            out = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    sys, "argv", ["memory_check.py", "--project=-tmp-demo", "--backup", *args]))
                if fail:
                    stack.enter_context(mock.patch.object(memory_check.shutil, "copy2", side_effect=copy))
                stack.enter_context(redirect_stdout(out))
                memory_check.main()
            return out.getvalue()

        text = run()
        self.assertIn("Backup FAILED (12 errors): ", text)
        self.assertIn("; and 2 more.", text)
        self.assertIn("Do not edit any memory file in this run", text)
        self.assertIn("MODE propose", text)
        self.assertNotIn("MODE fix", text)
        r = json.loads(run("--json"))
        self.assertIs(r["backup_failed"], True)
        self.assertEqual(len(r["backup_errors"]), 12)
        self.assertEqual(r["fact_check_mode"], "propose")
        text = run(fail=False)
        self.assertNotIn("FAILED", text)
        self.assertIn("MODE fix", text)
        r = json.loads(run("--json", fail=False))
        self.assertEqual((r["backup_failed"], r["fact_check_mode"]), (False, "fix"))

    # round 4, item 3
    def test_future_dated_backups_rotate_out(self):
        mem = self.mem()
        self.write(os.path.join(mem, "a.md"), "a\n")
        root = os.path.join(common.data_dir(), "backups", "memory", "-tmp-demo")
        future = time.time() + 10 * 365 * 86400
        for i in range(3):
            d = os.path.join(root, f"2000010{i + 1}-000000-000000")
            os.makedirs(d)
            os.utime(d, (future, future))
        paths = [memory_check.backup(mem)["path"] for _ in range(5)]
        stamped = sorted(n for n in os.listdir(root) if STAMPED_MEMORY.fullmatch(n))
        self.assertEqual(stamped, sorted(os.path.basename(p) for p in paths[-4:]))

    # round 4, item 5
    def test_home_is_not_checked_on_macos(self):
        mem = self.mem()
        path = "/home/someone/x.txt"
        self.write(os.path.join(mem, "h.md"), f"---\ntype: project\n---\nsee `{path}`\n")
        seen = []

        def fake_exists(p):  # never touch the real /home, which macOS automounts
            seen.append(os.fspath(p))
            return False

        for platform, odd in (("darwin", [path]), ("linux", [])):
            seen.clear()
            with mock.patch.object(sys, "platform", platform), mock.patch("os.path.exists", side_effect=fake_exists):
                row = self.rows()["h.md"]
            self.assertEqual(row["unusual_paths"], odd, platform)
            self.assertEqual(path in seen, not odd, platform)
            self.assertEqual([p["written"] for p in row["dead_paths"]], [] if odd else [path], platform)

    # round 3, item 5
    def test_dotdot_and_network_paths_are_not_checked(self):
        mem = self.mem()
        risky = ["/mnt/share/x.txt", "/Users/../net/host/x", "~/../../Volumes/disk/x", "/tmp/a/../b",
                 "/private/../Network/Servers/x"]
        self.write(os.path.join(mem, "n.md"),
                   "---\ntype: project\n---\n" + " ".join(f"`{p}`" for p in risky) + "\n")
        seen, real = [], os.path.exists

        def spy(p):
            seen.append(os.fspath(p))
            return real(p)

        with mock.patch("os.path.exists", side_effect=spy):
            row = self.rows()["n.md"]
        self.assertEqual(row["dead_paths"], [])
        self.assertEqual(row["unusual_paths"], risky)
        self.assertFalse({os.path.expanduser(p) for p in risky} & set(seen))

    def test_link_regex_is_linear(self):
        mem = self.mem()
        self.write(os.path.join(mem, "MEMORY.md"), "- [x" + "](" * 20_000 + "\n")
        start = time.monotonic()
        r = memory_check.check(self.pdir, common.config())
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(r["broken_index_links"], [])
        self.assertEqual(memory_check.LINK.findall("- [A](a.md) [B](sub/my%20note.md)"), ["a.md", "sub/my%20note.md"])
        self.assertEqual(memory_check.LINK.findall("](" + "a" * 201 + ".md)"), [])
        start = time.monotonic()
        for tail in ("c" * 40_000, ".md)" * 10_000):
            self.assertIsNone(memory_check.PLAIN_LINE.fullmatch("- [" + "a" * 300 + "](" + "b" * 200 + tail))
        self.assertLess(time.monotonic() - start, 0.5)


class SetupTests(Base):
    def settings(self):
        return os.path.join(self.tmp, "settings.json")

    def save_settings(self, s):
        self.write(self.settings(), json.dumps(s))

    def read_settings(self):
        return json.loads(self.read(self.settings()))

    def call(self, fn, *args, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(*args, **kw)
        return out.getvalue()

    def commands(self):
        return [h["command"] for g in self.read_settings()["hooks"]["UserPromptSubmit"] for h in g["hooks"]]

    def test_install_and_uninstall_roundtrip(self):
        other = {"type": "command", "command": "echo other"}
        old_guard = {"type": "command", "command": "python3 /old/context_guard.py hook"}
        self.save_settings({"hooks": {"UserPromptSubmit": [{"hooks": [other, old_guard]}]}})
        self.call(tc_setup.install, 300_000, True, replace_legacy=True)
        s = self.read_settings()
        cmds = self.commands()
        self.assertEqual(s["autoCompactWindow"], 300_000)
        self.assertEqual(len([c for c in cmds if "context_guard.py" in c]), 1)
        self.assertTrue(any(tc_setup.TAG in c for c in cmds))
        self.assertIn("echo other", cmds)
        self.call(tc_setup.uninstall)
        s = self.read_settings()
        self.assertNotIn("autoCompactWindow", s)
        self.assertEqual(s["hooks"]["UserPromptSubmit"], [{"hooks": [other]}])

    # item 4
    def test_untagged_guard_left_alone_unless_replace_legacy(self):
        legacy = {"type": "command", "command": "python3 /old/context_guard.py hook"}
        self.save_settings({"hooks": {"UserPromptSubmit": [{"hooks": [legacy]}]}})
        out = self.call(tc_setup.install, 300_000, True)
        cmds = self.commands()
        self.assertIn(legacy["command"], cmds)
        self.assertEqual(sum(tc_setup.TAG in c for c in cmds), 1)
        self.assertIn("left untouched", out)
        self.call(tc_setup.install, 300_000, True, replace_legacy=True)
        cmds = self.commands()
        self.assertNotIn(legacy["command"], cmds)
        self.assertEqual(sum(tc_setup.TAG in c for c in cmds), 1)

    def test_other_groups_untouched_and_no_empty_hooks_left(self):
        original = {"theme": "dark", "hooks": {
            "UserPromptSubmit": [{"hooks": []}, {"matcher": "", "hooks": [{"type": "command", "command": "echo o"}]}],
            "Stop": []}}
        for start in (original, {"theme": "dark"}):
            self.save_settings(start)
            self.call(tc_setup.install, 300_000, True)
            self.call(tc_setup.uninstall)
            self.assertEqual(self.read_settings(), start)

    def test_save_follows_symlink_keeps_mode_and_unicode(self):
        real_dir = os.path.join(self.tmp, "dotfiles")
        os.makedirs(real_dir)
        target = os.path.join(real_dir, "settings.json")
        self.write(target, json.dumps({"name": "citt\u00e0"}))
        os.chmod(target, 0o640)
        os.symlink(target, self.settings())
        self.call(tc_setup.install, 300_000, True)
        self.assertTrue(os.path.islink(self.settings()))
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o640)
        text = self.read(target)
        self.assertIn("citt\u00e0", text)
        self.assertIn("autoCompactWindow", text)
        self.assertEqual(os.listdir(real_dir), ["settings.json"])
        os.remove(self.settings())
        self.call(tc_setup.install, 300_000, True)
        self.assertEqual(os.stat(self.settings()).st_mode & 0o777, 0o600)

    def test_bad_settings_exit_without_writing(self):
        cases = ["{not json", "[1, 2]", json.dumps({"hooks": []}),
                 json.dumps({"hooks": {"UserPromptSubmit": {"a": 1}}}),
                 json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": "x"}]}}),
                 json.dumps({"hooks": {"UserPromptSubmit": ["x"]}}), '{"a": NaN}']
        for text in cases:
            for fn, args in ((tc_setup.install, (300_000, True)), (tc_setup.uninstall, ())):
                self.write(self.settings(), text)
                with self.assertRaises(SystemExit, msg=text) as cm:
                    self.call(fn, *args)
                self.assertIsInstance(cm.exception.code, str)
                self.assertEqual(self.read(self.settings()), text)
                self.assertFalse(os.path.exists(os.path.join(common.data_dir(), "backups")))
                self.assertFalse(os.path.exists(os.path.join(common.data_dir(), "install.json")))

    def test_settings_backups_unique_first_kept_last_five(self):
        self.save_settings({"v": 0})
        for i in range(8):
            self.call(tc_setup.install, 300_000 + i * 1000, True)
        bdir = os.path.join(common.data_dir(), "backups")
        names = sorted(os.listdir(bdir))
        self.assertIn("settings-first.json", names)
        self.assertEqual(len([n for n in names if STAMPED_SETTINGS.fullmatch(n)]), 5)
        self.assertEqual(json.loads(self.read(os.path.join(bdir, "settings-first.json"))), {"v": 0})
        for n in names:
            self.assertEqual(os.stat(os.path.join(bdir, n)).st_mode & 0o777, 0o600, n)

    def test_install_record_and_uninstall_restore_rules(self):
        rec_path = os.path.join(common.data_dir(), "install.json")
        self.save_settings({"autoCompactWindow": 500_000})
        self.call(tc_setup.install, 300_000, True)
        rec = common.read_json(rec_path)
        self.assertEqual((rec["previous_autoCompactWindow"], rec["installed_value"]), (500_000, 300_000))
        dt.datetime.fromisoformat(rec["installed_at"])
        self.call(tc_setup.install, 400_000, True)
        rec = common.read_json(rec_path)
        self.assertEqual((rec["previous_autoCompactWindow"], rec["installed_value"]), (500_000, 400_000))
        self.call(tc_setup.uninstall)
        self.assertEqual(self.read_settings()["autoCompactWindow"], 500_000)
        # the user changed the value after install: left as it is
        self.save_settings({})
        self.call(tc_setup.install, 300_000, True)
        s = self.read_settings()
        s["autoCompactWindow"] = 450_000
        self.save_settings(s)
        out = self.call(tc_setup.uninstall)
        self.assertEqual(self.read_settings()["autoCompactWindow"], 450_000)
        self.assertIn("left", out)
        # an unusable previous value: left as it is
        self.save_settings({"autoCompactWindow": 50})
        self.call(tc_setup.install, 300_000, True)
        out = self.call(tc_setup.uninstall)
        self.assertEqual(self.read_settings()["autoCompactWindow"], 300_000)
        self.assertIn("left", out)

    # round 2, item 6
    def test_reinstall_after_user_change_restores_user_value(self):
        self.save_settings({"autoCompactWindow": 500_000})
        self.call(tc_setup.install, 300_000, True)
        s = self.read_settings()
        s["autoCompactWindow"] = 400_000
        self.save_settings(s)
        out = self.call(tc_setup.install, 300_000, True)
        self.call(tc_setup.uninstall)
        self.assertEqual(self.read_settings()["autoCompactWindow"], 400_000)
        self.assertIn("400000", out)
        # the user removed the value after install: uninstall leaves it unset
        self.save_settings({"autoCompactWindow": 500_000})
        self.call(tc_setup.install, 300_000, True)
        s = self.read_settings()
        del s["autoCompactWindow"]
        self.save_settings(s)
        self.call(tc_setup.install, 300_000, True)
        self.call(tc_setup.uninstall)
        self.assertNotIn("autoCompactWindow", self.read_settings())

    def test_uninstall_with_nothing_installed_writes_nothing(self):
        self.assertIn("nothing to remove", self.call(tc_setup.uninstall))
        self.assertFalse(os.path.exists(self.settings()))
        self.save_settings({"theme": "dark"})
        before = os.stat(self.settings()).st_mtime_ns
        self.assertIn("nothing to remove", self.call(tc_setup.uninstall))
        self.assertEqual(os.stat(self.settings()).st_mtime_ns, before)
        self.assertFalse(os.path.exists(os.path.join(common.data_dir(), "backups")))

    def test_venv_warning(self):
        with mock.patch.object(sys, "prefix", "/fake/venv"):
            out = self.call(tc_setup.install, 300_000, True)
        self.assertIn("virtualenv", out)

    def test_check_reports_broken_hook_paths(self):
        self.call(tc_setup.install, 300_000, True)
        self.assertRegex(self.call(tc_setup.check), r"hook command: OK")
        s = self.read_settings()
        s["hooks"]["UserPromptSubmit"][-1]["hooks"][0]["command"] = (
            "'/nonexistent/python3' '/nonexistent/context_guard.py' hook 2>/dev/null || true " + tc_setup.TAG)
        self.save_settings(s)
        self.assertRegex(self.call(tc_setup.check), r"hook command: BROKEN")


if __name__ == "__main__":
    unittest.main()
