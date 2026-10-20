"""Isolated model launcher: run one agent inside a private Docker network.

``bench.run`` sets ``BENCH_*``; this builds the internal network, an egress
gateway to the model endpoint, and the agent, capturing the transcript
host-side so the usage file cannot be forged from inside the container.
"""
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


SCHEMA_VERSION = 1
RULES_VERSION = 1
EGRESS_PORT = 18888
DUMMY_KEY = "sk-bench-dummy"  # public-check: allow token
DUMMY_OAUTH_TOKEN = "sk-ant-oat01-bench-dummy"  # public-check: allow token
CLIENT_AUTHS = ("api-key", "oauth")
AGENT_TOOLS = "Bash,Read,Edit,Write,Glob,Grep,TodoWrite"
DENIED_TOOLS = ("WebFetch", "WebSearch")

RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
AUTH_STYLES = ("both", "x-api-key", "bearer")
ENDPOINT_RE = re.compile(r"^(?P<scheme>http|https)://"
                         r"(?P<host>[A-Za-z0-9.-]+)"
                         r"(?::(?P<port>[0-9]+))?"
                         r"(?:/)?$")

USAGE_KEYS = ("output_tokens", "input_tokens", "num_turns", "result_subtype", "is_error")


class UsageError(Exception):
    """Bad invocation of the launcher itself, e.g. a bad run id or endpoint."""


def _parse_editable_paths(value):
    """Return ``value`` parsed as a non-empty JSON array of non-empty strings."""
    try:
        paths = json.loads(value)
    except (TypeError, ValueError):
        raise UsageError("BENCH_EDITABLE_PATHS must be a JSON array of strings")
    if not isinstance(paths, list) or not paths or any(
            not isinstance(part, str) or not part for part in paths):
        raise UsageError("BENCH_EDITABLE_PATHS must be a non-empty JSON array of strings")
    return paths


def _parse_decimal_int(value, name):
    """Return ``value`` as a positive decimal int or raise ``UsageError``."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise UsageError(name + " must be a decimal int")
    number = int(value)
    if number < 1:
        raise UsageError(name + " must be a positive int")
    return number


def rules_text(editable_paths, wall_seconds, tool_calls) -> str:
    """Build the harness-rules block from the editable paths and the budget."""
    if (not isinstance(editable_paths, list) or not editable_paths or any(
            not isinstance(part, str) or not part for part in editable_paths)):
        raise UsageError("editable_paths must be a non-empty list of non-empty strings")
    for label, value in (("wall_seconds", wall_seconds), ("tool_calls", tool_calls)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise UsageError(label + " must be a positive int")
    bullets = "\n".join("  - `%s`" % path for path in editable_paths)
    return (
        "## Benchmark rules (harness rules v1)\n\n"
        "- `/work` is the workspace. You may create, change, or delete files only under these paths in it:\n"
        + bullets + "\n"
        "- Creating, changing, or deleting anything else under `/work` makes the whole submission invalid, "
        "even if the fix is correct.\n"
        "- Running the build from `/work` is fine: the top-level `build/`, `install/` and `log/` directories "
        "are ignored.\n"
        "- Put reproductions, scratch programs and extra builds under `/tmp`. It is writable and is discarded "
        "after the run.\n"
        "- Time limit: %d minutes. Tool-call limit: %d. There is no network access.\n"
    ) % (wall_seconds // 60, tool_calls)


def write_prompt(task_file, rules, dest) -> None:
    """Atomically write ``task_file`` (trailing whitespace trimmed) plus rules."""
    task_text = Path(task_file).read_text(encoding="utf-8").rstrip()
    content = task_text + "\n\n" + rules
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, dest)


def names(run_id):
    """Return the network/egress/agent names derived from ``run_id``.

    A run id not matching the name pattern raises ``UsageError``. When the id
    is already a valid network name (lower-case and short enough) it is used as
    is; otherwise it is normalised and given a short hash suffix so two ids
    that differ only in case or punctuation never share a Docker name.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise UsageError("invalid run id: " + repr(run_id))
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,47}", run_id):
        s = run_id
    else:
        s = re.sub(r"[^a-z0-9.-]", "-", run_id.lower())[:39]
        s = s + "-" + hashlib.sha256(run_id.encode("ascii")).hexdigest()[:8]
    base = {"network": "aab-net-", "egress": "aab-egress-", "agent": "aab-agent-"}
    return {key: prefix + s for key, prefix in base.items()}


