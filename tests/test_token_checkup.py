"""Tests on synthetic transcripts and memory folders in a temp CLAUDE_CONFIG_DIR.

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import common  # noqa: E402
import context_guard  # noqa: E402
import measure  # noqa: E402
import memory_check  # noqa: E402
import setup as tc_setup  # noqa: E402


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
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp
        self.pdir = os.path.join(self.tmp, "projects", "-tmp-demo")
        os.makedirs(self.pdir)

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        shutil.rmtree(self.tmp)

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def transcript(self, name, entries):
        path = os.path.join(self.pdir, name + ".jsonl")
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path


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


class MemoryTests(Base):
    def test_index_and_memory_checks(self):
        mem = os.path.join(self.pdir, "memory")
        os.makedirs(mem)
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
        self.assertEqual(rows["b.md"]["dead_paths"], ["~/definitely/not/here.txt"])

    def test_used_memory_is_not_archive_candidate(self):
        mem = os.path.join(self.pdir, "memory")
        os.makedirs(mem)
        self.write(os.path.join(mem, "MEMORY.md"), "- [P](p.md): x\n")
        self.write(os.path.join(mem, "p.md"), "---\nmetadata:\n  type: project\n---\nbody\n")
        self.transcript("u", [{"type": "user", "message": {"content": f"read {mem}/p.md"}}, assistant("m", 1000)])
        rows = {x["file"]: x for x in memory_check.check(self.pdir, common.config())["memories"]}
        self.assertFalse(rows["p.md"]["archive_candidate"])


class SetupTests(Base):
    def test_install_and_uninstall_roundtrip(self):
        other = {"type": "command", "command": "echo other"}
        old_guard = {"type": "command", "command": "python3 /old/context_guard.py hook"}
        with open(os.path.join(self.tmp, "settings.json"), "w") as f:
            json.dump({"hooks": {"UserPromptSubmit": [{"hooks": [other, old_guard]}]}}, f)
        with redirect_stdout(io.StringIO()):
            tc_setup.install(300_000, True)
        s = common.read_json(os.path.join(self.tmp, "settings.json"))
        cmds = [h["command"] for h in s["hooks"]["UserPromptSubmit"][0]["hooks"]]
        self.assertEqual(s["autoCompactWindow"], 300_000)
        self.assertEqual(len([c for c in cmds if "context_guard.py" in c]), 1)
        self.assertTrue(any(tc_setup.TAG in c for c in cmds))
        self.assertIn("echo other", cmds)
        with redirect_stdout(io.StringIO()):
            tc_setup.uninstall()
        s = common.read_json(os.path.join(self.tmp, "settings.json"))
        self.assertNotIn("autoCompactWindow", s)
        self.assertEqual([h["command"] for h in s["hooks"]["UserPromptSubmit"][0]["hooks"]], ["echo other"])


if __name__ == "__main__":
    unittest.main()
