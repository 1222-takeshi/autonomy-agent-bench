"""Host-side runner: one agent attempt on one ready case, then grade it.

Runs the agent as a child process inside a fresh, hash-pinned workspace,
enforces the editable paths and the usage budget, rebuilds a clean grading
workspace from the accepted changes, and invokes the existing grader
``bench.verify``. It never executes a model and never proves the
authenticity of the agent itself; the grader owns that job.
"""
import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bench.catalog as catalog
import bench.verify as verify
import bench.workspace as workspace

SCHEMA_VERSION = 1
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME")
RUN_ID_RE = r"[A-Za-z0-9][A-Za-z0-9._-]*"
STATUSES = ("pass", "task_fail", "invalid_submission", "infra_error")


class UsageError(Exception):
    """Bad invocation of the tool itself, for example a missing path or a bad image."""


def load_case(catalog_path, case_id):
    """Load and validate ``case_id`` from ``catalog_path``, requiring status ``ready``.

    Any read/parse failure or validation error, a missing case, or a case whose
    status is not ``ready`` raises ``UsageError``. Returns the case dict.
    """
    try:
        document = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UsageError("could not read catalog: " + str(catalog_path))
    errors = catalog.validate(document)
    if errors:
        raise UsageError("catalog invalid: " + "; ".join(errors))
    for case in document.get("cases", []):
        if not isinstance(case, dict) or case.get("id") != case_id:
            continue
        if case.get("status") != "ready":
            raise UsageError("case is not ready: " + str(case_id))
        return case
    raise UsageError("case not found: " + str(case_id))


def image_ref(runtime_image):
    """Return ``sha256:<hex>`` for a ``<name>@sha256:<64 hex>`` reference.

    Any form that is not a non-empty name pinned to 64 lowercase hex digits is a
    ``UsageError``.
    """
    match = re.fullmatch(r"(?P<name>.+)@sha256:(?P<hex>[0-9a-f]{64})", runtime_image or "")
    if not match:
        raise UsageError("invalid image reference: " + str(runtime_image))
    return "sha256:" + match.group("hex")


def load_agent(path):
    """Validate an agent JSON file and return it.

    Requires ``name`` (matching the name pattern) and a non-empty ``argv`` of
    non-empty strings; ``env`` and ``info`` are optional objects. Unknown keys
    or wrong types raise ``UsageError``.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UsageError("could not read agent file: " + str(path))
    if not isinstance(document, dict):
        raise UsageError("agent file must be a JSON object")
    unknown = set(document) - {"name", "argv", "env", "info"}
    if unknown:
        raise UsageError("agent file has unknown keys: " + ", ".join(sorted(unknown)))
    name = document.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise UsageError("agent name is invalid: " + str(name))
    argv = document.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise UsageError("agent argv is invalid")
    env = document.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
    ):
        raise UsageError("agent env is invalid")
    info = document.get("info", {})
    if not isinstance(info, dict):
        raise UsageError("agent info is invalid")
    return document


def new_run_id(now=None):
    """Return a ``YYYYmmddTHHMMSSZ-<8 hex>`` run id for the given aware UTC time."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return stamp + "-" + uuid.uuid4().hex[:8]


def agent_env(agent, bench_vars, base=None):
    """Build the agent environment from allowlisted base keys, the agent, and bench vars.

    Keys come from ``ENV_ALLOWLIST`` present in ``base`` (default ``os.environ``),
    then ``agent["env"]``, then ``bench_vars`` (later layers win). Nothing outside
    the allowlist leaks from the base.
    """
    base = base if base is not None else os.environ
    env = {key: base[key] for key in ENV_ALLOWLIST if key in base}
    env.update(agent.get("env", {}))
    env.update(bench_vars)
    return env


