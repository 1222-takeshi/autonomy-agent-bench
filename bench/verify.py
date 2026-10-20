"""Isolated build/verification controller for a single ROS 2 package case.

Outside the candidate's environment this copies a colcon workspace into a fresh
temporary dir, builds one package inside a locked-down container, runs its gtest
suite plus grader-owned test files, and records a structured JSON result. It
never executes a model and never proves the authenticity of its inputs.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
SKIPPED_PATHS = frozenset([".git", "build", "install", "log"])

# TEST / TEST_F / TEST_P macro, tolerant of surrounding whitespace. The suite is a
# single word component so a grader ``class`` is matched exactly or by its last
# dot-separated component (see ``expected_grader_tests_missing``).
GRADER_TEST_RE = re.compile(r"\bTEST(?:_F|_P)?\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)")

# candidate source extensions that may declare gtest macros.
GRADER_SOURCE_SUFFIXES = frozenset(
    (".cpp", ".cc", ".cxx", ".cppm", ".ipp", ".h", ".hh", ".hpp", ".hxx", ".inl")
)

# colcon stdout/stderr markers that pin a failure to the build phase.
BUILD_FAILURE_MARKERS = (
    "CMake Error",
    "build failed",
    "undefined reference",
    "internal compiler error",
    "ament_cmake error",
)


class InfraError(Exception):
    """Container/tooling failure: docker missing, timeout, bad input, unreadable XML."""


class UsageError(Exception):
    """Bad invocation of the tool itself, for example a missing --tests path."""


class InvalidSubmissionError(Exception):
    """The candidate submission itself is unacceptable, not a tool/environment fault.

    Carries ``phase`` (defaults to ``build``) so the record matches the phase the
    orchestrator should attribute to it. It exits 1 like ``task_fail`` but is a
    distinct status so the build step never starts (Docker is not launched).
    """

    def __init__(self, message, phase="build"):
        super().__init__(message)
        self.phase = phase


def _utcnow():
    return datetime.now(timezone.utc)


def sha256_tree(root):
    """Deterministic sha256 over sorted relative paths and file bytes.

    Skips directories that hold generated or non-input data so the value is
    stable across runs. Symlinks are never followed or hashed (defence in depth:
    an input workspace with symlinks is already rejected before this is called).
    Returns None when the root is missing.
    """
    path = Path(root)
    if not path.is_dir():
        return None
    entries = []
    for entry in sorted(path.rglob("*"), key=str):
        rel = str(entry.relative_to(path))
        if any(part in SKIPPED_PATHS for part in rel.split("/")):
            continue
        if entry.is_symlink():
            continue
        if not entry.is_file():
            continue
        digest = hashlib.sha256()
        digest.update(rel.encode("utf-8"))
        with entry.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        entries.append((rel, digest.hexdigest()))
    accumulator = hashlib.sha256()
    for rel, digest in entries:
        accumulator.update(rel.encode("utf-8"))
        accumulator.update(bytes([0]))
        accumulator.update(digest.encode("utf-8"))
        accumulator.update(bytes([0]))
    return accumulator.hexdigest()


def list_workspace_symlinks(root):
    """Relative paths of every symlink (file or directory) under ``root``.

    Walks without following directory symlinks so only what the container could
    reach is reported. Sorted and stable; up to a handful is enough for the
    host-side controller to see which candidate-created links to inspect.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # A symlinked component anywhere under root is skipped entirely, so the
        # container (and this tool) never traverse it to reach host-side paths.
        if _dirpath_has_symlink(root, dirpath):
            continue
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                found.append(os.path.relpath(full, root))
    return sorted(found)


def _dirpath_has_symlink(root, dirpath):
    """True when any path component of ``dirpath`` below ``root`` is a symlink."""
    rel = os.path.relpath(dirpath, root)
    if rel == os.sep or rel == ".":
        return False
    current = root
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def _read_name(package_xml):
    try:
        tree = ET.parse(str(package_xml))
        name = tree.getroot().find("name")
    except (ET.ParseError, OSError, ValueError):
        return None
    if name is None or not name.text:
        return None
    return name.text.strip()


