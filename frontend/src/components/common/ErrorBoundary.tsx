import { Component, type ErrorInfo, type ReactNode } from 'react';
import { logger } from '../../utils/logger';

interface Props {
  children: ReactNode;
  fallbackTitle?: string;
}

interface State {
  hasError: boolean;
  error?: Error;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    logger.error('UI_ErrorBoundary', `${error.message}`, errorInfo);
  }

  public render() {
    if (this.state.hasError) {
      return (
        <div className="p-4 m-2 rounded bg-red/10 border border-red/30 text-xs text-red">
          <b>{this.props.fallbackTitle || 'Component Error'}:</b> {this.state.error?.message}
          <button
            onClick={() => this.setState({ hasError: false })}
            className="ml-3 underline font-semibold text-text"
          >
            Retry
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