def read_usage(path):
    """Return ``(usage_object, None)`` or ``(None, note)`` for a usage file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return (None, "usage file missing")
    try:
        obj = json.loads(text)
    except ValueError:
        return (None, "usage file invalid")
    if not isinstance(obj, dict):
        return (None, "usage file invalid")
    return (obj, None)


def budget_exceeded(usage, limits):
    """Return the budget keys exceeded, in order; a bool is not treated as a number."""
    if not usage:
        return []
    exceeded = []
    for key in ("output_tokens", "tool_calls"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > limits.get(key, 0):
            exceeded.append(key)
    return exceeded


def decide(agent_result, violations, exceeded, verify_record):
    """Pick the first matching outcome: ``(status, reason)``.

    Precedence: an agent that never started, edits outside the editable paths,
    a wall-clock timeout, a budget breach, a valid verify record, otherwise an
    infra error for the bad record.
    """
    if not agent_result.get("started"):
        return ("infra_error", "agent_start_failed")
    if violations:
        return ("invalid_submission", "edit_outside_editable_paths")
    if agent_result.get("timed_out"):
        return ("task_fail", "wall_budget_exceeded")
    if exceeded:
        return ("task_fail", "budget_exceeded:" + ",".join(exceeded))
    if isinstance(verify_record, dict) and verify_record.get("status") in STATUSES:
        return (verify_record["status"], "graded")
    return ("infra_error", "verify_record_invalid")


def run_agent(argv, cwd, env, stdout_path, stderr_path, wall_seconds, kill_grace=10):
    """Run ``argv`` in its own session, capturing stdout/stderr to files.

    Waits up to ``wall_seconds`` then SIGTERMs the process group, waits up to
    ``kill_grace`` seconds, and SIGKILLs it. After the main process ends the
    group is SIGKILLed once more so leftover background children die. An agent
    that cannot start returns ``started=False`` rather than raising.
    """
    result = {
        "started": False,
        "exit_code": None,
        "timed_out": False,
        "duration_seconds": 0.0,
        "error": None,
    }
    try:
        with open(stdout_path, "w") as out, open(stderr_path, "w") as err:
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            except OSError as exc:
                result["error"] = str(exc)
                return result
            result["started"] = True
            start = time.monotonic()
            try:
                proc.wait(timeout=wall_seconds)
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=kill_grace)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    proc.wait()
            result["exit_code"] = proc.returncode
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            result["duration_seconds"] = time.monotonic() - start
    except OSError as exc:
        result["error"] = "cannot open output files: " + str(exc)
        return result
    return result


def grade(verify_cmd, image, workspace, package, grader_tests, out_path, timeout):
    """Invoke the verify grader and read back its JSON record.

    Runs ``verify_cmd`` with the grading arguments (exit code is not trusted,
    only the JSON file is). A subprocess timeout or a missing/invalid record
    returns ``record=None`` with a note; otherwise ``(record, None)``.
    """
    cmd = (
        list(verify_cmd)
        + [
            "--image", str(image),
            "--workspace", str(workspace),
            "--package", str(package),
            "--tests", str(grader_tests),
            "--out", str(out_path),
            "--timeout", str(timeout),
        ]
    )
    try:
        subprocess.run(
            cmd,
            timeout=timeout + 300,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return (None, "verify timed out")
    try:
        text = Path(out_path).read_text(encoding="utf-8")
    except OSError:
        return (None, "verify record missing or invalid")
    try:
        record = json.loads(text)
    except ValueError:
        return (None, "verify record missing or invalid")
    if not isinstance(record, dict):
        return (None, "verify record missing or invalid")
    return (record, None)


def _iso(dt):
    """Format an aware datetime as an ISO 8601 UTC stamp with a trailing ``Z``."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exit_code(status):
    """Map a run status to its process exit code."""
    return {"pass": 0, "task_fail": 1, "invalid_submission": 1, "infra_error": 3}[status]


def _verify_cmd_from(arg):
    """Return the parsed ``--verify-cmd`` list, or raise ``UsageError``."""
    if arg is None:
        return [sys.executable, "-m", "bench.verify"]
    try:
        parsed = json.loads(arg)
    except ValueError:
        raise UsageError("verify-cmd must be a JSON array")
    if (not isinstance(parsed, list) or not parsed
            or not all(isinstance(item, str) for item in parsed)):
        raise UsageError("verify-cmd must be a non-empty JSON array of strings")
    return parsed


