import { setUnauthorizedHandler, uploadTiff } from '@/lib/api';
import {
  authUrl,
  clearReturnTo,
  fetchMe,
  initUnauthorizedHandling,
  login,
  logout,
  restoreReturnTo,
  saveReturnTo,
  switchAccount,
  takeReturnTo,
} from '@/lib/auth';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

/** The sessionStorage key lib/auth.ts keeps the return-to under. */
const RETURN_TO_KEY = 'grp.returnTo';

const fetchMock = vi.fn();
let assignSpy: MockInstance<(url: string | URL) => void>;

/** A minimal Response whose status/json are all the auth helpers read. */
const response = (status: number, body: unknown = {}): Response =>
  ({
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  }) as unknown as Response;

/** Point the DOM at a path (history.pushState keeps location in sync). */
const goto = (path: string): void => {
  window.history.pushState({}, '', path);
};

beforeEach(() => {
  goto('/');
  window.sessionStorage.clear();
  fetchMock.mockReset().mockResolvedValue(response(200));
  vi.stubGlobal('fetch', fetchMock);
  // Record navigation calls instead of letting the page actually move.
  assignSpy = vi.spyOn(window.location, 'assign').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  // Never leave a 401 handler registered for the next test.
  setUnauthorizedHandler(null);
});

describe('authUrl', () => {
  it('is same-origin when no base is configured (the default)', () => {
    expect(authUrl('/auth/me')).toBe('/auth/me');
    expect(authUrl('/auth/login?next=%2F')).toBe('/auth/login?next=%2F');
  });

  it('targets the configured backend origin, trailing slash trimmed', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:8001/');
    vi.resetModules();
    const fresh = await import('@/lib/auth');
    expect(fresh.authUrl('/auth/me')).toBe('http://127.0.0.1:8001/auth/me');
    expect(fresh.authUrl('/auth/logout')).toBe('http://127.0.0.1:8001/auth/logout');
  });
});

describe('return-to storage', () => {
  it('stores the current path+search by default', () => {
    goto('/chat?q=flood');
    saveReturnTo();
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe('/chat?q=flood');
  });

  it('takeReturnTo reads and clears in one go', () => {
    saveReturnTo('/deep/link');
    expect(takeReturnTo()).toBe('/deep/link');
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
    expect(takeReturnTo()).toBeNull(); // nothing stored second time
  });

  it('clearReturnTo drops a stored value', () => {
    saveReturnTo('/x');
    clearReturnTo();
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
  });
});

describe('login', () => {
  it('remembers where the user was and sends them to /auth/login', () => {
    goto('/ask?q=risk');
    login();
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe('/ask?q=risk');
    expect(assignSpy).toHaveBeenCalledWith('/auth/login?next=%2Fask%3Fq%3Drisk');
  });

  it('honours an explicit return-to and encodes it as one query param', () => {
    login('/deep/link?x=1&y=2');
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe('/deep/link?x=1&y=2');
    expect(assignSpy).toHaveBeenCalledWith('/auth/login?next=%2Fdeep%2Flink%3Fx%3D1%26y%3D2');
  });
});

describe('logout', () => {
  it('POSTs /auth/logout with the cookie, clears the return-to and lands on /', async () => {
    saveReturnTo('/wherever');
    fetchMock.mockResolvedValue(response(302));
    await logout();
    expect(fetchMock).toHaveBeenCalledWith('/auth/logout', {
      method: 'POST',
      credentials: 'include',
    });
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
    expect(assignSpy).toHaveBeenCalledWith('/');
  });

  it('still lands on / when the logout POST fails (backend unreachable)', async () => {
    fetchMock.mockRejectedValue(new TypeError('network down'));
    await expect(logout()).resolves.toBeUndefined();
    expect(assignSpy).toHaveBeenCalledWith('/');
  });
});

