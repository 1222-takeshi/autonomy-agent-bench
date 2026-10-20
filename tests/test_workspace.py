import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

import bench.workspace as ws

TOP = "ALG-001"


def _write(root, rel, data=b"x", mode=None):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _file(name, data=b"", mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    return info, io.BytesIO(data)


def _special(name, type_, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = type_
    info.linkname = linkname
    return info, None


def _dir(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info, None


def _mode(path):
    return os.stat(path).st_mode & 0o777


class WorkspaceTestCase(unittest.TestCase):
    def tmp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def make_tar(self, members):
        path = self.tmp() / "bundle.tar"
        with tarfile.open(path, "w") as tar:
            for info, data in members:
                tar.addfile(info, data)
        return path

    def extract(self, members, top=TOP):
        tar_path = self.make_tar(members)
        dest = self.tmp() / "dest"
        dest.mkdir()
        ws.safe_extract(tar_path, dest, top)
        return dest

    def assert_unsafe(self, members, top=TOP):
        tar_path = self.make_tar(members)
        dest = self.tmp() / "dest"
        dest.mkdir()
        with self.assertRaises(ws.UnsafeBundle):
            ws.safe_extract(tar_path, dest, top)
        return dest


class Sha256FileTests(WorkspaceTestCase):
    def test_matches_hashlib_lowercase_hex(self):
        path = _write(self.tmp(), "f.bin", b"hello\x00world")
        self.assertEqual(ws.sha256_file(path), hashlib.sha256(b"hello\x00world").hexdigest())

    def test_empty_file(self):
        path = _write(self.tmp(), "e", b"")
        self.assertEqual(ws.sha256_file(str(path)), hashlib.sha256(b"").hexdigest())

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            ws.sha256_file(self.tmp() / "nope")


class SafeExtractTests(WorkspaceTestCase):
    def test_unsafe_bundle_is_a_value_error(self):
        self.assertTrue(issubclass(ws.UnsafeBundle, ValueError))

    def test_happy_path_strips_top_and_sets_modes(self):
        dest = self.extract([
            _dir(TOP), _dir(TOP + "/src"),
            _file(TOP + "/TASK.md", b"task\n", 0o600),
            _file(TOP + "/src/a.cpp", b"\x00\xffbytes", 0o664),
            _file(TOP + "/run.sh", b"#!/bin/sh\n", 0o744),
        ])
        self.assertEqual((dest / "TASK.md").read_bytes(), b"task\n")
        self.assertEqual((dest / "src/a.cpp").read_bytes(), b"\x00\xffbytes")
        self.assertEqual(_mode(dest / "TASK.md"), 0o644)
        self.assertEqual(_mode(dest / "src/a.cpp"), 0o644)
        self.assertEqual(_mode(dest / "run.sh"), 0o755)
        self.assertFalse((dest / TOP).exists())
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["TASK.md", "run.sh", "src"])

    def test_any_execute_bit_gives_755(self):
        dest = self.extract([_file(TOP + "/g.sh", b"x", 0o610)])
        self.assertEqual(_mode(dest / "g.sh"), 0o755)

    def test_parent_dirs_created_without_dir_members(self):
        dest = self.extract([_file(TOP + "/a/b/c.txt", b"deep")])
        self.assertEqual((dest / "a/b/c.txt").read_bytes(), b"deep")

    def test_leading_dot_slash_is_accepted(self):
        dest = self.extract([_dir("./" + TOP), _file("./" + TOP + "/a.txt", b"1")])
        self.assertEqual((dest / "a.txt").read_bytes(), b"1")

    def test_top_member_alone_creates_nothing(self):
        dest = self.extract([_dir(TOP)])
        self.assertEqual(list(dest.iterdir()), [])

    def test_member_outside_top_is_unsafe(self):
        self.assert_unsafe([_file("other/a.txt", b"1")])

    def test_sibling_prefix_of_top_is_unsafe(self):
        self.assert_unsafe([_file(TOP + "-evil/a.txt", b"1")])

    def test_dotdot_member_is_unsafe(self):
        self.assert_unsafe([_file(TOP + "/../escape.txt", b"1")])

    def test_absolute_member_is_unsafe(self):
        self.assert_unsafe([_file("/etc/evil", b"1")])

    def test_symlink_member_is_unsafe(self):
        self.assert_unsafe([_special(TOP + "/l", tarfile.SYMTYPE, "/etc/passwd")])

    def test_hardlink_member_is_unsafe(self):
        self.assert_unsafe([_file(TOP + "/a", b"1"), _special(TOP + "/h", tarfile.LNKTYPE, TOP + "/a")])

    def test_fifo_member_is_unsafe(self):
        self.assert_unsafe([_special(TOP + "/p", tarfile.FIFOTYPE)])

    def test_device_member_is_unsafe(self):
        self.assert_unsafe([_special(TOP + "/d", tarfile.CHRTYPE)])

    def test_unsafe_member_after_valid_file_writes_nothing(self):
        dest = self.assert_unsafe([
            _file(TOP + "/good.txt", b"ok"),
            _special(TOP + "/bad", tarfile.SYMTYPE, "x"),
        ])
        self.assertEqual(list(dest.iterdir()), [])

    def test_non_empty_dest_raises_value_error(self):
        tar_path = self.make_tar([_file(TOP + "/a", b"1")])
        dest = self.tmp()
        _write(dest, "existing", b"1")
        with self.assertRaises(ValueError):
            ws.safe_extract(tar_path, dest, TOP)
        self.assertEqual([p.name for p in dest.iterdir()], ["existing"])

    def test_missing_dest_raises_value_error(self):
        tar_path = self.make_tar([_file(TOP + "/a", b"1")])
        with self.assertRaises(ValueError):
            ws.safe_extract(tar_path, self.tmp() / "missing", TOP)


class SnapshotTests(WorkspaceTestCase):
    def test_regular_files_hashed_with_posix_keys(self):
        root = self.tmp()
        _write(root, "a.txt", b"A")
        _write(root, "src/pkg/b.txt", b"B")
        self.assertEqual(ws.snapshot(root), {
            "a.txt": hashlib.sha256(b"A").hexdigest(),
            "src/pkg/b.txt": hashlib.sha256(b"B").hexdigest(),
        })

    def test_symlinks_are_marked_and_not_followed(self):
        root = self.tmp()
        _write(root, "real/inner.txt", b"I")
        _write(root, "f.txt", b"F")
        os.symlink("f.txt", root / "link_file")
        os.symlink("real", root / "link_dir")
        snap = ws.snapshot(root)
        self.assertEqual(snap["link_file"], "symlink")
        self.assertEqual(snap["link_dir"], "symlink")
        self.assertNotIn("link_dir/inner.txt", snap)
        self.assertEqual(sorted(snap), ["f.txt", "link_dir", "link_file", "real/inner.txt"])

    def test_dangling_symlink_is_symlink(self):
        root = self.tmp()
        os.symlink("nowhere", root / "dangling")
        self.assertEqual(ws.snapshot(root), {"dangling": "symlink"})

    def test_fifo_is_special(self):
        root = self.tmp()
        os.mkfifo(root / "pipe")
        self.assertEqual(ws.snapshot(root), {"pipe": "special"})

    def test_root_level_colcon_dirs_skipped_but_nested_kept(self):
        root = self.tmp()
        for name in ("build", "install", "log"):
            _write(root, name + "/x", b"1")
        _write(root, "src/build/x", b"2")
        _write(root, "src/log/y", b"3")
        _write(root, "builder/z", b"4")
        self.assertEqual(sorted(ws.snapshot(root)), ["builder/z", "src/build/x", "src/log/y"])

    def test_root_level_file_named_build_is_kept(self):
        root = self.tmp()
        _write(root, "build", b"file")
        self.assertEqual(list(ws.snapshot(root)), ["build"])

    def test_git_skipped_at_any_depth(self):
        root = self.tmp()
        _write(root, ".git/config", b"1")
        _write(root, "src/.git/config", b"2")
        _write(root, "src/keep", b"3")
        _write(root, ".gitignore", b"4")
        self.assertEqual(sorted(ws.snapshot(root)), [".gitignore", "src/keep"])

    def test_empty_dirs_absent_and_empty_root(self):
        root = self.tmp()
        (root / "empty/deeper").mkdir(parents=True)
        self.assertEqual(ws.snapshot(root), {})


class DiffSnapshotsTests(unittest.TestCase):
    def test_added_modified_deleted_sorted(self):
        before = {"b": "1", "a": "1", "same": "1", "gone2": "1", "gone1": "1"}
        after = {"b": "2", "a": "2", "same": "1", "z": "1", "y": "1"}
        self.assertEqual(ws.diff_snapshots(before, after), {
            "added": ["y", "z"], "modified": ["a", "b"], "deleted": ["gone1", "gone2"],
        })

    def test_identical_and_empty(self):
        self.assertEqual(ws.diff_snapshots({}, {}), {"added": [], "modified": [], "deleted": []})
        self.assertEqual(ws.diff_snapshots({"a": "1"}, {"a": "1"}),
                         {"added": [], "modified": [], "deleted": []})

    def test_file_replaced_by_symlink_is_modified(self):
        self.assertEqual(ws.diff_snapshots({"a": "abc"}, {"a": "symlink"})["modified"], ["a"])


class EditableTests(unittest.TestCase):
    def test_exact_and_prefix_match(self):
        editable = ["src/pkg/src"]
        self.assertTrue(ws.is_editable("src/pkg/src", editable))
        self.assertTrue(ws.is_editable("src/pkg/src/a.cpp", editable))
        self.assertTrue(ws.is_editable("src/pkg/src/deep/b.cpp", editable))

    def test_sibling_prefix_and_parent_not_editable(self):
        editable = ["src/pkg/src"]
        self.assertFalse(ws.is_editable("src/pkg/srcfoo/x", editable))
        self.assertFalse(ws.is_editable("src/pkg", editable))
        self.assertFalse(ws.is_editable("TASK.md", editable))

    def test_trailing_slash_on_editable_is_ignored(self):
        self.assertTrue(ws.is_editable("src/a.py", ["src/"]))
        self.assertTrue(ws.is_editable("src", ["src/"]))
        self.assertFalse(ws.is_editable("srcfoo", ["src/"]))

    def test_any_of_several_and_empty_list(self):
        self.assertTrue(ws.is_editable("b/x", ["a", "b"]))
        self.assertFalse(ws.is_editable("a", []))

    def test_check_editable_lists_non_editable_changes_sorted_unique(self):
        changes = {"added": ["z.txt", "src/ok.py"], "modified": ["TASK.md", "z.txt"],
                   "deleted": ["README"]}
        after = {"z.txt": "1", "src/ok.py": "1", "TASK.md": "1"}
        self.assertEqual(ws.check_editable(changes, after, ["src"]),
                         ["README", "TASK.md", "z.txt"])

    def test_check_editable_ok_returns_empty_list(self):
        changes = {"added": ["src/a"], "modified": ["src/b"], "deleted": ["src/c"]}
        self.assertEqual(ws.check_editable(changes, {"src/a": "1", "src/b": "2"}, ["src"]), [])

    def test_symlink_or_special_inside_editable_is_violation(self):
        changes = {"added": ["src/l", "src/p"], "modified": [], "deleted": []}
        after = {"src/l": "symlink", "src/p": "special"}
        self.assertEqual(ws.check_editable(changes, after, ["src"]), ["src/l", "src/p"])

    def test_unchanged_symlink_in_after_is_still_reported(self):
        after = {"src/l": "symlink", "src/ok": "1"}
        no_changes = {"added": [], "modified": [], "deleted": []}
        self.assertEqual(ws.check_editable(no_changes, after, ["src"]), ["src/l"])


class MakePatchTests(WorkspaceTestCase):
    def setUp(self):
        self.base = self.tmp()
        self.work = self.tmp()

    def patch(self, **changes):
        full = {"added": [], "modified": [], "deleted": []}
        full.update(changes)
        return ws.make_patch(self.base, self.work, full)

    def test_no_changes_is_empty_string(self):
        self.assertEqual(self.patch(), "")

    def test_modified_has_headers_and_hunk_lines(self):
        _write(self.base, "src/a.txt", b"one\ntwo\n")
        _write(self.work, "src/a.txt", b"one\nTWO\n")
        out = self.patch(modified=["src/a.txt"])
        self.assertIn("--- a/src/a.txt", out)
        self.assertIn("+++ b/src/a.txt", out)
        self.assertIn("-two\n", out)
        self.assertIn("+TWO\n", out)
        self.assertIn("@@", out)

    def test_added_uses_dev_null_old_side(self):
        _write(self.work, "n.txt", b"new\n")
        out = self.patch(added=["n.txt"])
        self.assertIn("--- /dev/null", out)
        self.assertIn("+++ b/n.txt", out)
        self.assertIn("+new\n", out)

    def test_deleted_uses_dev_null_new_side(self):
        _write(self.base, "d.txt", b"bye\n")
        out = self.patch(deleted=["d.txt"])
        self.assertIn("--- a/d.txt", out)
        self.assertIn("+++ /dev/null", out)
        self.assertIn("-bye\n", out)

    def test_binary_files_line(self):
        _write(self.base, "bin", b"\xff\xfe\x00")
        _write(self.work, "bin", b"\xff\xfe\x01")
        _write(self.work, "new.bin", b"\xff\xff")
        out = self.patch(modified=["bin"], added=["new.bin"])
        self.assertIn("Binary files a/bin and b/bin differ\n", out)
        self.assertIn("Binary files /dev/null and b/new.bin differ\n", out)

    def test_binary_deleted_uses_dev_null_new_side(self):
        _write(self.base, "gone.bin", b"\xff\xfe")
        self.assertIn("Binary files a/gone.bin and /dev/null differ", self.patch(deleted=["gone.bin"]))

    def test_symlink_in_workspace_is_non_regular(self):
        _write(self.base, "l", b"text\n")
        os.symlink("elsewhere", self.work / "l")
        os.symlink("elsewhere", self.work / "added_l")
        out = self.patch(modified=["l"], added=["added_l"])
        self.assertIn("Non-regular file l\n", out)
        self.assertIn("Non-regular file added_l\n", out)

    def test_missing_trailing_newline_marker(self):
        _write(self.base, "t", b"a\nb")
        _write(self.work, "t", b"a\nc")
        out = self.patch(modified=["t"])
        self.assertIn("-b\n\\ No newline at end of file\n", out)
        self.assertIn("+c\n\\ No newline at end of file\n", out)

    def test_paths_processed_in_sorted_order_across_kinds(self):
        _write(self.base, "m.txt", b"1\n")
        _write(self.work, "m.txt", b"2\n")
        _write(self.base, "z_del.txt", b"3\n")
        _write(self.work, "a_add.txt", b"4\n")
        out = self.patch(modified=["m.txt"], added=["a_add.txt"], deleted=["z_del.txt"])
        positions = [out.index(name) for name in ("b/a_add.txt", "b/m.txt", "a/z_del.txt")]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(out, self.patch(deleted=["z_del.txt"], added=["a_add.txt"], modified=["m.txt"]))


class ApplyChangesTests(WorkspaceTestCase):
    def test_copies_added_and_modified_with_parent_dirs(self):
        work, dest = self.tmp(), self.tmp()
        _write(work, "deep/new/a.bin", b"\x00\x01new")
        _write(work, "m.txt", b"after")
        _write(dest, "m.txt", b"before")
        ws.apply_changes(work, {"added": ["deep/new/a.bin"], "modified": ["m.txt"], "deleted": []}, dest)
        self.assertEqual((dest / "deep/new/a.bin").read_bytes(), b"\x00\x01new")
        self.assertEqual((dest / "m.txt").read_bytes(), b"after")

    def test_execute_bit_preserved_other_files_644(self):
        work, dest = self.tmp(), self.tmp()
        _write(work, "run.sh", b"#!/bin/sh\n", 0o755)
        _write(work, "plain", b"p", 0o600)
        ws.apply_changes(work, {"added": ["run.sh", "plain"], "modified": [], "deleted": []}, dest)
        self.assertEqual(_mode(dest / "run.sh"), 0o755)
        self.assertEqual(_mode(dest / "plain"), 0o644)

    def test_deleted_removed_and_missing_ignored(self):
        work, dest = self.tmp(), self.tmp()
        _write(dest, "gone.txt", b"x")
        _write(dest, "keep.txt", b"y")
        ws.apply_changes(work, {"added": [], "modified": [], "deleted": ["gone.txt", "never.txt"]}, dest)
        self.assertFalse((dest / "gone.txt").exists())
        self.assertTrue((dest / "keep.txt").exists())

    def test_symlink_source_raises_value_error(self):
        work, dest = self.tmp(), self.tmp()
        os.symlink("/etc/passwd", work / "l")
        with self.assertRaises(ValueError):
            ws.apply_changes(work, {"added": ["l"], "modified": [], "deleted": []}, dest)
        self.assertFalse(os.path.lexists(dest / "l"))

    def test_fifo_source_raises_value_error(self):
        work, dest = self.tmp(), self.tmp()
        os.mkfifo(work / "p")
        with self.assertRaises(ValueError):
            ws.apply_changes(work, {"added": [], "modified": ["p"], "deleted": []}, dest)


if __name__ == "__main__":
    unittest.main()
