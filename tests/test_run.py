import contextlib
import copy
import datetime
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import bench.catalog as catalog
import bench.run as run
import bench.verify as verify
import bench.workspace as workspace

REAL_CATALOG = Path(__file__).resolve().parents[1] / "cases" / "catalog.json"
IMAGE_ID = "sha256:09653c30a388dbf9974d783c5e85d1ccc3df8d3cd52ce02f91c49abb2cc12407"
SECRET_VALUE = "s3cr3t-value-xyz"
RUN_KEYS = (
    "schema_version run_id case_id rerun_of reason status status_reason catalog_sha256 bundle_sha256 "
    "grader_tests_sha256 image package limits agent agent_result usage usage_note budget_exceeded "
    "changes violations graded verify verify_note verify_cmd start_utc end_utc duration_seconds python"
).split()

AGENT_SCRIPT = r'''
import json, os, signal, subprocess, sys, time
mode = sys.argv[1]
fix = lambda: open("src/pkg/src/a.cpp", "a").write("// FIXED\n")
usage = lambda n=10: json.dump({"output_tokens": n, "tool_calls": 2}, open(os.environ["BENCH_USAGE_FILE"], "w"))
if mode in ("fixer", "overbudget", "junk"):
    fix()
    open("ANALYSIS.md", "w").write("analysis\n")
    usage(5000 if mode == "overbudget" else 10)
if mode == "junk":
    os.makedirs("build/x"); open("build/x/obj.o", "w").write("junk")
elif mode == "outside":
    open("src/pkg/CMakeLists.txt", "a").write("# evil\n")
elif mode == "symlink":
    os.symlink("a.cpp", "src/pkg/src/link.cpp")
elif mode == "sleep":
    time.sleep(30)
elif mode == "ignoreterm":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
elif mode == "env":
    bad = [k for k in ("BENCH_CASE_ID", "BENCH_RUN_ID", "BENCH_WORKSPACE", "BENCH_TASK_FILE", "BENCH_USAGE_FILE",
                       "BENCH_IMAGE", "BENCH_PACKAGE", "BENCH_WALL_SECONDS", "BENCH_OUTPUT_TOKENS",
                       "BENCH_TOOL_CALLS", "BENCH_EDITABLE_PATHS") if k not in os.environ]
    e = os.environ
    if "RUNNER_TEST_SECRET" in e: bad.append("leak")
    if e.get("BENCH_CASE_ID") != "ALG-001" or e.get("BENCH_PACKAGE") != "pkg": bad.append("values")
    if e.get("BENCH_OUTPUT_TOKENS") != "1000" or e.get("BENCH_TOOL_CALLS") != "10": bad.append("limits")
    if not e.get("BENCH_IMAGE", "").startswith("sha256:"): bad.append("image")
    if os.path.realpath(e.get("BENCH_WORKSPACE", "")) != os.path.realpath(os.getcwd()): bad.append("cwd")
    if not os.path.isfile(e.get("BENCH_TASK_FILE", "")): bad.append("task")
    try:
        if json.loads(e.get("BENCH_EDITABLE_PATHS", "")) != ["src/pkg/src", "src/pkg/test", "ANALYSIS.md"]: bad.append("editable")
    except ValueError: bad.append("editable")
    open(e["OUT_FILE"], "w").write("ok" if not bad else "bad " + ",".join(bad))
elif mode == "hidden":
    found = [r for r, _, fs in os.walk(".") if "test_hidden_grader.cpp" in fs]
    open(os.environ["OUT_FILE"], "w").write("none" if not found else "found")
elif mode == "tamper_bundle":
    open(os.environ["BUNDLE_PATH"], "ab").write(b"x")
elif mode == "tamper_grader":
    open(os.path.join(os.environ["GRADER_DIR"], "test_hidden_grader.cpp"), "a").write("// pwned\n")
elif mode == "tamper_catalog":
    open(os.environ["CATALOG_PATH"], "a").write("\n")
elif mode == "relcwd":
    open(os.environ["OUT_FILE"], "w").write(os.environ.get("BENCH_WORKSPACE", ""))
elif mode == "bgchild":
    child = subprocess.Popen(["sleep", "30"])
    open(os.environ["PID_FILE"], "w").write(str(child.pid))
'''