def _build_parser():
    """Build the argument parser for the runner CLI."""
    parser = argparse.ArgumentParser(
        prog="python3 -m bench.run",
        description="Run one agent attempt on one ready case, then grade it.",
    )
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--grader-tests", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--rerun-of", default=None)
    parser.add_argument("--reason", default=None)
    parser.add_argument("--verify-cmd", default=None)
    parser.add_argument("--verify-timeout", type=int, default=1800)
    parser.add_argument("--kill-grace", type=int, default=10)
    return parser


def _preconditions(args):
    """Resolve and validate every input; raise ``UsageError`` or return a dict.

    Nothing is created on disk here: only after all of these pass does the run
    directory come into being.
    """
    case = load_case(args.catalog, args.case)
    image = image_ref(case["runtime"]["image"])
    agent = load_agent(args.agent)
    verify_cmd = _verify_cmd_from(args.verify_cmd)
    if (args.rerun_of is None) != (args.reason is None):
        raise UsageError("--rerun-of and --reason must be provided together")
    if args.rerun_of is not None and not re.fullmatch(RUN_ID_RE, args.rerun_of):
        raise UsageError("invalid rerun-of: " + str(args.rerun_of))
    run_id = args.run_id or new_run_id()
    if not re.fullmatch(RUN_ID_RE, run_id):
        raise UsageError("invalid run id: " + run_id)
    if not os.path.isfile(args.bundle):
        raise UsageError("bundle not found: " + args.bundle)
    expected = case["artifacts"]["bundle_sha256"]
    if workspace.sha256_file(args.bundle) != expected:
        raise UsageError("bundle sha256 mismatch")
    grader_tests = Path(args.grader_tests)
    if not grader_tests.is_dir():
        raise UsageError("grader-tests directory not found")
    run_dir = Path(args.runs_dir).resolve() / case["id"] / run_id
    if run_dir.exists():
        raise UsageError("run id already exists: " + run_id)
    return {
        "catalog": args.catalog, "case": case, "image": image, "agent": agent,
        "verify_cmd": verify_cmd, "rerun_of": args.rerun_of, "reason": args.reason,
        "run_id": run_id, "bundle": args.bundle, "package": args.package,
        "grader_tests": grader_tests, "run_dir": run_dir,
    }


def _write_manifest(path, manifest):
    """Write ``manifest`` atomically: a temp file, then ``os.replace``."""
    text = json.dumps(manifest, indent=2)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _build_manifest(ctx, agent_result, usage, usage_note, exceeded, changes,
                    violations, graded, verify_obj, verify_note, status, reason,
                    start_dt, end_dt, duration):
    """Assemble the ``run.json`` document from the collected run state."""
    case = ctx["case"]
    agent = ctx["agent"]
    limits = case["limits"]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": ctx["run_id"],
        "case_id": case["id"],
        "rerun_of": ctx["rerun_of"],
        "reason": ctx["reason"],
        "status": status,
        "status_reason": reason,
        "catalog_sha256": ctx["catalog_sha256"],
        "bundle_sha256": ctx["bundle_sha256"],
        "grader_tests_sha256": ctx["grader_tests_sha256"],
        "image": ctx["image"],
        "package": ctx["package"],
        "limits": {"wall_seconds": limits["wall_seconds"],
                   "output_tokens": limits["output_tokens"],
                   "tool_calls": limits["tool_calls"]},
        "agent": {"name": agent["name"], "argv": agent["argv"],
                  "info": agent.get("info", {}),
                  "env_keys": sorted(agent.get("env", {}).keys())},
        "agent_result": agent_result,
        "usage": usage,
        "usage_note": usage_note,
        "budget_exceeded": exceeded,
        "changes": changes,
        "violations": violations,
        "graded": graded,
        "verify": verify_obj,
        "verify_note": verify_note,
        "verify_cmd": ctx["verify_cmd"],
        "start_utc": _iso(start_dt),
        "end_utc": _iso(end_dt),
        "duration_seconds": duration,
        "python": platform.python_version(),
    }


