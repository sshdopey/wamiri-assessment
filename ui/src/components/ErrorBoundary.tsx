import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
  /** Optional fallback UI — if not provided, a default card is shown. */
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * React Error Boundary — catches rendering errors in child components
 * and displays a fallback UI instead of crashing the entire app.
 *
 * Usage:
 *   <ErrorBoundary>
 *     <YourComponent />
 *   </ErrorBoundary>
 */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Log to console in dev — in production, send to error tracking service
    console.error("[ErrorBoundary] Caught error:", error, info.componentStack);
  }

  private handleReset = () => {
    this.setState({ hasError: false, error: null });
  };

  private handleReload = () => {
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;

      return (
        <div
          role="alert"
          aria-live="assertive"
          className="flex flex-col items-center justify-center min-h-[400px] p-8 text-center"
        >
          <div className="rounded-full bg-destructive/10 p-4 mb-4">
            <AlertTriangle className="h-8 w-8 text-destructive" />
          </div>
          <h2 className="text-lg font-semibold mb-2">Something went wrong</h2>
          <p className="text-sm text-muted-foreground max-w-md mb-1">
            An unexpected error occurred while rendering this page.
          </p>
          {this.state.error && (
            <p className="text-xs text-muted-foreground font-mono bg-muted rounded-md px-3 py-1.5 mt-2 max-w-lg truncate">
              {this.state.error.message}
            </p>
          )}
          <div className="flex gap-3 mt-6">
            <Button variant="outline" onClick={this.handleReset}>
              <RefreshCw className="h-4 w-4 mr-1.5" />
              Try Again
            </Button>
            <Button onClick={this.handleReload}>Reload Page</Button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
