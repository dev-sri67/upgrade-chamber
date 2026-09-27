/**
 * Single API client for every browser request.
 *
 * Handles: same-origin relative paths, Authorization bearer injection,
 * JSON parsing, the {"error": {"code", "message"}} error shape, and a
 * 15-second timeout via AbortController.
 */

import type {
  ApiErrorBody,
  CancelResponse,
  EventsResponse,
  ProfilesResponse,
  RunCreated,
  RunDetail,
  RunSubmission,
} from './types';

const REQUEST_TIMEOUT_MS = 15_000;

/** A failed API call carrying the HTTP status and the backend error code. */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiRequestError';
    this.status = status;
    this.code = code;
  }
}

interface RequestOptions {
  method?: string;
  token?: string | null;
  body?: unknown;
}

function toApiError(response: Response, payload: unknown): ApiRequestError {
  const body = payload as ApiErrorBody | null;
  const error = body !== null && typeof body === 'object' ? body.error : undefined;
  const code = typeof error?.code === 'string' && error.code ? error.code : `http_${response.status}`;
  const message =
    typeof error?.message === 'string' && error.message
      ? error.message
      : `Request failed with HTTP ${response.status}.`;
  return new ApiRequestError(response.status, code, message);
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  try {
    const response = await fetch(path, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    const payload = await parseBody(response);
    if (!response.ok) {
      throw toApiError(response, payload);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiRequestError) {
      throw error;
    }
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiRequestError(0, 'timeout', 'Request timed out after 15 seconds.');
    }
    throw new ApiRequestError(0, 'network_error', 'Network request failed.');
  } finally {
    clearTimeout(timer);
  }
}

export function getProfiles(): Promise<ProfilesResponse> {
  return request<ProfilesResponse>('/api/profiles');
}

export function getReady(): Promise<{ ready: boolean; queue_slots: number; database: string }> {
  return request<{ ready: boolean; queue_slots: number; database: string }>('/api/ready');
}

export function createRun(submission: RunSubmission): Promise<RunCreated> {
  return request<RunCreated>('/api/runs', { method: 'POST', body: submission });
}

export function getRun(runId: number, token: string): Promise<RunDetail> {
  return request<RunDetail>(`/api/runs/${runId}`, { token });
}

export function getEvents(runId: number, token: string, after: number): Promise<EventsResponse> {
  return request<EventsResponse>(`/api/runs/${runId}/events?after=${after}`, { token });
}

export function cancelRun(runId: number, token: string): Promise<CancelResponse> {
  return request<CancelResponse>(`/api/runs/${runId}/cancel`, { method: 'POST', token });
}

/** Fetch one artifact as a Blob (the backend serves it as an attachment). */
export async function getArtifactBlob(runId: number, name: string, token: string): Promise<Blob> {
  return request<Blob>(`/api/runs/${runId}/artifacts/${encodeURIComponent(name)}`, {
    token,
  });
}

/** Fetch one artifact and decode it as UTF-8 text (JSON, diff, reports). */
export async function getArtifactText(runId: number, name: string, token: string): Promise<string> {
  const blob = await getArtifactBlob(runId, name, token);
  return blob.text();
}

/**
 * Fetch one artifact with the run token and trigger a browser download
 * through a Blob object URL. The suggested filename is the artifact name.
 */
export async function downloadArtifact(runId: number, name: string, token: string): Promise<void> {
  const blob = await getArtifactBlob(runId, name, token);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
