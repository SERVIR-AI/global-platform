import { API_BASE_URL, setUnauthorizedHandler } from '@/lib/api';

/**
 * The single auth surface of the SPA. Every /auth/* URL and everything that
 * touches the login session lives here and nowhere else: no other module
 * hardcodes an auth path, reads the cookie, or knows how a login is started.
 * Mostly stateless so the helpers are unit-testable.
 */

/** The four states the app can be in with respect to web login. */
export type AuthStatus = 'loading' | 'signed-out' | 'signed-in' | 'auth-off';

/** Who `/auth/me` says the request is. `email` may be null. */
export interface AuthUser {
  sub: string;
  email: string | null;
}

/** What a `/auth/me` probe can come back as. */
export type FetchMeResult =
  | { status: 'signed-in'; user: AuthUser }
  | { status: 'signed-out' }
  | { status: 'auth-off' };

/** Resolve an auth path against the same base the /api calls use. In prod this
 * is same-origin (empty base); in auth-on dev it is the backend origin. */
export const authUrl = (path: string): string => `${API_BASE_URL}${path}`;

const RETURN_TO_KEY = 'grp.returnTo';

const currentPath = (): string => `${window.location.pathname}${window.location.search}`;

/**
 * Remember where the user was before a login. sessionStorage (not a cookie) so
 * it survives the full-page login round-trip but is per-tab.
 */
export const saveReturnTo = (path: string = currentPath()): void => {
  window.sessionStorage.setItem(RETURN_TO_KEY, path);
};

/** Read and clear the saved return-to (null when nothing is stored). */
export const takeReturnTo = (): string | null => {
  const saved = window.sessionStorage.getItem(RETURN_TO_KEY);
  window.sessionStorage.removeItem(RETURN_TO_KEY);
  return saved;
};

export const clearReturnTo = (): void => {
  window.sessionStorage.removeItem(RETURN_TO_KEY);
};

/** Start a login: remember where the user was, then hand them to the backend's
 * /auth/login, which sends them to AuthKit. Auth-on dev is cross-origin, so the
 * navigation always targets the backend origin (see API_BASE_URL). */
export const login = (returnTo?: string): void => {
  const target = returnTo ?? currentPath();
  saveReturnTo(target);
  window.location.assign(authUrl(`/auth/login?next=${encodeURIComponent(target)}`));
};

/** POST /auth/logout to end the local session server-side, then drop client
 * auth state. Never throws (backend unreachable: the navigation that follows
 * still happens). The 302 body is never read. */
const endLocalSession = async (): Promise<void> => {
  await fetch(authUrl('/auth/logout'), { method: 'POST', credentials: 'include' }).catch(
    () => undefined,
  );
  clearReturnTo();
};

/**
 * Sign out: end the local session and land on the SPA root, which renders its
 * signed-out state (the landing page) — the gate serves "/" to anonymous
 * visitors, so there is no bounce through AuthKit and no silent re-login. A
 * fresh login from there is an explicit choice.
 */
export const logout = async (): Promise<void> => {
  await endLocalSession();
  window.location.assign('/');
};

/**
 * Sign in as a different account: end this session, then go straight to the
 * AuthKit login screen (prompt=login, forwarded by the backend) so a warm
 * session cannot silently resume the old account.
 */
export const switchAccount = async (): Promise<void> => {
  const target = currentPath();
  await endLocalSession();
  saveReturnTo(target);
  window.location.assign(authUrl(`/auth/login?next=${encodeURIComponent(target)}&prompt=login`));
};

/**
 * Who this request is, straight from /auth/me. Deliberately NOT routed through
 * api.ts's request(), so this probe can never trip the 401→login redirect.
 */
export const fetchMe = async (): Promise<FetchMeResult> => {
  let res: Response;
  try {
    res = await fetch(authUrl('/auth/me'), { credentials: 'include' });
  } catch {
    return { status: 'auth-off' }; // backend unreachable — not the same as signed-out
  }
  if (res.status === 200) {
    const body = (await res.json().catch(() => null)) as Partial<AuthUser> | null;
    if (body && typeof body.sub === 'string')
      return { status: 'signed-in', user: { sub: body.sub, email: body.email ?? null } };
    return { status: 'auth-off' }; // a 200 that is not a valid identity is a broken backend
  }
  if (res.status === 401) return { status: 'signed-out' };
  // 404 = the /auth routes are not mounted (web login off); any other status is
  // equally unusable. Both read as auth-off.
  return { status: 'auth-off' };
};

/** After a login round-trip that landed on '/', put the user back on the page
 * they left. This matters only in auth-on dev (cross-origin): in prod the
 * backend's own `next` already landed correctly, so this is a no-op there. */
export const restoreReturnTo = (): void => {
  if (window.location.pathname !== '/') return;
  const saved = takeReturnTo();
  if (saved && saved !== '/') window.history.replaceState(null, '', saved);
};

/** Wire api.ts's session-expiry 401 hook to start a login. Returns the
 * unsubscribe for the effect that registers it. */
export const initUnauthorizedHandling = (): (() => void) =>
  setUnauthorizedHandler(() => login());