def _integrity_reason(bundle_changed, grader_tests_changed, catalog_changed):
    """Return the highest-priority override reason, or ``None`` if the run is clean.

    Precedence follows the reported order: the bundle, then the grader tests,
    then the catalog.
    """
    if bundle_changed:
        return "bundle_changed"
    if grader_tests_changed:
        return "grader_tests_changed"
    if catalog_changed:
        return "catalog_changed"
    return None


def _prepare_workspace(ctx, run_dir, top):
    """Extract the bundle into a fresh workspace, or flag an unusable bundle.

    Returns the workspace directory on success. If the bundle is unsafe or has
    no ``TASK.md``, the run directory is removed and ``None`` is returned.
    """
    workspace_dir = run_dir / "workspace"
    workspace_dir.mkdir()
    try:
        workspace.safe_extract(ctx["bundle"], workspace_dir, top)
    except workspace.UnsafeBundle:
        shutil.rmtree(run_dir, ignore_errors=True)
        sys.stderr.write("unsafe bundle: cannot extract workspace\n")
        return None
    if not (workspace_dir / "TASK.md").is_file():
        shutil.rmtree(run_dir, ignore_errors=True)
        sys.stderr.write("bundle has no TASK.md\n")
        return None
    return workspace_dir


def _run_and_inspect(ctx, case, agent, workspace_dir, limits, args):
    """Run the agent, then capture usage, changes, and editable-path edits.

    Returns ``(start_dt, start_mono, agent_result, usage, usage_note, exceeded,
    changes, violations)``.
    """
    run_dir = ctx["run_dir"]
    start_dt = datetime.now(timezone.utc)
    start_mono = time.monotonic()
    before = workspace.snapshot(workspace_dir)

    bench_vars = {
        "BENCH_CASE_ID": case["id"],
        "BENCH_RUN_ID": ctx["run_id"],
        "BENCH_WORKSPACE": str(workspace_dir),
        "BENCH_TASK_FILE": str(workspace_dir / "TASK.md"),
        "BENCH_USAGE_FILE": str(run_dir / "usage.json"),
        "BENCH_IMAGE": ctx["image"],
        "BENCH_PACKAGE": ctx["package"],
        "BENCH_WALL_SECONDS": str(limits["wall_seconds"]),
        "BENCH_OUTPUT_TOKENS": str(limits["output_tokens"]),
        "BENCH_TOOL_CALLS": str(limits["tool_calls"]),
    }
    env = agent_env(agent, bench_vars, os.environ)
    agent_result = run_agent(
        agent["argv"], workspace_dir, env,
        run_dir / "agent.stdout", run_dir / "agent.stderr",
        limits["wall_seconds"], args.kill_grace)

    usage, usage_note = read_usage(run_dir / "usage.json")
    exceeded = budget_exceeded(usage, limits)
    after = workspace.snapshot(workspace_dir)
    changes = workspace.diff_snapshots(before, after)
    violations = workspace.check_editable(changes, after, case["editable_paths"])
    return (start_dt, start_mono, agent_result, usage, usage_note,
            exceeded, changes, violations)


def _grade_if_allowed(ctx, args, agent_result, workspace_dir, grading, changes,
                      bundle_changed, integrity_failure, violations, exceeded):
    """Rebuild the grading tree and, when gradable, run the grader.

    A changed bundle writes an empty patch with no grading tree. Otherwise the
    pristine tree is rebuilt and the accepted patch is written. The grader runs
    only when the run is gradable.
    """
    run_dir = ctx["run_dir"]
    if bundle_changed:
        grading = None
        (run_dir / "changes.patch").write_text("", encoding="utf-8")
    else:
        grading.mkdir()
        workspace.safe_extract(ctx["bundle"], grading, ctx["case"]["id"])
        (run_dir / "changes.patch").write_text(
            workspace.make_patch(grading, workspace_dir, changes), encoding="utf-8")

    graded = False
    grade_invoked = False
    verify_record = None
    verify_note = None
    gradable = (grading is not None and not integrity_failure
                and agent_result.get("started") and not violations
                and not agent_result.get("timed_out") and not exceeded)
    if gradable:
        workspace.apply_changes(workspace_dir, changes, grading)
        grade_invoked = True
        graded = True
        verify_record, verify_note = grade(
            ctx["verify_cmd"], ctx["image"], grading, ctx["package"],
            ctx["grader_tests"], run_dir / "verify.json", args.verify_timeout)
    return grading, graded, grade_invoked, verify_record, verify_note