def find_package_dirs(src_root, package):
    """Return every package dir whose package.xml <name> matches, sorted."""
    src = Path(src_root) / "src"
    found = set()
    for pkg_xml in src.rglob("package.xml"):
        if _read_name(pkg_xml) == package:
            found.add(pkg_xml.parent)
    return sorted(found)


def prepare_workspace(workspace, package, tests):
    """Copy a colcon workspace into a temp dir and drop in grader test files.

    The input directory is never modified. Grader files land in
    src/<package>/test of the copy. Raises InfraError on bad input.
    """
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise InfraError("workspace path does not exist: " + str(workspace))
    target = Path(tempfile.mkdtemp(prefix="bench-ws-")) / "workspace"
    shutil.copytree(workspace, target, symlinks=True)

    # Host-side symlink safety (1a): the container cannot reach the host through
    # the container's own symlinks, but the host controller can follow a link the
    # candidate planted. Detect it on the copy, refuse to build, and stop before
    # Docker is ever launched.
    symlinks = list_workspace_symlinks(target)
    if symlinks:
        raise InvalidSubmissionError(
            "workspace contains symlinks: " + ", ".join(symlinks[:5])
        )

    if tests is not None:
        tests = Path(tests)
        if not tests.is_dir():
            raise UsageError("--tests directory does not exist: " + str(tests))
        candidates = find_package_dirs(target, package)
        if not candidates:
            raise InfraError("no package named " + package + " found under src/")
        if len(candidates) > 1:
            raise InfraError(
                "package name is ambiguous; " + ", ".join(sorted(str(candidate) for candidate in candidates))
            )
        package_dir = candidates[0]
        test_dir = package_dir / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_dir_real = os.path.realpath(str(test_dir))
        for entry in sorted(tests.rglob("*"), key=str):
            if entry.is_dir():
                continue
            if entry.is_symlink():
                raise UsageError("--tests entry is a symlink: " + str(entry))
            rel = entry.relative_to(tests)
            destination = test_dir / rel
            resolved = os.path.realpath(str(destination))
            if resolved != test_dir_real and not resolved.startswith(test_dir_real + os.sep):
                raise UsageError("--tests entry escapes the test dir: " + str(entry))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, destination, follow_symlinks=False)
            if entry.suffix == ".xml":
                try:
                    ET.parse(str(destination))
                except (ET.ParseError, OSError, ValueError):
                    raise InfraError("result XML could not be parsed: " + entry.name)
    return target


def build_command_for(package):
    """One bash -lc string that builds, tests and reports one package."""
    select = shlex.quote(package)
    return (
        "source /opt/ros/humble/setup.bash && "
        "colcon build --packages-select " + select + " --cmake-args -DBUILD_TESTING=ON && "
        "colcon test --packages-select " + select + " --ctest-args -R '^test_' && "
        "colcon test-result --all"
    )


def build_docker_argv(image, workspace, command, memory, cpus):
    """The exact docker argv: locked-down container, workspace bind-mounted r/w.

    The bind spec is passed via ``--mount`` so docker treats the image as the
    first positional. Each run gets a unique ``--name`` so a timed-out run can be
    killed without colliding with a previous container of the same package.
    """
    mount = "type=bind,source=" + str(workspace) + ",target=/ws"
    container_name = "bench-verify-" + uuid.uuid4().hex
    return [
        "docker", "run", "--rm",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp",
        "--name", container_name,
        "--mount", mount,
        "--user", "1000:1000",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "1024",
        "--memory", memory,
        "--cpus", str(cpus),
        image, "bash", "-lc", command,
    ]


