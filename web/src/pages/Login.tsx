import { FormEvent, useState } from "react";
import { post } from "../api";
import { useAuth } from "../App";

export default function Login() {
  const { brainName, refresh } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await post("/api/auth/login", { username, password });
      await refresh();
    } catch (err: any) {
      setError(err.message || "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login card">
      <h1>{brainName}</h1>
      <p className="muted" style={{ textAlign: "center", marginTop: -8 }}>
        Sign in to your brain
      </p>
      <form onSubmit={submit}>
        <label className="muted">Username</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoCapitalize="none" />
        <label className="muted" style={{ marginTop: 12, display: "block" }}>Password</label>
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
        <button className="primary" type="submit" disabled={busy} style={{ width: "100%", marginTop: 16 }}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
