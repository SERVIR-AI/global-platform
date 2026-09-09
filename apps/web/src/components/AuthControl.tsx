import { useAuth } from '@/hooks/useAuth';
import { ChevronDown, LogOut, Repeat } from 'lucide-react';
import { FC } from 'react';

/**
 * The AppBar's right-hand account control. Hidden while the who-am-I probe is
 * loading and when web login is off; shows a Sign in button when signed out and
 * the email with a Sign out / Switch account menu when signed in.
 */
const AuthControl: FC = () => {
  const { status, user, login, logout, switchAccount } = useAuth();

  if (status === 'loading' || status === 'auth-off') return null;

  if (status === 'signed-out') {
    return (
      <button
        type="button"
        onClick={() => login()}
        className="flex items-center h-7 px-3 rounded-md text-sm border border-zinc-600 hover:bg-zinc-700 hover:border-zinc-500"
      >
        Sign in
      </button>
    );
  }

  // The daisyUI dropdown opens while the trigger has focus and closes when it
  // loses it (see src/components/Inputs/Dropdown.tsx for the same pattern).
  const close = () => (document.activeElement as HTMLElement | null)?.blur();

  return (
    <div className="dropdown dropdown-end">
      <div
        tabIndex={0}
        role="button"
        className="flex items-center gap-1.5 h-7 pl-2 pr-1.5 rounded-md text-sm hover:bg-zinc-700"
      >
        <span className="truncate max-w-[14rem]">{user ? (user.email ?? user.sub) : ''}</span>
        <ChevronDown size={14} />
      </div>
      <ul
        tabIndex={-1}
        className="dropdown-content menu bg-base-100 rounded-box z-1 w-60 p-2 shadow-sm text-zinc-800"
      >
        <li>
          <button
            type="button"
            onClick={() => {
              close();
              void logout();
            }}
          >
            <LogOut size={16} />
            Sign out
          </button>
        </li>
        <li>
          <button
            type="button"
            onClick={() => {
              close();
              void switchAccount();
            }}
          >
            <Repeat size={16} />
            Switch account
          </button>
        </li>
      </ul>
    </div>
  );
};

export default AuthControl;
