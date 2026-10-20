import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bench.sandbox as sandbox

DUMMY_KEY = "sk-bench-dummy"  # public-check: allow token
KEY_TEXT = "key-file-secret-0123456789"  # public-check: allow token
NAMES = {"network": "aab-net-r1", "egress": "aab-egress-r1", "agent": "aab-agent-r1"}
EDITABLE = ["src/pkg/src", "ANALYSIS.md"]
RULES = (
    "## Benchmark rules (harness rules v1)\n\n"
    "- `/work` is the workspace. You may create, change, or delete files only under these paths in it:\n"
    "  - `src/pkg/src`\n"
    "  - `ANALYSIS.md`\n"
    "- Creating, changing, or deleting anything else under `/work` makes the whole submission invalid, "
    "even if the fix is correct.\n"
    "- Running the build from `/work` is fine: the top-level `build/`, `install/` and `log/` directories "
    "are ignored.\n"
    "- Put reproductions, scratch programs and extra builds under `/tmp`. It is writable and is discarded "
    "after the run.\n"
    "- Time limit: 60 minutes. Tool-call limit: 200. There is no network access.\n")
ENDPOINT = "http://192.0.2.10:18888"
UPSTREAM = ("192.0.2.10", 18888)
TOOLS = "Bash,Read,Edit,Write,Glob,Grep,TodoWrite"
SCRIPT = ('exec claude -p "$(cat /run/task/PROMPT.md)" --model "$0" --output-format stream-json --verbose '
          '--max-turns "$1" --permission-mode acceptEdits --allowedTools "$2" --disallowedTools "$3" "$4" '
          '--strict-mcp-config --no-session-persistence')
TOOL = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
TEARDOWN = [["docker", "rm", "-f", "aab-agent-r1"], ["docker", "rm", "-f", "aab-egress-r1"],
            ["docker", "network", "rm", "aab-net-r1"]]
EMPTY = {"output_tokens": None, "input_tokens": None, "tool_calls": 0, "num_turns": None,
         "result_subtype": None, "is_error": None, "transcript_lines": 0}


def line(obj):
    return json.dumps(obj) + "\n"


def assistant(*items):
    return line({"type": "assistant", "message": {"role": "assistant", "content": list(items)}})


def result(**changes):
    obj = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
           "usage": {"input_tokens": 100, "output_tokens": 50}}
    obj.update(changes)
    return line(obj)


class PureFunctionTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((sandbox.SCHEMA_VERSION, sandbox.EGRESS_PORT), (1, 18888))
        self.assertEqual((sandbox.DUMMY_KEY, sandbox.AGENT_TOOLS), (DUMMY_KEY, TOOLS))
        self.assertEqual(tuple(sandbox.DENIED_TOOLS), ("WebFetch", "WebSearch"))
        self.assertTrue(issubclass(sandbox.UsageError, Exception))

    def test_simple_run_ids_are_used_as_is(self):
        self.assertEqual(sandbox.names("r1"), NAMES)
        self.assertEqual(sandbox.names("ornith-sbx.2")["network"], "aab-net-ornith-sbx.2")
        self.assertEqual(sandbox.names("a" * 48)["egress"], "aab-egress-" + "a" * 48)

    def test_other_run_ids_get_a_hash_suffix_so_names_never_collide(self):
        def expected(run_id):
            stem = re.sub(r"[^a-z0-9.-]", "-", run_id.lower())[:39]
            return stem + "-" + hashlib.sha256(run_id.encode("ascii")).hexdigest()[:8]
        for run_id in ("Run_1.X-2", "A_B", "20261020T214829Z-0a1b2c3d", "a" * 49, "A" * 100):
            with self.subTest(run_id):
                got = sandbox.names(run_id)
                s = expected(run_id)
                self.assertEqual(got, {"network": "aab-net-" + s, "egress": "aab-egress-" + s, "agent": "aab-agent-" + s})
                self.assertTrue(all(len(v) <= 63 for v in got.values()))
        for a, b in (("A_B", "A-B"), ("a_b", "a-b"), ("AB", "ab"), ("x_1", "X_1")):
            self.assertNotEqual(sandbox.names(a)["network"], sandbox.names(b)["network"], (a, b))

    def test_names_reject_invalid_run_id(self):
        for bad in ("", "-x", ".x", "_x", "a/b", "a b", "a\n", "../x", "café"):
            with self.subTest(bad=bad), self.assertRaises(sandbox.UsageError):
                sandbox.names(bad)

    def test_parse_endpoint_good(self):
        self.assertEqual(sandbox.parse_endpoint(ENDPOINT), UPSTREAM)
        self.assertEqual(sandbox.parse_endpoint("http://spark.example:80/"), ("spark.example", 80))
        self.assertEqual(sandbox.parse_endpoint("http://h:1")[1], 1)
        self.assertEqual(sandbox.parse_endpoint("http://h:65535")[1], 65535)

    def test_parse_endpoint_bad(self):
        bad_urls = ("https://h:1", "http://h", "http://h:1/path", "http://h:1/a/", "http://user@h:1",
                    "http://u:p@h:1", "http://h:0", "http://h:65536", "http://h:x", "", "h:1", "http://:1",
                    "http://h:1?x=1", "ftp://h:1", "http://h_x:1")
        for bad in bad_urls:
            with self.subTest(bad=bad), self.assertRaises(sandbox.UsageError):
                sandbox.parse_endpoint(bad)

    def test_egress_argv_exact(self):
        argv = sandbox.egress_argv(NAMES, "egress:img", "/opt/bench/egress.py", "/run/keys/token", UPSTREAM)
        self.assertEqual(argv, [
            "docker", "run", "-d", "--rm", "--name", "aab-egress-r1", "--network", "aab-net-r1",
            "--network-alias", "egress", "--user", "1000:1000", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only", "--pids-limit", "64", "--memory", "256m",
            "--mount", "type=bind,source=/opt/bench/egress.py,target=/opt/egress.py,readonly",
            "--mount", "type=bind,source=/run/keys/token,target=/run/egress/key,readonly",
            "egress:img", "python3", "/opt/egress.py", "--bind", "0.0.0.0",
            "--http", "18888=192.0.2.10:18888", "--key-file", "/run/egress/key"])

    def test_agent_argv_exact(self):
        argv = sandbox.agent_argv(NAMES, "agent:img", "/srv/ws", "/srv/run/prompt.md", "m-1", 200, "6g", "2")
        self.assertEqual(argv, [
            "docker", "run", "--rm", "-i", "--name", "aab-agent-r1", "--network", "aab-net-r1",
            "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "1024", "--memory", "6g", "--cpus", "2",
            "--tmpfs", "/tmp:rw,exec,size=4g", "--tmpfs", "/home/bench:rw,size=1g,uid=1000,gid=1000,mode=0700",
            "--mount", "type=bind,source=/srv/ws,target=/work",
            "--mount", "type=bind,source=/srv/run/prompt.md,target=/run/task/PROMPT.md,readonly",
            "-w", "/work",
            "-e", "ANTHROPIC_BASE_URL=http://egress:18888", "-e", "ANTHROPIC_API_KEY=" + DUMMY_KEY,
            "-e", "HOME=/home/bench", "-e", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384", "-e", "API_TIMEOUT_MS=900000",
            "agent:img", "bash", "-c", SCRIPT, "m-1", "200", TOOLS, "WebFetch", "WebSearch"])

    def test_agent_argv_defaults_to_8g_and_4_cpus(self):
        argv = sandbox.agent_argv(NAMES, "agent:img", "/srv/ws", "/srv/run/prompt.md", "m-1", 5)
        self.assertEqual(argv[argv.index("--memory") + 1], "8g")
        self.assertEqual(argv[argv.index("--cpus") + 1], "4")

    def test_relative_paths_raise(self):
        for source, key in (("egress.py", "/k"), ("/e.py", "token"), ("./e.py", "/k")):
            with self.subTest(source=source, key=key), self.assertRaises(sandbox.UsageError):
                sandbox.egress_argv(NAMES, "i", source, key, UPSTREAM)
        for workspace, task in (("ws", "/t/prompt.md"), ("/ws", "prompt.md"), ("../ws", "/t")):
            with self.subTest(workspace=workspace, task=task), self.assertRaises(sandbox.UsageError):
                sandbox.agent_argv(NAMES, "i", workspace, task, "m", 5, "8g", "4")

    def test_client_command_exact(self):
        self.assertEqual(sandbox.client_command("m-1", 200),
                         ["bash", "-c", SCRIPT, "m-1", "200", TOOLS, "WebFetch", "WebSearch"])

    def test_client_command_rejects_bad_max_turns(self):
        for bad in (0, -1, "5", 1.5, None):
            with self.subTest(bad=bad), self.assertRaises(sandbox.UsageError):
                sandbox.client_command("m", bad)

    def test_rules_version(self):
        self.assertEqual(sandbox.RULES_VERSION, 1)

    def test_rules_text_exact(self):
        self.assertEqual(sandbox.rules_text(EDITABLE, 3600, 200), RULES)

    def test_rules_text_has_one_bullet_per_path_in_order(self):
        paths = ["z/last", "a/first", "m/middle", "ANALYSIS.md"]
        text = sandbox.rules_text(paths, 600, 5)
        self.assertEqual([l for l in text.splitlines() if l.startswith("  - ")], ["  - `%s`" % p for p in paths])
        self.assertIn("Time limit: 10 minutes. Tool-call limit: 5.", text)
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))

    def test_rules_text_rejects_bad_arguments(self):
        cases = [([], 3600, 200), ("src", 3600, 200), (None, 3600, 200), ([""], 3600, 200),
                 (["a", 3], 3600, 200), ([None], 3600, 200), (EDITABLE, 0, 200), (EDITABLE, True, 200),
                 (EDITABLE, -5, 200), (EDITABLE, "3600", 200), (EDITABLE, 3600, 0), (EDITABLE, 3600, -1),
                 (EDITABLE, 3600, True), (EDITABLE, 3600, "200")]
        for paths, wall, calls in cases:
            with self.subTest(paths=paths, wall=wall, calls=calls), self.assertRaises(sandbox.UsageError):
                sandbox.rules_text(paths, wall, calls)

    def test_teardown_argvs_exact(self):
        self.assertEqual(sandbox.teardown_argvs(NAMES), TEARDOWN)


