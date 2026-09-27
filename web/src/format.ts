/**
 * Display helpers: timestamps, byte sizes, state color classes, and
 * compact human labels for the event timeline. All values rendered
 * come from the API; nothing here fabricates content.
 */

const SUCCESS_STATES = new Set(['completed', 'passed']);
const FAILURE_STATES = new Set([
  'upgrade_failed',
  'baseline_failed',
  'unsupported',
  'test_failed',
  'install_failed',
  'collection_failed',
  'failed',
]);
const WARN_STATES = new Set(['cancelled', 'timed_out', 'infrastructure_failed']);

/** State color coding: neutral running, green completed/passed, red failures, amber interruptions. */
export function stateClass(state: string): string {
  if (SUCCESS_STATES.has(state)) {
    return 'state-ok';
  }
  if (FAILURE_STATES.has(state)) {
    return 'state-fail';
  }
  if (WARN_STATES.has(state)) {
    return 'state-warn';
  }
  return 'state-neutral';
}

/** Compact UTC timestamp: "2026-09-26 12:34:56 UTC"; falls back to the raw string. */
export function formatUtc(value: string | null | undefined): string {
  if (!value) {
    return '';
  }
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/.exec(value);
  if (match) {
    return `${match[1]} ${match[2]} UTC`;
  }
  return value;
}

/** Human-readable byte size. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return `${bytes} B`;
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ['KiB', 'MiB', 'GiB'];
  let value = bytes;
  let unit = 'B';
  for (const next of units) {
    if (value < 1024) {
      break;
    }
    value /= 1024;
    unit = next;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

/** Compact human label for one timeline event, from its kind and data payload. */
export function eventLabel(kind: string, data: Record<string, unknown>): string {
  switch (kind) {
    case 'state': {
      const state = asText(data.state) ?? 'unknown';
      return `State changed: ${state}`;
    }
    case 'attempt': {
      const phase = asText(data.phase) ?? '?';
      const status = asText(data.status) ?? '?';
      const seconds = typeof data.elapsed_seconds === 'number' ? Math.round(data.elapsed_seconds) : null;
      const counts = countsLabel(data.counts);
      const parts = [`Attempt ${phase}: ${status}`];
      if (seconds !== null) {
        parts.push(`${seconds}s`);
      }
      if (counts) {
        parts.push(counts);
      }
      return parts.join(' — ');
    }
    case 'artifact': {
      const name = asText(data.name) ?? 'artifact';
      const bytes = typeof data.bytes === 'number' ? formatBytes(data.bytes) : null;
      return bytes ? `Artifact saved: ${name} (${bytes})` : `Artifact saved: ${name}`;
    }
    case 'selection': {
      const pkg = asText(data.package) ?? '';
      const version = asText(data.target_version) ?? '';
      return `Target selected: ${pkg} ${version}`.trim();
    }
    case 'advisory': {
      const position = asText(data.position) ?? '?';
      const status = asText(data.status) ?? '?';
      return `Advisory check (${position}): ${status}`;
    }
    case 'repair': {
      const attempt = typeof data.attempt === 'number' ? data.attempt : null;
      const status = asText(data.status) ?? '?';
      return attempt !== null ? `Repair ${attempt}: ${status}` : `Repair: ${status}`;
    }
    case 'terminal': {
      const state = asText(data.state) ?? 'unknown';
      return `Finished: ${state}`;
    }
    default:
      return kind;
  }
}

/** "12 passed, 0 failed, …" from a counts object in event data, or null. */
function countsLabel(counts: unknown): string | null {
  if (counts === null || typeof counts !== 'object') {
    return null;
  }
  const record = counts as Record<string, unknown>;
  const keys = ['passed', 'failed', 'errors', 'skipped', 'xfailed', 'xpassed'] as const;
  const known = keys.filter((key) => typeof record[key] === 'number');
  if (known.length === 0) {
    return null;
  }
  return known.map((key) => `${record[key]} ${key}`).join(', ');
}

/** Truncate a container id to its first 12 characters; empty ids stay visible. */
export function containerLabel(containerId: string): string {
  return containerId ? containerId.slice(0, 12) : '(none)';
}
