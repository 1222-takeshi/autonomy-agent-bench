"""Aggregate run manifests into a per-case comparison table.

``bench.run`` writes one ``run.json`` manifest per case/run under
``<runs-dir>/<case id>/<run id>/run.json``. This module reads those manifests
without running a model or Docker and summarises them into a Markdown (or JSON)
table with one row per case/agent. The output never carries the agent argv,
environment values or host paths, which may hold local or secret data.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

STATUSES = ("pass", "task_fail", "invalid_submission", "infra_error")
SCORED_STATUSES = ("pass", "task_fail", "invalid_submission")
SCHEMA_VERSION = 1
NOTES = "infra_error / incomplete runs are listed but not scored (docs/protocol.md)."

HEADER = (
    "| case | agent | model | runs | pass | task_fail | invalid | infra | incomplete "
    "| pass rate | median min | median out tokens | median in tokens | median tools |\n"
    "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
)


def median(values):
    """Median of ``values``: None for an empty list, else the mean of the two
    middle values for an even count, otherwise the single middle value."""
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _read_record(run_dir):
    """Parsed ``run.json`` when it is valid JSON with schema_version 1, else
    ``(None, note)`` describing why the run counts as incomplete."""
    manifest = run_dir / "run.json"
    if manifest.is_symlink() or not manifest.is_file():
        return None, "run.json is missing"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None, "run.json could not be parsed"
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None, "unsupported schema_version"
    return data, None


def _agent_name(record):
    """The agent's ``name`` when it is a non-empty str, otherwise ``"?"``."""
    if not isinstance(record, dict):
        return "?"
    agent = record.get("agent")
    if isinstance(agent, dict):
        name = agent.get("name")
        if isinstance(name, str) and name:
            return name
    return "?"


def _completeness_note(record):
    """A short reason the record is incomplete, or None when it is complete.

    A record is complete only when it is a ``schema_version == 1`` dict,
    ``agent`` is a dict whose ``name`` is a non-empty str, and ``status`` is one
    of ``STATUSES``.
    """
    if not isinstance(record, dict):
        return "record is not a manifest"
    if record.get("schema_version") != SCHEMA_VERSION:
        return "unsupported schema_version"
    agent = record.get("agent")
    if not isinstance(agent, dict) \
            or not isinstance(agent.get("name"), str) \
            or not agent.get("name"):
        return "agent name missing or empty"
    if record.get("status") not in STATUSES:
        return "unknown status"
    return None


def load_runs(runs_dir):
    """Runs discovered at depth two under ``runs_dir``, sorted by case then id.

    A run without a parseable schema_version 1 manifest is incomplete and carries
    agent "?", status None, a null record and a non-empty note.
    """
    root = Path(runs_dir)
    runs = []
    if not root.is_dir():
        return runs
    for case_dir in sorted(root.iterdir(), key=str):
        if case_dir.is_symlink() or not case_dir.is_dir():
            continue
        for run_dir in sorted(case_dir.iterdir(), key=str):
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            record, note = _read_record(run_dir)
            if record is not None:
                note = note or _completeness_note(record)
            note = note or ""
            complete = note == ""
            agent = _agent_name(record) if record is not None else "?"
            runs.append({
                "case_id": case_dir.name,
                "run_id": run_dir.name,
                "agent": agent,
                "status": record.get("status") if complete else None,
                "complete": complete,
                "record": record,
                "note": note or "",
            })
    runs.sort(key=lambda r: (r["case_id"], r["run_id"]))
    return runs


