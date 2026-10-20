import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bench.verify as verify

SUCCESS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<testsuite name="trajectory" tests="2" failures="0" skipped="0">\n'
    '  <testcase classname="trajectory" name="test_forward" time="0.01"/>\n'
    '  <testcase classname="trajectory" name="test_endpoint" time="0.01"/>\n'
    "</testsuite>\n"
)

FAIL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<testsuite name="trajectory" tests="3" failures="1" skipped="0">\n'
    '  <testcase classname="trajectory" name="test_a" time="0.01"/>\n'
    '  <testcase classname="trajectory" name="test_b" time="0.01"/>\n'
    '  <testcase classname="trajectory" name="test_c" time="0.01">\n'
    '    <failure message="assert close">got 0.5, expected 0.0</failure>\n'
    '  </testcase>\n'
    "</testsuite>\n"
)

BAD_XML = "<testsuite><testcase"


def _write_package_xml(directory, name):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath("package.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<package format="3">\n'
        "<name>" + name + "</name>\n"
        "<version>1.0.0</version>\n"
        "<description>trajectory</description>\n"
        "<maintainer email='t@t.t'>t</maintainer>\n"
        "<license>Apache-2.0</license>\n"
        "</package>\n"
    )


def _make_workspace(root, names):
    for name in names:
        _write_package_xml(Path(root) / "src" / name, name)
    return Path(root)


def _write_xml(directory, name, text):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath(name).write_text(text)


def _one_xml(suite, name, status="pass"):
    marker = {"pass": "", "fail": "<failure/>", "skip": "<skip/>"}[status]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{suite}" tests="1" failures="0" skipped="0">\n'
        f'  <testcase classname="{suite}" name="{name}" time="0.01"/>{marker}\n'
        "</testsuite>\n"
    )


class FakeContainer:
    """Stand-in for the docker run; records argv and can inject gtest XML."""

    def __init__(self, exit_code=0, stdout="", stderr="", inject=None):
        self.returncode = exit_code
        self.stdout = stdout.encode("utf-8")
        self.stderr = stderr.encode("utf-8")
        self.argv = None
        self.inject = inject

    def run(self, argv, timeout):
        self.argv = argv
        if self.inject is not None:
            self.inject(self)
        return self


