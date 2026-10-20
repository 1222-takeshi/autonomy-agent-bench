"""Pure file-tree helpers shared by the runner core.

This module never launches a process or a container: it extracts hash-pinned
bundle tars, snapshots a workspace, diffs two snapshots, decides whether an edit
is inside the editable paths, and builds/applies a text patch. It is the
low-level material ``bench.run`` needs to rebuild a clean grading workspace from
the agent's accepted changes. All helpers are plain functions and stay side
effect free apart from the files they are explicitly asked to read or write.
"""
import difflib
import hashlib
import os
import shutil
import stat
import tarfile
from pathlib import Path

IGNORED_TOP = frozenset({"build", "install", "log"})   # colcon outputs, ignored only at the workspace root
IGNORED_ANY = frozenset({".git"})                       # ignored at any depth


class UnsafeBundle(ValueError):
    """A tar member would let the bundle escape the extraction root."""


def sha256_file(path) -> str:
    """Lowercase hex sha256 over the raw bytes of ``path``.

    Propagates ``OSError`` for a missing or unreadable file so the caller can
    distinguish a changed bundle from a merely absent one.
    """
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strip_top(name, top):
    """Return the member path relative to ``top`` (or None for ``top`` itself)."""
    if name.startswith("./"):
        name = name[2:]
    if name == top:
        return None
    return name[len(top) + 1:].rstrip("/")


def _validate_member(member, top):
    name = member.name
    if name.startswith("./"):
        name = name[2:]
    if name.startswith("/") or ".." in name.split("/"):
        raise UnsafeBundle("unsafe bundle member: " + member.name)
    if name != top and not name.startswith(top + "/"):
        raise UnsafeBundle("unsafe bundle member: " + member.name)
    if not (member.isfile() or member.isdir()):
        raise UnsafeBundle("unsafe bundle member: " + member.name)


