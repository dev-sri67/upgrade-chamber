/**
 * Result view: comparison summary, patch preview, artifact downloads,
 * advisory snapshots, limitations, and the honest results language.
 */

import { useEffect, useState } from 'react';
import {
  ApiRequestError,
  downloadArtifact,
  getArtifactText,
  getRun,
} from '../api';
import { formatUtc, stateClass } from '../format';
import { clearRunIdentity } from '../session';
import { ErrorBanner, Field, Section, StateBadge } from './ui';
import type {
  AdvisorySnapshot,
  ArtifactRecord,
  ComparisonArtifact,
  ManifestArtifact,
  PhaseSummary,
  RunDetail,
} from '../types';

const PATCH_NAME = 'patch.diff';
const COMPARISON_NAME = 'comparison.json';
const MANIFEST_NAME = 'manifest.json';
const ADVISORY_NAMES = ['advisory-baseline.json', 'advisory-target.json'] as const;

/** One line per outcome, condensed from mission.md "Results and honest language". */
const OUTCOME_MEANINGS: Array<[string, string]> = [
  ['Verified upgrade', 'Fresh rerun passes the required checks and advisory lookup completed.'],
  [
    'Tests passed; advisory check unavailable',
    'Execution succeeded, but security data could not be retrieved.',
  ],
  [
    'Upgrade failed',
    'Candidate or repaired code did not satisfy the checks within the budget.',
  ],
  [
    'Baseline failed',
    'The original repository cannot establish a passing comparison. No compatibility repair is attempted.',
  ],
  ['Unsupported setup', 'The repository does not match an enabled execution profile.'],
  ['Timed out / cancelled', 'The execution was interrupted; cleanup status is reported separately.'],
  [
    'Infrastructure failure',
    'The runner, container runtime, or provider failed independently of a test assertion.',
  ],
];

interface ResultViewProps {
  runId: number;
  token: string;
  onBack: () => void;
}

interface LoadedState {
  detail: RunDetail | null;
  detailError: ApiRequestError | null;
  comparison: ComparisonArtifact | null;
  comparisonMissing: boolean;
  /** Honest inline error for comparison.json when it exists but cannot be shown. */
  comparisonError: string | null;
  manifest: ManifestArtifact | null;
  manifestMissing: boolean;
  manifestError: string | null;
  patchText: string | null;
  patchMissing: boolean;
  patchError: string | null;
  /** Advisory result per name: parsed snapshot, 'missing' (404), or an error message. */
  advisories: Record<string, AdvisorySnapshot | 'missing' | string>;
}

const EMPTY_STATE: LoadedState = {
  detail: null,
  detailError: null,
  comparison: null,
  comparisonMissing: false,
  comparisonError: null,
  manifest: null,
  manifestMissing: false,
  manifestError: null,
  patchText: null,
  patchMissing: false,
  patchError: null,
  advisories: {},
};

function isApiError(value: unknown): value is ApiRequestError {
  return value instanceof ApiRequestError;
}

/** Collapse any thrown error into an honest one-line message for the UI. */
function describeError(error: unknown): string {
  if (isApiError(error)) {
    return `${error.code}: ${error.message}`;
  }
  return 'Network request failed.';
}

/**
 * Fetch a text artifact without ever throwing. A 404 becomes `missing`, any
 * other failure becomes an inline error message so one bad artifact can
 * never leave the result view stuck on its loading state.
 */
async function optionalText(
  runId: number,
  name: string,
  token: string,
): Promise<{ text: string | null; missing: boolean; error: string | null }> {
  try {
    return { text: await getArtifactText(runId, name, token), missing: false, error: null };
  } catch (error) {
    if (isApiError(error) && error.status === 404) {
      return { text: null, missing: true, error: null };
    }
    return { text: null, missing: false, error: describeError(error) };
  }
}

/**
 * Fetch a JSON artifact without ever throwing. The bounded parse try/catch
 * renders an honest inline error instead of pretending the file is missing.
 */
async function optionalJson<T>(
  runId: number,
  name: string,
  token: string,
): Promise<{ value: T | null; missing: boolean; error: string | null }> {
  const { text, missing, error } = await optionalText(runId, name, token);
  if (error !== null) {
    return { value: null, missing: false, error };
  }
  if (text === null) {
    return { value: null, missing, error: null };
  }
  try {
    return { value: JSON.parse(text) as T, missing: false, error: null };
  } catch {
    // The artifact exists but is not valid JSON; say so instead of hanging.
    return { value: null, missing: false, error: 'Artifact is not valid JSON.' };
  }
}