def parse_endpoint(url):
    """Return ``(scheme, host, port)`` for ``http://`` or ``https://`` URLs.

    ``https`` ports default to 443; ``http`` ports are required. A path,
    user-info, out-of-range port or anything else raises ``UsageError``.
    """
    match = ENDPOINT_RE.fullmatch(url or "")
    if not match:
        raise UsageError("invalid endpoint: " + repr(url))
    scheme = match.group("scheme")
    host = match.group("host")
    port_str = match.group("port")
    if scheme == "https":
        port = 443 if port_str is None else int(port_str)
    else:
        if port_str is None:
            raise UsageError("invalid endpoint: " + repr(url))
        port = int(port_str)
    if not 1 <= port <= 65535:
        raise UsageError("invalid port in endpoint: " + repr(url))
    return (scheme, host, port)


def egress_argv(names, egress_image, egress_src, key_file, upstream,
                auth_style="both"):
    """Return the ``docker run`` argv for the egress gateway container.

    ``upstream`` is ``(scheme, host, port)``; the route flag is ``--https`` for
    ``https`` and ``--http`` otherwise. ``--auth-style`` always follows the key
    file, which makes runs self-describing. HTTPS upstreams rely on the image's
    system CA store, so no ``--ca-file`` is passed.
    """
    if (not os.path.isabs(egress_src)) or (not os.path.isabs(key_file)):
        raise UsageError("egress source and key file must be absolute")
    scheme, host, port = upstream
    route_flag = "--https" if scheme == "https" else "--http"
    return [
        "docker", "run", "-d", "--rm", "--name", names["egress"],
        "--network", names["network"],
        "--network-alias", "egress",
        "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--read-only",
        "--pids-limit", "64", "--memory", "256m",
        "--mount", "type=bind,source=%s,target=/opt/egress.py,readonly" % egress_src,
        "--mount", "type=bind,source=%s,target=/run/egress/key,readonly" % key_file,
        egress_image, "python3", "/opt/egress.py",
        "--bind", "0.0.0.0",
        route_flag, "%d=%s:%d" % (EGRESS_PORT, host, port),
        "--key-file", "/run/egress/key",
        "--auth-style", auth_style,
    ]


def agent_argv(names, agent_image, workspace, prompt_file, model, max_turns,
               memory="8g", cpus="4", client_auth="api-key"):
    """Return the ``docker run`` argv for the agent container."""
    if (not os.path.isabs(workspace)) or (not os.path.isabs(prompt_file)):
        raise UsageError("workspace and prompt file must be absolute")
    if client_auth not in CLIENT_AUTHS:
        raise UsageError("unknown client_auth: " + repr(client_auth))
    command = client_command(model, max_turns)
    client_env = ("CLAUDE_CODE_OAUTH_TOKEN=" + DUMMY_OAUTH_TOKEN
                  if client_auth == "oauth"
                  else "ANTHROPIC_API_KEY=" + DUMMY_KEY)
    return [
        "docker", "run", "--rm", "-i", "--name", names["agent"],
        "--network", names["network"],
        "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "1024", "--memory", memory, "--cpus", str(cpus),
        "--tmpfs", "/tmp:rw,exec,size=4g",
        "--tmpfs", "/home/bench:rw,size=1g,uid=1000,gid=1000,mode=0700",
        "--mount", "type=bind,source=%s,target=/work" % workspace,
        "--mount", "type=bind,source=%s,target=/run/task/PROMPT.md,readonly" % prompt_file,
        "-w", "/work",
        "-e", "ANTHROPIC_BASE_URL=http://egress:18888",
        "-e", client_env,
        "-e", "HOME=/home/bench",
        "-e", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384",
        "-e", "API_TIMEOUT_MS=900000",
        agent_image, *command,
    ]


def client_command(model, max_turns):
    """Return the ``claude`` client argv (image command) for ``model``/``max_turns``."""
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        raise UsageError("max_turns must be a positive int")
    return [
        "bash", "-c",
        "exec claude -p \"$(cat /run/task/PROMPT.md)\" --model \"$0\" "
        "--output-format stream-json --verbose "
        "--max-turns \"$1\" --permission-mode acceptEdits "
        "--allowedTools \"$2\" --disallowedTools \"$3\" \"$4\" "
        "--strict-mcp-config --no-session-persistence",
        model, str(max_turns), AGENT_TOOLS, DENIED_TOOLS[0], DENIED_TOOLS[1],
    ]