def _source_from_argv(argv):
    for token in argv:
        if token.startswith("type=bind,source="):
            return token[len("type=bind,source="):].split(",target=/ws")[0]
    return None


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self._tmps = []

    def _tmp(self):
        directory = tempfile.mkdtemp(prefix="verify-test-")
        self._tmps.append(directory)
        return directory

    def tearDown(self):
        for directory in self._tmps:
            shutil.rmtree(directory, ignore_errors=True)

    # ---- helpers -------------------------------------------------------
    def _run(self, workspace, package, tests=None, stub=None, extra=None):
        stub = stub or FakeContainer(exit_code=0, stdout="colcon build finished ok")
        with mock.patch.object(verify, "run_in_docker", side_effect=stub.run):
            out = Path(self._tmp()) / "result.json"
            argv = [
                "--image", "ros:humble@sha256:" + "a" * 64,
                "--workspace", str(workspace),
                "--package", package,
                "--out", str(out),
            ]
            if tests is not None:
                argv += ["--tests", str(tests)]
            if extra:
                argv += extra
            code = verify.main(argv)
            data = json.loads(out.read_text())
        return code, data, stub

    # ---- normal --------------------------------------------------------
    def test_pass_with_stable_hashes_and_unchanged_inputs(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            grader = Path(td)
            _write_xml(grader, "grader.cpp", "int main() { return 0; }\n")
            _write_xml(grader, "grader_ok.xml", SUCCESS_XML)
            before = verify.sha256_tree(workspace)
            before_tests = verify.sha256_tree(grader)

            def inject(container):
                target = Path(_source_from_argv(container.argv)) / "build" / "trajectory" / "test_results"
                target.mkdir(parents=True, exist_ok=True)
                target.joinpath("trajectory.gtest.xml").write_text(SUCCESS_XML)

            stub = FakeContainer(exit_code=0, stdout="colcon test ok", inject=inject)
            code1, data1, _ = self._run(workspace, "trajectory", tests=td, stub=stub)
            code2, data2, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

            # inputs are not modified by the verification run
            self.assertEqual(verify.sha256_tree(workspace), before)
            self.assertEqual(verify.sha256_tree(grader), before_tests)

        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        self.assertEqual(data1["status"], "pass")
        self.assertEqual(data1["phase"], "test")
        self.assertEqual(data1["counts"], {"total": 2, "pass": 2, "fail": 0, "skip": 0})
        self.assertEqual({(t["name"], t["status"]) for t in data1["tests"]},
                         {("test_forward", "pass"), ("test_endpoint", "pass")})
        self.assertEqual(data1["schema_version"], 1)
        self.assertEqual(data1["image"], "ros:humble@sha256:" + "a" * 64)
        self.assertEqual(data1["package"], "trajectory")
        self.assertEqual(data1["exit_code"], 0)
        self.assertGreater(data1["duration_seconds"], 0)
        self.assertIn("workspace_sha256", data1)
        self.assertIn("tests_sha256", data1)
        self.assertFalse(data1["timed_out"])
        self.assertTrue(data1["docker_argv"][0] == "docker")

        # hashes are stable across two runs on identical input
        self.assertEqual(data1["workspace_sha256"], data2["workspace_sha256"])
        self.assertEqual(data1["tests_sha256"], data2["tests_sha256"])

    def test_zero_tests_is_task_fail(self):
        stub = FakeContainer(exit_code=0, stdout="colcon build ok, nothing to test")
        with tempfile.TemporaryDirectory() as ws:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            code, data, _ = self._run(workspace, "trajectory", stub=stub)

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "task_fail")
        self.assertEqual(data["phase"], "test")
        self.assertEqual(data["tests"], [])
        self.assertEqual(data["counts"]["total"], 0)

    def test_one_failing_test_marks_that_test_fail(self):
        def inject(container):
            target = Path(_source_from_argv(container.argv)) / "build" / "trajectory" / "test_results"
            target.mkdir(parents=True, exist_ok=True)
            target.joinpath("trajectory.gtest.xml").write_text(FAIL_XML)

        stub = FakeContainer(exit_code=1, stdout="1 of 3 tests failed", inject=inject)
        with tempfile.TemporaryDirectory() as ws:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            code, data, _ = self._run(workspace, "trajectory", stub=stub)

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "task_fail")
        self.assertEqual(data["phase"], "test")
        self.assertEqual(data["counts"], {"total": 3, "pass": 2, "fail": 1, "skip": 0})
        failing = [t for t in data["tests"] if t["status"] == "fail"]
        self.assertEqual([t["name"] for t in failing], ["test_c"])

    def test_grader_tests_copied_into_target_package_test_dir(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory", "planner"])
            grader = Path(td)
            _write_xml(grader, "grader.cpp", "int main() { return 0; }\n")
            _write_xml(grader, "grader_ok.xml", SUCCESS_XML)

            target = verify.prepare_workspace(workspace, "trajectory", grader)
            try:
                placed = target.joinpath("src", "trajectory", "test", "grader_ok.xml")
                not_placed = target.joinpath("src", "planner", "test", "grader_ok.xml")
                self.assertTrue(placed.is_file())
                self.assertFalse(not_placed.exists())
            finally:
                shutil.rmtree(target, ignore_errors=True)

    # ---- invalid / edge ------------------------------------------------
    def test_package_not_found_is_infra(self):
        stub = FakeContainer(exit_code=1)
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            _write_xml(Path(td), "grader.cpp", "x\n")
            code, data, _ = self._run(workspace, "ghost", tests=td, stub=stub)

        self.assertEqual(code, 3)
        self.assertEqual(data["status"], "infra_error")
        self.assertEqual(data["phase"], "infra")

    def test_ambiguous_package_is_infra(self):
        stub = FakeContainer(exit_code=1)
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            _write_package_xml(Path(ws) / "src" / "trajectory", "trajectory")
            _write_xml(Path(td), "grader.cpp", "x\n")
            code, data, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

        self.assertEqual(code, 3)
        self.assertEqual(data["status"], "infra_error")

    def test_malformed_test_xml_is_infra(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])

            # (a) the --tests copy guard rejects truly malformed grader XML.
            with self.subTest("tests_guard"):
                _write_xml(Path(td), "bad.xml", BAD_XML)
                stub = FakeContainer(exit_code=1)
                code, data, _ = self._run(workspace, "trajectory", tests=Path(td), stub=stub)
                self.assertEqual(code, 3)
                self.assertEqual(data["status"], "infra_error")
                self.assertIn("could not be parsed", data["note"])

            # (b) the results-collection path also raises on malformed result XML.
            with self.subTest("results_collection"):
                def inject(container):
                    target = Path(_source_from_argv(container.argv)) / "build" / "trajectory" / "test_results"
                    target.mkdir(parents=True, exist_ok=True)
                    target.joinpath("trajectory.gtest.xml").write_text(BAD_XML)
                stub = FakeContainer(exit_code=1, inject=inject)
                code, data, _ = self._run(workspace, "trajectory", stub=stub)
                self.assertEqual(code, 3)
                self.assertEqual(data["status"], "infra_error")
                self.assertIn("could not be parsed", data["note"])

    def test_missing_workspace_is_infra(self):
        stub = FakeContainer(exit_code=1)
        with tempfile.TemporaryDirectory() as td:
            missing = Path(self._tmp()) / "does-not-exist"
            code, data, _ = self._run(missing, "trajectory", tests=td, stub=stub)

        self.assertEqual(code, 3)
        self.assertEqual(data["status"], "infra_error")
        self.assertEqual(data["phase"], "infra")

    def test_nonzero_exit_with_build_marker_is_task_fail_phase_build(self):
        stub = FakeContainer(exit_code=1, stdout="colcon build: build failed", stderr="make: *** error")
        with tempfile.TemporaryDirectory() as ws:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            code, data, _ = self._run(workspace, "trajectory", stub=stub)

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "task_fail")
        self.assertEqual(data["phase"], "build")
        self.assertEqual(data["tests"], [])

    def test_nonzero_exit_without_marker_is_infra(self):
        stub = FakeContainer(exit_code=1, stdout="error pulling image: manifest unknown")
        with tempfile.TemporaryDirectory() as ws:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            code, data, _ = self._run(workspace, "trajectory", stub=stub)

        self.assertEqual(code, 3)
        self.assertEqual(data["status"], "infra_error")
        self.assertEqual(data["phase"], "infra")

    def test_docker_missing_is_infra(self):
        def boom(argv, timeout):
            raise verify.InfraError("docker not found")

        with mock.patch.object(verify, "run_in_docker", side_effect=boom):
            with tempfile.TemporaryDirectory() as ws:
                workspace = _make_workspace(Path(ws), ["trajectory"])
                out = Path(self._tmp()) / "result.json"
                code = verify.main([
                    "--image", "ros:humble@sha256:" + "a" * 64,
                    "--workspace", str(workspace),
                    "--package", "trajectory", "--out", str(out),
                ])
                data = json.loads(out.read_text())

        self.assertEqual(code, 3)
        self.assertEqual(data["status"], "infra_error")

    def test_infra_record_has_full_field_set(self):
        with self.subTest("infra_error"):
            def boom(argv, timeout):
                raise verify.InfraError("docker not found")

            with mock.patch.object(verify, "run_in_docker", side_effect=boom):
                with tempfile.TemporaryDirectory() as ws:
                    workspace = _make_workspace(Path(ws), ["trajectory"])
                    out = Path(self._tmp()) / "result.json"
                    code = verify.main([
                        "--image", "ros:humble@sha256:" + "a" * 64,
                        "--workspace", str(workspace),
                        "--package", "trajectory",
                        "--require-suite", "trajectory",
                        "--out", str(out),
                    ])
                    data = json.loads(out.read_text())

            self.assertEqual(code, 3)
            self.assertEqual(data["status"], "infra_error")
            self.assertEqual(data["phase"], "infra")
            # same fields as a normal record
            self.assertEqual(data["tests"], [])
            self.assertEqual(data["counts"], {"total": 0, "pass": 0, "fail": 0, "skip": 0})
            self.assertEqual(data["image"], "ros:humble@sha256:" + "a" * 64)
            self.assertEqual(data["package"], "trajectory")
            self.assertEqual(data["required_suites"], ["trajectory"])
            self.assertFalse(data["timed_out"])
            self.assertIsNone(data["docker_argv"])
            self.assertNotIn("detail", data)
            self.assertIn("note", data)

        with self.subTest("usage_error"):
            with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
                workspace = _make_workspace(Path(ws), ["trajectory"])
                missing = Path(self._tmp()) / "no-such-tests"
                out = Path(self._tmp()) / "result.json"
                code = verify.main([
                    "--image", "ros:humble@sha256:" + "a" * 64,
                    "--workspace", str(workspace),
                    "--package", "trajectory",
                    "--tests", str(missing),
                    "--out", str(out),
                ])
                data = json.loads(out.read_text())

            self.assertEqual(code, 2)
            self.assertEqual(data["status"], "infra_error")
            self.assertEqual(data["tests"], [])
            self.assertEqual(data["counts"], {"total": 0, "pass": 0, "fail": 0, "skip": 0})
            self.assertIsNone(data["docker_argv"])

    def test_missing_required_args_is_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            verify.main([])
        self.assertEqual(ctx.exception.code, 2)

    # ---- regression ----------------------------------------------------
    def test_docker_argv_is_isolated(self):
        def inject(container):
            target = Path(_source_from_argv(container.argv)) / "build" / "trajectory" / "test_results"
            target.mkdir(parents=True, exist_ok=True)
            target.joinpath("trajectory.gtest.xml").write_text(SUCCESS_XML)

        stub = FakeContainer(exit_code=0, stdout="ok", inject=inject)
        with tempfile.TemporaryDirectory() as ws:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            self._run(workspace, "trajectory", stub=stub)

        argv = stub.argv
        joined = " ".join(argv)
        self.assertEqual(argv[0], "docker")
        self.assertIn("--rm", argv)
        self.assertIn("--network", argv)
        self.assertIn("none", argv)
        idx = argv.index("--network")
        self.assertEqual(argv[idx + 1], "none")
        self.assertIn("--read-only", argv)
        self.assertIn("--cap-drop", argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertIn("--user", argv)
        self.assertEqual(argv[argv.index("--user") + 1], "1000:1000")
        self.assertIn("--security-opt", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertIn("--pids-limit", argv)
        self.assertIn("bash", argv)
        self.assertIn("-lc", argv)
        self.assertIn("colcon build", joined)
        self.assertIn("colcon test", joined)
        for token in argv:
            self.assertNotIn("/var/run/docker.sock", token)
        self.assertNotIn("--privileged", argv)
        self.assertNotIn("host", argv)
        # build command uses quoting for the package selection
        self.assertIn("--packages-select trajectory", joined)

    # ---- mount spec / argv position ------------------------------------
    def test_build_docker_argv_mounts_via_mount_flag_and_image_position(self):
        image = "ros:humble@sha256:" + "a" * 64
        argv = verify.build_docker_argv(image, "/ws", "colcon build", "8g", 4)

        mount_idx = next(
            index for index, token in enumerate(argv)
            if token.startswith("type=bind,source=")
        )
        # the bind spec is passed through --mount, not as a bare image
        self.assertEqual(argv[mount_idx - 1], "--mount")
        # unique container name is present
        name_idx = argv.index("--name")
        self.assertTrue(argv[name_idx + 1].startswith("bench-verify-"))
        self.assertEqual(len(argv[name_idx + 1]), len("bench-verify-") + 32)

        # image is immediately followed by bash -lc command (last three elements)
        image_idx = argv.index(image)
        self.assertEqual(argv[image_idx + 1], "bash")
        self.assertEqual(argv[image_idx + 2], "-lc")
        self.assertEqual(argv[image_idx + 3], "colcon build")
        self.assertEqual(argv[-3:], ["bash", "-lc", "colcon build"])

    # ---- --tests nesting / symlink -------------------------------------
    def test_grader_nested_paths_preserved(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            grader = Path(td)
            nested = grader.joinpath("src")
            nested.mkdir(parents=True)
            nested.joinpath("a.cpp").write_text("int f() { return 0; }\n")

            target = verify.prepare_workspace(workspace, "trajectory", grader)
            try:
                placed = target.joinpath("src", "trajectory", "test", "src", "a.cpp")
                self.assertTrue(placed.is_file())
            finally:
                shutil.rmtree(target, ignore_errors=True)

    def test_symlink_in_tests_is_rejected(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            grader = Path(td)
            grader.joinpath("real.txt").write_text("x\n")
            link = grader.joinpath("evil.xml")
            link.symlink_to(grader.joinpath("real.txt"))

            with self.assertRaises(verify.UsageError):
                verify.prepare_workspace(workspace, "trajectory", grader)

    def test_workspace_file_symlink_is_invalid_submission_without_docker(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = Path(ws)
            _make_workspace(workspace, ["trajectory"])
            real = workspace / "src" / "trajectory" / "notes.txt"
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_text("real\n")
            workspace.joinpath("src", "trajectory", "link.txt").symlink_to(real)
            grader = Path(td)
            grader.joinpath("grader.cpp").write_text("int main() { return 0; }\n")
            stub = FakeContainer(exit_code=0, stdout="colcon build finished ok")
            code, data, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "invalid_submission")
        self.assertEqual(data["phase"], "build")
        self.assertIn("workspace contains symlinks", data["note"])
        self.assertIn("link.txt", data["note"])
        # Docker was never launched for an unacceptable submission.
        self.assertIsNone(stub.argv)

    def test_workspace_directory_symlink_is_invalid_submission_without_docker(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = Path(ws)
            _make_workspace(workspace, ["trajectory"])
            real = workspace / "src" / "external"
            real.mkdir(parents=True, exist_ok=True)
            real.joinpath("data.cpp").write_text("int x() { return 0; }\n")
            workspace.joinpath("src", "linked").symlink_to(real)
            grader = Path(td)
            grader.joinpath("grader.cpp").write_text("int main() { return 0; }\n")
            stub = FakeContainer(exit_code=0, stdout="colcon build finished ok")
            code, data, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "invalid_submission")
        self.assertEqual(data["phase"], "build")
        self.assertIn("workspace contains symlinks", data["note"])
        self.assertIn("linked", data["note"])
        self.assertIsNone(stub.argv)

    def test_symlinked_result_xml_is_ignored(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            grader = Path(td)
            grader.joinpath("grader.cpp").write_text("TEST(TrajectorySuite, test_forward)\n")

            def inject(container):
                copy_root = Path(_source_from_argv(container.argv))
                results = copy_root / "build" / "trajectory" / "test_results"
                results.mkdir(parents=True, exist_ok=True)
                # a real, non-symlinked result
                results.joinpath("real.gtest.xml").write_text(
                    _one_xml("TrajectorySuite", "test_forward"))
                # a valid gtest XML only reachable through a symlink under results
                outside = copy_root / "outside.xml"
                outside.write_text(_one_xml("TrajectorySuite", "other_test"))
                results.joinpath("evil.gtest.xml").symlink_to(outside)

            stub = FakeContainer(exit_code=0, stdout="colcon test ok", inject=inject)
            code, data, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

        # the symlinked file is skipped, so only the real result is counted
        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "pass")
        self.assertEqual({t["name"] for t in data["tests"]}, {"test_forward"})
        self.assertEqual(data["expected_grader_tests"], 1)

    def test_sha256_tree_ignores_symlinks(self):
        with tempfile.TemporaryDirectory() as ws:
            workspace = Path(ws)
            _make_workspace(workspace, ["trajectory"])
            real = workspace / "src" / "trajectory" / "notes.txt"
            real.write_text("extra\n")
            # baseline taken before any symlink exists
            baseline = verify.sha256_tree(workspace)
            # adding a symlink must not change the digest: it is never hashed
            workspace.joinpath("src", "trajectory", "link.txt").symlink_to(real)
            self.assertEqual(verify.sha256_tree(workspace), baseline)

    # ---- --require-suite ------------------------------------------------
    def _inject_results(self, xml_text):
        def inject(container):
            target = Path(_source_from_argv(container.argv)) / "build" / "trajectory" / "test_results"
            target.mkdir(parents=True, exist_ok=True)
            target.joinpath("trajectory.gtest.xml").write_text(xml_text)
        return inject

    def test_required_suite_present_is_unaffected(self):
        stub = FakeContainer(exit_code=0, stdout="colcon test ok",
                             inject=self._inject_results(SUCCESS_XML))
        with tempfile.TemporaryDirectory() as ws:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            code, data, _ = self._run(workspace, "trajectory",
                                      extra=["--require-suite", "trajectory"], stub=stub)

        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["required_suites"], ["trajectory"])
        self.assertFalse(data["timed_out"])

    def test_missing_required_suite_is_invalid_submission(self):
        stub = FakeContainer(exit_code=0, stdout="colcon test ok",
                             inject=self._inject_results(SUCCESS_XML))
        with tempfile.TemporaryDirectory() as ws:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            code, data, _ = self._run(workspace, "trajectory",
                                      extra=["--require-suite", "grader::Absent"], stub=stub)

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "invalid_submission")
        self.assertEqual(data["phase"], "test")
        self.assertIn("grader::Absent", data["note"])
        self.assertEqual(data["required_suites"], ["grader::Absent"])

    def test_missing_suite_precedes_failing_tests(self):
        stub = FakeContainer(exit_code=1, stdout="1 of 3 tests failed",
                             inject=self._inject_results(FAIL_XML))
        with tempfile.TemporaryDirectory() as ws:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            code, data, _ = self._run(workspace, "trajectory",
                                      extra=["--require-suite", "grader::Absent"], stub=stub)

        # even though tests failed, the missing suite forces invalid_submission
        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "invalid_submission")

    # ---- expected grader tests -----------------------------------------
    def _grader_inject(self, suite, name):
        def inject(container):
            target = Path(_source_from_argv(container.argv)) / "build" / "trajectory" / "test_results"
            target.mkdir(parents=True, exist_ok=True)
            target.joinpath("trajectory.gtest.xml").write_text(_one_xml(suite, name))
        return inject

    def test_expected_grader_test_present_is_unaffected(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            grader = Path(td)
            grader.joinpath("grader.cpp").write_text("TEST(TrajectorySuite, test_forward)\n")
            stub = FakeContainer(exit_code=0, stdout="colcon test ok",
                                 inject=self._grader_inject("TrajectorySuite", "test_forward"))
            code, data, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["expected_grader_tests"], 1)

    def test_expected_grader_test_missing_is_invalid_submission(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            grader = Path(td)
            grader.joinpath("grader.cpp").write_text("TEST(TrajectorySuite, test_forward)\n")
            # a same-suite fake test passes but the real declared test never ran
            stub = FakeContainer(exit_code=0, stdout="colcon test ok",
                                 inject=self._grader_inject("TrajectorySuite", "fake_test"))
            code, data, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "invalid_submission")
        self.assertEqual(data["phase"], "test")
        self.assertIn("TrajectorySuite.test_forward", data["note"])
        self.assertEqual(data["expected_grader_tests"], 1)

    def test_expected_grader_test_missing_precedes_failing(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            grader = Path(td)
            grader.joinpath("grader.cpp").write_text("TEST(TrajectorySuite, test_forward)\n")

            def inject(container):
                target = Path(_source_from_argv(container.argv)) / "build" / "trajectory" / "test_results"
                target.mkdir(parents=True, exist_ok=True)
                target.joinpath("trajectory.gtest.xml").write_text(FAIL_XML)
            # FAIL_XML has suite "trajectory" so the declared TrajectorySuite test
            # is absent while a different suite's test fails
            stub = FakeContainer(exit_code=1, stdout="1 of 3 tests failed", inject=inject)
            code, data, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "invalid_submission")

    def test_expected_grader_test_f_and_whitespace_parsed(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as td:
            workspace = _make_workspace(Path(ws), ["trajectory"])
            grader = Path(td)
            grader.joinpath("grader.cpp").write_text(
                "TEST_F(  GraderFTests ,  test_one )\n"
            )
            stub = FakeContainer(exit_code=0, stdout="colcon test ok",
                                 inject=self._grader_inject("GraderFTests", "test_one"))
            code, data, _ = self._run(workspace, "trajectory", tests=td, stub=stub)

        self.assertEqual(code, 0)
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["expected_grader_tests"], 1)

    # ---- timeout --------------------------------------------------------
    def _timeout_stub(self, output):
        def stub(argv, timeout):
            exc = subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            exc.output = output
            raise exc
        return stub

    def test_timeout_after_build_is_task_fail(self):
        captured = {}
        def stub(argv, timeout):
            captured["argv"] = argv
            return self._timeout_stub(b"...\nFinished <<< trajectory\n")(argv, timeout)
        with mock.patch.object(verify, "_kill_container", side_effect=lambda a: captured.setdefault("kill_argv", a)):
            with mock.patch.object(verify, "run_in_docker", side_effect=stub):
                with tempfile.TemporaryDirectory() as ws:
                    workspace = _make_workspace(Path(ws), ["trajectory"])
                    out = Path(self._tmp()) / "result.json"
                    code = verify.main([
                        "--image", "ros:humble@sha256:" + "a" * 64,
                        "--workspace", str(workspace),
                        "--package", "trajectory",
                        "--out", str(out),
                    ])
                    data = json.loads(out.read_text())

        self.assertEqual(code, 1)
        self.assertEqual(data["status"], "task_fail")
        self.assertEqual(data["phase"], "test")
        self.assertTrue(data["timed_out"])
        # kill was invoked with the same argv that carried the --name
        self.assertIn("kill_argv", captured)
        self.assertEqual(captured["kill_argv"], captured["argv"])
        self.assertTrue(
            captured["kill_argv"][captured["argv"].index("--name") + 1].startswith("bench-verify-")
        )

    def test_timeout_before_build_is_infra_error(self):
        captured = {}
        def stub(argv, timeout):
            captured["argv"] = argv
            return self._timeout_stub(b"...\nBuilding trajectory\n")(argv, timeout)
        with mock.patch.object(verify, "_kill_container", side_effect=lambda a: captured.setdefault("kill_argv", a)):
            with mock.patch.object(verify, "run_in_docker", side_effect=stub):
                with tempfile.TemporaryDirectory() as ws:
                    workspace = _make_workspace(Path(ws), ["trajectory"])
                    out = Path(self._tmp()) / "result.json"
                    code = verify.main([
                        "--image", "ros:humble@sha256:" + "a" * 64,
                        "--workspace", str(workspace),
                        "--package", "trajectory",
                        "--out", str(out),
                    ])
                    data = json.loads(out.read_text())

        self.assertEqual(code, 3)
        self.assertEqual(data["status"], "infra_error")
        self.assertEqual(data["phase"], "infra")
        self.assertTrue(data["timed_out"])
        self.assertEqual(captured["kill_argv"], captured["argv"])

    def test_kill_on_timeout_uses_same_name_as_docker_name(self):
        argv = ["docker", "run", "--name", "bench-verify-deadbeef",
                "image", "bash", "-lc", "cmd"]
        with mock.patch.object(verify.subprocess, "run", return_value=mock.MagicMock()) as run:
            verify._kill_container(argv)

        self.assertEqual(run.call_count, 1)
        killed = run.call_args_list[0].args[0]
        self.assertEqual(killed[0], "docker")
        self.assertEqual(killed[1], "kill")
        self.assertEqual(killed[2], "bench-verify-deadbeef")
        self.assertEqual(killed[2], argv[argv.index("--name") + 1])


if __name__ == "__main__":
    unittest.main()
