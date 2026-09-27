/**
 * Small shared presentational pieces: error banner, state badge with lamp,
 * section module, and a compact key/value row.
 */

import type { ReactNode } from 'react';
import { stateClass } from '../format';

/** Honest error rendering: the backend error code and message verbatim. */
export function ErrorBanner({ code, message }: { code: string; message: string }) {
  return (
    <div className="banner banner-error" role="alert">
      <strong>{code}</strong>
      <p>{message}</p>
    </div>
  );
}

export function NoticeBanner({ children }: { children: ReactNode }) {
  return (
    <div className="banner banner-notice" role="status">
      {children}
    </div>
  );
}

/** State badge: a status lamp plus the exact state string from the API. */
export function StateBadge({ state }: { state: string }) {
  return (
    <span className={`badge ${stateClass(state)}`}>
      <span className="lamp" aria-hidden="true" />
      {state}
    </span>
  );
}

/** Section module: engraved heading directly on the panel ground. */
export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="module">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

/**
 * One "label: value" row. Values render as "—" when absent; pass `mono`
 * for measured values (ids, hashes, versions, timestamps, counts).
 */
export function Field({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className={mono ? 'field-value is-mono' : 'field-value'}>
        {value === '' || value === null || value === undefined ? '—' : value}
      </span>
    </div>
  );
}