def parse_transcript(lines):
    """Parse stream-json ``lines``; return summary statistics.

    Only top-level JSON object lines count; text inside a string is ignored.
    """
    usage = {key: None for key in USAGE_KEYS}
    usage["tool_calls"] = 0
    usage["transcript_lines"] = 0
    for raw in lines:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or text[0] != "{":
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        usage["transcript_lines"] += 1
        if obj.get("type") == "assistant":
            usage["tool_calls"] += _count_tool_uses(obj)
        elif obj.get("type") == "result":
            _copy_result(obj, usage)
    return usage


def _count_tool_uses(obj):
    """Count ``content`` items of ``type == tool_use`` in an assistant message."""
    message = obj.get("message")
    if not isinstance(message, dict):
        return 0
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    return sum(1 for item in content if isinstance(item, dict)
               and item.get("type") == "tool_use")


def _copy_result(obj, usage):
    """Copy usage/subtype/is_error/num_turns; a bool is never an int."""
    util = obj.get("usage")
    if isinstance(util, dict):
        output = util.get("output_tokens")
        input_ = util.get("input_tokens")
        usage["output_tokens"] = output if isinstance(output, int) and not isinstance(output, bool) else None
        usage["input_tokens"] = input_ if isinstance(input_, int) and not isinstance(input_, bool) else None
    else:
        usage["output_tokens"] = None
        usage["input_tokens"] = None
    num_turns = obj.get("num_turns")
    usage["num_turns"] = num_turns if isinstance(num_turns, int) and not isinstance(num_turns, bool) else None
    subtype = obj.get("subtype")
    usage["result_subtype"] = subtype if isinstance(subtype, str) else None
    is_error = obj.get("is_error")
    usage["is_error"] = is_error if isinstance(is_error, bool) else None


def teardown_argvs(names):
    """Return the teardown commands: kill agent, kill egress, remove network."""
    return [
        ["docker", "rm", "-f", names["agent"]],
        ["docker", "rm", "-f", names["egress"]],
        ["docker", "network", "rm", names["network"]],
    ]


def _docker(docker, argv, timeout):
    """Return the ``returncode``; an OSError or timeout counts as a failure (1)."""
    try:
        return docker(argv, capture_output=True, text=True, timeout=timeout).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def _egress_src():
    """Return the resolved absolute path of ``bench/egress.py``."""
    return Path(__file__).with_name("egress.py").resolve()


