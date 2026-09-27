/**
 * Start view: validated example card, prefilled submission form,
 * research candidates (not runnable), and the supported-scope statement.
 */

import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiRequestError, createRun, getProfiles } from '../api';
import { ErrorBanner, Field, NoticeBanner, Section } from './ui';
import type { ProfilesResponse } from '../types';

interface StartViewProps {
  onStarted: (runId: number, token: string) => void;
}

export function StartView({ onStarted }: StartViewProps) {
  const [catalog, setCatalog] = useState<ProfilesResponse | null>(null);
  const [catalogError, setCatalogError] = useState<ApiRequestError | null>(null);
  const [loadingCatalog, setLoadingCatalog] = useState(true);

  const [repositoryUrl, setRepositoryUrl] = useState('');
  const [ref, setRef] = useState('');
  const [dependency, setDependency] = useState('');
  const [profileId, setProfileId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiRequestError | null>(null);
  const [replayNote, setReplayNote] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getProfiles()
      .then((response) => {
        if (!active) {
          return;
        }
        setCatalog(response);
        const example = response.profiles[0];
        if (example) {
          setRepositoryUrl(example.repository_url);
          setDependency(example.dependency);
          setProfileId(example.id);
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setCatalogError(
            error instanceof ApiRequestError
              ? error
              : new ApiRequestError(0, 'network_error', 'Could not load the profile catalog.'),
          );
        }
      })
      .finally(() => {
        if (active) {
          setLoadingCatalog(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  const example = catalog?.profiles[0] ?? null;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!profileId || !repositoryUrl || !dependency || submitting) {
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    setReplayNote(null);
    // One fresh idempotency key per click.
    const idempotencyKey = crypto.randomUUID();
    try {
      const created = await createRun({
        repository_url: repositoryUrl,
        ref: ref.trim() ? ref.trim() : null,
        dependency,
        profile_id: profileId,
        idempotency_key: idempotencyKey,
      });
      if (created.token === null) {
        // Idempotent replay: the token was issued at first creation only.
        setReplayNote(
          `Run ${created.run_id} already exists (state ${created.state}). ${created.note ?? ''}`.trim(),
        );
      } else {
        onStarted(created.run_id, created.token);
      }
    } catch (error) {
      if (error instanceof ApiRequestError) {
        setSubmitError(error);
      } else {
        setSubmitError(new ApiRequestError(0, 'network_error', 'Network request failed.'));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="view">
      <header className="view-header">
        <h1>Upgrade Chamber</h1>
        <p className="lead">
          A dependency upgrade, tested against your repository, with the patch and evidence to
          review.
        </p>
      </header>

      <Section title="Validated example">
        {loadingCatalog && <p>Loading the profile catalog…</p>}
        {catalogError && <ErrorBanner code={catalogError.code} message={catalogError.message} />}
        {catalog && !example && (
          <p>No validated execution profile is currently enabled.</p>
        )}
        {example && (
          <div className="panel">
            <h3 className="example-id">{example.id}</h3>
            <Field label="Repository" value={example.repository_url} mono />
            <Field label="Commit" value={example.commit_sha} mono />
            <Field label="Dependency" value={example.dependency} mono />
            <Field
              label="Upgrade"
              value={`${example.baseline_version} → ${example.target_version}`}
              mono
            />
            <Field label="Python" value={example.python} mono />
            <Field label="Description" value={example.description} />
            <Field label="Disclosure" value={example.disclosure} />
            <Field label="Evidence" value={example.evidence} />
          </div>
        )}
      </Section>

      {example && (
        <Section title="Start a run">
          <p className="scope-note">
            Execution is enabled only for the listed validated profile during the hackathon. Any
            other repository receives an unsupported-repository explanation before execution; no
            arbitrary-repository support is claimed.
          </p>
          <form className="form panel" onSubmit={handleSubmit}>
            <div className="form-row">
              <label htmlFor="repository-url">Repository URL</label>
              <input
                id="repository-url"
                name="repository_url"
                type="text"
                value={repositoryUrl}
                onChange={(event) => setRepositoryUrl(event.target.value)}
                spellCheck={false}
                autoComplete="off"
                required
              />
            </div>
            <div className="form-row">
              <label htmlFor="ref">commit/ref (must match the pinned commit)</label>
              <input
                id="ref"
                name="ref"
                type="text"
                value={ref}
                onChange={(event) => setRef(event.target.value)}
                placeholder={example.commit_sha}
                spellCheck={false}
                autoComplete="off"
              />
            </div>
            <div className="form-row">
              <label htmlFor="dependency">Dependency</label>
              <input
                id="dependency"
                name="dependency"
                type="text"
                value={dependency}
                onChange={(event) => setDependency(event.target.value)}
                spellCheck={false}
                autoComplete="off"
                required
              />
            </div>
            <div className="form-row">
              <label htmlFor="profile-id">Profile</label>
              <input
                id="profile-id"
                name="profile_id"
                type="text"
                value={profileId}
                onChange={(event) => setProfileId(event.target.value)}
                spellCheck={false}
                autoComplete="off"
                required
              />
            </div>
            <button type="submit" className="button" disabled={submitting}>
              {submitting ? 'Starting…' : 'Start run'}
            </button>
          </form>
          {replayNote && <NoticeBanner>{replayNote}</NoticeBanner>}
          {submitError && <ErrorBanner code={submitError.code} message={submitError.message} />}
          <p className="muted">
            Uploaded source and logs may be sent to Vultr inference for the bounded repair step.
          </p>
        </Section>
      )}

      <Section title="Research candidates (not runnable)">
        {catalog && catalog.research_candidates.length === 0 && (
          <p className="muted">None listed.</p>
        )}
        <ul className="candidate-list">
          {(catalog?.research_candidates ?? []).map((candidate) => (
            <li key={candidate.id} className="panel candidate-plate">
              <h3 className="example-id">{candidate.id}</h3>
              <Field label="Repository" value={candidate.repository_url} mono />
              <Field label="Commit" value={candidate.commit_sha} mono />
              <Field label="Dependency" value={candidate.dependency} mono />
              <Field label="Status" value={`${candidate.status} — not runnable`} />
              <Field label="Reason" value={candidate.reason} />
            </li>
          ))}
        </ul>
      </Section>

      <footer className="view-footer">
        <p className="muted">
          Passing tests establish compatibility with the executed suite under the recorded
          environment only. They do not prove complete application correctness, exploitability, or
          absence of vulnerabilities.
        </p>
      </footer>
    </main>
  );
}
