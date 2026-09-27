/**
 * Application shell: three views (start, live run, result) managed with
 * plain state — no router library. The current run id and token persist
 * in sessionStorage so a browser reload restores the run view.
 */

import { useCallback, useEffect, useState } from 'react';
import { ApiRequestError, getRun } from './api';
import { clearRunIdentity, loadRunIdentity, saveRunIdentity } from './session';
import { ErrorBanner } from './components/ui';
import { RunView } from './components/RunView';
import { ResultView } from './components/ResultView';
import { StartView } from './components/StartView';
import { TERMINAL_STATES } from './types';
import type { RunDetail } from './types';

type View = 'restoring' | 'start' | 'run' | 'result';

export default function App() {
  const [view, setView] = useState<View>('restoring');
  const [runId, setRunId] = useState<number | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [restoreError, setRestoreError] = useState<ApiRequestError | null>(null);

  // Reload recovery: if a run identity is stored, decide between the
  // run view (still active) and the result view (terminal state).
  useEffect(() => {
    const identity = loadRunIdentity();
    if (!identity) {
      setView('start');
      return;
    }
    let active = true;
    getRun(identity.id, identity.token)
      .then((detail) => {
        if (!active) {
          return;
        }
        setRunId(identity.id);
        setToken(identity.token);
        setView(TERMINAL_STATES.has(detail.state) ? 'result' : 'run');
      })
      .catch((error: unknown) => {
        if (!active) {
          return;
        }
        if (
          error instanceof ApiRequestError &&
          (error.status === 401 || error.status === 404)
        ) {
          // Render the real error, clear the unusable identity, offer the start view.
          setRestoreError(error);
          clearRunIdentity();
          setView('start');
        } else {
          // Network trouble: keep the identity and let the run view poll.
          setRunId(identity.id);
          setToken(identity.token);
          setView('run');
        }
      });
    return () => {
      active = false;
    };
  }, []);

  const handleStarted = useCallback((id: number, runToken: string) => {
    setRestoreError(null);
    saveRunIdentity(id, runToken);
    setRunId(id);
    setToken(runToken);
    setView('run');
  }, []);

  const handleTerminal = useCallback((_detail: RunDetail) => {
    setView('result');
  }, []);

  const handleAuthFailure = useCallback(() => {
    clearRunIdentity();
    setRunId(null);
    setToken(null);
    setView('start');
  }, []);

  const handleBackToStart = useCallback(() => {
    setRunId(null);
    setToken(null);
    setView('start');
  }, []);

  if (view === 'restoring') {
    return <p className="restoring">Restoring session…</p>;
  }
  if (view === 'run' && runId !== null && token !== null) {
    return (
      <RunView
        runId={runId}
        token={token}
        onTerminal={handleTerminal}
        onAuthFailure={handleAuthFailure}
      />
    );
  }
  if (view === 'result' && runId !== null && token !== null) {
    return <ResultView runId={runId} token={token} onBack={handleBackToStart} />;
  }
  if (view === 'start') {
    return (
      <>
        {restoreError && (
          <div className="restore-error" role="alert">
            <ErrorBanner code={restoreError.code} message={restoreError.message} />
            <p className="restore-note">
              The stored run identity was unusable and has been cleared.
            </p>
          </div>
        )}
        <StartView onStarted={handleStarted} />
      </>
    );
  }
  return null;
}
