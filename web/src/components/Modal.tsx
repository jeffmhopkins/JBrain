import { ReactNode, useEffect, useRef } from "react";
import { createPortal } from "react-dom";

interface ModalProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;        // sticky action bar; omit → no footer rendered
  headerExtra?: ReactNode;   // e.g. a source badge, between title and Close
  size?: "default" | "wide" | "compact"; // "compact" = small centered popup (not a full-height sheet)
}

// One responsive modal for the whole app. On phone it's a full-height,
// keyboard-aware bottom sheet (sized to --app-height so the sticky footer rides
// above the keyboard); on desktop (≥900px, pure CSS) it's a centered card.
// Portalled to <body> so it escapes the height-locked, transform-prone shell.
export default function Modal({ title, onClose, children, footer, headerExtra, size = "default" }: ModalProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const prevFocus = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    document.body.classList.add("modal-open");
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("modal-open");
      prevFocus?.focus?.();
    };
  }, [onClose]);

  return createPortal(
    <div className={"modal-backdrop" + (size === "compact" ? " compact" : "")} onClick={onClose}>
      <div ref={ref} tabIndex={-1} role="dialog" aria-modal="true"
           className={"modal" + (size === "wide" ? " modal-wide" : "") + (size === "compact" ? " modal-compact" : "")}
           onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong className="modal-title">{title}</strong>
          {headerExtra}
          <span className="spacer" />
          <button className="ghost" onClick={onClose} aria-label="Close">Close</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
