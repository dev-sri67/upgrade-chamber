# Upgrade Chamber — 24-Hour Roadmap

Status: implementation in progress as of 2026-09-26. Gates remain open until their acceptance evidence exists.
Scope authority: [mission.md](mission.md). Architecture and limits: [tech-stack.md](tech-stack.md).

## Current position and remaining work

The [public repository](https://github.com/dev-sri67/upgrade-chamber) is pushed. The Vultr VM at `45.76.66.244` runs the API with a healthy `GET /healthz` response on `127.0.0.1:8000` only. Source is deployed at commit `9df41cb` ("retry staging exec on container init race") under the reviewed replacement plan: the earlier installations are retained at `/opt/upgrade-chamber-0d73362-backup`, `/opt/upgrade-chamber-31ae613-backup`, and `/opt/upgrade-chamber-012326e-backup`, the current source fingerprint is `277fa7eb36083f9da12996f0ee3028102ee3533abfdadefd11388ce8a445dcdd`, and the previously verified runner image `sha256:9eca30cba682f2aa74df8892a1b2ddccad12438628e41aefcc541f49ac80acab` was reused because no image input changed. A real Vultr Serverless Inference structured-response smoke call passed using `glm-5.3-flash`.

The read-only-root staging blocker is fixed. Commits `c54ad64`, `31ae613`, and `9df41cb` replaced all eight `put_archive` staging sites and both `get_archive` readers in `src/upgrade_chamber/runner.py` with a bounded exec transport: 8-byte little-endian length-framed stdin writes to a fixed `python -I -c` script opened with `O_EXCL`, tarfile data-filter extraction for the attempt input, and `O_NOFOLLOW` regular-file reads demultiplexed by a `_FrameDemuxer` that parses Docker's non-TTY stream framing (8-byte `>BxxxL` headers) before validation; every Docker and socket call is bounded by a monotonic deadline, and a `_start_exec` helper retries the `exec_create`/`exec_start` pair up to 5 attempts (0.25s apart, deadline-checked) only when docker-py reports the `FailedPrecondition` "container init process is not running" race that can follow an immediate exec after `container.start()`. The local suite passed 60 tests. Two live experiment records exist on the VM: `g2-012326e-1` honestly documents the un-retried init-race failure at preparation (status "stopped"), and `g2-9df41cb-1/summary.json` (2026-09-26, 20:31:04–20:32:03 UTC, status "recorded") records the containment evidence: the timeout probe was killed at its 5.0s deadline and removed; the memory probe was OOM-killed at the enforced 1 GiB cgroup limit (exit 137) and removed; the recovery success probe completed in 0.79s; preparation completed in 5.26s with the bounded retry in place; the baseline passed (requests 2.31.0; 5 collected test IDs, 5 passed, 0 failed; `pip check` clean); the candidate was honestly recorded as `test_failed` (requests 2.34.2; the identical 5 test IDs, 0 passed, 5 failed) from the genuine requests 2.34.2 incompatibility; both failing and normal paths cleaned up, and `docker ps -a` showed zero owned containers afterward with `orphan_cleanup` finding none. G1 and G2 containment evidence is recorded. Still open: the manually understood reference repair and everything from step 3 onward. The historical research profile remains disabled. See [acceptance tracking](docs/implementation-checklist.md) and [operator handoff](docs/RESUME.md) for stage evidence.

Complete the remaining work in this order:

1. Implement and test input staging and bounded artifact retrieval that work with the read-only root and tmpfs. Preserve the no-mount, no-credential execution policy. Establish both directions on Vultr before retrying the full live experiment. Then inspect timeout removal and follow-up success, preparation artifacts, installed versions, source/image identities, collected test IDs, JUnit reports, and baseline/candidate outcomes. Record failures as observed; do not enable a profile on synthetic evidence. If the historical case fails the feasibility gate, use the labeled reference-fixture fallback below.
2. Run the memory-limit probe and complete G2 live containment evidence, including cleanup after normal and failing execution. Fix any failed policy or lifecycle check before opening public execution.
3. Implement SQLite run, attempt, event, and artifact records; serial job orchestration; protected-edit and test-collection checks; independent verifier; patch/evidence export; OSV status; and authenticated run, status, cancel, and artifact API endpoints. Exercise cancellation and baseline-failure paths.
4. Add the bounded Vultr model selection and repair loop. Verify actual model attempts with fresh containers, then enable only a profile whose live evidence satisfies admission checks.
5. Build the browser workflow and public HTTPS deployment. Run a fresh browser job, complete M01–M12 end-to-end checks, record containment/recovery and demo video, then verify public links for submission.

The hour ranges and gate hours below preserve the original **24-hour planning estimates**. They are not elapsed-time claims, current deadlines, or evidence that a gate passed.

## Delivery strategy

Build one complete, reproducible workflow before adding repository coverage or UI decoration. The first risk is a usable baseline and upgrade case. The second is bounded sandbox execution. The third is model repair reliability. Deployment begins early; the last four hours are reserved for validation and submission.

This was estimated as a 24-hour elapsed schedule for one experienced builder. If teammates are available, work can be divided across runner, web UI, and deployment once contracts are agreed, without increasing the feature set. Do not require a multi-agent runtime.

## Hours 0–3: Prove feasibility

### Work

- Provision Vultr access, VM, and inference subscription; verify one actual model call using the required endpoint.
- Create the repository skeleton, environment example, and a brief implementation checklist mapped to M01–M12.
- Prepare a fixed Python execution image and basic resource-limited Docker runner on Vultr.
- Investigate the historical requests-unixsocket candidate. Inspect the exact historical tests and dependencies; freeze a reproducible baseline and package bundles.
- Execute baseline, dependency-only upgrade, and a manually understood reference repair. Manual repair is feasibility evidence, not the final agent demo.
- Record commands, versions, commit SHA, test IDs/counts, and actual outcome. Decide whether a reproduction fork is required and label it honestly.

### Gate G1, hour 3

An actual baseline passes and at least one upgrade result is reproducible inside a Vultr container. Prefer a genuine failure with a small known repair. Inference is reachable. No fabricated pass counts or cached logs presented as a new run.

If the historical case remains unstable by hour 2, switch to a small attributed public reference application using a real dependency incompatibility and meaningful tests. Stop arbitrary-repository investigation. If inference or Vultr execution is unavailable at hour 3, this is a mandatory-infrastructure blocker; resolve it before UI work.

## Hours 3–6: Establish containment and lifecycle

### Work

- Implement the worker's fixed container policy, staged offline inputs, wall-clock watchdog, log limits, and bounded artifact export.
- Enforce non-root execution, dropped capabilities, no-new-privileges, read-only image root, tmpfs limits, no network, and no host mounts or secrets.
- Separate worker privileges from the API; keep the inference key out of the worker and all containers.
- Implement cleanup on completion/failure and label-based orphan removal.
- Run an infinite-loop probe, a memory-limit probe, and a successful follow-up job. Inspect Docker state to confirm removal.

### Gate G2, hour 6

Normal and intentionally failing executions are bounded and clean up. Record real containment evidence. If network-restricted dependency fetching is not ready, use pre-staged wheel bundles only. Do not weaken the execution policy to make a package install work.

## Hours 6–9: Build deterministic orchestration

### Work

- Implement SQLite run/attempt/event/artifact records and the serial worker lease.
- Implement profile admission, immutable commit resolution, baseline gate, candidate attempt, and terminal statuses.
- Capture installed versions, pip reports, collection IDs, JUnit, and process status.
- Query OSV for baseline and candidate inventories; record unavailable responses explicitly.
- Implement the independent verifier and protected-file comparison.
- Add create/status/events/cancel/artifact endpoints and per-run access tokens.

### Gate G3, hour 9

A run can be submitted through the API and produces a real comparison, patch, and evidence download. Cancellation removes the active container. Baseline failure stops mutation. Zero tests or changed test IDs cannot produce a verified result.

## Hours 9–12: Add the bounded agent loop

### Work

- Supply the Vultr model with eligible target versions and bounded repository context.
- Validate version selection and structured edits against server-owned schemas and profile rules.
- Feed actual failure reports to the model; allow at most two repairs within the job deadline.
- Rebuild each attempt from the immutable source plus accepted edits; never reuse a previous container's state.
- Reject attempts to edit tests, disable checks, alter installation hooks, or exceed patch limits.
- Record concise model summaries and usage metadata; exclude hidden reasoning and credentials.

### Gate G4, hour 12

The real Vultr model completes selection and at least one full attempted workflow. Preferred: it repairs the known compatibility case and final verification passes. If repair fails, inspect the concrete failure and narrow context; do not script the answer and label it agent-generated. A straight-through verified upgrade is the fallback demo.

## Hours 12–16: Complete the web product

### Work

- Build the start page with validated examples, URL/ref inputs, dependency selection, and supported-scope explanation.
- Build the run timeline, attempt cards, before/after test comparison, expandable logs, and containment panel.
- Add patch preview, artifact downloads, cancellation, and recovery after page reload.
- Render all logs/source as text. Show incomplete advisory checks, baseline failures, and cleanup errors accurately.
- Deploy the frontend and API behind Caddy on the Vultr VM. Configure HTTPS, service supervision, and persistent data directories.

### Gate G5, hour 16

The public HTTPS URL completes a fresh run from a browser. A reviewer can understand the outcome and download the exact patch and evidence without using a terminal. The UI reports observed limits and cleanup events rather than decorative security badges.

## Hours 16–20: Validate and stabilize

### Work

- Exercise M01–M12 end to end and fix concrete failures.
- Test rejected edits, invalid URLs, zero collected tests, provider failure, OSV failure, cancellation, output flooding, and worker interruption.
- Confirm repository attempts cannot reach outbound services or obtain app/provider secrets.
- Confirm resource caps, storage admission checks, queue limits, artifact authorization, and orphan cleanup.
- Run the intended live demo twice under the final configuration. Record elapsed time and model/attempt variability.
- Finish README setup steps, architecture, limitations, supported profile instructions, and the containment explanation.

### Gate G6, hour 20: Feature freeze

One full workflow and the containment/recovery sequence are reproducible on the public deployment. All mandatory challenge requirements are covered. After this point, only fix release-blocking defects and prepare submission materials.

## Hours 20–24: Record and submit

### Work

- Export a final evidence bundle with commit/image identity and real reports.
- Record a 3–4 minute demo, adjusted to the organizer's required duration.
- Verify the repository is public and contains setup, configuration example, scope, architecture, and evidence interpretation.
- Verify the public URL from a fresh browser session and retain enough capacity for judging.
- Submit repository URL, public demo URL, video URL, architecture explanation, and Vultr/inference usage details.
- Reserve at least the last hour for upload, broken links, and deployment recovery.

### Video sequence

1. Problem and supported repository, approximately 20 seconds.
2. Submit a fresh job; show the pinned source and passing baseline, approximately 35 seconds.
3. Show the upgrade failure and model's actual repair, approximately 60 seconds. If accelerated, label time compression.
4. Open the unchanged test comparison, patch, advisory result, and evidence download, approximately 40 seconds.
5. Run the labeled infinite-loop probe; show timeout and removal, approximately 35 seconds.
6. Start a successful follow-up job and explain Vultr orchestration/inference and remaining limits, approximately 30 seconds.

Never imply a prerecorded run is live. Never hide a failed repair behind invented output. A failed upgrade can be shown as a legitimate outcome, but the submission also needs one successful executed result.

## Priority and cut order

| Priority | Included work |
| --- | --- |
| P0 — cannot cut | Vultr VM/web deployment, Vultr inference, isolated real execution, one passing baseline, actual upgrade result, deterministic verification, patch/evidence, limits/cleanup, containment video, documentation. |
| P1 — target | Actual compatibility repair, two repair attempts, polished diff viewer, cancel/reconnect UX. |
| P2 — only after freeze criteria pass early | Second validated profile, SSE instead of polling, richer advisory display. |
| Deferred | Arbitrary repositories, private repos, PR creation, multi-language support, multiple concurrent jobs, distributed workers, general package egress proxy, microVM migration. |

Cut in this order: second profile; advanced visuals; SSE; rich diff rendering; second repair attempt. Keep plain patch download and one repair opportunity. Never cut isolation, observed cleanup, immutable source identity, honest result reporting, or the required Vultr services.

## Risk register

| Risk | Early signal | Response |
| --- | --- | --- |
| Historical repo cannot run | Baseline setup still failing at hour 2 | Switch to labeled reference fixture; retain meaningful tests. |
| Upgrade passes immediately | No compatibility failure | Accept the successful upgrade; choose another case only before G1 closes. |
| Model cannot produce valid edits | Repeated schema or repair failure | Reduce context and edit scope; preserve bounded retries; report failure honestly. |
| Package needs network/native build | Wheel bundle incomplete | Reject that profile/version; use prepared supported bundles. |
| Test suite needs external services | Offline baseline fails | Choose another profile; do not enable general egress. |
| Tests can be bypassed | Protected edit or changed collection | Reject patch; require verifier result; keep profiles curated. |
| Container remains after timeout | Removal not observed | Block new execution until cleanup succeeds; fix before recording. |
| Inference model unavailable | Provider errors at startup | Select another available Vultr model and retest; no provider substitution. |
| Public endpoint abused | Queue/storage fills | Tighten profile admission and rate limits; enable operator pause. |
| Schedule slips | G4 incomplete at hour 12 | Ship one profile and simple UI; freeze feature expansion. |

## Submission checklist

- [ ] Public GitHub repository with setup and environment example; no committed secrets.
- [ ] Public web application and controller deployed on Vultr.
- [ ] Actual agent calls use Vultr Serverless Inference.
- [ ] Installation/tests execute outside the application process in disposable containers.
- [ ] One successful fresh upgrade run with downloadable patch and evidence.
- [ ] Test scope, known limitations, and any reference-fixture modifications are disclosed.
- [ ] Timeout, container removal, and successful follow-up execution recorded.
- [ ] Resource caps, secret separation, lifecycle, and network policy explained.
- [ ] Demo video and architecture explanation included.
- [ ] Public links and fresh-browser workflow checked immediately before submission.
