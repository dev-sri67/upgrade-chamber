# Upgrade Chamber — 24-Hour Roadmap

Status: implementation in progress as of 2026-09-26. Gates remain open until their acceptance evidence exists.
Scope authority: [mission.md](mission.md). Architecture and limits: [tech-stack.md](tech-stack.md).

## Current position and remaining work

The [public repository](https://github.com/dev-sri67/upgrade-chamber) is pushed. The Vultr VM at `45.76.66.244` runs two systemd services — the API with a healthy `GET /healthz` response on `127.0.0.1:8000` only, and the serial worker (no listening port) that leases and executes runs from the shared SQLite state at `/var/lib/upgrade-chamber` — with source deployed at commit `82ac0b8` ("fix: give the agent file hashes and accept empty osv replies", source fingerprint `e253a76a1a68fbf90c49b3a0908c53fb54d7290fbd64c41bf15f43b25cb2d0aa`); the earlier installations are retained at `/opt/upgrade-chamber-0d73362-backup`, `/opt/upgrade-chamber-31ae613-backup`, `/opt/upgrade-chamber-012326e-backup`, `/opt/upgrade-chamber-9df41cb-backup`, `/opt/upgrade-chamber-d725d19-backup`, `/opt/upgrade-chamber-ca68e8a-backup`, `/opt/upgrade-chamber-17ca53a-backup`, `/opt/upgrade-chamber-b94cdee-backup`, `/opt/upgrade-chamber-1dfef82-backup`, `/opt/upgrade-chamber-51204f8-backup`, and `/opt/upgrade-chamber-c80f586-backup`, `/opt/upgrade-chamber-9a10300-backup`, `/opt/upgrade-chamber-9482012-backup`, and the runner image `sha256:9eca30cba682f2aa74df8892a1b2ddccad12438628e41aefcc541f49ac80acab` was reused because no image input changed. The inference key and model ID now live only in the API service environment, transferred over SSH without ever being printed or committed; `INTERNAL_TOKEN` guards the internal endpoints (401 without the header). The worker service account has Docker group membership and no inference key.

G1–G4 evidence is recorded on the VM. G1/G2: `/root/upgrade-chamber-evidence/g2-9df41cb-1/summary.json` (2026-09-26, status "recorded") covers the bounded exec transport over the read-only root, the timeout kill at its 5.0 s deadline, the OOM kill at the enforced 1 GiB cgroup limit (exit 137), recovery, preparation with the `_start_exec` retry, a passing baseline (requests 2.31.0, 5/5 collected test IDs, `pip check` clean), an honest `test_failed` candidate (requests 2.34.2, 0/5), observed removal on every path, and zero owned containers; the un-retried init-race failure stays recorded at `g2-012326e-1` (status "stopped"). G3: `/root/upgrade-chamber-evidence/g3-d659bec-1/` records API-submitted runs through the real orchestration — `queued → preparing → baseline → selecting → upgrading` → honest `upgrade_failed`, plus a `cancelled` run, patch/comparison/manifest/advisory artifacts, and authenticated endpoints. G4: `/root/upgrade-chamber-evidence/g4-66d5bed-1/api/` (run 4, 2026-09-26T22:04:31–22:05:34 UTC) records the live agentic case: the real `glm-5.3-flash` model completed selection (source `model`, target requests 2.34.2, 2 attempts including the structured-correction retry, usage 398 prompt / 275 completion / 673 total tokens), the baseline passed (2.31.0, 5/5), the candidate was honestly `test_failed` (2.34.2, 0/5, the genuine urllib3 `URLSchemeUnknown: Not supported URL scheme http+unix` incompatibility), and the bounded repair attempt ended on a transient provider error (`502 inference_unavailable: Vultr completion has no text content`) before any agent turn, so the run terminated `upgrade_failed` with `cleanup_state` observed and zero owned containers; the preferred repaired-and-verified path was not reached, and a post-run probe confirmed the agent-turn endpoint answers valid tool calls again. Run 5, the single authorized retry (2026-09-26T22:08:51–22:09:22 UTC, evidence at `/root/upgrade-chamber-evidence/g4-66d5bed-2/api/`), got as far as a passing baseline (2.31.0, 5/5 collected IDs, 24.2 s) before its selection call itself failed with the same transient empty-completion provider error (`502 inference_unavailable: Vultr completion has no text content`), ending honestly `infrastructure_failed` with `cleanup_state` observed, zero owned containers, no agent turns, no verifier run, and no repair; post-run select probes returned 502, 200, and 502, confirming intermittent empty completions on the provider side, and no further run was attempted. Run 6 (2026-09-26T22:57:26–22:58:56 UTC, evidence at `/root/upgrade-chamber-evidence/g4-1dfef82-1/api/`, commit `1dfef82`) re-ran the same payload under idempotency key `g4-1dfef82-1`: the new bounded transport retry carried the selection call through one empty-completion transient (2 attempts, 465 prompt / 301 completion / 766 total tokens), the baseline passed (2.31.0, 5/5), the candidate was honestly `test_failed` (2.34.2, 0/5), and the repair agent-turn call then exhausted the bounded retry — three raw attempts with 3 s backoff all returned empty completions — ending honestly `upgrade_failed` with `cleanup_state` observed and zero owned containers; no agent turns, accepted edits, or verifier run, and no second run was submitted. Run 7 (2026-09-26T23:05:07–23:09:02 UTC, evidence at `/root/upgrade-chamber-evidence/g4-51204f8-1/api/`, commit `51204f8`) re-ran the same payload under idempotency key `g4-51204f8-1` after the reasoning-token budget fix: the selection call completed on its first attempt (target `2.34.2`, 179 prompt / 1366 completion / 1545 total tokens with 1077 reasoning tokens — the budget headroom the diagnosis called for), the baseline passed (2.31.0, 5/5), the candidate was honestly `test_failed` (2.34.2, 0/5, the genuine urllib3 `http+unix` incompatibility), and the repair agent-turn call failed with a different transient provider error (`502 inference_unavailable: transport failed: ReadTimeout: timed out`) before any agent turn, ending honestly `upgrade_failed` with `cleanup_state` observed and zero owned containers; no agent turns, accepted edits, or verifier run, and no second run was submitted. Run 8 (2026-09-26T23:14:45–23:19:08 UTC, evidence at `/root/upgrade-chamber-evidence/g4-c80f586-1/api/`, commit `c80f586`) re-ran the same payload under idempotency key `g4-c80f586-1` after the per-call timeout fix: the extended window held — the repair agent-turn call consumed about 205 s of provider inference with no client-side cutoff, the failure mode that killed run 7 — but the bounded transport-retry window again ended on the empty-content provider error (`502 inference_unavailable: Vultr completion has no text content`), so the run ended honestly `upgrade_failed` with `cleanup_state` observed, zero owned containers, and no agent turns, accepted edits, or verifier run; no second run was submitted. Run 9 (2026-09-27T00:06:39–00:18:54 UTC, evidence at `/root/upgrade-chamber-evidence/g4-9a10300-1/api/`, commit `9a10300`, submitted after waiting out the 3-per-hour window) re-ran the same payload under idempotency key `g4-9a10300-1` with the decisive-prompt and 32768-token repair configuration: selection completed in one attempt (target `2.34.2`, 179 prompt / 542 completion / 721 total tokens, 243 reasoning tokens), and the repair agent-turn call ran its full extended windows — about 675 s in the repairing phase with no client-side cutoff — but every completion again returned no text content, so the call failed with `502 inference_unavailable: Vultr completion has no text content` and the run ended honestly `upgrade_failed` with `cleanup_state` observed, zero owned containers, an empty agent turn sequence, no accepted edits, and no verifier run; no second run was submitted. Run 3 remains recorded as the honest configuration-gap failure (`infrastructure_failed`) at `g4-17ca53a-1`. The agentic layer is a bounded fixed-tool session (at most 8 model turns over six server-executed tools) with controller-owned verdicts; the model never runs commands, never sees credentials, and never decides pass/fail. Run 10 (2026-09-27T00:27–00:29 UTC, database-only record after the cancelled session switched `VULTR_MODEL_ID` to `minimax-m3`) then completed a `minimax-m3` selection but ended repair turn 1 on the fence/prose-wrapping failure (`structured response invalid after one correction retry`), which commit `dc3fbfc` fixes with a bounded tolerant JSON extraction before the single correction retry; run 11 (commit `9482012`, evidence at `/root/upgrade-chamber-evidence/g4-9482012-1/api/`, 2026-09-27T00:38:33–00:40:40 UTC) proved the fix live — the first real agent turn records exist (`list_repo_files` ok, one `read_file` miss on a zip-prefixed path, five `read_file` reads, then a `finish` rejected because Edit 0's `original_sha256` did not match the current content of `requests_unixsocket/adapters.py`) — but the session then hit its 8-turn budget and ended `exhausted`, so no edits were applied, the verifier did not run, and the run ended honestly `upgrade_failed` ("repair session exhausted") with cleanup observed and zero owned containers; run 12 (commit `82ac0b8`, evidence at `/root/upgrade-chamber-evidence/g4-82ac0b8-1/api/`, 2026-09-27T01:07:09–01:08:44 UTC) then completed the first live accepted-edit cycle — 7 ok agent turns (`list_repo_files`, four `read_file` reads now ending with the file sha256, `propose_edits` accepted on static validation, `finish` accepted) with the edit applied to `requests_unixsocket/adapters.py` and executed in the rebuilt `repair-1` attempt, which honestly failed collection on the model's broken replacement (`IndentationError`, no tests collected), ending `upgrade_failed` ("collected test IDs changed from baseline") with cleanup observed and zero owned containers; the verifier still has not run (`comparison.json` reports `verifier: null`) and the advisory target snapshot stayed the stale cached `unavailable` record because the advisory cache predates the `osv.py` empty-body fix, so the preferred repaired-and-verified outcome is still unreached and the G4 gate rests on run 4's full attempted workflow plus runs 11–12's live turn records and the first accepted edit.

Complete the remaining work in this order:

1. Evidenced (G1/G2 records): input staging and bounded artifact retrieval work with the read-only root and tmpfs via the bounded exec transport; the live experiment record `/root/upgrade-chamber-evidence/g2-9df41cb-1/summary.json` covers preparation artifacts, installed versions, source/image identities, collected test IDs, JUnit reports, baseline/candidate outcomes, and observed removal. The no-mount, no-credential execution policy held.
2. Evidenced (same G2 record): the memory-limit probe was OOM-killed at the enforced 1 GiB cgroup limit, cleanup after normal and failing execution was observed, and the follow-up success probe recovered; containment and lifecycle checks recorded.
3. Evidenced (G3 record): SQLite run/attempt/event/artifact records, serial worker lease, immutable commit resolution, baseline gate, candidate attempt, patch/evidence export, OSV advisory status, and authenticated run, status, events, cancel, and artifact endpoints. Remaining gaps: a live verifier result and a live protected-edit rejection (both enforced in worker code and covered by tests; run 4's candidate collected identical test IDs and failed on test outcomes, its repair session ended on a provider error before any proposal, and the run 5 retry failed at model selection on the same provider error class, so neither rejection path was triggered live).
4. Evidenced per the actual outcome (G4 record): the real Vultr model completed selection and at least one full attempted workflow — preparation, passing baseline, model selection, candidate attempt, and a bounded repair attempt — with model identity, rationale, attempts, and usage metadata recorded without credentials. The preferred repaired-and-verified outcome was not reached: run 4's repair session died on a transient provider error before its first agent turn, the run 5 retry failed even earlier at the selection call itself, run 6 confirmed the transport-retry fix's bounded scope — its transport retry carried selection through one empty-completion transient, but the repair agent-turn then exhausted the bounded retry on the same empty-completion condition — and run 7 (commit `51204f8`, the reasoning-token budget fix) confirmed the starvation diagnosis while hitting a different transient: with reasoning budgeted, selection completed in one attempt (1077 reasoning tokens inside the 4096 cap), but the repair agent-turn failed on `ReadTimeout: timed out` before any agent turn — so no agent turn records, accepted edits, or verifier run exist from any live run. Run 8 (commit `c80f586`, the per-call timeout extension) removed that read-timeout cutoff — the repair agent-turn call ran about 205 s of provider inference with no client-side cutoff — but the call still ended on the empty-content provider error, so live agent turn records, accepted edits, and a verifier run remain absent. Run 9 (commit `9a10300`, the brief-reasoning prompt plus the 32768-token repair budget with 300 s API-side and 360 s worker windows) then held every client-side bound — selection in one attempt, the repair agent-turn consuming about 675 s with no client-side cutoff — but still ended on the empty-content provider error with no turn records, accepted edits, or verifier run, so no live agent turn records, accepted edits, or verifier run exist from any live run. Run 11 (commit `9482012`, the tolerant-extraction deployment with the same `minimax-m3` model) changed that for turn records only: the repair session produced the first live agent turn records (8 model calls, 50569 prompt / 22866 completion tokens), but it ended `exhausted` on its 8-turn budget after the `finish` gate rejected a stale-`original_sha256` edit proposal, so no edits were applied and the verifier still has not run; the honest outcome remains `upgrade_failed`. Run 12 (commit `82ac0b8`, the file-hash plus empty-OSV fix) then carried the session through the first live accepted edit — 7 ok turns, the edit applied to `requests_unixsocket/adapters.py`, the rebuilt `repair-1` attempt executed — but that attempt failed collection on the model's broken replacement (`IndentationError: unexpected indent`, no tests collected), so the run ended honestly `upgrade_failed` ("collected test IDs changed from baseline") with cleanup observed and zero owned containers, the verifier still has not run, and the single authorized run means no resubmit; live agent turn records and one accepted edit now exist, while accepted-edit-plus-verifier evidence still does not.
5. Next: build the browser workflow and public HTTPS deployment. Run a fresh browser job, complete M01–M12 end-to-end checks, record containment/recovery and demo video, then verify public links for submission.

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
