"""Tests for edit validation, application, and candidate tar rebuilding."""

import hashlib
import io
import json
import tarfile
import unittest
import zipfile

from upgrade_chamber.baseline import COMMIT_SHA
from upgrade_chamber.edits import (
    MAX_PATCH_FILES,
    MAX_REPLACEMENT_BYTES,
    ROOT,
    EditValidationError,
    apply_edits,
    changed_file_diffs,
    load_source_zip,
    member_name,
    rebuild_candidate_tar,
    validate_edits,
)
from upgrade_chamber.runner import _validate_attempt_archive


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar_bytes(members: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), content)
    return output.getvalue()


FILES = {
    "requests_unixsocket/__init__.py": b"from . import adapters\n",
    "requests_unixsocket/adapters.py": b"HTTPAdapter = object\nUNIXSocketAdapter = object\n",
    "requests_unixsocket/tests/test_requests_unixsocket.py": b"def test_placeholder():\n    assert True\n",
    "setup.py": b"from setuptools import setup\nsetup()\n",
}
ADAPTERS_PATH = "requests_unixsocket/adapters.py"
TEST_FILE_PATH = "requests_unixsocket/tests/test_requests_unixsocket.py"


def source_zip() -> bytes:
    return _zip_bytes([(member_name(path), content) for path, content in FILES.items()])


def candidate_tar(source: bytes) -> bytes:
    manifest = {
        "schema_version": 1,
        "profile_id": "requests-unixsocket-historical-0.3.0-research",
        "commit_sha": COMMIT_SHA,
        "phase": "candidate",
        "requests_version": "2.34.2",
        "source_sha256": _sha256(source),
        "requirements_sha256": _sha256(b"requests==2.34.2\n"),
        "wheels": {"requests-2.34.2-py3-none-any.whl": "a" * 64},
    }
    return _tar_bytes([
        ("source.zip", source),
        ("requirements.txt", b"requests==2.34.2\n"),
        ("manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8")),
        ("wheels/requests.whl", b"wheel"),
    ])


def edit_for(path: str, replacement: str) -> dict:
    return {
        "path": path,
        "original_sha256": _sha256(FILES[path]),
        "replacement_text": replacement,
    }