def _kill_container(argv):
    """Best-effort ``docker kill`` of a timed-out container (ignored on error).

    The container name comes from ``--name`` in argv so the kill targets exactly
    the container that ``docker run`` was started with.
    """
    try:
        index = argv.index("--name")
    except ValueError:
        return
    name = argv[index + 1] if index + 1 < len(argv) else None
    if not name:
        return
    try:
        subprocess.run(
            ["docker", "kill", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def run_in_docker(argv, timeout):
    """Run the container. Tests replace this function; it never needs Docker.

    A missing docker raises InfraError; a timeout is allowed to propagate so the
    caller can kill the named container and record ``timed_out``.
    """
    try:
        return subprocess.run(argv, timeout=timeout, capture_output=True)
    except FileNotFoundError:
        raise InfraError("docker executable not found on host")


def _classify_testcase(suite, name, testcase):
    for child in testcase.iter():
        if child.tag in ("failure", "error"):
            return dict(suite=suite, name=name, status="fail")
        if child.tag == "skip":
            return dict(suite=suite, name=name, status="skip")
    return dict(suite=suite, name=name, status="pass")


def collect_tests(tests_root):
    """Parse gtest XML files under the copy into suite/name/status records.

    Walks the results tree without following directory symlinks and never opens
    a symlinked file: a link under the results root is skipped outright so the
    container cannot smuggle host-side results in. Malformed result XML is
    raised as an infra error rather than dropped, so a forged or undelivered
    result is reported instead of treated as a clean run.
    """
    root = Path(tests_root)
    results, seen = [], set()
    if not root.is_dir():
        return results
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if _dirpath_has_symlink(root, dirpath):
            # A symlinked component anywhere under the results root means every
            # entry reached through it is untrusted; os.walk would not descend,
            # but guard the current directory anyway.
            continue
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            if not _looks_like_result_xml(name):
                continue
            try:
                parsed = ET.parse(full)
            except (ET.ParseError, OSError, ValueError):
                raise InfraError(
                    "result XML could not be parsed: " + os.path.relpath(full, root)
                )
            root_element = parsed.getroot()
            if root_element.tag not in ("testsuite", "testsuites"):
                continue
            for testcase in root_element.iter("testcase"):
                suite = testcase.get("classname") or ""
                name = testcase.get("name") or ""
                record = _classify_testcase(suite, name, testcase)
                key = (record["suite"], record["name"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(record)
    return results


def _looks_like_result_xml(name):
    """True for gtest result file names, excluding hidden / ctest internals."""
    if name.startswith("."):
        return False
    if name.startswith("CTestTestfile"):
        return False
    return name.endswith(".xml") or name.endswith(".gtest.xml")


def _present_suites(tests):
    """Names seen in gtest output: the full classname and its last dot component."""
    present = set()
    for record in tests:
        suite = record["suite"]
        present.add(suite)
        if suite:
            present.add(suite.split(".")[-1])
    return present


def missing_required_suites(tests, required):
    """Required grader suite names that produced zero test cases (deduped)."""
    present = _present_suites(tests)
    seen, missing = set(), []
    for name in required:
        if name and name not in present and name not in seen:
            seen.add(name)
            missing.append(name)
    return missing


def extract_expected_grader_tests(tests_dir):
    """(suite, name) pairs a grader C++ file declares via TEST/TEST_F/TEST_P.

    Walks the grader directory for candidate source files and regex-extracts the
    declared pairs so a build that runs the candidate's own same-suite ``TEST``
    instead of the grader's cannot satisfy ``--tests``. Symlinks are skipped.
    """
    if tests_dir is None:
        return []
    root = Path(tests_dir)
    if not root.is_dir():
        return []
    pairs, seen = [], set()
    for entry in root.rglob("*"):
        if not entry.is_file() or entry.is_symlink():
            continue
        if entry.suffix not in GRADER_SOURCE_SUFFIXES:
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for suite, name in GRADER_TEST_RE.findall(text):
            key = (suite, name)
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)
    return pairs


def expected_grader_tests_missing(tests, pairs):
    """Declared (suite, name) grader pairs absent from results, as ``suite.name``.

    A pair is satisfied when a result record shares its name and matches the
    suite exactly or by its last dot-separated component (the gtest classname).
    """
    missing = []
    seen = set()
    for suite, name in pairs:
        for record in tests:
            if record["name"] != name:
                continue
            if record["suite"] == suite or record["suite"].split(".")[-1] == suite:
                break
        else:
            key = suite + "." + name
            if key not in seen:
                seen.add(key)
                missing.append(key)
    return missing[:5]


def evaluate(exit_code, tests, output):
    """Map a container run to a status/phase/note decision."""
    failed = [test for test in tests if test["status"] == "fail"]
    passed = [test for test in tests if test["status"] == "pass"]
    ran = len(passed) + len(failed)
    has_marker = any(marker in output for marker in BUILD_FAILURE_MARKERS)

    if not tests:
        if exit_code != 0 and has_marker:
            return dict(status="task_fail", phase="build",
                        note="container exited with a build failure marker and no test output")
        if exit_code != 0:
            return dict(status="infra_error", phase="infra",
                        note="container exited without any build output")
        return dict(status="task_fail", phase="test",
                    note="build finished but no tests ran")

    if failed:
        return dict(status="task_fail", phase="test",
                    note=str(len(failed)) + " test(s) failed")
    if exit_code != 0:
        return dict(status="task_fail", phase="test",
                    note="container exited with test output present")
    if ran >= 1:
        return dict(status="pass", phase="test",
                    note="build ok and all tests passed")
    return dict(status="task_fail", phase="test",
                note="tests ran but none passed")


def run_verification(image, workspace, package, tests, timeout, memory, cpus):
    """Prepare the copy, run the container, and return a structured record.

    On timeout the container is killed via ``--name`` and the partial output is
    reported so the caller can distinguish a finished build from a stalled one.
    """
    copy = prepare_workspace(workspace, package, tests)
    command = build_command_for(package)
    argv = build_docker_argv(image, str(copy), command, memory, cpus)
    timed_out = False
    stdout = ""
    stderr = ""
    exit_code = None
    records = []
    try:
        try:
            proc = run_in_docker(argv, timeout)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            _kill_container(argv)
            stdout = (error.output or b"").decode("utf-8", "replace")
        else:
            exit_code = proc.returncode
            stdout = proc.stdout.decode("utf-8", "replace")
            stderr = proc.stderr.decode("utf-8", "replace")
            records = collect_tests(Path(copy) / "build" / package / "test_results")
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    return dict(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        tests=records,
        argv=argv,
        timed_out=timed_out,
    )


def _base_record(args, start, end):
    record = dict()
    record["schema_version"] = SCHEMA_VERSION
    record["workspace_sha256"] = sha256_tree(args.workspace)
    record["tests_sha256"] = sha256_tree(args.tests) if args.tests is not None else None
    record["start_utc"] = start.isoformat()
    record["end_utc"] = end.isoformat()
    record["duration_seconds"] = round((end - start).total_seconds(), 3)
    return record


def write_result(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="digest-pinned ros:humble-ros-base reference")
    parser.add_argument("--workspace", required=True, type=Path, help="colcon workspace root that contains src/")
    parser.add_argument("--package", required=True, help="package name to build and test")
    parser.add_argument("--tests", type=Path, default=None,
                        help="optional grader test files copied into the package test dir")
    parser.add_argument("--require-suite", action="append", default=[], metavar="NAME",
                        help="required grader suite name (repeatable); the submission is "
                             "invalid if it ran zero cases from any listed suite")
    parser.add_argument("--out", required=True, type=Path, help="output JSON path")
    parser.add_argument("--timeout", type=int, default=1800, help="container timeout in seconds")
    parser.add_argument("--memory", default="8g", help="container memory limit")
    parser.add_argument("--cpus", type=float, default=4, help="container cpu limit")
    return parser


def _write_record(args, start, end, decision, result=None, expected_grader_tests=0):
    """Write a complete JSON record for any outcome (normal, infra, or build refuse).

    An early infra/usage record shares every field with a normal one: ``tests: []``,
    ``counts`` all zero, ``image``/``package``/``required_suites`` populated,
    ``timed_out``, ``docker_argv`` null, and the message in ``note`` (never
    ``detail``). ``result`` supplies the collected tests and container output;
    ``expected_grader_tests`` records how many grader pairs were expected.
    """
    tests, stdout, stderr = [], "", ""
    exit_code, argv, timed_out = None, None, False
    if result:
        tests = result.get("tests", [])
        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        exit_code = result.get("exit_code")
        argv = result.get("argv")
        timed_out = result.get("timed_out", False)
    counts = {
        "total": len(tests),
        "pass": sum(1 for test in tests if test["status"] == "pass"),
        "fail": sum(1 for test in tests if test["status"] == "fail"),
        "skip": sum(1 for test in tests if test["status"] == "skip"),
    }
    record = _base_record(args, start, end)
    record["status"] = decision["status"]
    record["phase"] = decision["phase"]
    record["note"] = decision.get("note", "")
    record["tests"] = tests
    record["counts"] = counts
    record["image"] = str(args.image)
    record["package"] = args.package
    record["required_suites"] = list(args.require_suite)
    record["expected_grader_tests"] = expected_grader_tests
    record["timed_out"] = timed_out
    record["docker_argv"] = argv
    record["exit_code"] = exit_code
    record["stdout_tail"] = stdout[-4000:]
    record["stderr_tail"] = stderr[-4000:]
    write_result(args.out, record)
    return record


def _emit_infra(args, start, error):
    """Write an early infra/usage record and return its exit code (2 or 3)."""
    _write_record(
        args, start, _utcnow(),
        decision=dict(status="infra_error", phase="infra", note=str(error)),
    )
    if isinstance(error, UsageError):
        return 2
    return 3


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    start = _utcnow()

    # Expected grader pairs declared by the candidate's grader C++ sources. These
    # must each appear in the run results; a same-suite replacement TEST that
    # passes cannot satisfy them. Computed up front so every record, including an
    # early infra/usage one, records the count.
    expected_pairs = (
        extract_expected_grader_tests(args.tests) if args.tests is not None else []
    )

    try:
        result = run_verification(
            image=str(args.image),
            workspace=args.workspace,
            package=args.package,
            tests=args.tests,
            timeout=args.timeout,
            memory=args.memory,
            cpus=args.cpus,
        )
    except InvalidSubmissionError as error:
        # The submission itself is unacceptable (e.g. symlinks in the input
        # workspace): record it and stop before Docker is ever launched.
        _write_record(
            args, start, _utcnow(),
            decision=dict(status="invalid_submission",
                          phase=error.phase or "build", note=str(error)),
        )
        return 1
    except (InfraError, UsageError) as error:
        return _emit_infra(args, start, error)

    decision = evaluate(result["exit_code"], result["tests"],
                        result["stdout"] + result["stderr"])
    end = _utcnow()

    # On timeout the container never returned, so decide from its partial output:
    # task_fail/test when the package finished building, else infra_error.
    if result["timed_out"]:
        if "Finished <<< " + args.package in result["stdout"]:
            decision = dict(status="task_fail", phase="test",
                            note="container timed out after the package built")
        else:
            decision = dict(status="infra_error", phase="infra",
                            note="container timed out before the package built")

    # A grader suite with zero cases means the candidate's edits skipped it, so the
    # result cannot be scored. This only applies once the build actually ran tests
    # (it cannot be asserted while the container is still running after a timeout).
    required_suites = list(args.require_suite)
    grader_missing = []
    if not result["timed_out"]:
        build_succeeded = bool(result["tests"]) or ("Finished <<< " + args.package in result["stdout"])
        if build_succeeded:
            grader_missing = expected_grader_tests_missing(result["tests"], expected_pairs)
            missing = missing_required_suites(result["tests"], required_suites)
            # Required-suite gaps are reported first; both are invalid_submission.
            if missing:
                decision = dict(
                    status="invalid_submission",
                    phase="test",
                    note="required grader tests did not run: " + ", ".join(missing),
                )
            elif grader_missing:
                decision = dict(
                    status="invalid_submission",
                    phase="test",
                    note="grader tests did not run: " + ", ".join(grader_missing),
                )

    record = _write_record(
        args, start, end, decision, result, expected_grader_tests=len(expected_pairs),
    )
    status_code = dict()
    status_code["pass"] = 0
    status_code["task_fail"] = 1
    status_code["invalid_submission"] = 1
    status_code["infra_error"] = 3
    return status_code[decision["status"]]


if __name__ == "__main__":
    sys.exit(main())
