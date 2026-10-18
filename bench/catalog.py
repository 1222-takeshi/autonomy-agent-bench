"""Static catalog validation; this module never executes a case or a model."""
import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath

TRACKS = {"algorithm_debug": "ALG", "integration_debug": "INT", "feature_implementation": "FEAT"}
HEX40, HEX64 = r"[0-9a-f]{40}", r"[0-9a-f]{64}"
FIELDS = {
    "catalog": "schema_version cases",
    "case": "id track status title issue source runtime artifacts controls limits editable_paths acceptance",
    "source": "repository revision kind license",
    "runtime": "image ros_distro rmw platform network",
    "artifacts": "bundle_sha256 verification_sha256",
    "controls": "baseline reference wrong_patch regression repetitions",
    "limits": "wall_seconds output_tokens tool_calls",
    "acceptance": "normal edge invalid regression",
}


def validate(document, require_ready=False):
    errors, seen = [], set()
    if not isinstance(document, dict):
        return ["catalog must be an object"]

    def check_fields(value, kind, label):
        if set(value).difference(FIELDS[kind].split()):
            errors.append(f"{label} contains unknown fields")

    check_fields(document, "catalog", "catalog")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        errors.append("schema_version must be integer 1")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        return errors + ["cases must be a nonempty array"]
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        check_fields(case, "case", prefix)

        def check(condition, field):
            if not condition:
                errors.append(f"{prefix}.{field} is invalid or incomplete")

        def section(name):
            value = case.get(name)
            check(isinstance(value, dict), name)
            if isinstance(value, dict):
                check_fields(value, name, f"{prefix}.{name}")
            return value if isinstance(value, dict) else {}

        def pattern(value, expression, field, required=False):
            check(value is None and not required or isinstance(value, str) and re.fullmatch(expression, value), field)

        ready = case.get("status") == "ready"
        check(case.get("status") in ("draft", "ready"), "status")
        check(not require_ready or ready, "status (ready required)")
        track = case.get("track")
        check(isinstance(track, str) and track in TRACKS, "track")
        identifier = case.get("id")
        expected = TRACKS.get(track, "INVALID") if isinstance(track, str) else "INVALID"
        pattern(identifier, expected + r"-[0-9]{3}", "id", True)
        if isinstance(identifier, str):
            check(identifier not in seen, "id (duplicate)")
            seen.add(identifier)
        check(isinstance(case.get("title"), str) and case["title"].strip(), "title")
        pattern(case.get("issue"), r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*", "issue", True)
        source, runtime, artifacts, controls = (section(name) for name in ("source", "runtime", "artifacts", "controls"))
        pattern(source.get("repository"), r"https://github.com/[\w.-]+/[\w.-]+", "source.repository", True)
        pattern(source.get("revision"), HEX40, "source.revision", ready)
        kinds = ("historical", "injected", "feature")
        check(source.get("kind") in (kinds if ready else (*kinds, "candidate")), "source.kind")
        license_id = source.get("license")
        check(license_id is None and not ready or isinstance(license_id, str) and license_id.strip(), "source.license")
        for field, expression in {"image": r"[\w./:-]+@sha256:" + HEX64, "ros_distro": r"[a-z][a-z0-9_]*", "rmw": r"rmw_[a-z0-9_]+"}.items():
            pattern(runtime.get(field), expression, "runtime." + field, ready)
        for field, allowed in {"platform": ("linux/amd64", "linux/arm64"), "network": ("none", "internal")}.items():
            check(runtime.get(field) in allowed or runtime.get(field) is None and not ready, "runtime." + field)
        for field in ("bundle_sha256", "verification_sha256"):
            pattern(artifacts.get(field), HEX64, "artifacts." + field, ready)
        for field, expected in {"baseline": "fail", "reference": "pass", "wrong_patch": "fail", "regression": "pass"}.items():
            value = controls.get(field)
            check(value == expected if ready else value in (None, "pass", "fail"), "controls." + field)
        repetitions = controls.get("repetitions")
        check(repetitions is None and not ready or type(repetitions) is int and repetitions >= (3 if ready else 1), "controls.repetitions")
        limits = case.get("limits")
        check(isinstance(limits, dict) or limits is None and not ready, "limits")
        if isinstance(limits, dict):
            check_fields(limits, "limits", f"{prefix}.limits")
            for field in ("wall_seconds", "output_tokens", "tool_calls"):
                check(type(limits.get(field)) is int and limits[field] > 0, "limits." + field)
        paths = case.get("editable_paths")
        check(isinstance(paths, list) and (bool(paths) or not ready), "editable_paths")
        if isinstance(paths, list):
            for path in paths:
                valid = isinstance(path, str) and re.fullmatch(r"[A-Za-z0-9_./+-]+", path)
                check(valid and not PurePosixPath(path).is_absolute() and path != "." and not {"..", ".git"}.intersection(PurePosixPath(path).parts) and str(PurePosixPath(path)) == path, "editable_paths entry")
        acceptance = section("acceptance")
        for field in ("normal", "edge", "invalid", "regression"):
            check(isinstance(acceptance.get(field), str) and acceptance[field].strip(), "acceptance." + field)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--require-ready", action="store_true", help="require ready metadata for every selected case")
    args = parser.parse_args()
    try:
        document = json.loads(args.catalog.read_text(encoding="utf-8"))
        errors = validate(document, args.require_ready)
    except (OSError, ValueError) as error:
        errors = [str(error)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 2
    ready = sum(case["status"] == "ready" for case in document["cases"])
    print(f"validated {len(document['cases'])} cases ({ready} ready); metadata only, execution not checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