VERIFY_SCRIPT = r'''
import argparse, json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
p = argparse.ArgumentParser()
for name in ("image", "workspace", "package", "tests", "out", "timeout"):
    p.add_argument("--" + name)
a = p.parse_args()
open(os.path.join(here, "verify_called.json"), "w").write(json.dumps(vars(a)))
mode = open(os.path.join(here, "verify_mode")).read().strip() if os.path.exists(os.path.join(here, "verify_mode")) else ""
if mode == "none":
    sys.exit(0)
if mode == "invalid":
    open(a.out, "w").write("{not json")
    sys.exit(0)
fixed = "FIXED" in open(os.path.join(a.workspace, "src/pkg/src/a.cpp")).read()
status = "infra_error" if mode == "infra" else "pass" if fixed else "task_fail"
json.dump({"status": status, "phase": "test", "counts": {"tests": 1, "failures": 0 if fixed else 1}, "note": "fake"},
          open(a.out, "w"))
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_tar(path, members=None, top="ALG-001"):
    members = members if members is not None else {
        "TASK.md": "fix the bug\n",
        "src/pkg/src/a.cpp": "int a() { return 0; }\n",
        "src/pkg/CMakeLists.txt": "project(pkg)\n",
        "src/pkg/test/test_a.cpp": "// test\n",
    }
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo(top)
        info.type, info.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(info)
        for name, content in members.items():
            if isinstance(content, tarfile.TarInfo):
                tar.addfile(content)
                continue
            data = content.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))


def pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.scripts = self.tmp / "scripts"
        self.scripts.mkdir()
        (self.scripts / "agent.py").write_text(AGENT_SCRIPT)
        (self.scripts / "fake_verify.py").write_text(VERIFY_SCRIPT)
        self.bundle = self.tmp / "ALG-001.tar"
        make_tar(self.bundle)
        self.grader = self.tmp / "grader"
        self.grader.mkdir()
        (self.grader / "test_hidden_grader.cpp").write_text("// hidden\n")
        self.runs = self.tmp / "runs"
        self.catalog = self.tmp / "catalog.json"
        self.write_catalog()

    def write_catalog(self, bundle=None, wall=20, **changes):
        document = json.loads(REAL_CATALOG.read_text())
        case = next(c for c in document["cases"] if c["id"] == "ALG-001")
        case["artifacts"]["bundle_sha256"] = sha(bundle or self.bundle)
        case["editable_paths"] = ["src/pkg/src", "src/pkg/test", "ANALYSIS.md"]
        case["limits"] = {"wall_seconds": wall, "output_tokens": 1000, "tool_calls": 10}
        case.update(changes)
        self.assertEqual(catalog.validate(document), [])
        self.catalog.write_text(json.dumps(document))

    def agent_file(self, mode, env=None, name="fake"):
        path = self.tmp / f"agent-{mode}.json"
        data = {"name": name, "argv": [sys.executable, str(self.scripts / "agent.py"), mode], "info": {"v": 1}}
        if env is not None:
            data["env"] = env
        path.write_text(json.dumps(data))
        return path

    def verify_cmd(self):
        return json.dumps([sys.executable, str(self.scripts / "fake_verify.py")])

    def cli(self, mode="fixer", case="ALG-001", env=None, extra=(), run_id="r1", agent=None, verify_mode=None):
        if verify_mode:
            (self.scripts / "verify_mode").write_text(verify_mode)
        args = ["--catalog", str(self.catalog), "--case", case, "--bundle", str(self.bundle), "--package", "pkg",
                "--grader-tests", str(self.grader), "--agent", str(agent or self.agent_file(mode, env)),
                "--runs-dir", str(self.runs), "--run-id", run_id, "--verify-cmd", self.verify_cmd(),
                "--kill-grace", "1", *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(args)
        self.out, self.err = out.getvalue(), err.getvalue()
        self.run_dir = self.runs / case / run_id
        return code

    def record(self):
        return json.loads((self.run_dir / "run.json").read_text())

    def verify_called(self):
        return (self.scripts / "verify_called.json").exists()


class UnitTests(RunnerCase):
    def test_image_ref(self):
        self.assertEqual(run.image_ref("aab/ros:humble@sha256:" + "ab" * 32), "sha256:" + "ab" * 32)
        for bad in ("name:tag", "name@sha256:abc", "name@sha256:" + "AB" * 32, "name@sha256:" + "ab" * 33, "", "@sha256:" + "a" * 64 + "x"):
            with self.subTest(bad=bad), self.assertRaises(run.UsageError):
                run.image_ref(bad)

    def test_load_agent(self):
        good = self.agent_file("noop", env={"A": "1"})
        agent = run.load_agent(good)
        self.assertEqual((agent["name"], agent["argv"][-1]), ("fake", "noop"))
        bad_docs = {
            "unknown key": {"name": "a", "argv": ["x"], "extra": 1},
            "empty argv": {"name": "a", "argv": []},
            "empty arg": {"name": "a", "argv": [""]},
            "bad name": {"name": "a b", "argv": ["x"]},
            "no name": {"argv": ["x"]},
            "env value": {"name": "a", "argv": ["x"], "env": {"K": 1}},
            "env type": {"name": "a", "argv": ["x"], "env": ["K"]},
            "info type": {"name": "a", "argv": ["x"], "info": "text"},
        }
        for label, doc in bad_docs.items():
            with self.subTest(label):
                path = self.tmp / "bad.json"
                path.write_text(json.dumps(doc))
                with self.assertRaises(run.UsageError):
                    run.load_agent(path)
        (self.tmp / "bad.json").write_text("not json")
        with self.assertRaises(run.UsageError):
            run.load_agent(self.tmp / "bad.json")

    def test_new_run_id(self):
        now = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
        value = run.new_run_id(now)
        self.assertRegex(value, r"^20260102T030405Z-[0-9a-f]{8}$")
        self.assertRegex(value, "^" + run.RUN_ID_RE + "$")
        self.assertNotEqual(value, run.new_run_id(now))
        self.assertRegex(run.new_run_id(), "^" + run.RUN_ID_RE + "$")

    def test_agent_env_layers(self):
        base = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "SPARK_LLM_TOKEN": "t", "LANG": "C"}
        env = run.agent_env({"env": {"HOME": "/agent", "FOO": "1"}}, {"FOO": "bench", "BENCH_A": "2"}, base)
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/agent", "LANG": "C", "FOO": "bench", "BENCH_A": "2"})
        self.assertEqual(run.agent_env({}, {}, base), {"PATH": "/bin", "HOME": "/h", "LANG": "C"})
        with mock.patch.dict(os.environ, {"PATH": "/p", "RUNNER_TEST_SECRET": "x"}):
            env = run.agent_env({}, {})
        self.assertEqual(env["PATH"], "/p")
        self.assertNotIn("RUNNER_TEST_SECRET", env)

    def test_read_usage(self):
        path = self.tmp / "usage.json"
        self.assertEqual(run.read_usage(path), (None, "usage file missing"))
        path.write_text("{oops")
        self.assertEqual(run.read_usage(path), (None, "usage file invalid"))
        path.write_text("[1, 2]")
        self.assertEqual(run.read_usage(path), (None, "usage file invalid"))
        path.write_text('{"output_tokens": 3}')
        self.assertEqual(run.read_usage(path), ({"output_tokens": 3}, None))

    def test_budget_exceeded(self):
        limits = {"output_tokens": 4, "tool_calls": 2}
        self.assertEqual(run.budget_exceeded({"output_tokens": 5, "tool_calls": 3}, limits), ["output_tokens", "tool_calls"])
        self.assertEqual(run.budget_exceeded({"tool_calls": 3, "output_tokens": 1}, limits), ["tool_calls"])
        self.assertEqual(run.budget_exceeded({"output_tokens": 4, "tool_calls": 2}, limits), [])
        self.assertEqual(run.budget_exceeded({"output_tokens": True, "tool_calls": True}, {"output_tokens": 0, "tool_calls": 0}), [])
        self.assertEqual(run.budget_exceeded({"output_tokens": "99"}, limits), [])
        self.assertEqual(run.budget_exceeded({}, limits), [])
        self.assertEqual(run.budget_exceeded(None, limits), [])

    def test_decide_rules_and_precedence(self):
        ok = {"started": True, "timed_out": False}
        verify_pass = {"status": "pass"}
        self.assertEqual(run.decide({"started": False, "timed_out": False}, ["x"], ["tool_calls"], verify_pass), ("infra_error", "agent_start_failed"))
        self.assertEqual(run.decide({"started": True, "timed_out": True}, ["x"], ["tool_calls"], verify_pass), ("invalid_submission", "edit_outside_editable_paths"))
        self.assertEqual(run.decide({"started": True, "timed_out": True}, [], ["tool_calls"], verify_pass), ("task_fail", "wall_budget_exceeded"))
        self.assertEqual(run.decide(ok, [], ["output_tokens", "tool_calls"], verify_pass), ("task_fail", "budget_exceeded:output_tokens,tool_calls"))
        for status in run.STATUSES:
            self.assertEqual(run.decide(ok, [], [], {"status": status}), (status, "graded"))
        for bad in (None, {"status": "weird"}, {}, {"status": 1}, ["pass"]):
            self.assertEqual(run.decide(ok, [], [], bad), ("infra_error", "verify_record_invalid"))

    def _run_agent(self, mode, wall, extra_env=None, grace=1):
        env = dict(os.environ, **(extra_env or {}))
        return run.run_agent([sys.executable, str(self.scripts / "agent.py"), mode], str(self.tmp), env,
                             self.tmp / "o.txt", self.tmp / "e.txt", wall, grace)

    def test_run_agent_normal_exit_captures_output(self):
        script = self.tmp / "talk.py"
        script.write_text("import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)")
        result = run.run_agent([sys.executable, str(script)], str(self.tmp), dict(os.environ), self.tmp / "o.txt", self.tmp / "e.txt", 10, 1)
        self.assertEqual((result["started"], result["exit_code"], result["timed_out"], result["error"]), (True, 7, False, None))
        self.assertEqual((self.tmp / "o.txt").read_text().strip(), "out")
        self.assertEqual((self.tmp / "e.txt").read_text().strip(), "err")

    def test_run_agent_timeout_terminates(self):
        result = self._run_agent("sleep", 1)
        self.assertTrue(result["started"] and result["timed_out"])
        self.assertLess(result["duration_seconds"], 10)

    def test_run_agent_ignoring_sigterm_is_killed(self):
        result = self._run_agent("ignoreterm", 1)
        self.assertTrue(result["timed_out"])
        self.assertLess(result["duration_seconds"], 10)
        self.assertNotEqual(result["exit_code"], 0)

    def test_run_agent_missing_executable(self):
        result = run.run_agent([str(self.tmp / "nope")], str(self.tmp), {}, self.tmp / "o", self.tmp / "e", 5, 1)
        self.assertFalse(result["started"])
        self.assertTrue(result["error"])
        self.assertIsNone(result["exit_code"])
        self.assertFalse(result["timed_out"])

    def test_run_agent_kills_background_child(self):
        pid_file = self.tmp / "child.pid"
        result = self._run_agent("bgchild", 10, {"PID_FILE": str(pid_file)})
        self.assertEqual(result["exit_code"], 0)
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 3
        while not pid_gone(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_gone(pid), "background child survived run_agent")
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


class CliOutcomes(RunnerCase):
    def test_fixer_passes_with_complete_manifest(self):
        with mock.patch.dict(os.environ, {"RUNNER_TEST_SECRET": "leak-me"}):
            code = self.cli("fixer", env={"SECRET_NAME": SECRET_VALUE})
        self.assertEqual(code, 0)
        self.assertEqual(self.out.split(), ["pass", "graded", str(self.run_dir)])
        text = (self.run_dir / "run.json").read_text()
        record = json.loads(text)
        self.assertEqual(set(record), set(RUN_KEYS))
        self.assertEqual((record["schema_version"], record["run_id"], record["case_id"]), (1, "r1", "ALG-001"))
        self.assertEqual((record["status"], record["status_reason"], record["graded"]), ("pass", "graded", True))
        self.assertIsNone(record["rerun_of"])
        self.assertIsNone(record["reason"])
        self.assertEqual(record["agent"]["env_keys"], ["SECRET_NAME"])
        self.assertEqual(record["agent"]["name"], "fake")
        self.assertEqual(record["agent"]["info"], {"v": 1})
        self.assertNotIn(SECRET_VALUE, text)
        self.assertNotIn("leak-me", text)
        self.assertEqual(record["limits"], {"wall_seconds": 20, "output_tokens": 1000, "tool_calls": 10})
        self.assertEqual(record["image"], IMAGE_ID)
        self.assertEqual(record["package"], "pkg")
        self.assertEqual(record["bundle_sha256"], sha(self.bundle))
        self.assertEqual(record["catalog_sha256"], sha(self.catalog))
        self.assertEqual(record["grader_tests_sha256"], verify.sha256_tree(self.grader))
        self.assertEqual(record["changes"], {"added": ["ANALYSIS.md"], "modified": ["src/pkg/src/a.cpp"], "deleted": []})
        self.assertEqual((record["violations"], record["budget_exceeded"]), ([], []))
        self.assertEqual(record["usage"], {"output_tokens": 10, "tool_calls": 2})
        self.assertEqual(record["verify"]["status"], "pass")
        self.assertEqual(record["verify"]["counts"], {"tests": 1, "failures": 0})
        self.assertEqual(record["verify"]["record_sha256"], sha(self.run_dir / "verify.json"))
        self.assertEqual(record["verify_cmd"], json.loads(self.verify_cmd()))
        self.assertTrue(record["agent_result"]["started"] and not record["agent_result"]["timed_out"])
        self.assertRegex(record["start_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d.*Z$")
        self.assertRegex(record["end_utc"], r"Z$")
        self.assertEqual(record["python"], platform.python_version())
        self.assertIn("a/src/pkg/src/a.cpp", (self.run_dir / "changes.patch").read_text())
        self.assertFalse((self.run_dir / "run.json.tmp").exists())
        called = json.loads((self.scripts / "verify_called.json").read_text())
        self.assertEqual((called["image"], called["package"]), (IMAGE_ID, "pkg"))
        self.assertEqual(Path(called["workspace"]).resolve(), (self.run_dir / "grading").resolve())
        self.assertEqual(Path(called["tests"]).resolve(), self.grader.resolve())
        self.assertIn("FIXED", (self.run_dir / "grading/src/pkg/src/a.cpp").read_text())

    def test_noop_is_graded_task_fail(self):
        self.assertEqual(self.cli("noop"), 1)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"], record["graded"]), ("task_fail", "graded", True))
        self.assertEqual(record["changes"], {"added": [], "modified": [], "deleted": []})
        self.assertEqual((self.run_dir / "changes.patch").read_text(), "")
        self.assertIsNone(record["usage"])
        self.assertEqual(record["usage_note"], "usage file missing")

    def test_agent_environment_is_scrubbed_and_bench_vars_set(self):
        out_file = self.tmp / "env.out"
        with mock.patch.dict(os.environ, {"RUNNER_TEST_SECRET": "x"}):
            self.assertEqual(self.cli("env", env={"OUT_FILE": str(out_file)}), 1)
        self.assertEqual(out_file.read_text(), "ok")

    def test_hidden_grader_tests_not_visible_to_agent(self):
        out_file = self.tmp / "hidden.out"
        self.cli("hidden", env={"OUT_FILE": str(out_file)})
        self.assertEqual(out_file.read_text(), "none")

    def test_build_junk_is_ignored_and_not_graded(self):
        self.assertEqual(self.cli("junk"), 0)
        self.assertNotIn("build/x/obj.o", json.dumps(self.record()["changes"]))
        self.assertFalse((self.run_dir / "grading" / "build").exists())

    def test_edit_outside_editable_paths_is_invalid_and_not_graded(self):
        self.assertEqual(self.cli("outside"), 1)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"], record["graded"]), ("invalid_submission", "edit_outside_editable_paths", False))
        self.assertEqual(record["violations"], ["src/pkg/CMakeLists.txt"])
        self.assertFalse((self.run_dir / "verify.json").exists())
        self.assertFalse(self.verify_called())
        self.assertIsNone(record["verify"])
        self.assertIn("src/pkg/CMakeLists.txt", (self.run_dir / "changes.patch").read_text())

    def test_symlink_is_invalid_submission(self):
        self.assertEqual(self.cli("symlink"), 1)
        record = self.record()
        self.assertEqual(record["status"], "invalid_submission")
        self.assertIn("src/pkg/src/link.cpp", record["violations"])
        self.assertFalse(self.verify_called())

    def test_over_budget_is_task_fail_without_grading(self):
        self.assertEqual(self.cli("overbudget"), 1)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"]), ("task_fail", "budget_exceeded:output_tokens"))
        self.assertEqual((record["budget_exceeded"], record["graded"]), (["output_tokens"], False))
        self.assertFalse((self.run_dir / "verify.json").exists())
        self.assertFalse(self.verify_called())

    def test_wall_timeout_is_task_fail(self):
        self.write_catalog(wall=2)
        self.assertEqual(self.cli("sleep"), 1)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"], record["graded"]), ("task_fail", "wall_budget_exceeded", False))
        self.assertTrue(record["agent_result"]["timed_out"])
        self.assertFalse(self.verify_called())

    def test_missing_agent_executable_is_infra_error(self):
        path = self.tmp / "missing.json"
        path.write_text(json.dumps({"name": "m", "argv": [str(self.tmp / "no-such-binary")]}))
        self.assertEqual(self.cli(agent=path), 3)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"], record["graded"]), ("infra_error", "agent_start_failed", False))
        self.assertFalse(record["agent_result"]["started"])
        self.assertTrue(record["agent_result"]["error"])

    def test_invalid_verify_record_is_infra_error(self):
        self.assertEqual(self.cli("fixer", verify_mode="invalid"), 3)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"], record["graded"]), ("infra_error", "verify_record_invalid", True))
        self.assertIsNone(record["verify"])
        self.assertTrue(record["verify_note"])

    def test_missing_verify_record_is_infra_error(self):
        self.assertEqual(self.cli("fixer", verify_mode="none"), 3)
        self.assertEqual(self.record()["status_reason"], "verify_record_invalid")

    def test_verify_infra_error_exit_code(self):
        self.assertEqual(self.cli("fixer", verify_mode="infra"), 3)
        record = self.record()
        self.assertEqual((record["status"], record["verify"]["status"]), ("infra_error", "infra_error"))

    def test_bundle_tamper_is_invalid_submission(self):
        self.assertEqual(self.cli("tamper_bundle", env={"BUNDLE_PATH": str(self.bundle)}), 1)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"], record["graded"]), ("invalid_submission", "bundle_changed", False))
        self.assertEqual((self.run_dir / "changes.patch").read_text(), "")
        self.assertFalse((self.run_dir / "grading").exists())
        self.assertFalse(self.verify_called())

    def test_grader_tests_tamper_is_invalid_submission(self):
        self.assertEqual(self.cli("tamper_grader", env={"GRADER_DIR": str(self.grader)}), 1)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"], record["graded"]), ("invalid_submission", "grader_tests_changed", False))
        self.assertFalse(self.verify_called())
        self.assertFalse((self.run_dir / "verify.json").exists())

    def test_catalog_tamper_is_invalid_submission(self):
        code = self.cli("tamper_catalog", env={"CATALOG_PATH": str(self.catalog)})
        self.assertEqual(code, 1)
        record = self.record()
        self.assertEqual((record["status"], record["status_reason"]), ("invalid_submission", "catalog_changed"))
        self.assertFalse(record["graded"])

    def test_relative_runs_dir_gives_absolute_bench_paths(self):
        out_file = self.tmp / "ws.out"
        old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old_cwd)
        args = ["--catalog", str(self.catalog), "--case", "ALG-001", "--bundle", str(self.bundle), "--package", "pkg",
                "--grader-tests", str(self.grader), "--agent", str(self.agent_file("relcwd", {"OUT_FILE": str(out_file)})),
                "--runs-dir", "runs", "--run-id", "rel", "--verify-cmd", self.verify_cmd(), "--kill-grace", "1"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            run.main(args)
        reported = out_file.read_text()
        self.assertTrue(os.path.isabs(reported), reported)
        self.assertEqual(os.path.realpath(reported), os.path.realpath(self.tmp / "runs" / "ALG-001" / "rel" / "workspace"))

    def test_execute_helpers_are_short(self):
        import ast
        source = Path(run.__file__).read_text(encoding="utf-8")
        long = [node.name for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.FunctionDef) and node.end_lineno - node.lineno + 1 > 70]
        self.assertEqual(long, [])

    def test_rerun_metadata_is_recorded(self):
        self.assertEqual(self.cli("fixer", extra=["--rerun-of", "r0", "--reason", "flaky host"], run_id="r2"), 0)
        record = self.record()
        self.assertEqual((record["rerun_of"], record["reason"]), ("r0", "flaky host"))

    def test_default_run_id_is_generated(self):
        args = ["--catalog", str(self.catalog), "--case", "ALG-001", "--bundle", str(self.bundle), "--package", "pkg",
                "--grader-tests", str(self.grader), "--agent", str(self.agent_file("noop")), "--runs-dir", str(self.runs),
                "--verify-cmd", self.verify_cmd()]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.main(args), 1)
        (run_dir,) = list((self.runs / "ALG-001").iterdir())
        self.assertRegex(run_dir.name, "^" + run.RUN_ID_RE + "$")


class CliPreconditions(RunnerCase):
    def assert_usage_error(self, code, check_dir=True):
        self.assertEqual(code, 2)
        self.assertTrue(self.err.strip())
        if check_dir:
            self.assertFalse(self.run_dir.exists())
        self.assertFalse(self.verify_called())

    def test_wrong_bundle_sha(self):
        self.write_catalog()
        document = json.loads(self.catalog.read_text())
        document["cases"][[c["id"] for c in document["cases"]].index("ALG-001")]["artifacts"]["bundle_sha256"] = "0" * 64
        self.catalog.write_text(json.dumps(document))
        self.assert_usage_error(self.cli("fixer"))

    def test_draft_and_unknown_cases(self):
        self.assert_usage_error(self.cli("fixer", case="INT-001"))
        self.assert_usage_error(self.cli("fixer", case="ALG-999"))

    def test_existing_run_id(self):
        existing = self.runs / "ALG-001" / "r1"
        existing.mkdir(parents=True)
        (existing / "marker").write_text("keep")
        self.assert_usage_error(self.cli("fixer"), check_dir=False)
        self.assertEqual([p.name for p in existing.iterdir()], ["marker"])

    def test_unsafe_bundle_with_matching_sha(self):
        link = tarfile.TarInfo("ALG-001/src/pkg/src/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        make_tar(self.bundle, {"TASK.md": "t\n", "src/pkg/src/a.cpp": "x\n", "evil": link})
        self.write_catalog()
        self.assert_usage_error(self.cli("fixer"))

    def test_bundle_without_task_md(self):
        make_tar(self.bundle, {"src/pkg/src/a.cpp": "x\n"})
        self.write_catalog()
        self.assert_usage_error(self.cli("fixer"))

    def test_rerun_flags_must_pair(self):
        self.assert_usage_error(self.cli("fixer", extra=["--rerun-of", "r0"]))
        self.assert_usage_error(self.cli("fixer", extra=["--reason", "why"]))

    def test_rerun_of_must_be_a_valid_run_id(self):
        self.assert_usage_error(self.cli("fixer", extra=("--rerun-of", "../x", "--reason", "retry")))

    def test_bad_verify_cmd(self):
        for bad in ("not json", "[]", '"python"', '[1, 2]', '{"a": 1}'):
            with self.subTest(bad=bad):
                self.assert_usage_error(self._cli_bad_verify(bad))

    def _cli_bad_verify(self, bad):
        args = ["--catalog", str(self.catalog), "--case", "ALG-001", "--bundle", str(self.bundle), "--package", "pkg",
                "--grader-tests", str(self.grader), "--agent", str(self.agent_file("noop")), "--runs-dir", str(self.runs),
                "--run-id", "r1", "--verify-cmd", bad]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = run.main(args)
        self.err, self.run_dir = err.getvalue(), self.runs / "ALG-001" / "r1"
        return code

    def test_bad_run_id_agent_and_grader_dir(self):
        self.assert_usage_error(self.cli("fixer", run_id="bad/id"), check_dir=False)
        self.assertFalse(self.runs.exists() and any(self.runs.rglob("run.json")))
        bad_agent = self.tmp / "bad-agent.json"
        bad_agent.write_text('{"name": "x"}')
        self.assert_usage_error(self.cli(agent=bad_agent))
        shutil.rmtree(self.grader)
        self.assert_usage_error(self.cli("fixer"))


if __name__ == "__main__":
    unittest.main()
