/**
 * Live run view: polls the run detail and events every 2 seconds with the
 * bearer token, renders the phase strip, timeline, attempt rows, cancel
 * control, and the containment placard. Polling stops at a terminal state
 * and the parent switches to the result view.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiRequestError,
  cancelRun,
  getEvents,
  getRun,
} from '../api';
import { containerLabel, eventLabel, formatUtc, stateClass } from '../format';
import { ErrorBanner, NoticeBanner, Section, StateBadge } from './ui';
import { TERMINAL_STATES } from '../types';
import type { CancelResponse, RunDetail, RunEvent } from '../types';

const POLL_INTERVAL_MS = 2000;

interface RunViewProps {
  runId: number;
  token: string;
  onTerminal: (detail: RunDetail) => void;
  onAuthFailure: (error: ApiRequestError) => void;
}

/** Strip phases in state-machine order; repair attempts fold into UPGRADE. */
const STRIP_PHASES: ReadonlyArray<{ state: string; label: string }> = [
  { state: 'preparing', label: 'Prepare' },
  { state: 'baseline', label: 'Baseline' },
  { state: 'selecting', label: 'Select' },
  { state: 'upgrading', label: 'Upgrade' },
  { state: 'verifying', label: 'Verify' },
];

function attemptStripIndex(phase: string): number {
  if (phase === 'preparation') {
    return 0;
  }
  if (phase === 'baseline') {
    return 1;
  }
  if (phase.startsWith('repair') || phase === 'candidate') {
    return 3;
  }
  if (phase === 'verifier') {
    return 4;
  }
  return -1;
}

interface PhaseLamp {
  state: 'past' | 'current' | 'failed' | 'warned' | 'off';
}

/** Derive chase-lamp states from the run's current state and recorded attempts. */
function phaseLamps(detail: RunDetail): PhaseLamp[] {
  const order = STRIP_PHASES.map((phase) => phase.state);
  const currentIndex = order.indexOf(detail.state);
  const lamps: PhaseLamp[] = STRIP_PHASES.map(() => ({ state: 'off' }));

  if (detail.state === 'completed') {
    return STRIP_PHASES.map(() => ({ state: 'past' }));
  }
  if (detail.state === 'unsupported') {
    return lamps;
  }

  // Furthest phase the run actually reached, from the recorded attempts.
  let reached = -1;
  for (const attempt of detail.attempts) {
    reached = Math.max(reached, attemptStripIndex(attempt.phase));
  }
  const interrupted = detail.state === 'timed_out' || detail.state === 'cancelled';
  const failed = !interrupted && TERMINAL_STATES.has(detail.state);

  if (currentIndex >= 0) {
    // Active run: every earlier phase dimmed, the current phase chasing.
    for (let i = 0; i < currentIndex; i += 1) {
      lamps[i] = { state: 'past' };
    }
    lamps[currentIndex] = { state: 'current' };
    return lamps;
  }

  // Terminal failure/interruption: the furthest reached phase carries the verdict.
  if (reached >= 0) {
    for (let i = 0; i < reached; i += 1) {
      lamps[i] = { state: 'past' };
    }
    lamps[reached] = { state: failed ? 'failed' : 'warned' };
  }
  return lamps;
}

function PhaseStrip({ detail }: { detail: RunDetail }) {
  const lamps = phaseLamps(detail);
  return (
    <div className="phase-strip" role="img" aria-label={stripAriaLabel(lamps)}>
      {STRIP_PHASES.map((phase, index) => (
        <div key={phase.state} className={`phase-cell is-${lamps[index].state}`}>
          <span className="lamp" aria-hidden="true" />
          <span className="phase-name">{phase.label}</span>
        </div>
      ))}
    </div>
  );
}

function stripAriaLabel(lamps: PhaseLamp[]): string {
  const parts = STRIP_PHASES.map((phase, index) => `${phase.label}: ${lamps[index].state}`);
  return `Run phases. ${parts.join(', ')}.`;
}