class ParseTranscriptTests(unittest.TestCase):
    def test_empty_input_gives_defaults(self):
        self.assertEqual(sandbox.parse_transcript([]), EMPTY)
        self.assertEqual(sandbox.parse_transcript(iter(["", "\n"])), EMPTY)

    def test_counts_tool_use_only_in_top_level_assistant_messages(self):
        lines = [
            line({"type": "system", "subtype": "init"}),
            assistant({"type": "text", "text": "hi"}, TOOL, dict(TOOL, id="t2")),
            line({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}, TOOL]}}),
            assistant(TOOL),
            result(),
        ]
        parsed = sandbox.parse_transcript(iter(lines))
        self.assertEqual(parsed, {"output_tokens": 50, "input_tokens": 100, "tool_calls": 3, "num_turns": 3,
                                  "result_subtype": "success", "is_error": False, "transcript_lines": 5})

    def test_malformed_assistant_messages_are_not_counted_and_do_not_raise(self):
        lines = [line({"type": "assistant"}), line({"type": "assistant", "message": "x"}),
                 line({"type": "assistant", "message": {"content": "plain"}}),
                 line({"type": "assistant", "message": {"content": ["x", 3, None, {"type": "text"}]}})]
        parsed = sandbox.parse_transcript(lines)
        self.assertEqual((parsed["tool_calls"], parsed["transcript_lines"]), (0, 4))

    def test_fields_come_from_the_last_result_line(self):
        first = result(subtype="error_max_turns", is_error=True, num_turns=1,
                       usage={"input_tokens": 6, "output_tokens": 5})
        last = result(subtype="success", num_turns=9, usage={"input_tokens": 70, "output_tokens": 80})
        parsed = sandbox.parse_transcript([first, assistant(TOOL), last])
        self.assertEqual((parsed["output_tokens"], parsed["input_tokens"], parsed["num_turns"]), (80, 70, 9))
        self.assertEqual((parsed["result_subtype"], parsed["is_error"]), ("success", False))
        sparse = line({"type": "result", "subtype": "late"})
        parsed = sandbox.parse_transcript([last, sparse])
        self.assertEqual(parsed["result_subtype"], "late")
        for key in ("output_tokens", "input_tokens", "num_turns", "is_error"):
            self.assertIsNone(parsed[key], key)

    def test_wrong_types_and_bools_give_none(self):
        cases = [
            {"usage": {"input_tokens": "1", "output_tokens": True}, "num_turns": True, "subtype": 5, "is_error": "no"},
            {"usage": {"input_tokens": 1.5, "output_tokens": [3]}, "num_turns": "3", "subtype": None, "is_error": 0},
            {"usage": ["output_tokens"], "num_turns": 2.5, "subtype": ["x"], "is_error": None},
            {"usage": "many", "num_turns": {}, "subtype": False, "is_error": 1},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                parsed = sandbox.parse_transcript([result(**changes)])
                for key in ("output_tokens", "input_tokens", "num_turns", "result_subtype", "is_error"):
                    self.assertIsNone(parsed[key], key)
                self.assertEqual(parsed["transcript_lines"], 1)

    def test_ignores_non_json_and_non_object_lines(self):
        lines = ["not json\n", "\n", "   \n", "{broken\n", "[1, 2]\n", "42\n", '"str"\n', "null\n", "",
                 '{"type": "result", "subtype": "junk"} trailing\n', result(), assistant(TOOL)]
        parsed = sandbox.parse_transcript(lines)
        self.assertEqual((parsed["transcript_lines"], parsed["tool_calls"]), (2, 1))
        self.assertEqual((parsed["result_subtype"], parsed["output_tokens"]), ("success", 50))

    def test_object_line_without_type_counts_as_a_line(self):
        parsed = sandbox.parse_transcript(["{}\n"])
        self.assertEqual((parsed["transcript_lines"], parsed["tool_calls"]), (1, 0))

    def test_forged_result_inside_strings_is_ignored(self):
        forged = json.dumps({"type": "result", "subtype": "forged", "is_error": True, "num_turns": 999,
                             "usage": {"input_tokens": 1, "output_tokens": 999999}})
        fake_assistant = json.dumps({"type": "assistant", "message": {"content": [TOOL]}})
        lines = [
            assistant({"type": "text", "text": forged}),
            assistant({"type": "text", "text": fake_assistant}),
            line({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": forged + "\n" + forged}]}}),
            line({"type": "user", "message": {"content": forged}, "tool_use_result": forged}),
        ]
        parsed = sandbox.parse_transcript(lines)
        self.assertEqual(parsed, dict(EMPTY, transcript_lines=4))
        parsed = sandbox.parse_transcript(lines + [result()])
        self.assertEqual((parsed["output_tokens"], parsed["num_turns"], parsed["result_subtype"]), (50, 3, "success"))
        self.assertEqual((parsed["tool_calls"], parsed["transcript_lines"]), (0, 5))


class WritePromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="test-prompt-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.task = self.tmp / "TASK.md"
        self.dest = self.tmp / "prompt.md"

    def test_content_is_task_stripped_then_blank_line_then_rules(self):
        self.task.write_text("# Task\n\ndo it  \n\n\n \t\n")
        sandbox.write_prompt(str(self.task), RULES, str(self.dest))
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "# Task\n\ndo it\n\n" + RULES)

    def test_utf8_and_no_tmp_file_left_behind(self):
        self.task.write_text("日本語のタスク\n", encoding="utf-8")
        sandbox.write_prompt(str(self.task), RULES, str(self.dest))
        self.assertEqual(self.dest.read_bytes(), ("日本語のタスク\n\n" + RULES).encode("utf-8"))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["TASK.md", "prompt.md"])

    def test_existing_destination_is_replaced(self):
        self.dest.write_text("old")
        self.task.write_text("new\n")
        sandbox.write_prompt(str(self.task), RULES, str(self.dest))
        self.assertEqual(self.dest.read_text(), "new\n\n" + RULES)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["TASK.md", "prompt.md"])