export function ResultView({ runId, token, onBack }: ResultViewProps) {
  const [state, setState] = useState<LoadedState>(EMPTY_STATE);
  const [loaded, setLoaded] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [detailResult, comparison, manifest, patch, advisoryResults] =
          await Promise.allSettled([
            getRun(runId, token),
            optionalJson<ComparisonArtifact>(runId, COMPARISON_NAME, token),
            optionalJson<ManifestArtifact>(runId, MANIFEST_NAME, token),
            optionalText(runId, PATCH_NAME, token),
            // All advisory artifacts resolve independently; one failure must
            // not block the others or the rest of the result view.
            Promise.all(
              ADVISORY_NAMES.map(async (name) => {
                const result = await optionalJson<AdvisorySnapshot>(runId, name, token);
                return [name, result] as const;
              }),
            ),
          ]);
        if (!active) {
          return;
        }
        const next: LoadedState = { ...EMPTY_STATE };
        if (detailResult.status === 'fulfilled') {
          next.detail = detailResult.value;
        } else if (isApiError(detailResult.reason)) {
          next.detailError = detailResult.reason;
        } else {
          next.detailError = new ApiRequestError(0, 'network_error', 'Network request failed.');
        }
        if (comparison.status === 'fulfilled') {
          next.comparison = comparison.value.value;
          next.comparisonMissing = comparison.value.missing;
          next.comparisonError = comparison.value.error;
        } else {
          next.comparisonError = describeError(comparison.reason);
        }
        if (manifest.status === 'fulfilled') {
          next.manifest = manifest.value.value;
          next.manifestMissing = manifest.value.missing;
          next.manifestError = manifest.value.error;
        } else {
          next.manifestError = describeError(manifest.reason);
        }
        if (patch.status === 'fulfilled') {
          next.patchText = patch.value.text;
          next.patchMissing = patch.value.missing;
          next.patchError = patch.value.error;
        } else {
          next.patchError = describeError(patch.reason);
        }
        if (advisoryResults.status === 'fulfilled') {
          for (const [name, result] of advisoryResults.value) {
            next.advisories[name] = result.error ?? result.value ?? 'missing';
          }
        } else {
          for (const name of ADVISORY_NAMES) {
            next.advisories[name] = describeError(advisoryResults.reason);
          }
        }
        setState(next);
        setLoaded(true);
      } catch (error) {
        // Absolute fallback: the view must never hang on "Loading run…".
        if (!active) {
          return;
        }
        setState({
          ...EMPTY_STATE,
          detailError: new ApiRequestError(0, 'network_error', describeError(error)),
        });
        setLoaded(true);
      }
    })();
    return () => {
      active = false;
    };
  }, [runId, token]);

  async function handleDownload(name: string) {
    setDownloadError(null);
    try {
      await downloadArtifact(runId, name, token);
    } catch (error) {
      setDownloadError(
        isApiError(error) ? `${error.code}: ${error.message}` : 'Network request failed.',
      );
    }
  }

  if (!loaded) {
    return (
      <main className="view">
        <h1>Result</h1>
        <p>Loading run {runId}…</p>
      </main>
    );
  }

  if (state.detailError) {
    return (
      <main className="view">
        <h1>Result</h1>
        <ErrorBanner code={state.detailError.code} message={state.detailError.message} />
        <p className="muted">
          The run could not be loaded with the stored token. Returning to the start view clears the
          stored run identity.
        </p>
        <button
          type="button"
          className="button"
          onClick={() => {
            clearRunIdentity();
            onBack();
          }}
        >
          Back to start
        </button>
      </main>
    );
  }

  const detail = state.detail;
  if (!detail) {
    return (
      <main className="view">
        <h1>Result</h1>
        <ErrorBanner code="not_found" message="No run detail is available." />
        <button
          type="button"
          className="button"
          onClick={() => {
            clearRunIdentity();
            onBack();
          }}
        >
          Back to start
        </button>
      </main>
    );
  }

  const resultPayload = detail.result;
  const baseline = state.comparison?.baseline ?? resultPayload?.baseline ?? null;
  const candidate = state.comparison?.candidate ?? resultPayload?.candidate ?? null;
  const verifier = state.comparison?.verifier ?? null;
  const repairs = state.comparison?.repairs ?? [];
  const collectedIdsMatch =
    state.comparison !== null
      ? state.comparison.collected_ids_match
      : baseline !== null &&
        candidate !== null &&
        baseline.collected_test_ids.length > 0 &&
        arraysEqual(baseline.collected_test_ids, candidate.collected_test_ids);

  const artifacts: ArtifactRecord[] = detail.artifacts;
  const patchArtifact = artifacts.find((artifact) => artifact.name === PATCH_NAME) ?? null;

  return (
    <main className="view">
      <header className="view-header">
        <h1>Result — run {runId}</h1>
        <p className="current-state" aria-live="polite">
          <StateBadge state={detail.state} />
          {detail.status_detail && <span className="detail-text">{detail.status_detail}</span>}
        </p>
      </header>

      <Section title="Comparison">
        <div className="panel">
          <ComparisonTable
            baseline={baseline}
            candidate={candidate}
            verifier={verifier}
            collectedIdsMatch={collectedIdsMatch}
          />
          {repairs.length > 0 && (
            <div className="repair-list-wrap">
              <h3>Repairs</h3>
              <ul className="repair-list">
                {repairs.map((repair) => (
                  <li key={repair.attempt}>
                    <strong>Repair {repair.attempt}</strong> — status {repair.status}
                    {repair.summary && `: ${repair.summary}`}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <Field label="Result state" value={detail.state} mono />
          <Field label="Result detail" value={resultPayload?.detail ?? detail.status_detail ?? ''} />
          <Field label="Cleanup state" value={detail.cleanup_state} mono />
          <Field label="Terminal at" value={formatUtc(detail.terminal_utc)} mono />
          {resultPayload?.model && (
            <>
              <Field label="Model" value={resultPayload.model.model_id ?? 'unknown'} mono />
              <Field label="Repairs applied" value={resultPayload.model.repair_count} mono />
            </>
          )}
        </div>
      </Section>

      <Section title="Advisories">
        <div className="advisory-grid">
          <AdvisoryPanel
            label="Baseline"
            version={detail.baseline_version}
            snapshot={state.advisories[ADVISORY_NAMES[0]] ?? 'missing'}
          />
          <AdvisoryPanel
            label="Target"
            version={detail.target_version}
            snapshot={state.advisories[ADVISORY_NAMES[1]] ?? 'missing'}
          />
        </div>
      </Section>

      <Section title="Patch preview">
        {state.patchText !== null ? (
          <pre className="readout" tabIndex={0}>
            {state.patchText}
          </pre>
        ) : state.patchError ? (
          <p className="muted">patch.diff could not be loaded: {state.patchError}</p>
        ) : (
          <p className="muted">
            {state.patchMissing
              ? 'No patch.diff was produced for this run (a patch is only created when the upgrade phase was reached).'
              : 'patch.diff could not be loaded.'}
          </p>
        )}
        {patchArtifact && (
          <button
            type="button"
            className="button"
            onClick={() => void handleDownload(PATCH_NAME)}
          >
            Download patch.diff
          </button>
        )}
      </Section>

      <Section title="Artifacts">
        {artifacts.length === 0 && <p className="muted">No artifacts recorded.</p>}
        {artifacts.length > 0 && (
          <div className="panel">
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col" className="num">Size</th>
                    <th scope="col">SHA-256</th>
                    <th scope="col">Download</th>
                  </tr>
                </thead>
                <tbody>
                  {artifacts.map((artifact) => (
                    <tr key={artifact.name}>
                      <td className="mono">{artifact.name}</td>
                      <td className="num">{artifact.bytes} B</td>
                      <td className="sha-cell">{artifact.sha256}</td>
                      <td>
                        <button
                          type="button"
                          className="button button-small"
                          onClick={() => void handleDownload(artifact.name)}
                        >
                          Download
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {downloadError && <ErrorBanner code="download_failed" message={downloadError} />}
          </div>
        )}
      </Section>

      <Section title="Raw evidence">
        <details className="raw-evidence">
          <summary>comparison.json</summary>
          {state.comparison ? (
            <pre className="readout json-view" tabIndex={0}>
              {JSON.stringify(state.comparison, null, 2)}
            </pre>
          ) : (
            <p className="muted detail-empty">
              {state.comparisonError
                ? `comparison.json could not be loaded: ${state.comparisonError}`
                : state.comparisonMissing
                  ? 'comparison.json was not produced for this run.'
                  : 'comparison.json could not be loaded.'}
            </p>
          )}
        </details>
        <details className="raw-evidence">
          <summary>manifest.json</summary>
          {state.manifest ? (
            <pre className="readout json-view" tabIndex={0}>
              {JSON.stringify(state.manifest, null, 2)}
            </pre>
          ) : (
            <p className="muted detail-empty">
              {state.manifestError
                ? `manifest.json could not be loaded: ${state.manifestError}`
                : state.manifestMissing
                  ? 'manifest.json was not produced for this run.'
                  : 'manifest.json could not be loaded.'}
            </p>
          )}
        </details>
      </Section>

      <Section title="Limitations">
        <div className="panel">
          {state.manifest?.limitations?.length ? (
            <ul className="limitation-list">
              {state.manifest.limitations.map((limitation, index) => (
                <li key={index}>{limitation}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">Manifest limitations were not available for this run.</p>
          )}
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Outcome</th>
                  <th scope="col">Meaning</th>
                </tr>
              </thead>
              <tbody>
                {OUTCOME_MEANINGS.map(([outcome, meaning]) => (
                  <tr key={outcome}>
                    <td>
                      <span className={`badge ${stateClass(outcomeToState(outcome))}`}>
                        <span className="lamp" aria-hidden="true" />
                        {outcome}
                      </span>
                    </td>
                    <td>{meaning}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </Section>

      <div className="button-row">
        <button
          type="button"
          className="button"
          onClick={() => void handleDownload(MANIFEST_NAME)}
        >
          Download evidence
        </button>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => {
            clearRunIdentity();
            onBack();
          }}
        >
          Back to start
        </button>
      </div>
    </main>
  );
}

function outcomeToState(outcome: string): string {
  switch (outcome) {
    case 'Verified upgrade':
      return 'completed';
    case 'Upgrade failed':
      return 'upgrade_failed';
    case 'Baseline failed':
      return 'baseline_failed';
    case 'Unsupported setup':
      return 'unsupported';
    case 'Timed out / cancelled':
      return 'cancelled';
    case 'Infrastructure failure':
      return 'infrastructure_failed';
    default:
      return 'completed';
  }
}

function arraysEqual(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function ComparisonTable({
  baseline,
  candidate,
  verifier,
  collectedIdsMatch,
}: {
  baseline: PhaseSummary | null;
  candidate: PhaseSummary | null;
  verifier: PhaseSummary | null;
  collectedIdsMatch: boolean;
}) {
  const rows: Array<{ label: string; phase: PhaseSummary | null }> = [
    { label: 'Baseline', phase: baseline },
    { label: 'Candidate', phase: candidate },
    ...(verifier ? [{ label: 'Verifier', phase: verifier }] : []),
  ];
  return (
    <div>
      <p className="match-indicator" aria-live="polite">
        <span className={`badge ${collectedIdsMatch ? 'state-ok' : 'state-neutral'}`}>
          <span className="lamp" aria-hidden="true" />
          {collectedIdsMatch
            ? 'Collected test IDs match the baseline.'
            : 'Collected test IDs match: not established.'}
        </span>
      </p>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Phase</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">Passed</th>
              <th scope="col" className="num">Failed</th>
              <th scope="col" className="num">Errors</th>
              <th scope="col" className="num">Skipped</th>
              <th scope="col" className="num">Xfailed</th>
              <th scope="col" className="num">Xpassed</th>
              <th scope="col">Requests installed</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ label, phase }) => (
              <tr key={label}>
                <td>{label}</td>
                <td>
                  {phase ? <span className={`badge ${stateClass(phase.status)}`}><span className="lamp" aria-hidden="true" />{phase.status}</span> : '—'}
                </td>
                {(
                  ['passed', 'failed', 'errors', 'skipped', 'xfailed', 'xpassed'] as const
                ).map((key) => (
                  <td key={key} className="num">{phase ? phase.counts[key] : '—'}</td>
                ))}
                <td className="mono">{phase ? phase.installed_requests_version : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AdvisoryPanel({
  label,
  version,
  snapshot,
}: {
  label: string;
  version: string;
  snapshot: AdvisorySnapshot | 'missing' | string;
}) {
  if (typeof snapshot === 'string' && snapshot !== 'missing') {
    return (
      <div className="advisory-cell">
        <h3>
          {label} ({version})
        </h3>
        <p>
          <span className="badge state-warn">
            <span className="lamp" aria-hidden="true" />
            Advisory check unavailable
          </span>
        </p>
        <p className="muted">Advisory data could not be loaded: {snapshot}</p>
      </div>
    );
  }
  if (snapshot === 'missing' || snapshot.status === 'unavailable') {
    return (
      <div className="advisory-cell">
        <h3>
          {label} ({version})
        </h3>
        <p>
          <span className="badge state-warn">
            <span className="lamp" aria-hidden="true" />
            Advisory check unavailable
          </span>
        </p>
        <p className="muted">No advisory data is recorded for this version.</p>
      </div>
    );
  }
  const ids = (snapshot.vulnerabilities ?? [])
    .map((item) => (typeof item?.id === 'string' ? item.id : null))
    .filter((id): id is string => id !== null);
  const uniqueIds = [...new Set(ids)];
  return (
    <div className="advisory-cell">
      <h3>
        {label} ({snapshot.version || version})
      </h3>
      {uniqueIds.length === 0 && label === 'Target' ? (
        <p>Target advisory no longer reported for this installed version</p>
      ) : uniqueIds.length === 0 ? (
        <p>No known advisories reported for this exact version.</p>
      ) : (
        <>
          <p className="muted">Advisories still reported for this exact version:</p>
          <ul className="advisory-ids">
            {uniqueIds.map((id) => (
              <li key={id}>{id}</li>
            ))}
          </ul>
        </>
      )}
      <p className="advisory-queried">Queried {formatUtc(snapshot.fetched_utc) || '—'}</p>
    </div>
  );
}
