/**
 * Run identity persistence: the run id and access token live in
 * sessionStorage under distinct keys so a browser reload restores
 * the run/result view without the token ever appearing in a URL.
 */

const RUN_ID_KEY = 'upgrade-chamber.run.id';
const RUN_TOKEN_KEY = 'upgrade-chamber.run.token';

export interface RunIdentity {
  id: number;
  token: string;
}

export function saveRunIdentity(id: number, token: string): void {
  try {
    window.sessionStorage.setItem(RUN_ID_KEY, String(id));
    window.sessionStorage.setItem(RUN_TOKEN_KEY, token);
  } catch {
    // Storage unavailable (e.g. disabled); reload recovery simply won't work.
  }
}

export function loadRunIdentity(): RunIdentity | null {
  try {
    const idText = window.sessionStorage.getItem(RUN_ID_KEY);
    const token = window.sessionStorage.getItem(RUN_TOKEN_KEY);
    if (idText === null || token === null) {
      return null;
    }
    const id = Number(idText);
    if (!Number.isInteger(id) || id <= 0 || !token) {
      return null;
    }
    return { id, token };
  } catch {
    return null;
  }
}

export function clearRunIdentity(): void {
  try {
    window.sessionStorage.removeItem(RUN_ID_KEY);
    window.sessionStorage.removeItem(RUN_TOKEN_KEY);
  } catch {
    // Nothing to clear.
  }
}