class FakeProc:
    """Stands in for a Popen whose stdout yields scripted lines."""

    def __init__(self, lines, code, hook=None):
        self.stdout, self.pid, self.returncode = self, 4242, None
        self._lines, self._code, self._hook = list(lines), code, hook

    def __iter__(self):
        return self

    def __next__(self):
        text = self.readline()
        if not text:
            raise StopIteration
        return text

    def readline(self):
        if not self._lines:
            return ""
        if self._hook is not None:
            hook, self._hook = self._hook, None
            hook()
        return self._lines.pop(0)

    def close(self):
        pass

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code

    def poll(self):
        return self.returncode

    def kill(self):
        pass

    terminate = kill

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RunSandboxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="test-sandbox-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        self.task = self.workspace / "TASK.md"
        self.task.write_text("do the task\n")
        (self.tmp / "usage").mkdir()
        self.usage = self.tmp / "usage" / "usage.json"
        self.key = self.tmp / "token"
        self.key.write_text(KEY_TEXT)
        self.key.chmod(0o600)
        self.env = {"BENCH_RUN_ID": "r1", "BENCH_WORKSPACE": str(self.workspace), "BENCH_TASK_FILE": str(self.task),
                    "BENCH_USAGE_FILE": str(self.usage), "BENCH_TOOL_CALLS": "200",
                    "BENCH_WALL_SECONDS": "3600", "BENCH_EDITABLE_PATHS": json.dumps(EDITABLE)}
        self.prompt = self.usage.with_name("prompt.md")
        self.args = SimpleNamespace(agent_image="agent:img", egress_image="egress:img", endpoint=ENDPOINT,
                                    key_file=str(self.key), model="m-1", max_turns=200, memory="8g", cpus="4")
        self.reset()
        for signum in (signal.SIGTERM, signal.SIGINT):
            original = signal.getsignal(signum) or signal.SIG_DFL
            self.addCleanup(signal.signal, signum, original)
        patcher = mock.patch.object(time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def reset(self):
        self.events, self.docker_failures, self.logs, self.out = [], {}, ["ready 18888\n"], io.StringIO()

    def docker(self, argv, **kw):
        self.events.append(("docker", list(argv), kw))
        code, stdout = 0, ""
        for prefix, outcome in self.docker_failures.items():
            if tuple(argv[:len(prefix)]) == prefix:
                if isinstance(outcome, BaseException):
                    raise outcome
                code = outcome
        if argv[:2] == ["docker", "logs"]:
            stdout = self.logs.pop(0) if len(self.logs) > 1 else self.logs[0]
        if code and kw.get("check"):
            raise subprocess.CalledProcessError(code, argv, stdout, "")
        return subprocess.CompletedProcess(argv, code, stdout, "")

    def run_it(self, lines=(), code=0, hook=None):
        def popen(argv, **kw):
            self.events.append(("popen", list(argv), kw))
            return FakeProc(lines, code, hook)
        return sandbox.run_sandbox(self.args, self.env, docker=self.docker, popen=popen, out=self.out)

    def seq(self):
        return [(kind, argv) for kind, argv, _ in self.events]

    def docker_calls(self):
        return [argv for kind, argv, _ in self.events if kind == "docker"]

    def usage_doc(self):
        return json.loads(self.usage.read_text())

    def assert_torn_down(self):
        self.assertEqual(self.docker_calls()[-3:], TEARDOWN)


class RunSandboxTests(RunSandboxCase):
    def test_success_runs_steps_in_order_then_tears_down(self):
        self.assertEqual(self.run_it([result()], 0), 0)
        seq = self.seq()
        self.assertEqual(len(seq), 8)
        self.assertEqual(seq[0], ("docker", ["docker", "network", "create", "--internal", "aab-net-r1"]))
        egress = seq[1][1]
        marker = "type=bind,source="
        (mount,) = [a for a in egress if a.startswith(marker) and a.endswith(",target=/opt/egress.py,readonly")]
        source = mount[len(marker):-len(",target=/opt/egress.py,readonly")]
        self.assertTrue(os.path.isabs(source))
        self.assertEqual(egress, sandbox.egress_argv(NAMES, "egress:img", source, str(self.key), UPSTREAM))
        self.assertEqual(seq[2], ("docker", ["docker", "network", "connect", "bridge", "aab-egress-r1"]))
        self.assertEqual(seq[3], ("docker", ["docker", "logs", "aab-egress-r1"]))
        logs_kw = [kw for kind, argv, kw in self.events if argv == ["docker", "logs", "aab-egress-r1"]]
        self.assertTrue(logs_kw and all(kw.get("timeout") == 10 for kw in logs_kw), logs_kw)
        agent = sandbox.agent_argv(NAMES, "agent:img", str(self.workspace), str(self.prompt), "m-1", 200, "8g", "4")
        self.assertEqual(seq[4], ("popen", agent))
        self.assertEqual(seq[5:], [("docker", argv) for argv in TEARDOWN])

    def test_prompt_is_written_next_to_the_usage_file_and_mounted(self):
        self.task.write_text("do the task\n\n")
        self.run_it([result()])
        self.assertEqual(self.prompt.read_text(encoding="utf-8"), "do the task\n\n" + RULES)
        self.assertEqual(sorted(os.listdir(self.usage.parent)), ["prompt.md", "usage.json"])
        (agent,) = [argv for kind, argv, _ in self.events if kind == "popen"]
        mount = "type=bind,source=%s,target=/run/task/PROMPT.md,readonly" % self.prompt
        self.assertIn(mount, agent)
        self.assertFalse([a for a in agent if "TASK.md" in a])

    def test_prompt_is_written_before_any_docker_call(self):
        seen = []
        original = self.docker

        def docker(argv, **kw):
            seen.append(self.prompt.exists())
            return original(argv, **kw)

        self.docker = docker
        self.run_it([result()])
        self.assertTrue(seen)
        self.assertTrue(all(seen))

    def test_bad_rules_variables_raise_before_any_docker_call(self):
        bad_values = {
            "BENCH_EDITABLE_PATHS": [None, "", "not json", "{bad", '"src"', '{"a": "b"}', "42", "[1]",
                                     '["a", 2]', '["a", null]', "[]", '[""]'],
            "BENCH_WALL_SECONDS": [None, "", "x", "1.5", "0", "-3", "True", " "],
            "BENCH_TOOL_CALLS": [None, "", "x", "1.5", "0", "-3", "True", " "],
        }
        for name, values in bad_values.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    full = self.env
                    self.env = {k: v for k, v in full.items() if k != name}
                    if value is not None:
                        self.env[name] = value
                    with self.assertRaises(sandbox.UsageError):
                        self.run_it([result()])
                    self.assertEqual(self.events, [])
                    self.assertFalse(self.prompt.exists())
                    self.env = full

    def test_agent_is_started_with_stdout_piped_and_stderr_inherited(self):
        self.run_it([result()])
        (kw,) = [kw for kind, _, kw in self.events if kind == "popen"]
        self.assertEqual(kw.get("stdout"), subprocess.PIPE)
        self.assertIsNone(kw.get("stderr"))

    def test_teardown_commands_use_a_60_second_timeout(self):
        self.run_it([result()])
        for kind, argv, kw in self.events:
            if kind == "docker" and argv in TEARDOWN:
                self.assertEqual(kw.get("timeout"), 60, argv)

    def test_logs_are_polled_until_ready(self):
        self.logs = ["", "starting\n", "ready 18888\n"]
        self.assertEqual(self.run_it([result()]), 0)
        self.assertEqual(self.docker_calls().count(["docker", "logs", "aab-egress-r1"]), 3)
        self.assertIn("popen", [kind for kind, _ in self.seq()])

    def test_agent_stdout_is_copied_to_out(self):
        lines = [line({"type": "system"}), "plain text line\n", assistant(TOOL), result()]
        self.run_it(lines)
        self.assertEqual(self.out.getvalue(), "".join(lines))

    def test_usage_file_is_written_from_the_transcript(self):
        lines = [line({"type": "system"}), assistant(TOOL, TOOL), "noise\n", result()]
        self.assertEqual(self.run_it(lines), 0)
        self.assertEqual(self.usage_doc(), {
            "output_tokens": 50, "input_tokens": 100, "tool_calls": 2, "num_turns": 3, "result_subtype": "success",
            "is_error": False, "transcript_lines": 3, "schema_version": 1, "source": "transcript",
            "client": "claude-code"})
        self.assertEqual(sorted(os.listdir(self.usage.parent)), ["prompt.md", "usage.json"])

    def test_agent_failure_returns_its_exit_code_and_still_writes_usage(self):
        self.assertEqual(self.run_it([assistant(TOOL), "boom\n"], code=7), 7)
        self.assert_torn_down()
        doc = self.usage_doc()
        self.assertEqual((doc["tool_calls"], doc["transcript_lines"], doc["output_tokens"]), (1, 1, None))
        self.assertEqual((doc["schema_version"], doc["source"], doc["client"]), (1, "transcript", "claude-code"))
        self.assertEqual(sorted(os.listdir(self.usage.parent)), ["prompt.md", "usage.json"])

    def test_setup_failures_return_3_without_starting_the_agent(self):
        create, run, connect = ("docker", "network", "create"), ("docker", "run"), ("docker", "network", "connect")
        cases = {"create": ({create: 1}, 1), "egress": ({run: 125}, 2),
                 "connect": ({connect: 1}, 3), "never ready": ({}, 3 + 15)}
        for label, (fail, before_teardown) in cases.items():
            with self.subTest(label):
                self.reset()
                self.docker_failures = fail
                if label == "never ready":
                    self.logs = ["starting\n"]
                self.assertEqual(self.run_it([result()]), 3)
                self.assertNotIn("popen", [kind for kind, _ in self.seq()])
                self.assertEqual(len(self.docker_calls()), before_teardown + 3)
                self.assertEqual(self.docker_calls()[0][:3], ["docker", "network", "create"])
                self.assertEqual(self.docker_calls()[-3:], TEARDOWN)
                self.assertEqual(self.out.getvalue(), "")

    def test_logs_are_polled_at_most_15_times(self):
        self.logs = ["starting\n"]
        self.run_it([result()])
        self.assertEqual(self.docker_calls().count(["docker", "logs", "aab-egress-r1"]), 15)

    def test_teardown_failures_are_ignored(self):
        self.docker_failures = {("docker", "rm", "-f", "aab-agent-r1"): 1,
                     ("docker", "rm", "-f", "aab-egress-r1"): OSError("gone"),
                     ("docker", "network", "rm"): subprocess.TimeoutExpired(["docker"], 60)}
        self.assertEqual(self.run_it([result()], code=0), 0)
        self.assertEqual(self.docker_calls()[-3:], TEARDOWN)
        self.assertEqual(self.usage_doc()["output_tokens"], 50)

    def test_loose_key_file_raises_before_any_docker_call(self):
        for mode in (0o644, 0o660, 0o666):
            with self.subTest(mode=oct(mode)):
                self.key.chmod(mode)
                with self.assertRaises(sandbox.UsageError):
                    self.run_it([result()])
                self.assertEqual(self.events, [])
        self.key.chmod(0o600)
        for bad in (self.tmp / "missing", self.tmp / "usage"):
            with self.subTest(path=bad.name):
                self.args.key_file = str(bad)
                with self.assertRaises(sandbox.UsageError):
                    self.run_it([result()])
                self.assertEqual(self.events, [])

    def test_missing_bench_variable_raises_before_any_docker_call(self):
        for name in ("BENCH_RUN_ID", "BENCH_WORKSPACE", "BENCH_TASK_FILE", "BENCH_USAGE_FILE"):
            with self.subTest(name):
                full = self.env
                self.env = {k: v for k, v in full.items() if k != name}
                with self.assertRaises(sandbox.UsageError):
                    self.run_it([result()])
                self.assertEqual(self.events, [])
                self.env = full

    def test_key_file_content_never_leaks(self):
        self.run_it([assistant(TOOL), result()])
        recorded = " ".join(" ".join(argv) + repr(kw) for _, argv, kw in self.events)
        for text in (recorded, self.out.getvalue(), self.usage.read_text()):
            self.assertNotIn(KEY_TEXT, text)
        self.assertIn(str(self.key), recorded)


class SignalTests(RunSandboxCase):
    def test_signal_handler_kills_the_agent_container(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum.name):
                self.reset()
                seen = []

                def hook():
                    handler = signal.getsignal(signum)
                    seen.append(callable(handler))
                    handler(signum, None)

                self.assertEqual(self.run_it([assistant(TOOL), result()], code=137, hook=hook), 137)
                self.assertEqual(seen, [True])
                kills = [i for i, (kind, argv, kw) in enumerate(self.events) if argv == ["docker", "kill", "aab-agent-r1"]]
                self.assertTrue(kills)
                self.assertEqual(self.events[kills[0]][2].get("timeout"), 30)
                popen_at = [i for i, (kind, _, _) in enumerate(self.events) if kind == "popen"][0]
                first_teardown = [i for i, (_, argv, _) in enumerate(self.events) if argv == TEARDOWN[0]][0]
                self.assertTrue(popen_at < kills[0] < first_teardown)
                self.assertEqual(self.docker_calls()[-3:], TEARDOWN)
                self.assertEqual(self.usage_doc()["source"], "transcript")


class MainTests(unittest.TestCase):
    ARGV = ["--agent-image", "agent:img", "--egress-image", "egress:img", "--endpoint", ENDPOINT,
            "--key-file", "/k/token", "--model", "m-1", "--max-turns", "200"]

    def call_main(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                code = sandbox.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def test_missing_required_arguments_exit_2(self):
        self.assertEqual(self.call_main([])[0], 2)
        for index in range(0, len(self.ARGV), 2):
            with self.subTest(option=self.ARGV[index]):
                argv = self.ARGV[:index] + self.ARGV[index + 2:]
                with mock.patch.object(sandbox, "run_sandbox") as run_sandbox:
                    code, err = self.call_main(argv)
                self.assertEqual(code, 2)
                self.assertTrue(err.strip())
                run_sandbox.assert_not_called()

    def test_usage_error_exits_2_with_one_stderr_line(self):
        with mock.patch.object(sandbox, "run_sandbox", side_effect=sandbox.UsageError("bad key file")):
            code, err = self.call_main(self.ARGV)
        self.assertEqual(code, 2)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.strip())

    def test_arguments_and_environment_reach_run_sandbox(self):
        with mock.patch.object(sandbox, "run_sandbox", return_value=5) as run_sandbox:
            code, _ = self.call_main(self.ARGV)
            self.assertEqual(code, 5)
            args, env = run_sandbox.call_args.args[:2]
            self.assertEqual((args.agent_image, args.egress_image, args.endpoint, args.key_file, args.model),
                             ("agent:img", "egress:img", ENDPOINT, "/k/token", "m-1"))
            self.assertEqual((int(args.max_turns), str(args.memory), str(args.cpus)), (200, "8g", "4"))
            self.assertEqual(dict(env), dict(os.environ))
            self.call_main(self.ARGV + ["--memory", "4g", "--cpus", "2"])
            args = run_sandbox.call_args.args[0]
            self.assertEqual((str(args.memory), str(args.cpus)), ("4g", "2"))


if __name__ == "__main__":
    unittest.main()
