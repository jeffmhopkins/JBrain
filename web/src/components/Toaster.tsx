import { dismissToast, useToasts } from "../toast";

// Mounted once (App). Fixed overlay; non-blocking, dismissible, auto-expiring.
// The live region stays mounted (even when empty) so screen readers reliably
// announce; it goes assertive when an error is present, polite otherwise.
export default function Toaster() {
  const toasts = useToasts();
  const hasError = toasts.some((t) => t.tone === "error");
  return (
    <div className="toaster" role={hasError ? "alert" : "status"} aria-live={hasError ? "assertive" : "polite"}>
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.tone}`}>
          <span className="toast-msg">{t.message}</span>
          <button className="toast-x" aria-label="Dismiss" onClick={() => dismissToast(t.id)}>×</button>
        </div>
      ))}
    </div>
  );
}
