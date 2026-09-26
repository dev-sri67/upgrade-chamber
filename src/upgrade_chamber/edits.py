"""Server-side validation and application of model-proposed source edits.

Pure functions over bytes: validate a model's proposed edit list against the
fixed patch contract, apply accepted edits to the staged source zip, rebuild
the attempt input tar, and produce deterministic diff text. No I/O, no
inference, stdlib only.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
import tarfile
import zipfile

from .baseline import COMMIT_SHA
from .runner import _validate_attempt_archive


ROOT = f"requests-unixsocket-{COMMIT_SHA}"
ALLOWED_EDIT_PATHS = (
    "requests_unixsocket/adapters.py",
    "requests_unixsocket/__init__.py",
    "requests_unixsocket/monkeypatch.py",
    "setup.py",
)
MAX_PATCH_FILES = 5
MAX_CHANGED_LINES = 200
MAX_REPLACEMENT_BYTES = 256 * 1024
_SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")


class EditValidationError(ValueError):
    """A proposed edit violates the fixed patch contract."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_source_zip(candidate_tar: bytes) -> bytes:
    """Return the bytes of the "source.zip" member of an attempt input tar."""
    with tarfile.open(fileobj=io.BytesIO(candidate_tar), mode="r:") as archive:
        for member in archive:
            if member.name == "source.zip" and member.isfile():
                content = archive.extractfile(member)
                if content is not None:
                    return content.read()
    raise EditValidationError("Attempt input is missing source.zip")


def member_name(path: str) -> str:
    """Return the full source-zip member name for a repository-relative path."""
    return f"{ROOT}/{path}"


def _read_member(source_zip: bytes, path: str) -> bytes:
    name = member_name(path)
    with zipfile.ZipFile(io.BytesIO(source_zip)) as archive:
        try:
            return archive.read(name)
        except KeyError:
            raise EditValidationError(f"Edit path is not present in source: {path}") from None


def _member_text(source_zip: bytes, path: str) -> str:
    """Decode one member as UTF-8 text; binary content cannot receive edits."""
    try:
        return _read_member(source_zip, path).decode("utf-8")
    except UnicodeDecodeError:
        raise EditValidationError(f"Current content of {path} is not UTF-8 text") from None


def _validate_replacement_text(prefix: str, replacement: object) -> str:
    if not isinstance(replacement, str):
        raise EditValidationError(f"{prefix} replacement_text is not a string")
    if not replacement:
        raise EditValidationError(f"{prefix} replacement_text is empty")
    if "\x00" in replacement:
        raise EditValidationError(f"{prefix} replacement_text contains a NUL byte")
    try:
        encoded = replacement.encode("utf-8")
    except UnicodeEncodeError:
        raise EditValidationError(f"{prefix} replacement_text is not encodable as UTF-8") from None
    if len(encoded) > MAX_REPLACEMENT_BYTES:
        raise EditValidationError(
            f"{prefix} replacement_text exceeds {MAX_REPLACEMENT_BYTES} bytes"
        )
    return replacement