def _egress_ready(docker, name):
    """Poll ``docker logs`` up to 15 times for the gateway's ``ready`` line."""
    for _ in range(15):
        try:
            completed = docker(["docker", "logs", name],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        logs = completed.stdout if completed is not None else None
        if logs is not None and "ready" in logs:
            return True
        time.sleep(1)
    return False


_CASE_PLACEHOLDER = "{case}"


def _resolve_agent_image(agent_image, env):
    """Return ``agent_image`` with ``{case}`` replaced by the lowercased case id.

    When the image contains the placeholder it is expanded from
    ``BENCH_CASE_ID`` so each case gets its own grading image; a missing or
    empty id raises ``UsageError`` before any docker call. Without the
    placeholder the image is returned unchanged.
    """
    if _CASE_PLACEHOLDER not in agent_image:
        return agent_image
    case_id = env.get("BENCH_CASE_ID")
    if not case_id:
        raise UsageError(
            "BENCH_CASE_ID is required when --agent-image contains "
            + repr(_CASE_PLACEHOLDER))
    return agent_image.replace(_CASE_PLACEHOLDER, case_id.lower())


def run_sandbox(args, env, docker=subprocess.run, popen=subprocess.Popen, out=sys.stdout):
    """Read ``BENCH_*``, build the network, run the egress gateway and agent,
    capture the transcript, write the usage file, tear everything down, restore
    the SIGTERM/SIGINT handlers, and return the exit code.
    """
    for name in ("BENCH_RUN_ID", "BENCH_WORKSPACE", "BENCH_TASK_FILE", "BENCH_USAGE_FILE"):
        if not env.get(name):
            raise UsageError("missing " + name)
    for name in ("BENCH_EDITABLE_PATHS", "BENCH_WALL_SECONDS", "BENCH_TOOL_CALLS"):
        if not env.get(name):
            raise UsageError("missing " + name)
    args.agent_image = _resolve_agent_image(args.agent_image, env)
    run_id = env["BENCH_RUN_ID"]
    workspace = env["BENCH_WORKSPACE"]
    task_file = env["BENCH_TASK_FILE"]
    usage_file = env["BENCH_USAGE_FILE"]
    editable_paths = _parse_editable_paths(env["BENCH_EDITABLE_PATHS"])
    wall_seconds = _parse_decimal_int(env["BENCH_WALL_SECONDS"], "BENCH_WALL_SECONDS")
    tool_calls = _parse_decimal_int(env["BENCH_TOOL_CALLS"], "BENCH_TOOL_CALLS")
    names_obj = names(run_id)
    if (not os.path.isfile(args.key_file)) or oct(os.stat(args.key_file).st_mode & 0o777) != "0o600":
        raise UsageError("key file must be a regular file with mode 600")
    prompt_path = Path(usage_file).with_name("prompt.md")
    write_prompt(task_file, rules_text(editable_paths, wall_seconds, tool_calls), str(prompt_path))

    state = {"proc": None}

    def on_signal(signum, frame):
        proc = state["proc"]
        if proc is not None:
            _docker(docker, ["docker", "kill", names_obj["agent"]], 30)

    old_term = signal.signal(signal.SIGTERM, on_signal)
    old_int = signal.signal(signal.SIGINT, on_signal)
    try:
        proc = None
        transcript = []
        try:
            if _docker(docker, ["docker", "network", "create", "--internal", names_obj["network"]], 120):
                raise RuntimeError("network create failed")
            if _docker(docker, egress_argv(
                    names_obj, args.egress_image, _egress_src(), args.key_file,
                    parse_endpoint(args.endpoint), args.auth_style), 120):
                raise RuntimeError("egress start failed")
            if _docker(docker, ["docker", "network", "connect", "bridge", names_obj["egress"]], 120):
                raise RuntimeError("network connect failed")
            if not _egress_ready(docker, names_obj["egress"]):
                raise RuntimeError("egress did not become ready")
            proc = popen(agent_argv(
                names_obj, args.agent_image, workspace, str(prompt_path),
                args.model, args.max_turns, args.memory, args.cpus,
                getattr(args, "client_auth", "api-key")),
                stdout=subprocess.PIPE, text=True)
            state["proc"] = proc
            for line in proc.stdout:
                out.write(line)
                out.flush()
                transcript.append(line)
            code = proc.wait()
        except Exception:
            code = 3
            transcript = []
        finally:
            for command in teardown_argvs(names_obj):
                _docker(docker, command, 60)
        _write_usage(usage_file, transcript)
        return code
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def _write_usage(usage_file, transcript):
    """Atomically write the usage document: ``parse_transcript`` plus metadata."""
    doc = parse_transcript(transcript)
    doc["schema_version"] = SCHEMA_VERSION
    doc["source"] = "transcript"
    doc["client"] = "claude-code"
    directory = os.path.dirname(usage_file) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = usage_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, usage_file)


def main(argv=None):
    """CLI entry point: parse arguments, run the sandbox, return its exit code."""
    parser = argparse.ArgumentParser(
        prog="python3 -m bench.sandbox",
        description="Isolate a model run inside a private Docker network.")
    parser.add_argument("--agent-image", required=True,
                        help="Agent image, e.g. aab/agent-claude:{case}. The "
                             "{case} placeholder is replaced with BENCH_CASE_ID "
                             "(lowercased) before the run.")
    parser.add_argument("--egress-image", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-turns", type=int, required=True)
    parser.add_argument("--auth-style", choices=AUTH_STYLES, default="both")
    parser.add_argument("--client-auth", choices=CLIENT_AUTHS, default="api-key",
                        help="Agent client auth mode. For 'oauth' the agent container gets a "
                             "dummy OAuth token; also pass --auth-style bearer so the egress "
                             "serves bearer auth.")
    parser.add_argument("--memory", default="8g")
    parser.add_argument("--cpus", default="4")
    try:
        args = parser.parse_args(argv)
        return run_sandbox(args, os.environ)
    except UsageError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
