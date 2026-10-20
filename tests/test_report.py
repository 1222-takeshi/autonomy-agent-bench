"""Tests for bench.report: aggregating run.json manifests into a table."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import bench.report as report

SECRET_ARGV = "sk-live-abc123def456"  # public-check: allow token
SECRET_ENV = "s3cr3t-env-value-xyz"  # public-check: allow token
HOST_PATH = "/home/someuser/private/workdir"  # public-check: allow home-path
NOTES = "infra_error / incomplete runs are listed but not scored (docs/protocol.md)."
HEADER = (
    "| case | agent | model | runs | pass | task_fail | invalid | infra | incomplete "
    "| pass rate | median min | median out tokens | median in tokens | median tools |\n"
    "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
)


def usage(out, inp, tools):
    return {"output_tokens": out, "input_tokens": inp, "tool_calls": tools}


def make_record(case, run_id, agent, status, seconds, usage_value=None, model=None):
    """Build a realistic run.json document (schema_version 1)."""
    info = {"model": model} if model is not None else {}
    return {
        "schema_version": 1, "run_id": run_id, "case_id": case, "rerun_of": None,
        "reason": None, "status": status, "status_reason": "graded",
        "catalog_sha256": "c" * 64, "bundle_sha256": "b" * 64,
        "grader_tests_sha256": "g" * 64, "image": "sha256:" + "1" * 64,
        "package": "pkg",
        "limits": {"wall_seconds": 1800, "output_tokens": 50000, "tool_calls": 200},
        "agent": {"name": agent, "argv": ["agent-cli", "--key", SECRET_ARGV, HOST_PATH],
                  "info": info, "env_keys": ["API_TOKEN"], "env": {"API_TOKEN": SECRET_ENV}},
        "agent_result": {"exit_code": 0}, "usage": usage_value, "usage_note": None,
        "budget_exceeded": None, "changes": [], "violations": [], "graded": True,
        "verify": None, "verify_note": None, "verify_cmd": None,
        "start_utc": "2026-01-01T00:00:00Z", "end_utc": "2026-01-01T00:10:00Z",
        "duration_seconds": seconds, "python": "3.10.12",
    }


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs = Path(tmp.name) / "runs"
        self.runs.mkdir()

    def put(self, case, run_id, record=None, raw=None):
        run_dir = self.runs / case / run_id
        run_dir.mkdir(parents=True)
        if record is not None:
            raw = json.dumps(record)
        if raw is not None:
            (run_dir / "run.json").write_text(raw, encoding="utf-8")
        return run_dir

    def build_fixture(self):
        # Created out of order on purpose; load_runs must sort.
        self.put("B", "r2", make_record("B", "r2", "beta|x", "invalid_submission", 125,
                                        {"output_tokens": True, "input_tokens": "5",
                                         "tool_calls": 4}))
        self.put("B", "r1", make_record("B", "r1", "beta|x", "pass", 100, None))
        self.put("B", "r3", make_record("B", "r3", "gamma", "infra_error", 5, usage(1, 1, 1),
                                        model="m3"))
        self.put("A", "r5")  # no run.json
        self.put("A", "r4", make_record("A", "r4", "alpha", "infra_error", 9999,
                                        usage(99999, 99999, 999), model="m1"))
        self.put("A", "r3", make_record("A", "r3", "alpha", "pass", 660, usage(2000, 6000, 30),
                                        model="m1"))
        self.put("A", "r2", make_record("A", "r2", "alpha", "task_fail", 780,
                                        usage(3000, 7000, 20), model="m-other"))
        self.put("A", "r1", make_record("A", "r1", "alpha", "pass", 600, usage(1000, 5000, 10),
                                        model="m1"))
        self.put("C", "r1", raw="{not json")
        schema2 = make_record("C", "r2", "delta", "pass", 60, usage(1, 1, 1))
        schema2["schema_version"] = 2
        self.put("C", "r2", schema2)
        # Decoys that are not runs: depth 1 file, depth 2 file, depth 3 run.json.
        (self.runs / "stray.json").write_text("{}", encoding="utf-8")
        (self.runs / "A" / "notes.txt").write_text("hi", encoding="utf-8")
        nested = self.runs / "A" / "r1" / "nested"
        nested.mkdir()
        (nested / "run.json").write_text(json.dumps(make_record("A", "x", "zzz", "pass", 1)))

    def rows(self):
        return report.summarize(report.load_runs(self.runs))

    def row(self, case, agent):
        found = [r for r in self.rows() if r["case_id"] == case and r["agent"] == agent]
        self.assertEqual(len(found), 1)
        return found[0]

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = report.main(list(argv))
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()


class MedianTests(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(report.median([]))

    def test_odd_count_unsorted(self):
        self.assertEqual(report.median([3, 1, 2]), 2)
        self.assertEqual(report.median([5]), 5)

    def test_even_count_is_mean_of_middle_two(self):
        self.assertEqual(report.median([4, 1, 3, 2]), 2.5)
        self.assertEqual(report.median([1.0, 2.0]), 1.5)

    def test_statuses_constant(self):
        self.assertEqual(report.STATUSES,
                         ("pass", "task_fail", "invalid_submission", "infra_error"))


class LoadRunsTests(ReportTestCase):
    def test_discovers_depth_two_only_and_sorts(self):
        self.build_fixture()
        runs = report.load_runs(self.runs)
        self.assertEqual([(r["case_id"], r["run_id"]) for r in runs],
                         [("A", "r1"), ("A", "r2"), ("A", "r3"), ("A", "r4"), ("A", "r5"),
                          ("B", "r1"), ("B", "r2"), ("B", "r3"), ("C", "r1"), ("C", "r2")])

    def test_complete_run_fields(self):
        self.build_fixture()
        run = report.load_runs(self.runs)[0]
        self.assertTrue(run["complete"])
        self.assertEqual((run["agent"], run["status"]), ("alpha", "pass"))
        self.assertEqual(run["record"]["duration_seconds"], 600)

    def test_missing_run_json_is_incomplete(self):
        self.build_fixture()
        run = [r for r in report.load_runs(self.runs) if r["run_id"] == "r5"][0]
        self.assertFalse(run["complete"])
        self.assertEqual((run["agent"], run["status"], run["record"]), ("?", None, None))

    def test_unparseable_and_schema_v2_are_incomplete_with_note(self):
        self.build_fixture()
        runs = {(r["case_id"], r["run_id"]): r for r in report.load_runs(self.runs)}
        for key in (("C", "r1"), ("C", "r2")):
            self.assertFalse(runs[key]["complete"])
            self.assertEqual((runs[key]["agent"], runs[key]["status"]), ("?", None))
            self.assertTrue(runs[key]["note"])

    def test_empty_runs_dir(self):
        self.assertEqual(report.load_runs(self.runs), [])


class SummarizeTests(ReportTestCase):
    def test_group_order(self):
        self.build_fixture()
        self.assertEqual([(r["case_id"], r["agent"]) for r in self.rows()],
                         [("A", "?"), ("A", "alpha"), ("B", "beta|x"), ("B", "gamma"),
                          ("C", "?")])

    def test_counts_and_rate(self):
        self.build_fixture()
        row = self.row("A", "alpha")
        self.assertEqual((row["runs"], row["pass"], row["task_fail"],
                          row["invalid_submission"], row["infra_error"], row["incomplete"]),
                         (4, 2, 1, 0, 1, 0))
        self.assertEqual(row["scored"], 3)
        self.assertAlmostEqual(row["pass_rate"], 2 / 3)

    def test_medians_use_scored_runs_only(self):
        self.build_fixture()
        row = self.row("A", "alpha")
        self.assertEqual(row["median_minutes"], 11.0)  # 600/660/780 s; infra run excluded
        self.assertEqual(row["median_output_tokens"], 2000)
        self.assertEqual(row["median_input_tokens"], 6000)
        self.assertEqual(row["median_tool_calls"], 20)

    def test_model_comes_from_first_complete_run(self):
        self.build_fixture()
        self.assertEqual(self.row("A", "alpha")["model"], "m1")
        self.assertIsNone(self.row("B", "beta|x")["model"])
        self.assertEqual(self.row("B", "gamma")["model"], "m3")
        self.assertIsNone(self.row("A", "?")["model"])

    def test_null_usage_and_bad_fields_are_ignored_and_minutes_rounded(self):
        self.build_fixture()
        row = self.row("B", "beta|x")
        self.assertEqual((row["pass"], row["invalid_submission"], row["scored"]), (1, 1, 2))
        self.assertEqual(row["pass_rate"], 0.5)
        self.assertEqual(row["median_minutes"], 1.9)  # 112.5 s / 60 = 1.875
        self.assertIsNone(row["median_output_tokens"])  # null usage, bool value
        self.assertIsNone(row["median_input_tokens"])  # null usage, str value
        self.assertEqual(row["median_tool_calls"], 4)

    def test_nothing_scored(self):
        self.build_fixture()
        row = self.row("B", "gamma")
        self.assertEqual((row["runs"], row["infra_error"], row["scored"]), (1, 1, 0))
        for key in ("pass_rate", "median_minutes", "median_output_tokens",
                    "median_input_tokens", "median_tool_calls"):
            self.assertIsNone(row[key], key)

    def test_incomplete_counted_per_group(self):
        self.build_fixture()
        row = self.row("A", "?")
        self.assertEqual((row["runs"], row["incomplete"], row["scored"]), (1, 1, 0))
        row = self.row("C", "?")
        self.assertEqual((row["runs"], row["incomplete"], row["pass"]), (2, 2, 0))

    def test_summarize_empty(self):
        self.assertEqual(report.summarize([]), [])


class MalformedFieldTests(ReportTestCase):
    """Real run.json files can lack usage keys or hold odd types; summarize must not crash."""

    def test_missing_usage_keys_are_ignored(self):
        self.put("A", "r1", make_record("A", "r1", "alpha", "pass", 60, {"output_tokens": 10, "tool_calls": 1}))
        self.put("A", "r2", make_record("A", "r2", "alpha", "pass", 120, usage(30, 300, 3)))
        row = self.row("A", "alpha")
        self.assertEqual((row["median_output_tokens"], row["median_input_tokens"], row["median_tool_calls"]),
                         (20, 300, 2))

    def test_non_dict_usage_agent_and_info_do_not_crash(self):
        bad = make_record("A", "r1", "alpha", "pass", 60, ["not", "a", "dict"])
        bad["agent"]["info"] = "oops"
        self.put("A", "r1", bad)
        worse = make_record("A", "r2", "alpha", "task_fail", 90, usage(1, 2, 3))
        worse["duration_seconds"] = "slow"
        self.put("A", "r2", worse)
        row = self.row("A", "alpha")
        self.assertEqual((row["pass"], row["task_fail"], row["model"]), (1, 1, None))
        self.assertEqual(row["median_minutes"], 1.0)
        self.assertEqual(row["median_output_tokens"], 1)

    def test_record_without_agent_name_is_incomplete(self):
        rec = make_record("A", "r1", "alpha", "pass", 60)
        del rec["agent"]["name"]
        self.put("A", "r1", rec)
        (run,) = report.load_runs(self.runs)
        self.assertFalse(run["complete"])
        self.assertEqual(run["agent"], "?")
        self.assertTrue(run["note"])

    def test_unknown_status_is_incomplete(self):
        self.put("A", "r1", make_record("A", "r1", "alpha", "maybe", 60))
        (run,) = report.load_runs(self.runs)
        self.assertFalse(run["complete"])
        self.assertTrue(run["note"])


class SymlinkTests(ReportTestCase):
    def test_symlinked_case_and_run_dirs_are_not_followed(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "R" / "x1"
        target.mkdir(parents=True)
        (target / "run.json").write_text(json.dumps(make_record("R", "x1", "outsider", "pass", 1)))
        self.put("A", "r1", make_record("A", "r1", "alpha", "pass", 60))
        os.symlink(Path(outside.name) / "R", self.runs / "LINKED")
        os.symlink(target, self.runs / "A" / "r2")
        runs = report.load_runs(self.runs)
        self.assertEqual([(r["case_id"], r["run_id"]) for r in runs], [("A", "r1")])


def table_row(**overrides):
    row = {"case_id": "A", "agent": "alpha", "model": "m1", "runs": 4, "pass": 2,
           "task_fail": 1, "invalid_submission": 0, "infra_error": 1, "incomplete": 0,
           "scored": 3, "pass_rate": 2 / 3, "median_minutes": 11.5,
           "median_output_tokens": 12000, "median_input_tokens": 6000,
           "median_tool_calls": 20}
    row.update(overrides)
    return row


class MarkdownTests(unittest.TestCase):
    def test_exact_text(self):
        rows = [
            table_row(),
            table_row(case_id="B", agent="beta|x", model=None, runs=1, **{"pass": 0},
                      task_fail=0, infra_error=1, scored=0, pass_rate=None,
                      median_minutes=None, median_output_tokens=None,
                      median_input_tokens=None, median_tool_calls=None),
        ]
        expected = (
            HEADER
            + "| A | alpha | m1 | 4 | 2 | 1 | 0 | 1 | 0 | 2/3 (67%) | 11.5 | 12000 | 6000 | 20 |\n"
            + "| B | beta\\|x | - | 1 | 0 | 0 | 0 | 1 | 0 | - | - | - | - | - |\n"
            + "\n" + NOTES + "\n"
        )
        self.assertEqual(report.to_markdown(rows), expected)

    def test_header_and_alignment_row(self):
        text = report.to_markdown([table_row()])
        self.assertTrue(text.startswith(HEADER))

    def test_trailing_newline_and_notes(self):
        text = report.to_markdown([table_row()])
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))
        self.assertTrue(text.endswith("\n\n" + NOTES + "\n"))

    def test_pipe_in_case_and_model_is_escaped(self):
        text = report.to_markdown([table_row(case_id="a|b", model="m|1")])
        self.assertIn("| a\\|b | alpha | m\\|1 |", text)

    def test_percentage_rounding(self):
        text = report.to_markdown([table_row(**{"pass": 1}, scored=3, pass_rate=1 / 3)])
        self.assertIn("| 1/3 (33%) |", text)


class MainTests(ReportTestCase):
    def test_markdown_to_stdout_by_default(self):
        self.build_fixture()
        code, out, _ = self.run_main("--runs-dir", str(self.runs))
        self.assertEqual(code, 0)
        self.assertEqual(out, report.to_markdown(self.rows()))
        self.assertIn("| beta\\|x |", out)
        self.assertIn("| 2/3 (67%) |", out)

    def test_json_round_trips(self):
        self.build_fixture()
        code, out, _ = self.run_main("--runs-dir", str(self.runs), "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.rows())

    def test_out_file(self):
        self.build_fixture()
        target = self.runs.parent / "report.md"
        code, out, _ = self.run_main("--runs-dir", str(self.runs), "--out", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(target.read_text(encoding="utf-8"), report.to_markdown(self.rows()))

    def test_case_and_agent_filters(self):
        self.build_fixture()
        _, out, _ = self.run_main("--runs-dir", str(self.runs), "--format", "json",
                                  "--case", "A")
        self.assertEqual({r["case_id"] for r in json.loads(out)}, {"A"})
        _, out, _ = self.run_main("--runs-dir", str(self.runs), "--format", "json",
                                  "--agent", "alpha", "--agent", "gamma")
        self.assertEqual([(r["case_id"], r["agent"]) for r in json.loads(out)],
                         [("A", "alpha"), ("B", "gamma")])
        _, out, _ = self.run_main("--runs-dir", str(self.runs), "--format", "json",
                                  "--case", "A", "--case", "C", "--agent", "?")
        self.assertEqual([(r["case_id"], r["agent"]) for r in json.loads(out)],
                         [("A", "?"), ("C", "?")])

    def test_no_runs(self):
        code, out, _ = self.run_main("--runs-dir", str(self.runs))
        self.assertEqual(code, 0)
        self.assertIn("no runs", out)
        self.build_fixture()
        code, out, _ = self.run_main("--runs-dir", str(self.runs), "--case", "NOPE")
        self.assertEqual(code, 0)
        self.assertIn("no runs", out)
        self.assertNotIn("| case |", out)

    def test_missing_runs_dir_exits_2(self):
        code, out, _ = self.run_main("--runs-dir", str(self.runs / "does-not-exist"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")

    def test_secrets_and_host_paths_never_reach_output(self):
        self.build_fixture()
        for fmt in ("md", "json"):
            code, out, _ = self.run_main("--runs-dir", str(self.runs), "--format", fmt)
            self.assertEqual(code, 0)
            for leaked in (SECRET_ARGV, SECRET_ENV, HOST_PATH, str(self.runs), "agent-cli"):
                self.assertNotIn(leaked, out, (fmt, leaked))


if __name__ == "__main__":
    unittest.main()
