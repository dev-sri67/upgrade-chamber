"""Admission catalog. Research candidates are never executable profiles."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ResearchCandidate:
    id: str
    repository_url: str
    commit_sha: str
    dependency: str
    status: str
    enabled: bool
    reason: str


RESEARCH_CANDIDATES = (
    ResearchCandidate(
        id="requests-unixsocket-historical-0.3.0-research",
        repository_url="https://github.com/msabramo/requests-unixsocket",
        commit_sha="8449bc0f76a2ce410644b5e8aab45829ddca54f7",
        dependency="requests",
        status="unvalidated",
        enabled=False,
        reason="Historical baseline, upgrade failure, and repaired rerun have not been verified in a Vultr container.",
    ),
)


class UnsupportedProfileError(ValueError):
    """No validated execution profile matches the requested identifier."""


def enabled_profiles() -> tuple[()]:
    return ()


def require_enabled_profile(profile_id: str) -> None:
    raise UnsupportedProfileError(f"No validated execution profile: {profile_id}")


def public_catalog() -> dict[str, list[dict[str, object]]]:
    return {
        "profiles": [],
        "research_candidates": [asdict(candidate) for candidate in RESEARCH_CANDIDATES],
    }
