import { Component, type ErrorInfo, type ReactNode } from 'react';

type TraceErrorBoundaryProps = {
  children: ReactNode;
};

type TraceErrorBoundaryState = {
  failed: boolean;
};

/**
 * Keeps a broken trace from taking down the answer it describes.
 *
 * The backend already made this call — `chat.py:97-104` assembles and persists the
 * envelope inside a bare `except` so "a bug in envelope assembly/persistence must never
 * break the actual answer". This is the same rule on the client: the answer is the
 * product, the trace explains it, and an explanation that can break the thing it explains
 * is worse than no explanation.
 *
 * `parseEnvelope` already handles malformed DATA. This catches the other half — a render
 * bug in the trace components themselves, which would otherwise unmount the whole bubble.
 */
class TraceErrorBoundary extends Component<TraceErrorBoundaryProps, TraceErrorBoundaryState> {
  state: TraceErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): TraceErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Swallowed on screen, but never silently: without this the failure is invisible to
    // whoever has to fix it.
    console.error('Trace panel failed to render', error, info);
  }

  render(): ReactNode {
    if (this.state.failed) {
      return (
        <div className="text-xs text-base-content/50">
          The step-by-step breakdown couldn&apos;t be displayed for this answer.
        </div>
      );
    }
    return this.props.children;
  }
}

export default TraceErrorBoundary;