def _count_changed_lines(current_text: str, replacement_text: str) -> int:
    diff = difflib.unified_diff(
        current_text.splitlines(keepends=True),
        replacement_text.splitlines(keepends=True),
        n=0,
    )
    return sum(
        1
        for line in diff
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


def validate_edits(source_zip: bytes, edits: object) -> list[dict]:
    """Strictly validate the model's edit list against the fixed patch contract."""
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_PATCH_FILES:
        raise EditValidationError(f"Edits must be a list of 1 to {MAX_PATCH_FILES} entries")
    seen: set[str] = set()
    total_changed = 0
    for index, edit in enumerate(edits):
        prefix = f"Edit {index}:"
        if not isinstance(edit, dict) or set(edit) != {
            "path", "original_sha256", "replacement_text",
        }:
            raise EditValidationError(
                f"{prefix} must be a dict with exactly path, original_sha256, replacement_text"
            )
        path = edit["path"]
        if not isinstance(path, str) or path not in ALLOWED_EDIT_PATHS:
            raise EditValidationError(f"{prefix} path is not an allowed edit path: {path!r}")
        if path in seen:
            raise EditValidationError(f"{prefix} path is a duplicate: {path}")
        seen.add(path)
        digest = edit["original_sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise EditValidationError(f"{prefix} original_sha256 is not 64 lowercase hex digits")
        if _sha256(_read_member(source_zip, path)) != digest:
            raise EditValidationError(
                f"{prefix} original_sha256 does not match current content of {path}"
            )
        replacement = _validate_replacement_text(prefix, edit["replacement_text"])
        total_changed += _count_changed_lines(_member_text(source_zip, path), replacement)
        if total_changed > MAX_CHANGED_LINES:
            raise EditValidationError(
                f"Edits change more than {MAX_CHANGED_LINES} lines in total"
            )
    return list(edits)


def apply_edits(source_zip: bytes, edits: list[dict]) -> tuple[bytes, str]:
    """Rebuild the source zip with accepted edits applied, preserving everything else."""
    replacements = {
        member_name(edit["path"]): edit["replacement_text"].encode("utf-8")
        for edit in edits
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source_zip)) as source:
        original: dict[str, bytes] = {}
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                content = source.read(info.filename)
                original[info.filename] = content
                target.writestr(
                    zipfile.ZipInfo(info.filename, date_time=info.date_time),
                    replacements.get(info.filename, content),
                )
    new_zip = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(new_zip)) as rebuilt:
        for name, content in original.items():
            expected = replacements.get(name, content)
            if _sha256(rebuilt.read(name)) != _sha256(expected):
                raise RuntimeError(f"Rebuilt source zip member mismatch: {name}")
    return new_zip, _sha256(new_zip)


def _rewrite_manifest(content: bytes, new_source_sha256: str) -> bytes:
    try:
        manifest = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Attempt manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("Attempt manifest is not a JSON object")
    updated = dict(manifest)
    updated["source_sha256"] = new_source_sha256
    return (json.dumps(updated, indent=2) + "\n").encode("utf-8")


def rebuild_candidate_tar(
    candidate_tar: bytes, new_source_zip: bytes, new_source_sha256: str,
) -> bytes:
    """Rebuild the attempt input tar with the new source zip and manifest digest."""
    members: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(candidate_tar), mode="r:") as archive:
        for member in archive:
            if not member.isfile():
                raise EditValidationError("Attempt input contains a non-file entry")
            content = archive.extractfile(member).read()
            if member.name == "source.zip":
                content = new_source_zip
            elif member.name == "manifest.json":
                content = _rewrite_manifest(content, new_source_sha256)
            members.append((member.name, content))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as output:
        for name, content in members:
            entry = tarfile.TarInfo(name)
            entry.mode = 0o600
            entry.uid = entry.gid = 10001
            entry.size = len(content)
            output.addfile(entry, io.BytesIO(content))
    result = buffer.getvalue()
    try:
        _validate_attempt_archive(result)
    except ValueError as exc:
        raise EditValidationError(f"Rebuilt attempt input failed validation: {exc}") from exc
    return result


def changed_file_diffs(source_zip_before: bytes, source_zip_after: bytes) -> str:
    """Unified diff text for every changed member, in member-name order."""
    sections: list[str] = []
    with zipfile.ZipFile(io.BytesIO(source_zip_before)) as before, \
            zipfile.ZipFile(io.BytesIO(source_zip_after)) as after:
        for name in sorted(info.filename for info in after.infolist()):
            before_content = before.read(name)
            after_content = after.read(name)
            if before_content == after_content:
                continue
            before_lines = before_content.decode("utf-8", errors="replace").splitlines(keepends=True)
            after_lines = after_content.decode("utf-8", errors="replace").splitlines(keepends=True)
            sections.extend(difflib.unified_diff(
                before_lines, after_lines,
                fromfile=f"{name}@original", tofile=f"{name}@repaired",
            ))
    return "".join(sections)
