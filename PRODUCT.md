# Product

<!-- impeccable:product-schema 1 -->

> **Interview substitution (disclosed):** The user interview could not run in this session
> (user directed full autonomy; the substitution is pre-approved by the assignment). Every
> statement below that is inferred rather than read from a repository document is prefixed
> `ASSUMPTION:`. Confirmed sources: `mission.md`, `tech-stack.md`, `roadmap.md`,
> `src/upgrade_chamber/api.py`, `src/upgrade_chamber/profiles.py`, `src/upgrade_chamber/worker.py`,
> `src/upgrade_chamber/osv.py`, and the frontend spec at
> `C:\Users\Chinni\AppData\Local\Temp\opencode\specs\frontend-spec.md`.

## Platform

web

## Stack

Delegated: React 19 + TypeScript (strict, no `any`) + Vite + plain CSS, scaffolded with the
Vite `react-ts` template and no other runtime dependency. No router library (three views
managed with plain state), no state library, polling via fetch + `setInterval`. The spec in
`frontend-spec.md` fixed the stack; this records that delegation.

## Users

- **Primary user:** a maintainer of a small Python project who sees a dependency advisory and
  cannot quickly tell whether upgrading the dependency will break their code. Their job:
  hand the bounded workflow to the product and get back a tested patch plus inspectable
  evidence, without installing anything locally.
- **Secondary audience:** hackathon judges ("Blast Radius Zero — Safe Agent Execution on
  Vultr") who must verify the containment story, evidence honesty, and the real
  Vultr-backed execution path from the public URL.
- ASSUMPTION: the primary user is technically fluent (comfortable reading a unified diff and
  a JSON manifest) but should not need a terminal to understand the outcome.

## Product Purpose

Upgrade Chamber evaluates one dependency upgrade by executing the repository's own test
suite in disposable Vultr-hosted containers: a passing baseline first, then the upgraded
candidate, an optional bounded agent repair, an independent verification rerun, and a
cleanup record. It returns a downloadable patch and an evidence bundle (manifest,
comparison, JUnit reports, installed inventories, advisory snapshots, logs). Success means
the maintainer can apply the patch manually after review — not a claim of general
correctness.

## Positioning

The distinguishing result is a **compatibility patch backed by an independently scheduled
fresh-container rerun**, not merely a vulnerability list or an edited requirement file.
Every datum shown is recorded from a real execution; failures, unavailable advisory data,
and cleanup outcomes are reported honestly. Neighboring products (Dependabot-style
version suggestions, vulnerability dashboards) do not execute the user's tests or return a
verified patch.

## Operating Context

- Hackathon entry targeting Pattern A: sandboxed code execution. Browser automation and
  vision are out of scope.
- Public HTTPS web app on a Vultr VM: static React client served same-origin with the
  FastAPI controller. One serial worker; one active container; queue and per-IP rate caps.
- Runs are 15-minute-max jobs: prepare → baseline → select → upgrade → (repair →
  upgrade) → verify → cleanup. Status events should appear within ~3 seconds (target, not
  measured claim).
- The run token lives in browser sessionStorage (never URLs/logs); reload must restore the
  live run view or the terminal result view.
- Evidence artifacts are retained for 24 hours under a storage cap.
- ASSUMPTION: judges evaluate the product primarily on desktop browsers over the public
  deployment; a phone-sized viewport still needs a usable single-column layout.

## Capabilities and Constraints

- Public input contract: a public GitHub repository URL, an optional commit/ref that must
  match the profile's pinned commit, and a dependency name. During the hackathon,
  execution is enabled **only** for the published validated profile. Unsupported
  repositories get a clear explanation before execution.
- **No claim of arbitrary-repository support is permitted.**
- The agent cannot edit tests, test configuration, CI configuration, the sandbox policy,
  or the runner. Bounded repairs only; at most two; accepted only after a fresh
  controller-run verifier with unchanged collected test IDs and no new skips/xfails.
- Honest-language rules (binding): show "target advisory no longer reported for this
  installed version," never "secure"; when advisory data is unavailable, show "Advisory
  check unavailable" exactly; a package advisory does not establish that the application
  reaches the vulnerable behavior; advisory query timestamps and remaining advisories stay
  visible.
- Failure states (`baseline_failed`, `upgrade_failed`, `timed_out`, `cancelled`,
  `infrastructure_failed`, `unsupported`) render prominently with the recorded
  `status_detail`; no fabricated counts, no decorative security badges.
- The containment panel describes configuration (read-only root, non-root UID 10001,
  dropped capabilities, no-new-privileges, network none, 1 vCPU / 1 GiB / 128 PIDs, tmpfs
  limits, observed removal per attempt) and is **labeled configuration, not proof**; it
  also shows the run's actual `cleanup_state` and image identity.
- Cleanup status is stored separately from the result so a passing run never conceals
  failed container removal.
- Assumptions:
  - ASSUMPTION: demo/evaluation usage is dominated by the requests-unixsocket historical
    profile (requests 2.31.0 → 2.34.2, CVE-2024-35195 research case).
  - ASSUMPTION: runs take minutes, so the live view's primary job is trustworthy progress
    legibility (state, timeline, attempts) with polling, not sub-second liveness.

## Brand Commitments

- Name: **Upgrade Chamber**.
- Voice: honest, specific, non-promotional; every claim traceable to a recorded artifact.
  Never say "secure"; never imply arbitrary-repository support; never imply a prerecorded
  run is live.
- Product promise, verbatim: **"A dependency upgrade, tested against your repository, with
  the patch and evidence to review."**

## Evidence on Hand

- Validated execution profile published via `GET /api/profiles` with `description`,
  `disclosure`, and `evidence` fields (live Vultr gate records g1-31ae613-1,
  g2-9df41cb-1: passing baseline, reproducible upgrade failure, containment probes).
- Research candidate `requests-unixsocket-historical-0.3.0-research` is explicitly
  unvalidated; its reason is displayed and it must not be presented as runnable.
- Containment/recovery records exist on the VM (timeout kill at deadline, OOM kill at the
  1 GiB cgroup limit, observed removal on every path). These are recorded evidence, not UI
  assets; the UI renders only API-provided values.
- Absences the UI must not fabricate: no completed repaired-and-verified run has been
  recorded from any live G4 run yet; no user testimonials, no benchmark numbers, no
  third-party attestation. Hashes are consistency checks, not attestation.

## Product Principles

1. **Recorded truth only.** Every rendered datum comes from the API. Absent evidence is
   shown as absent, never invented or smoothed over.
2. **The outcome, legible first.** Users must not decode infrastructure details to
   understand the result; state, detail, and comparison lead the hierarchy.
3. **Failure is a valid result.** Baseline failures, timeouts, unavailable advisories, and
   cleanup problems stay visible at full prominence.
4. **Scope honesty.** The interface explains what execution supports (one validated
   profile) and what a passing result proves (the executed suite under the recorded
   environment) — no more.
5. **Evidence over assertion.** The patch, hashes, attempts, and manifest are the product;
   badges and claims are not.

## Accessibility & Inclusion

- ASSUMPTION: keyboard-operable controls, visible focus states, semantic elements, and
  associated form labels are the required baseline (also stated in the frontend spec).
- ASSUMPTION: state information is carried by text labels, not color alone (e.g. state
  badges always render their state name).
- ASSUMPTION: no product-specific WCAG conformance target was recorded beyond those
  basics.