def _verify_summary(run_dir, grade_invoked, verify_record, verify_note):
    """Turn a completed grade into its manifest ``(verify, note)`` pair.

    When grading ran, a valid record becomes the public ``verify`` object and
    the note is cleared; an invalid one surfaces the note instead.
    """
    if not grade_invoked:
        return (None, None)
    if isinstance(verify_record, dict) and verify_record.get("status") in STATUSES:
        return ({
            "status": verify_record.get("status"),
            "phase": verify_record.get("phase"),
            "counts": verify_record.get("counts"),
            "note": verify_record.get("note"),
            "record_sha256": workspace.sha256_file(run_dir / "verify.json"),
        }, None)
    return (None, verify_note)


def _execute(args, ctx):
    """Perform the run end to end and return its exit code."""
    case = ctx["case"]
    limits = case["limits"]
    run_dir = ctx["run_dir"]

    ctx["catalog_sha256"] = workspace.sha256_file(ctx["catalog"])
    ctx["bundle_sha256"] = workspace.sha256_file(ctx["bundle"])
    ctx["grader_tests_sha256"] = verify.sha256_tree(ctx["grader_tests"])

    run_dir.mkdir(parents=True)
    workspace_dir = _prepare_workspace(ctx, run_dir, case["id"])
    if workspace_dir is None:
        return 2

    (start_dt, start_mono, agent_result, usage, usage_note, exceeded,
     changes, violations) = _run_and_inspect(
        ctx, case, ctx["agent"], workspace_dir, limits, args)

    bundle_changed = workspace.sha256_file(ctx["bundle"]) != ctx["bundle_sha256"]
    grader_tests_changed = verify.sha256_tree(ctx["grader_tests"]) != ctx["grader_tests_sha256"]
    catalog_changed = workspace.sha256_file(ctx["catalog"]) != ctx["catalog_sha256"]
    integrity_failure = bundle_changed or grader_tests_changed or catalog_changed
    override_reason = _integrity_reason(
        bundle_changed, grader_tests_changed, catalog_changed)

    grading, graded, grade_invoked, verify_record, verify_note = _grade_if_allowed(
        ctx, args, agent_result, workspace_dir, run_dir / "grading",
        changes, bundle_changed, integrity_failure, violations, exceeded)

    verify_obj, verify_note_out = _verify_summary(
        run_dir, grade_invoked, verify_record, verify_note)

    if override_reason:
        if agent_result.get("started"):
            status, reason = ("invalid_submission", override_reason)
        else:
            status, reason = decide(agent_result, violations, exceeded, verify_record)
    else:
        status, reason = decide(agent_result, violations, exceeded, verify_record)

    end_dt = datetime.now(timezone.utc)
    duration = time.monotonic() - start_mono

    manifest = _build_manifest(
        ctx, agent_result, usage, usage_note, exceeded, changes, violations,
        graded, verify_obj, verify_note_out, status, reason, start_dt, end_dt,
        duration)
    _write_manifest(run_dir / "run.json", manifest)

    print(f"{status} {reason} {run_dir}")
    return _exit_code(status)


def main(argv=None):
    """CLI entry point: parse, precondition, run, and report the exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        ctx = _preconditions(args)
    except UsageError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    return _execute(args, ctx)


if __name__ == "__main__":
    sys.exit(main())