describe('switchAccount', () => {
  it('ends the session, then forces the login screen with the current page as next', async () => {
    goto('/fs?tab=brief');
    await switchAccount();
    expect(fetchMock).toHaveBeenCalledWith('/auth/logout', {
      method: 'POST',
      credentials: 'include',
    });
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe('/fs?tab=brief');
    expect(assignSpy).toHaveBeenCalledWith('/auth/login?next=%2Ffs%3Ftab%3Dbrief&prompt=login');
  });
});

describe('fetchMe', () => {
  it('maps a 200 identity to signed-in', async () => {
    fetchMock.mockResolvedValue(response(200, { sub: 'u1', email: 'a@b.c' }));
    await expect(fetchMe()).resolves.toEqual({
      status: 'signed-in',
      user: { sub: 'u1', email: 'a@b.c' },
    });
    expect(fetchMock).toHaveBeenCalledWith('/auth/me', { credentials: 'include' });
  });

  it('maps a 200 with a null email to signed-in', async () => {
    fetchMock.mockResolvedValue(response(200, { sub: 'u1', email: null }));
    await expect(fetchMe()).resolves.toEqual({
      status: 'signed-in',
      user: { sub: 'u1', email: null },
    });
  });

  it('maps 401 to signed-out', async () => {
    fetchMock.mockResolvedValue(response(401, { detail: 'login required' }));
    await expect(fetchMe()).resolves.toEqual({ status: 'signed-out' });
  });

  it('maps 404 (web login off) to auth-off', async () => {
    fetchMock.mockResolvedValue(response(404));
    await expect(fetchMe()).resolves.toEqual({ status: 'auth-off' });
  });

  it('maps any other status to auth-off', async () => {
    fetchMock.mockResolvedValue(response(500));
    await expect(fetchMe()).resolves.toEqual({ status: 'auth-off' });
  });

  it('maps a network failure to auth-off, not signed-out', async () => {
    fetchMock.mockRejectedValue(new TypeError('failed to fetch'));
    await expect(fetchMe()).resolves.toEqual({ status: 'auth-off' });
  });

  it('maps a 200 that is not a valid identity to auth-off', async () => {
    fetchMock.mockResolvedValue(response(200, { hello: 'world' }));
    await expect(fetchMe()).resolves.toEqual({ status: 'auth-off' });
  });

  it('maps a 200 whose body is not JSON to auth-off', async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => {
        throw new SyntaxError('bad json');
      },
    } as unknown as Response);
    await expect(fetchMe()).resolves.toEqual({ status: 'auth-off' });
  });
});

describe('restoreReturnTo', () => {
  it('is a no-op when the SPA is not at /', () => {
    goto('/chat');
    saveReturnTo('/chat');
    restoreReturnTo();
    expect(window.location.pathname).toBe('/chat');
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe('/chat');
  });

  it('restores the stored path when at / and clears it', () => {
    saveReturnTo('/chat?x=1');
    restoreReturnTo();
    expect(window.location.pathname).toBe('/chat');
    expect(window.location.search).toBe('?x=1');
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
  });

  it('ignores a stored "/"', () => {
    saveReturnTo('/');
    restoreReturnTo();
    expect(window.location.pathname).toBe('/');
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
  });

  it('does nothing when nothing was stored', () => {
    restoreReturnTo();
    expect(window.location.pathname).toBe('/');
  });
});

describe('initUnauthorizedHandling (401 → login wiring)', () => {
  it('routes a gated /api 401 to login()', async () => {
    initUnauthorizedHandling();
    fetchMock.mockResolvedValue(response(401, { detail: 'login required' }));
    await expect(uploadTiff(new FormData())).rejects.toThrow('API request failed (401)');
    expect(assignSpy).toHaveBeenCalledWith('/auth/login?next=%2F');
  });

  it('returns an unsubscribe that stops further redirects', async () => {
    const unsubscribe = initUnauthorizedHandling();
    unsubscribe();
    fetchMock.mockResolvedValue(response(401, { detail: 'login required' }));
    await expect(uploadTiff(new FormData())).rejects.toThrow('API request failed (401)');
    expect(assignSpy).not.toHaveBeenCalled();
  });
});
