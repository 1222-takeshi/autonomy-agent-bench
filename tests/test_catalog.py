import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bench.catalog import validate

ROOT = Path(__file__).resolve().parents[1]


def fixture(ready=False):
    case = copy.deepcopy(json.loads((ROOT / "cases/catalog.json").read_text())["cases"][0])
    if ready:
        case.update(status="ready", editable_paths=["src/trajectory.cpp"], limits={"wall_seconds":60,"output_tokens":4096,"tool_calls":8})
        case["source"].update(revision="a"*40, kind="injected", license="Apache-2.0")
        case["runtime"].update(image="example/ros@sha256:"+"b"*64, ros_distro="jazzy", rmw="rmw_cyclonedds_cpp", platform="linux/arm64", network="none")
        case["artifacts"].update(bundle_sha256="c"*64, verification_sha256="d"*64)
        case["controls"].update(baseline="fail", reference="pass", wrong_patch="fail", regression="pass", repetitions=3)
    return {"schema_version":1,"cases":[case]}


class CatalogTests(unittest.TestCase):
    def test_draft_and_ready_metadata(self):
        self.assertEqual(validate(fixture()), [])
        self.assertTrue(validate(fixture(), require_ready=True))
        self.assertEqual(validate(fixture(True), require_ready=True), [])

    def test_catalog_has_three_draft_tracks(self):
        catalog = json.loads((ROOT / "cases/catalog.json").read_text())
        self.assertEqual(validate(catalog), [])
        self.assertEqual({c["track"] for c in catalog["cases"]}, {"algorithm_debug","integration_debug","feature_implementation"})
        self.assertTrue(all(c["status"] == "draft" for c in catalog["cases"]))

    def test_invalid_structure_and_duplicate(self):
        for document in [[], {}, {"schema_version":True,"cases":[]}, {"schema_version":1,"cases":[None]}]:
            with self.subTest(document=document):
                self.assertTrue(validate(document))
        document = fixture()
        document["cases"] *= 2
        self.assertTrue(any("duplicate" in e for e in validate(document)))

    def test_case_identity_and_status(self):
        for field, value in [("id", "ALG-００１"), ("id", "INT-001"),
                             ("track", "unknown"), ("status", "complete"),
                             ("title", " "), ("issue", "https://example.com/issues/1")]:
            with self.subTest(field=field, value=value):
                document = fixture()
                document["cases"][0][field] = value
                self.assertTrue(validate(document))

    def test_ready_rejects_unpinned_or_unverified_cases(self):
        mutations = [("source","revision","main"),("source","kind","candidate"),("source","license",None),
                     ("runtime","image","ros:jazzy"),("runtime","network","host"),("runtime","platform","native"),
                     ("artifacts","bundle_sha256",None),("artifacts","verification_sha256","x"*64),
                     ("controls","baseline","pass"),("controls","reference","fail"),("controls","wrong_patch","pass"),
                     ("controls","regression","fail"),("controls","repetitions",2),("controls","repetitions",True),
                     ("limits","wall_seconds",0),("limits","output_tokens",True),("acceptance","edge","")]
        for section, field, value in mutations:
            with self.subTest(section=section, field=field, value=value):
                document = fixture(True)
                document["cases"][0][section][field] = value
                self.assertTrue(validate(document, True))

    def test_bad_types_and_edit_boundaries(self):
        for section in ["source","runtime","artifacts","controls","acceptance"]:
            document = fixture()
            document["cases"][0][section] = []
            self.assertTrue(validate(document))
        for path in ["../outside", "/tmp/file", ".git/config", "a/../../outside", "a\\b", "a//b", ".", ""]:
            document = fixture(True)
            document["cases"][0]["editable_paths"] = [path]
            self.assertTrue(validate(document), path)
        document = fixture()
        document["cases"][0]["track"] = []
        self.assertTrue(validate(document))

    def test_cli_validation_and_release_gate(self):
        for options, expected in [([],0), (["--require-ready"],2)]:
            result = subprocess.run([sys.executable,"-m","bench.catalog","cases/catalog.json",*options],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,expected,result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"broken.json"
            result = subprocess.run([sys.executable,"-m","bench.catalog",str(path)],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            path.write_text("{")
            result = subprocess.run([sys.executable,"-m","bench.catalog",str(path)],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,2)


if __name__ == "__main__":
    unittest.main()
