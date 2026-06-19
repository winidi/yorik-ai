/**
 * App-level error boundary. Catches render-phase errors anywhere in
 * the routed app and shows a useful fallback instead of a black screen.
 *
 * In production we still want the page to be recoverable — without this,
 * any uncaught render error (e.g. React #300/#310) wipes the entire
 * React tree and leaves the user staring at nothing. With it, they see
 * the error, can copy the stack, and can click "try again" to remount
 * the failing subtree.
 *
 * Class component because that's still the only API for error boundaries
 * (no hook equivalent as of React 19).
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props { children: ReactNode }
interface State {
  err: Error | null;
  componentStack: string | null;
  resetCount: number;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { err: null, componentStack: null, resetCount: 0 };

  static getDerivedStateFromError(err: Error): Partial<State> {
    return { err };
  }

  componentDidCatch(err: Error, info: ErrorInfo) {
    // Log to console so the maintainer can copy the full stack.
    // React 19 puts the offending component path in info.componentStack.
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary] caught:", err, "\nComponent stack:", info.componentStack);
    this.setState({ componentStack: info.componentStack || null });
  }

  reset = () => {
    this.setState(s => ({
      err: null,
      componentStack: null,
      // bumping resetCount changes the key on children → forces remount
      resetCount: s.resetCount + 1,
    }));
  };

  render() {
    if (this.state.err) {
      return (
        <div className="min-h-screen flex items-center justify-center p-6 bg-background text-foreground">
          <div className="max-w-2xl w-full bg-card border border-border rounded-xl p-6 space-y-4">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-full bg-red-500/15 text-red-500 flex items-center justify-center text-lg">!</div>
              <div>
                <h1 className="text-lg font-semibold leading-tight">Something broke</h1>
                <p className="text-xs text-muted-foreground mt-0.5">
                  The page hit a render error. Try Reset; if it keeps
                  happening, copy the details below to share with Yorik
                  maintainers.
                </p>
              </div>
            </div>
            <div className="text-sm font-mono bg-muted/50 rounded-md p-3 overflow-auto max-h-40">
              <div className="font-semibold text-red-600">{this.state.err.name}: {this.state.err.message}</div>
              {this.state.componentStack && (
                <pre className="mt-2 text-xs text-muted-foreground whitespace-pre-wrap">
                  {this.state.componentStack.trim()}
                </pre>
              )}
            </div>
            <div className="flex gap-2">
              <button
                onClick={this.reset}
                className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition"
              >
                Reset
              </button>
              <button
                onClick={() => window.location.reload()}
                className="px-4 py-2 rounded-md bg-muted text-foreground text-sm font-medium hover:bg-muted/80 transition"
              >
                Reload page
              </button>
            </div>
          </div>
        </div>
      );
    }
    return <div key={this.state.resetCount}>{this.props.children}</div>;
  }
}
