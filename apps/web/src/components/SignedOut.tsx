import { useAuth } from '@/hooks/useAuth';
import { TrafficCone } from 'lucide-react';
import { FC } from 'react';

/** Shown by App when the who-am-I probe says the visitor is signed out: one
 * explanation and one action, and no auto-redirect on load. */
const SignedOut: FC = () => {
  const { login } = useAuth();

  return (
    <div className="grow flex items-center justify-center p-4">
      <div className="flex flex-col items-center text-center gap-3 max-w-md">
        <span className="text-zinc-300">
          <TrafficCone size={44} strokeWidth={1.2} />
        </span>
        <div className="text-lg font-semibold">Sign in to continue</div>
        <p className="text-sm text-zinc-500 leading-relaxed">
          The Global Risk Platform is private — sign in to ask questions, upload data and view risk
          maps.
        </p>
        <button type="button" className="btn btn-primary mt-1" onClick={() => login()}>
          Sign in
        </button>
      </div>
    </div>
  );
};

export default SignedOut;
