"""Synthetic preparation checks. These do not run a historical repository."""

import hashlib
import io
import json
import stat
import zipfile

import pytest

from execution.attempt import (
    AttemptError,
    COMMIT_SHA,
    _collect_ids,
    _extract_source,
    _load_manifest,
    _parse_junit,
)
from upgrade_chamber.baseline import (
    BASELINE_VERSION,
    COMMON_PINS,
    PROFILE_ID,
    SOURCE_URL,
    TARGET_VERSION,
    PreparationError,
    build_input_tar,
    prepare_fixed_bundles,
)
from upgrade_chamber.runner import _validate_attempt_archive


ROOT = f"requests-unixsocket-{COMMIT_SHA}"
TEST_PATH = f"{ROOT}/requests_unixsocket/tests/test_requests_unixsocket.py"


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in entries:
            if isinstance(value, tuple):
                content, mode = value
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, content)
            else:
                archive.writestr(name, value)
    return buffer.getvalue()


def test_safe_extract_accepts_normal_github_root_directory(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(make_zip([(f"{ROOT}/", b""), (TEST_PATH, b"def test_example(): pass\n")]))
    _extract_source(source, tmp_path / "extracted")
    assert (tmp_path / "extracted/requests_unixsocket/tests/test_requests_unixsocket.py").is_file()


@pytest.mark.parametrize("entries", [
    [(f"{ROOT}/", b""), (f"{ROOT}/../escape", b"bad"), (TEST_PATH, b"ok")],
    [(f"{ROOT}/", b""), (f"other-root/{TEST_PATH}", b"bad"), (TEST_PATH, b"ok")],
    [(f"{ROOT}/", b""), (TEST_PATH, (b"link", stat.S_IFLNK | 0o777))],
])
def test_safe_extract_rejects_traversal_multiple_roots_and_links(tmp_path, entries):
    source = tmp_path / "source.zip"
    source.write_bytes(make_zip(entries))
    with pytest.raises(AttemptError):
        _extract_source(source, tmp_path / "extracted")


def test_zero_and_duplicate_collection_rejected():
    with pytest.raises(AttemptError, match="zero or duplicate"):
        _collect_ids(b"no tests collected\n")
    node = b"requests_unixsocket/tests/test_requests_unixsocket.py::test_example\n"
    with pytest.raises(AttemptError, match="zero or duplicate"):
        _collect_ids(node + node)
    assert _collect_ids(node) == [node.decode().strip()]


def test_junit_counts_and_unique_names(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuite><testcase name="test_one"/><testcase name="test_two">'
                     '<skipped type="pytest.xfail"/></testcase></testsuite>', encoding="utf-8")
    counts, names = _parse_junit(junit)
    assert names == ["test_one", "test_two"]
    assert counts["passed"] == 1
    assert counts["skipped"] == counts["xfailed"] == 1
    junit.write_text('<testsuite><testcase name="test_one"/><testcase name="test_one"/></testsuite>',
                     encoding="utf-8")
    with pytest.raises(AttemptError, match="duplicate"):
        _parse_junit(junit)


class FakeFetcher:
    def __init__(self):
        self.source = make_zip([(f"{ROOT}/", b""), (TEST_PATH, b"def test_example(): pass\n")])

    def fetch(self, url, *, max_bytes, body=None):
        if url == SOURCE_URL:
            return self.source
        if url == "https://api.osv.dev/v1/query":
            assert body is not None
            return b'{"vulns":[]}'
        if url.startswith("https://pypi.org/pypi/"):
            package, version = url.removeprefix("https://pypi.org/pypi/").removesuffix("/json").split("/")
            tag = "py2.py3" if package == "urllib3" else "py3"
            filename = f"{package.replace('-', '_')}-{version}-{tag}-none-any.whl"
            wheel = f"fake wheel {package} {version}".encode()
            digest = hashlib.sha256(wheel).hexdigest()
            wheel_url = f"https://files.pythonhosted.org/packages/fixed/{filename}"
            return json.dumps({"info": {"version": version, "yanked": False, "requires_python": ">=3.11"},
                               "urls": [{"packagetype": "bdist_wheel", "filename": filename,
                                         "url": wheel_url, "yanked": False,
                                         "digests": {"sha256": digest}}],
                               "vulnerabilities": []}).encode()
        if url.startswith("https://files.pythonhosted.org/packages/fixed/"):
            filename = url.rsplit("/", 1)[-1]
            name, version = filename.split("-")[:2]
            return f"fake wheel {name.replace('_', '-')} {version}".encode()
        raise AssertionError(f"Unexpected URL: {url}")


def test_preparation_builds_two_distinct_hash_checked_runner_inputs(tmp_path):
    summaries = prepare_fixed_bundles(tmp_path, fetcher=FakeFetcher())
    assert summaries["baseline"]["requests_version"] == BASELINE_VERSION
    assert summaries["candidate"]["requests_version"] == TARGET_VERSION
    for phase in ("baseline", "candidate"):
        phase_dir = tmp_path / phase
        manifest = json.loads((phase_dir / "manifest.json").read_text())
        assert manifest["profile_id"] == PROFILE_ID
        assert manifest["phase"] == phase
        assert len(manifest["wheels"]) == len(COMMON_PINS) + 1
        assert any(name.endswith("-py2.py3-none-any.whl") for name in manifest["wheels"])
        assert _load_manifest(phase_dir, phase)["source_sha256"] == manifest["source_sha256"]
        _validate_attempt_archive(build_input_tar(phase_dir))

    baseline = json.loads((tmp_path / "baseline/manifest.json").read_text())
    candidate = json.loads((tmp_path / "candidate/manifest.json").read_text())
    assert set(baseline["wheels"]) ^ set(candidate["wheels"]) == {
        "requests-2.31.0-py3-none-any.whl", "requests-2.34.2-py3-none-any.whl"
    }


def test_modified_wheel_is_rejected_before_container_staging(tmp_path):
    prepare_fixed_bundles(tmp_path, fetcher=FakeFetcher())
    phase_dir = tmp_path / "baseline"
    wheel = next((phase_dir / "wheels").glob("urllib3-*.whl"))
    wheel.write_bytes(b"modified")
    with pytest.raises(PreparationError, match="hash mismatch"):
        build_input_tar(phase_dir)
