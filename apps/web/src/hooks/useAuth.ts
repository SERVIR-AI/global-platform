import { queryKeys } from '@/lib/queryKeys';
import {
  fetchMe,
  initUnauthorizedHandling,
  login,
  logout,
  restoreReturnTo,
  switchAccount,
  type AuthStatus,
  type AuthUser,
} from '@/lib/auth';
import { useQuery } from '@tanstack/react-query';
import { useEffect } from 'react';

/**
 * The app's auth state: who `/auth/me` says the request is, plus the auth
 * actions. A plain TanStack Query hook — nothing outside React reads or mutates
 * auth state, so there is no store behind it. The 401 handler registered on
 * mount only navigates (back to login); it never touches the query.
 *
 * Auth state changes only via a full-page round-trip (login/logout/switch
 * account all location.assign), so the who-am-I probe runs once on mount and
 * the SPA reloads into a fresh answer — nothing here ever needs a background
 * refetch.
 */
export const useAuth = () => {
  const query = useQuery({
    queryKey: queryKeys.auth.me(),
    queryFn: fetchMe,
    staleTime: Infinity,
    retry: false,
  });

  useEffect(() => {
    // Session expiry on a gated /api call → start a login (see lib/auth.ts).
    const unsubscribe = initUnauthorizedHandling();
    // A login round-trip that landed on '/' puts the user back where they were.
    restoreReturnTo();
    return unsubscribe;
  }, []);

  const data = query.data;
  const status: AuthStatus =
    data === undefined ? (query.isError ? 'auth-off' : 'loading') : data.status;
  const user: AuthUser | null = data && data.status === 'signed-in' ? data.user : null;

  return { status, user, login, logout, switchAccount };
};
