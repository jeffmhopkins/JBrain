// Friendly, schema-driven editor for a workflow action_config. It edits the SAME
// JSON string the raw editor shows (single source of truth), mutating only the
// keys it manages — so unmodeled / hand-added keys are preserved. The raw JSON
// stays available as an escape hatch. If the JSON isn't a valid object, fields
// hide rather than risk clobbering.
export interface FieldSpec {
  key: string;
  label: string;
  type: "text" | "textarea" | "number" | "select" | "boolean" | "review";
  required?: boolean;
  help?: string;
  options?: string[];
  default?: boolean; // review: post a review by default · boolean: the default value
}

function Help({ t }: { t?: string }) {
  return t ? <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{t}</div> : null;
}

export default function ConfigFields({
  schema, value, onChange,
}: {
  schema: FieldSpec[];
  value: string;
  onChange: (json: string) => void;
}) {
  if (!schema?.length) return null;

  let obj: any = null;
  try {
    const p = JSON.parse(value || "{}");
    if (p && typeof p === "object" && !Array.isArray(p)) obj = p;
  } catch { /* invalid */ }

  if (obj === null) {
    return <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
      The raw JSON below isn’t a valid object, so fields are hidden — fix it to edit them.
    </p>;
  }

  const set = (k: string, v: any) => {
    const next = { ...obj };
    if (v === undefined) delete next[k]; else next[k] = v;
    onChange(JSON.stringify(next, null, 2));
  };

  return <div>{schema.map((f) => <Field key={f.key} f={f} value={obj[f.key]} set={set} />)}</div>;
}

function Field({ f, value, set }: { f: FieldSpec; value: any; set: (k: string, v: any) => void }) {
  if (f.type === "review") return <ReviewField f={f} value={value} set={set} />;

  if (f.type === "boolean") {
    const checked = value === undefined ? f.default === true : !!value;
    return (
      <div style={{ marginTop: 12 }}>
        <label className="row" style={{ gap: 8 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={checked}
                 onChange={(e) => set(f.key, e.target.checked)} />
          {f.label}
        </label>
        <Help t={f.help} />
      </div>
    );
  }

  const label = (
    <label className="muted" style={{ display: "block", marginTop: 10 }}>
      {f.label}{f.required && " *"}
    </label>
  );

  if (f.type === "textarea") {
    return <div>{label}
      <textarea rows={3} value={value ?? ""} onChange={(e) => set(f.key, e.target.value || undefined)} />
      <Help t={f.help} />
    </div>;
  }
  if (f.type === "number") {
    return <div>{label}
      <input type="number" value={value ?? ""} placeholder="default"
             onChange={(e) => set(f.key, e.target.value === "" ? undefined : Number(e.target.value))} />
      <Help t={f.help} />
    </div>;
  }
  if (f.type === "select") {
    return <div>{label}
      <select className="modal-select" value={value ?? ""} onChange={(e) => set(f.key, e.target.value || undefined)}>
        <option value="">(default)</option>
        {(f.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
      <Help t={f.help} />
    </div>;
  }
  return <div>{label}
    <input value={value ?? ""} placeholder="default" onChange={(e) => set(f.key, e.target.value || undefined)} />
    <Help t={f.help} />
  </div>;
}

// review is tri-state: undefined (use the action's default), false (off), or an
// object {title?, message?}. Checking writes an object; unchecking writes false
// (universally "off"); the checkbox reflects the live default when omitted.
function ReviewField({ f, value, set }: { f: FieldSpec; value: any; set: (k: string, v: any) => void }) {
  const isObj = value && typeof value === "object";
  const checked = isObj || (value === undefined && f.default === true);
  const title = isObj ? (value.title ?? "") : "";
  return (
    <div style={{ marginTop: 12 }}>
      <label className="row" style={{ gap: 8 }}>
        <input type="checkbox" style={{ width: "auto" }} checked={checked}
               onChange={(e) => set(f.key, e.target.checked ? (title ? { title } : {}) : false)} />
        {f.label}
      </label>
      {checked && (
        <input placeholder="Card title (optional)" value={title} style={{ marginTop: 6 }}
               onChange={(e) => set(f.key, { ...(isObj ? value : {}), title: e.target.value || undefined })} />
      )}
    </div>
  );
}