export function RunView({ runId, token, onTerminal, onAuthFailure }: RunViewProps) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [pollError, setPollError] = useState<ApiRequestError | null>(null);
  const [cancelResult, setCancelResult] = useState<CancelResponse | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const lastSeenRef = useRef(0);

  const handleTerminal = useCallback(onTerminal, [onTerminal]);
  const handleAuthFailure = useCallback(onAuthFailure, [onAuthFailure]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;

    async function tick() {
      try {
        const [runDetail, eventsResponse] = await Promise.all([
          getRun(runId, token),
          getEvents(runId, token, lastSeenRef.current),
        ]);
        if (stopped) {
          return;
        }
        setPollError(null);
        setDetail(runDetail);
        if (eventsResponse.events.length > 0) {
          const nextEvents = eventsResponse.events;
          setEvents((previous) => [...previous, ...nextEvents]);
          lastSeenRef.current = eventsResponse.last;
        }
        if (TERMINAL_STATES.has(runDetail.state)) {
          stopped = true;
          if (timer !== undefined) {
            window.clearInterval(timer);
          }
          handleTerminal(runDetail);
        }
      } catch (error) {
        if (stopped) {
          return;
        }
        if (error instanceof ApiRequestError && (error.status === 401 || error.status === 404)) {
          stopped = true;
          if (timer !== undefined) {
            window.clearInterval(timer);
          }
          handleAuthFailure(error);
        } else if (error instanceof ApiRequestError) {
          setPollError(error);
        } else {
          setPollError(new ApiRequestError(0, 'network_error', 'Polling failed; retrying.'));
        }
      }
    }

    void tick();
    timer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      if (timer !== undefined) {
        window.clearInterval(timer);
      }
    };
  }, [runId, token, handleTerminal, handleAuthFailure]);

  const isActive = detail !== null && !TERMINAL_STATES.has(detail.state);

  async function handleCancel() {
    if (cancelling) {
      return;
    }
    setCancelling(true);
    setCancelError(null);
    try {
      const response = await cancelRun(runId, token);
      // Render the real response verbatim; never pretend cancellation succeeded.
      setCancelResult(response);
    } catch (error) {
      setCancelError(
        error instanceof ApiRequestError
          ? `${error.code}: ${error.message}`
          : 'Network request failed.',
      );
    } finally {
      setCancelling(false);
    }
  }

  if (pollError && detail === null) {
    return (
      <main className="view">
        <header className="view-header">
          <h1>Run {runId}</h1>
        </header>
        <ErrorBanner code={pollError.code} message={pollError.message} />
        <p className="muted">Polling continues; the run view retries every 2 seconds.</p>
      </main>
    );
  }

  return (
    <main className="view">
      <header className="view-header">
        <h1>Run {runId}</h1>
        {detail && (
          <p className="current-state" aria-live="polite">
            <StateBadge state={detail.state} />
            {detail.status_detail && <span className="detail-text">{detail.status_detail}</span>}
          </p>
        )}
        {detail && <PhaseStrip detail={detail} />}
      </header>

      {pollError && <ErrorBanner code={pollError.code} message={pollError.message} />}

      {isActive && (
        <div className="cancel-row">
          <button
            type="button"
            className="button"
            onClick={() => void handleCancel()}
            disabled={cancelling}
          >
            {cancelling ? 'Cancelling…' : 'Cancel run'}
          </button>
          {cancelResult && (
            <NoticeBanner>
              Cancel request: requested={String(cancelResult.requested)}, state={cancelResult.state}
            </NoticeBanner>
          )}
          {cancelError && <ErrorBanner code="cancel_failed" message={cancelError} />}
        </div>
      )}

      {detail && (
        <>
          <Section title="Events">
            {events.length === 0 && <p className="muted">No events recorded yet.</p>}
            <ol className="timeline">
              {events.map((event) => (
                <li
                  key={event.id}
                  className={`timeline-item ${
                    event.kind === 'terminal'
                      ? stateClass(String(event.data.state ?? ''))
                      : 'state-neutral'
                  }`}
                >
                  <span className="timeline-time">{formatUtc(event.created_utc)}</span>
                  <span className="timeline-kind">{event.kind}</span>
                  <span className="timeline-label">{eventLabel(event.kind, event.data)}</span>
                </li>
              ))}
            </ol>
          </Section>

          <Section title="Attempts">
            {detail.attempts.length === 0 && <p className="muted">No attempts recorded yet.</p>}
            <ul className="attempt-list">
              {detail.attempts.map((attempt, index) => (
                <li key={`${attempt.phase}-${index}`} className="attempt-row">
                  <span className="attempt-phase">{attempt.phase}</span>
                  <span>
                    <StateBadge state={attempt.status} />
                  </span>
                  <span className="attempt-cell">container {containerLabel(attempt.container_id)}</span>
                  <span className="attempt-cell">{Math.round(attempt.elapsed_seconds)} s</span>
                  <span className="attempt-times">
                    <span>started {formatUtc(attempt.started_utc) || '—'}</span>
                    <span>finished {formatUtc(attempt.finished_utc) || '—'}</span>
                  </span>
                </li>
              ))}
            </ul>
          </Section>

          <Section title="Containment">
            <div className="placard">
              <h2>Execution policy — configuration, not proof</h2>
              <p className="placard-note">
                The fixed execution policy below describes how execution containers are
                configured. It is a configuration record, not evidence that every escape is
                impossible.
              </p>
              <ul className="policy-list">
                <li>Read-only container root filesystem.</li>
                <li>Processes run as non-root UID 10001.</li>
                <li>All Linux capabilities dropped; no-new-privileges set.</li>
                <li>Network disabled (none) for installation, tests, and repair.</li>
                <li>1 vCPU, 1 GiB memory, 128 PIDs per execution container.</li>
                <li>Bounded writable tmpfs for work and temporary files.</li>
                <li>
                  Container removal is observed and recorded per attempt (recorded cleanup
                  state: {detail.cleanup_state}).
                </li>
              </ul>
              <dl className="placard-facts">
                <div className="field">
                  <dt className="field-label">Cleanup state (recorded)</dt>
                  <dd className="field-value is-mono">{detail.cleanup_state}</dd>
                </div>
                <div className="field">
                  <dt className="field-label">Execution image</dt>
                  <dd className="field-value is-mono">{detail.image_identity || '—'}</dd>
                </div>
                <div className="field">
                  <dt className="field-label">Job deadline</dt>
                  <dd className="field-value is-mono">{detail.limits.job_deadline_seconds} s</dd>
                </div>
              </dl>
            </div>
          </Section>
        </>
      )}
    </main>
  );
}