def safe_extract(tar_path, dest, top) -> None:
    """Extract ``tar_path`` into the empty directory ``dest`` under ``top``.

    Every member is validated before anything is written, so an unsafe bundle
    leaves ``dest`` untouched. The leading ``top/`` is stripped and file modes
    collapse to 0o755 (any execute bit) or 0o644; ownership and mtimes are not
    restored. ``dest`` must exist and be empty, otherwise ``ValueError``.
    """
    if not os.path.isdir(str(dest)) or os.listdir(str(dest)):
        raise ValueError("dest must be an existing empty directory")
    with tarfile.open(str(tar_path), "r:*") as tar:
        members = tar.getmembers()
        for member in members:
            _validate_member(member, top)
        for member in members:
            rel = _strip_top(member.name, top)
            if rel is None:
                continue
            dest_path = os.path.join(str(dest), *rel.split("/"))
            if member.isdir():
                os.makedirs(dest_path, mode=0o755, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest_path), mode=0o755, exist_ok=True)
                payload = tar.extractfile(member)
                if payload is None:
                    continue
                with payload as source, open(dest_path, "wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(dest_path, 0o755 if member.mode & 0o111 else 0o644)


def _rel(root, path):
    return os.path.relpath(path, str(root)).replace(os.sep, "/")


def _classify_entry(full):
    try:
        mode = os.lstat(full).st_mode
    except OSError:
        return "special"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        try:
            return sha256_file(full)
        except OSError:
            return "special"
    return "special"


def snapshot(root) -> dict:
    """Snapshot ``root`` as {posix relpath: sha256|'symlink'|'special'}.

    Directories named ``IGNORED_TOP`` are skipped only at the workspace root;
    ``IGNORED_ANY`` directories are skipped at any depth. Directory symlinks are
    recorded but never followed; empty directories do not appear.
    """
    root = Path(root)
    result = {}
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        at_root = os.path.relpath(dirpath, str(root)) == "."
        kept = [d for d in dirnames
                if d not in IGNORED_ANY and not (at_root and d in IGNORED_TOP)]
        real_dirs = []
        for d in kept:
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                result[_rel(root, full)] = "symlink"
            else:
                real_dirs.append(d)
        dirnames[:] = real_dirs
        for name in filenames:
            result[_rel(root, os.path.join(dirpath, name))] = _classify_entry(
                os.path.join(dirpath, name))
    return result


def diff_snapshots(before, after) -> dict:
    """Split two snapshots into sorted added / modified / deleted key lists."""
    before_keys, after_keys = set(before), set(after)
    modified = sorted(k for k in before_keys if k in after_keys and before[k] != after[k])
    return {
        "added": sorted(after_keys - before_keys),
        "modified": modified,
        "deleted": sorted(before_keys - after_keys),
    }


def is_editable(path, editable_paths) -> bool:
    """True iff ``path`` equals or sits under any ``editable_paths`` entry.

    A trailing slash on an entry is ignored, and ``src/pkg/srcfoo`` is not
    considered under ``src/pkg/src``.
    """
    for entry in editable_paths:
        base = entry.rstrip("/")
        if not base:
            continue
        if path == base or path.startswith(base + "/"):
            return True
    return False


def check_editable(changes, after, editable_paths) -> list:
    """Sorted unique paths that edit outside ``editable_paths`` or are links.

    A violation is any changed path outside the editable set, or any path whose
    value is ``symlink``/``special`` in the workspace (even if untouched).
    """
    violations = set()
    for path in changes["added"] + changes["modified"] + changes["deleted"]:
        if not is_editable(path, editable_paths):
            violations.add(path)
    for path, value in after.items():
        if value == "symlink" or value == "special":
            violations.add(path)
    return sorted(violations)


def _read_bytes(path):
    with open(str(path), "rb") as handle:
        return handle.read()


def _valid_utf8(data) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _nonregular(path) -> bool:
    try:
        return not stat.S_ISREG(os.lstat(str(path)).st_mode)
    except OSError:
        return False


def _diff_lines(old_lines, new_lines, fromfile, tofile):
    out = []
    for line in difflib.unified_diff(old_lines, new_lines, fromfile, tofile):
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    return out


def make_patch(base_root, workspace, changes) -> str:
    """Deterministic unified text patch describing ``changes`` (base -> workspace).

    Paths are processed in sorted order; the empty change set yields "". Each
    non-regular workspace entry yields ``Non-regular file <path>``, invalid
    UTF-8 yields the ``Binary files ... differ`` line, otherwise a unified diff
    is produced with ``/dev/null`` on the missing side.
    """
    added, modified, deleted = set(changes["added"]), set(changes["modified"]), set(changes["deleted"])
    paths = sorted(added | modified | deleted)
    if not paths:
        return ""
    out = []
    for path in paths:
        if path in added:
            if _nonregular(workspace / path):
                out.append("Non-regular file %s\n" % path)
                continue
            if not _valid_utf8(_read_bytes(workspace / path)):
                out.append("Binary files /dev/null and b/%s differ\n" % path)
                continue
            new_lines = _read_bytes(workspace / path).decode("utf-8").splitlines(keepends=True)
            out.extend(_diff_lines([], new_lines, "/dev/null", "b/" + path))
        elif path in deleted:
            old_bytes = _read_bytes(base_root / path)
            if not _valid_utf8(old_bytes):
                out.append("Binary files a/%s and /dev/null differ\n" % path)
                continue
            old_lines = old_bytes.decode("utf-8").splitlines(keepends=True)
            out.extend(_diff_lines(old_lines, [], "a/" + path, "/dev/null"))
        else:
            if _nonregular(workspace / path):
                out.append("Non-regular file %s\n" % path)
                continue
            old_bytes, new_bytes = _read_bytes(base_root / path), _read_bytes(workspace / path)
            if not _valid_utf8(old_bytes) or not _valid_utf8(new_bytes):
                out.append("Binary files a/%s and b/%s differ\n" % (path, path))
                continue
            old_lines = old_bytes.decode("utf-8").splitlines(keepends=True)
            new_lines = new_bytes.decode("utf-8").splitlines(keepends=True)
            out.extend(_diff_lines(old_lines, new_lines, "a/" + path, "b/" + path))
    return "".join(out)


def apply_changes(workspace, changes, dest) -> None:
    """Apply accepted additions/modifications to ``dest`` and remove deletions.

    Source must be a regular file; a symlink or other non-regular entry raises
    ``ValueError`` so callers need only run ``check_editable`` beforehand.
    """
    for path in changes["added"] + changes["modified"]:
        source = workspace / path
        if _nonregular(source):
            raise ValueError("cannot apply non-regular file: " + path)
        dest_path = dest / path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(dest_path), "wb") as out:
            out.write(_read_bytes(source))
        os.chmod(dest_path, 0o755 if os.lstat(str(source)).st_mode & 0o111 else 0o644)
    for path in changes["deleted"]:
        dest_path = dest / path
        if os.path.lexists(str(dest_path)):
            os.remove(str(dest_path))