/**
 * TypeScript interfaces mirroring the backend API contract
 * (src/upgrade_chamber/api.py, profiles.py, worker.py artifacts).
 * No field is invented: every property below exists in the responses.
 */

/** Error body shape: {"error": {"code", "message"}} */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
  };
}

/** GET /api/profiles — an enabled, validated execution profile. */
export interface Profile {
  id: string;
  repository_url: string;
  commit_sha: string;
  dependency: string;
  python: string;
  baseline_version: string;
  target_version: string;
  description: string;
  disclosure: string;
  evidence: string;
}

/** GET /api/profiles — a research candidate (not runnable). */
export interface ResearchCandidate {
  id: string;
  repository_url: string;
  commit_sha: string;
  dependency: string;
  status: string;
  enabled: boolean;
  reason: string;
}

export interface ProfilesResponse {
  profiles: Profile[];
  research_candidates: ResearchCandidate[];
}

/** POST /api/runs request body. */
export interface RunSubmission {
  repository_url: string;
  ref: string | null;
  dependency: string;
  profile_id: string;
  idempotency_key: string;
}

/** POST /api/runs response (201 created; 200 idempotent replay has token null). */
export interface RunCreated {
  run_id: number;
  token: string | null;
  state: string;
  note?: string;
}

/** Pytest outcome counts recorded by the runner marker. */
export interface TestCounts {
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
  xfailed: number;
  xpassed: number;
}

/** Comparable per-phase summary recorded in comparison.json / run result. */
export interface PhaseSummary {
  status: string;
  counts: TestCounts;
  collected_test_ids: string[];
  installed_requests_version: string;
}

/** Result payload embedded in GET /api/runs/{id} as "result". */
export interface RunResultPayload {
  state: string;
  detail: string | null;
  baseline: PhaseSummary | null;
  candidate: PhaseSummary | null;
  model?: {
    model_id: string | null;
    repair_count: number;
  };
}

/** GET /api/runs/{id} — one recorded attempt. */
export interface AttemptRecord {
  phase: string;
  status: string;
  container_id: string;
  elapsed_seconds: number;
  started_utc: string;
  finished_utc: string;
}

/** GET /api/runs/{id} — one recorded artifact. */
export interface ArtifactRecord {
  name: string;
  bytes: number;
  sha256: string;
  kind: string;
}

/** GET /api/runs/{id} response. */
export interface RunDetail {
  id: number;
  state: string;
  status_detail: string | null;
  profile_id: string;
  repository_url: string;
  commit_sha: string;
  dependency: string;
  baseline_version: string;
  target_version: string;
  image_identity: string;
  source_sha256: string;
  created_utc: string;
  updated_utc: string;
  terminal_utc: string | null;
  cleanup_state: string;
  result: RunResultPayload | null;
  attempts: AttemptRecord[];
  artifacts: ArtifactRecord[];
  limits: {
    job_deadline_seconds: number;
  };
  queue_slots_remaining: number;
}

/** One event in GET /api/runs/{id}/events. */
export interface RunEvent {
  id: number;
  kind: string;
  data: Record<string, unknown>;
  created_utc: string;
}

/** GET /api/runs/{id}/events response. */
export interface EventsResponse {
  events: RunEvent[];
  last: number;
}

/** POST /api/runs/{id}/cancel response. */
export interface CancelResponse {
  requested: boolean;
  state: string;
}

/** advisory-baseline.json / advisory-target.json artifact (osv.py snapshot). */
export interface AdvisorySnapshot {
  schema_version: number;
  package: string;
  version: string;
  status: string;
  fetched_utc: string;
  vulnerabilities?: Array<{ id?: unknown }>;
  error?: string;
}

/** comparison.json artifact (worker.py _save_comparison). */
export interface ComparisonArtifact {
  baseline: PhaseSummary | null;
  candidate: PhaseSummary | null;
  collected_ids_match: boolean;
  repairs: Array<{
    attempt: number;
    status: string;
    summary: string;
    edit_paths?: string[];
    model_calls?: number;
  }>;
  verifier: PhaseSummary | null;
  result: string;
  detail: string | null;
}

/** manifest.json artifact (worker.py _save_manifest). */
export interface ManifestArtifact {
  schema_version: number;
  run_id: number;
  created_utc: string;
  profile_id: string;
  repository_url: string;
  commit_sha: string;
  source_sha256: string;
  image_identity: string;
  python: string;
  dependency: {
    name: string;
    baseline_version: string;
    target_version: string;
  };
  test_scope: {
    collected: number;
    first: string | null;
    last: string | null;
  };
  model: {
    model_id: string | null;
    selection: { rationale: string | null };
    repairs: Array<{
      attempt: number;
      status: string;
      summary: string;
      model_calls: number;
    }>;
  } | null;
  advisories: {
    baseline: { status: string; vulnerability_ids: string[] };
    target: { status: string; vulnerability_ids: string[] };
  };
  result: { state: string; detail: string | null };
  cleanup_state: string;
  limitations: string[];
  artifacts: Record<string, { sha256: string; bytes: number }>;
}

/** Run states at which polling stops and the result view is shown. */
export const TERMINAL_STATES: ReadonlySet<string> = new Set([
  'completed',
  'upgrade_failed',
  'baseline_failed',
  'infrastructure_failed',
  'timed_out',
  'cancelled',
  'unsupported',
]);