class EditsTest(unittest.TestCase):
    def setUp(self):
        self.zip = source_zip()
        self.tar = candidate_tar(self.zip)

    def test_member_name_uses_fixed_root(self):
        self.assertEqual(member_name("setup.py"), f"requests-unixsocket-{COMMIT_SHA}/setup.py")

    def test_load_source_zip_returns_zip(self):
        self.assertEqual(load_source_zip(self.tar), self.zip)

    def test_load_source_zip_missing_member(self):
        with self.assertRaisesRegex(EditValidationError, "source.zip"):
            load_source_zip(_tar_bytes([("requirements.txt", b"requests==2.34.2\n")]))

    def test_validate_and_apply_round_trip(self):
        replacement = "HTTPAdapter = object\nUNIXSocketAdapter = object  # repaired\n"
        edits = [edit_for(ADAPTERS_PATH, replacement)]
        self.assertEqual(validate_edits(self.zip, edits), edits)
        new_zip, new_sha = apply_edits(self.zip, edits)
        self.assertNotEqual(new_sha, _sha256(self.zip))
        self.assertEqual(new_sha, _sha256(new_zip))
        with zipfile.ZipFile(io.BytesIO(new_zip)) as rebuilt:
            self.assertEqual(
                rebuilt.read(member_name(ADAPTERS_PATH)), replacement.encode("utf-8")
            )
            self.assertEqual(rebuilt.read(member_name(TEST_FILE_PATH)), FILES[TEST_FILE_PATH])
            self.assertEqual(rebuilt.read(member_name("setup.py")), FILES["setup.py"])

    def test_rejects_disallowed_test_path(self):
        with self.assertRaisesRegex(EditValidationError, "allowed"):
            validate_edits(self.zip, [edit_for(TEST_FILE_PATH, "def test_x():\n    pass\n")])

    def test_rejects_traversal_path(self):
        traversal = {
            "path": "../setup.py",
            "original_sha256": _sha256(FILES["setup.py"]),
            "replacement_text": "setup()\n",
        }
        with self.assertRaisesRegex(EditValidationError, "allowed"):
            validate_edits(self.zip, [traversal])

    def test_rejects_duplicate_paths(self):
        edits = [edit_for(ADAPTERS_PATH, "a\n"), edit_for(ADAPTERS_PATH, "b\n")]
        with self.assertRaisesRegex(EditValidationError, "duplicate"):
            validate_edits(self.zip, edits)

    def test_rejects_wrong_original_hash(self):
        edit = edit_for(ADAPTERS_PATH, "replacement\n")
        edit["original_sha256"] = "0" * 64
        with self.assertRaisesRegex(EditValidationError, "does not match"):
            validate_edits(self.zip, [edit])

    def test_rejects_empty_replacement(self):
        with self.assertRaisesRegex(EditValidationError, "empty"):
            validate_edits(self.zip, [edit_for(ADAPTERS_PATH, "")])

    def test_rejects_oversized_replacement(self):
        oversized = "a" * (MAX_REPLACEMENT_BYTES + 1)
        with self.assertRaisesRegex(EditValidationError, "exceeds"):
            validate_edits(self.zip, [edit_for(ADAPTERS_PATH, oversized)])

    def test_rejects_nul_byte(self):
        with self.assertRaisesRegex(EditValidationError, "NUL"):
            validate_edits(self.zip, [edit_for(ADAPTERS_PATH, "a\x00b\n")])

    def test_rejects_non_utf8_replacement(self):
        with self.assertRaisesRegex(EditValidationError, "UTF-8"):
            validate_edits(self.zip, [edit_for(ADAPTERS_PATH, "unpaired\udcffsurrogate\n")])

    def test_rejects_too_many_changed_lines(self):
        huge = "".join(f"line {index}\n" for index in range(201))
        with self.assertRaisesRegex(EditValidationError, "more than 200 lines"):
            validate_edits(self.zip, [edit_for(ADAPTERS_PATH, huge)])

    def test_accepts_exactly_max_changed_lines(self):
        boundary = "".join(f"line {index}\n" for index in range(198))
        edits = [edit_for(ADAPTERS_PATH, boundary)]
        self.assertEqual(validate_edits(self.zip, edits), edits)

    def test_rejects_non_list_edits(self):
        edit = edit_for(ADAPTERS_PATH, "replacement\n")
        for value in (dict(edit), json.dumps(edit), "edits"):
            with self.assertRaisesRegex(EditValidationError, "list"):
                validate_edits(self.zip, value)

    def test_rejects_entry_with_extra_keys(self):
        edit = edit_for(ADAPTERS_PATH, "replacement\n")
        edit["extra"] = "key"
        with self.assertRaisesRegex(EditValidationError, "exactly"):
            validate_edits(self.zip, [edit])

    def test_rejects_empty_edit_list(self):
        with self.assertRaisesRegex(EditValidationError, "list of 1"):
            validate_edits(self.zip, [])

    def test_rejects_more_than_max_patch_files(self):
        # Six entries exceed MAX_PATCH_FILES; the length bound fires before the
        # duplicate check could.
        edits = [edit_for(ADAPTERS_PATH, "a\n"), edit_for("setup.py", "b\n")]
        edits.extend(dict(edit) for edit in edits * 2)
        self.assertEqual(len(edits), MAX_PATCH_FILES + 1)
        with self.assertRaisesRegex(EditValidationError, "list of 1 to 5"):
            validate_edits(self.zip, edits)

    def test_changed_file_diffs_names_both_sides(self):
        new_zip, _ = apply_edits(
            self.zip, [edit_for(ADAPTERS_PATH, "HTTPAdapter = replaced\n")]
        )
        diff = changed_file_diffs(self.zip, new_zip)
        name = member_name(ADAPTERS_PATH)
        self.assertIn(f"--- {name}@original", diff)
        self.assertIn(f"+++ {name}@repaired", diff)
        self.assertIn("-HTTPAdapter = object", diff)
        self.assertIn("+HTTPAdapter = replaced", diff)

    def test_changed_file_diffs_empty_for_identical(self):
        self.assertEqual(changed_file_diffs(self.zip, self.zip), "")

    def test_rebuild_candidate_tar_updates_manifest(self):
        new_zip, new_sha = apply_edits(
            self.zip, [edit_for(ADAPTERS_PATH, "HTTPAdapter = replaced\n")]
        )
        rebuilt = rebuild_candidate_tar(self.tar, new_zip, new_sha)
        _validate_attempt_archive(rebuilt)  # must not raise
        with tarfile.open(fileobj=io.BytesIO(rebuilt), mode="r:") as archive:
            contents = {member.name: archive.extractfile(member).read() for member in archive}
        manifest = json.loads(contents["manifest.json"])
        self.assertEqual(manifest["source_sha256"], new_sha)
        original_manifest = json.loads(
            [content for name, content in _tar_members(self.tar) if name == "manifest.json"][0]
        )
        self.assertEqual(
            set(manifest), set(original_manifest)
        )
        for key, value in original_manifest.items():
            if key != "source_sha256":
                self.assertEqual(manifest[key], value)
        self.assertEqual(contents["source.zip"], new_zip)
        self.assertEqual(contents["requirements.txt"], b"requests==2.34.2\n")
        self.assertEqual(contents["wheels/requests.whl"], b"wheel")

    def test_rebuild_candidate_tar_rejects_invalid_result(self):
        broken = _tar_bytes([
            ("source.zip", self.zip),
            ("requirements.txt", b"x"),
            ("manifest.json", b"{}"),
            ("not-a-wheel.txt", b"y"),
        ])
        new_zip, new_sha = apply_edits(
            self.zip, [edit_for(ADAPTERS_PATH, "HTTPAdapter = replaced\n")]
        )
        with self.assertRaisesRegex(EditValidationError, "failed validation"):
            rebuild_candidate_tar(broken, new_zip, new_sha)


def _tar_members(data: bytes) -> list[tuple[str, bytes]]:
    members = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            members.append((member.name, archive.extractfile(member).read()))
    return members


if __name__ == "__main__":
    unittest.main()
