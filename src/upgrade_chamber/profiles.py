"""Admission catalog. Validated execution profiles are executable; research candidates are not."""

from dataclasses import asdict, dataclass

from .baseline import BASELINE_VERSION, COMMON_PINS, COMMIT_SHA, PROFILE_ID, SOURCE_URL, TARGET_VERSION


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


@dataclass(frozen=True)
class ExecutionProfile:
    id: str
    repository_url: str
    commit_sha: str
    dependency: str
    python: str
    baseline_version: str
    target_version: str
    description: str
    disclosure: str
    evidence: str


_ENABLED_REPOSITORY_URL = "https://github.com/msabramo/requests-unixsocket"

ENABLED_PROFILES = (
    ExecutionProfile(
        id=PROFILE_ID,
        repository_url=_ENABLED_REPOSITORY_URL,
        commit_sha=COMMIT_SHA,
        dependency="requests",
        python="3.11",
        baseline_version=BASELINE_VERSION,
        target_version=TARGET_VERSION,
        description=(
            "Historical reproduction of the public requests-unixsocket repository "
            f"({_ENABLED_REPOSITORY_URL}, source archive {SOURCE_URL}) at recorded commit "
            f"{COMMIT_SHA}, running its own pytest suite offline against pre-staged, "
            "hash-checked wheel bundles."
        ),
        disclosure=(
            f"This reproduction pins fixture wheels for {', '.join(COMMON_PINS)} and "
            f"targets the requests upgrade {BASELINE_VERSION} -> {TARGET_VERSION} at the "
            f"recorded commit only; upstream attribution: msabramo/requests-unixsocket "
            f"({_ENABLED_REPOSITORY_URL})."
        ),
        evidence=(
            "Live Vultr records: g1-31ae613-1 and g2-9df41cb-1 (passing baseline, "
            "reproducible upgrade failure, containment probes)"
        ),
    ),
)


def enabled_profiles() -> tuple[ExecutionProfile, ...]:
    return ENABLED_PROFILES


def require_enabled_profile(profile_id: str) -> ExecutionProfile:
    for profile in ENABLED_PROFILES:
        if profile.id == profile_id:
            return profile
    raise UnsupportedProfileError(f"No validated execution profile: {profile_id}")


def require_profile_repository(profile_id: str, repository_url: str, commit_sha: str) -> ExecutionProfile:
    """Admission gate for POST /api/runs: submitted origin must equal the validated profile."""
    profile = require_enabled_profile(profile_id)
    if repository_url != profile.repository_url or commit_sha != profile.commit_sha:
        raise UnsupportedProfileError(
            f"Submitted repository or commit does not match validated profile: {profile_id}")
    return profile


def public_catalog() -> dict[str, list[dict[str, object]]]:
    return {
        "profiles": [asdict(profile) for profile in ENABLED_PROFILES],
        "research_candidates": [asdict(candidate) for candidate in RESEARCH_CANDIDATES],
    }
