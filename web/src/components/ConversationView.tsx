import ReactMarkdown from "react-markdown";

export interface Msg { role: string; content: string; }

// One standardized conversation viewer for BOTH guided intakes and research Q&A,
// rendered as chat bubbles (assistant left, person right). Optionally shows the
// guided intake's drafted artifact and a "Delete conversation" action. Owner-only.
export default function ConversationView({
  messages, name, documentMd, noteSlug, onDelete,
}: {
  messages: Msg[];
  name?: string | null;
  documentMd?: string | null;
  noteSlug?: string | null;
  onDelete?: () => void;
}) {
  return (
    <div className="guided-convo">
      <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
        Your eyes only — kept as a record, never saved to your brain or searchable.
      </div>
      {documentMd && (
        <details style={{ marginBottom: 8 }}>
          <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>
            Drafted document{noteSlug ? " (saved as a note — that copy is canonical)" : ""}
          </summary>
          <div className="md guided-doc"><ReactMarkdown>{documentMd}</ReactMarkdown></div>
        </details>
      )}
      {messages.length === 0
        ? <div className="muted" style={{ fontSize: 12 }}>No messages.</div>
        : messages.map((m, i) => (
            <div key={i} className={"msg " + (m.role === "assistant" ? "assistant" : "user")}>
              <span className="convo-who">{m.role === "assistant" ? "AI" : (name || "Them")}</span>{m.content}
            </div>
          ))}
      {onDelete && (
        <button className="ghost danger-hover" style={{ fontSize: 12, marginTop: 8 }} onClick={onDelete}>
          Delete conversation
        </button>
      )}
    </div>
  );
}
