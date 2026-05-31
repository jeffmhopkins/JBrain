import { Component, ErrorInfo, ReactNode } from "react";

// Catches render-time throws so a single bad page shows a recoverable message
// instead of a white screen.
export default class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(_error: Error, _info: ErrorInfo) { /* surface in console */ }

  render() {
    if (this.state.error) {
      return (
        <div className="content">
          <h2>Something went wrong</h2>
          <p className="muted" style={{ fontSize: 13 }}>{this.state.error.message}</p>
          <button className="primary" onClick={() => { this.setState({ error: null }); location.reload(); }}>Reload</button>
        </div>
      );
    }
    return this.props.children;
  }
}
