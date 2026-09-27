---
version: 1
slug: "web-src-app-tsx"
primary_target: "web/src/App.tsx"
related_targets: ["web/src/components/StartView.tsx","web/src/components/RunView.tsx","web/src/components/ResultView.tsx"]
---

# Surface brief — Upgrade Chamber app (single-page, three views)

## Scope and visitor mode

One SPA (`web/src/App.tsx`) with three views — start, live run, result — managed by plain
state; no URL routing. Mode: **Operate** throughout. The run id + token persist in
sessionStorage; reload restores the run view (or the result view once terminal).

## Audience, job, action, proof, constraints

- Audience: Python maintainers evaluating one dependency upgrade; hackathon judges
  verifying containment and evidence honesty.
- Job: submit the validated example run, watch its phases, inspect the recorded outcome,
  download the patch and evidence bundle.
- Actions: start run (POST /api/runs), cancel (POST /cancel), download artifacts, back to
  start (clears stored identity).
- Proof: every rendered datum comes from the API — profile `description`/`disclosure`/
  `evidence`, attempt rows, events, counts, hashes, advisory snapshots, manifest
  limitations. Honest failure and unavailable-advisory wording are binding.
- Constraints: TypeScript strict, no `any`; single api-client module (same-origin paths,
  Bearer header, `{"error":{code,message}}` extraction, 15s AbortController timeout); poll
  every 2s and stop at terminal states; no decorative security badges; no claim of
  arbitrary-repository support.

## Chosen direction and memorable moment

Direction "Controller faceplate" (daylight test-rig panel; DESIGN.md owns the system).
Memorable moment: the phase strip's chase lamps stepping PREPARE → BASELINE → SELECT →
UPGRADE → VERIFY as the run progresses.

## Unresolved decisions

- None blocking. SSE upgrade, dark variant, and a second validated profile are explicitly
  out of scope for this surface.