def _info_of(record, *path):
    """Nested dict value following ``path`` keys, or None if absent or not a dict."""
    current = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def summarize(runs):
    """Collapse runs into one row per (case_id, agent), sorted by case then agent.

    ``infra_error`` and incomplete runs are counted and shown, but excluded from
    ``scored`` and from the medians.
    """
    grouped = {}
    for run in runs:
        grouped.setdefault((run["case_id"], run["agent"]), []).append(run)

    rows = []
    for (case_id, agent) in sorted(grouped):
        group = grouped[(case_id, agent)]
        scored = [r for r in group if r["status"] in SCORED_STATUSES]

        model = None
        for run in group:  # the first complete run supplies the model
            if run["complete"]:
                candidate = _info_of(run["record"], "agent", "info", "model")
                model = candidate if isinstance(candidate, str) else None
                break

        counts = Counter(r["status"] for r in group if r["status"] in STATUSES)
        incomplete = sum(1 for run in group if not run["complete"])
        scored_total = counts["pass"] + counts["task_fail"] + counts["invalid_submission"]

        durations = []
        for run in scored:
            seconds = run["record"].get("duration_seconds")
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                durations.append(seconds / 60)
        median_minutes = round(median(durations), 1) if durations else None

        def median_field(field):
            values = []
            for run in scored:
                usage = run["record"].get("usage")
                if not isinstance(usage, dict):
                    continue
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    values.append(value)
            value = median(values)
            return round(value) if value is not None else None

        rows.append({
            "case_id": case_id,
            "agent": agent,
            "model": model,
            "runs": len(group),
            "pass": counts["pass"],
            "task_fail": counts["task_fail"],
            "invalid_submission": counts["invalid_submission"],
            "infra_error": counts["infra_error"],
            "incomplete": incomplete,
            "scored": scored_total,
            "pass_rate": counts["pass"] / scored_total if scored_total else None,
            "median_minutes": median_minutes,
            "median_output_tokens": median_field("output_tokens"),
            "median_input_tokens": median_field("input_tokens"),
            "median_tool_calls": median_field("tool_calls"),
        })
    return rows


def _str_cell(value):
    if value is None:
        return "-"
    return str(value).replace("|", "\\|")


def _int_cell(value):
    return "-" if value is None else str(int(value))


def _rate_cell(row):
    scored = row["scored"]
    if scored == 0:
        return "-"
    percent = round(row["pass"] / scored * 100)
    return f"{row['pass']}/{scored} ({percent}%)"


def to_markdown(rows):
    """Render rows as a GitHub table followed by the protocol notes line."""
    lines = [HEADER]
    for row in rows:
        cells = [
            _str_cell(row["case_id"]), _str_cell(row["agent"]), _str_cell(row["model"]),
            _int_cell(row["runs"]), _int_cell(row["pass"]), _int_cell(row["task_fail"]),
            _int_cell(row["invalid_submission"]), _int_cell(row["infra_error"]),
            _int_cell(row["incomplete"]), _rate_cell(row),
            str(row["median_minutes"]) if row["median_minutes"] is not None else "-",
            _int_cell(row["median_output_tokens"]), _int_cell(row["median_input_tokens"]),
            _int_cell(row["median_tool_calls"]),
        ]
        lines.append("| " + " | ".join(cells) + " |\n")
    return "".join(lines) + "\n" + NOTES + "\n"


def _filter(rows, cases, agents):
    if cases:
        rows = [r for r in rows if r["case_id"] in cases]
    if agents:
        rows = [r for r in rows if r["agent"] in agents]
    return rows


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True, help="directory holding the run manifests")
    parser.add_argument("--case", action="append", default=[], metavar="ID", help="repeatable case id")
    parser.add_argument("--agent", action="append", default=[], metavar="NAME", help="repeatable agent name")
    parser.add_argument("--format", choices=("md", "json"), default="md", help="output format; default md")
    parser.add_argument("--out", default=None, help="write output to this file instead of stdout")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        print("runs dir does not exist: " + str(runs_dir), file=sys.stderr)
        return 2

    rows = _filter(summarize(load_runs(runs_dir)), args.case, args.agent)
    if not rows:
        text = "no runs\n"
    elif args.format == "json":
        text = json.dumps(rows, indent=2)
    else:
        text = to_markdown(rows)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
