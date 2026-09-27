# Upgrade Chamber — 24-Hour Roadmap

Status: complete and live as of 2026-09-27; all gates G1–G6 are evidenced and the validation sweep is recorded.
Scope authority: [mission.md](mission.md). Architecture and limits: [tech-stack.md](tech-stack.md).

## Current position and remaining work

The entry is end-to-end complete. The Vultr VM at `45.76.66.244` runs the deployed source at commit `84edaa1` ("fix: treat completed runs as terminal during lease requeue", source fingerprint `296fe90eaf79ee729a7be5f1d4dfbfaabab64a3978a5b393b0f480dd807df6fb`; prior installations retained under `/opt/upgrade-chamber-<sha>-backup`) with two systemd services — the API listening on `127.0.0.1:8000` only and the serial worker sharing the SQLite state at `/var/lib/upgrade-chamber` — behind Caddy serving the public `https://45.76.66.244` (self-signed certificate, disclosed) with frontend bundle `index-13yOF-NI.js`. The runner image `sha256:9eca30cba682f2aa74df8892a1b2ddccad12438628e41aefcc541f49ac80acab` is unchanged since first verification. The inference key and model ID (`minimax-m3`, chosen after live reliability probing of `glm-5.3-flash`) live only in the API service environment; the worker holds no inference key, and no execution container receives credentials, mounts, or the Docker socket.

All five gates are evidenced on the VM under `/root/upgrade-chamber-evidence/` (22 directories: `g1-*`, `g2-*`, `g3-*`, `g4-*`, `demo-e2e-1/2/3/4`). Six completed end-to-end runs — 14, 16, 17, 18, 19, and 20 — each show the repaired-and-verified outcome: preparation, baseline passed 5/5 on requests 2.31.0, the candidate honestly `test_failed` on the genuine urllib3 `http+unix` incompatibility, one accepted `minimax-m3` edit overriding `UnixAdapter.get_connection_with_tls_context` in `requests_unixsocket/adapters.py`, the rebuilt `repair-1` attempt passed 5/5, the fresh verifier passed 5/5 on requests 2.34.2, terminal `completed` with `cleanup_state` observed and zero owned containers. Runs 16, 17, 18, 19, and 20 were driven through the public browser UI (`demo-e2e-2`, `demo-e2e-3`, `demo-e2e-4`; the rehearsal attempts 18 and 19 completed server-side but their automated browser checks aborted on a defect in the test harness's own comparison regex, leaving no local evidence for either; run 20 completed the final rehearsal with the full result view, the manifest.json download, and reload recovery), and the same rehearsal re-verified the four admission-rejection responses live (422 `unsupported_repository` ×2, `dependency_mismatch`, `unsupported_profile`, no run created).

The honest failure trail is retained verbatim: runs 1–13 and 15 record the cancelled run with its container removed (run 2), the configuration-gap `infrastructure_failed` (run 3), the `glm-5.3-flash` provider error series (runs 4–9), the first live protected-edit rejection (run 11), the first live accepted edit with an honestly failing repair attempt (run 12), the turn-budget exhaustion and first honest available-zero advisory (run 13), and the browser blob/limitations defects with their fixes (`927b6a3`, `ef34617`, run 15). The lease-requeue defect that briefly mis-marked completed runs 14 and 16 was fixed at `84edaa1` with a regression test, and those runs were restored from their recorded terminal evidence — the events, artifacts, and result JSON were never corrupted; run 17 was protected by stopping the worker before its lease expired.

The validation sweep is recorded: the local suite passes 229 tests + 5 subtests with one pre-existing warning; the VM checks pass (all three services active, `healthz`/`ready` ok, zero owned containers, runs 14/16/17 `completed` with `cleanup_state` observed); and the external checks pass (`https://45.76.66.244` serves `healthz`, the enabled profile, the live asset reference, and the JS bundle over HTTPS).

Remaining work is operator-only: produce the narrated demo video from the raw footage on the VM (`/root/upgrade-chamber-evidence/demo-e2e-3/run3/e2e3-run.webm`) and perform one fresh human browser pass over `https://45.76.66.244` immediately before submission (optionally point a real domain at the VM to remove the certificate warning; the Caddyfile migration is documented in the deployment notes).

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

- [x] Public GitHub repository with setup and environment example; no committed secrets. (README with setup steps plus `.env.example`; `.env` and `.local` are git-ignored and never committed.)
- [x] Public web application and controller deployed on Vultr. (`https://45.76.66.244`, Caddy HTTPS with the self-signed certificate disclosed up front.)
- [x] Actual agent calls use Vultr Serverless Inference. (`minimax-m3`; selection and repair-turn calls recorded with model identity, rationale, and usage metadata, no credentials.)
- [x] Installation/tests execute outside the application process in disposable containers. (Distinct runner containers per attempt with observed removal; no mounts, socket, or credentials inside.)
- [x] One successful fresh upgrade run with downloadable patch and evidence. (Runs 14, 16, and 17; `patch.diff`, `comparison.json`, and the full evidence bundle downloadable through the API and the UI.)
- [x] Test scope, known limitations, and any reference-fixture modifications are disclosed. (README scope/limitations sections plus the profile's own disclosure field.)
- [x] Timeout, container removal, and successful follow-up execution recorded. (`g2-9df41cb-1` containment record plus the run history through runs 14/16/17.)
- [x] Resource caps, secret separation, lifecycle, and network policy explained. (README and `tech-stack.md` cover the caps, the env separation, the cleanup lifecycle, and the no-egress policy.)
- [ ] Demo video and architecture explanation included. (Architecture explanation: yes — README. Demo video: raw browser footage recorded on the VM at `/root/upgrade-chamber-evidence/demo-e2e-3/run3/e2e3-run.webm`; the narrated video is still to be produced by the operator, so this box stays unticked.)
- [ ] Public links and fresh-browser workflow checked immediately before submission. (E2E-3 ran in a fresh automated browser session over the public URL; the final fresh-human check immediately before submission is still outstanding, so this box stays unticked.)
