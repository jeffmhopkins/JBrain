import { createContext, useContext, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { clearAccessKey, get, getAccessKey, getServer, setAccessKey, setServer } from "./api";
import { isDemo, setDemo } from "./demo";
import Shell from "./components/Shell";
import KeyEntry from "./pages/KeyEntry";
import Chat from "./pages/Chat";
import Wiki from "./pages/Wiki";
import NotePage from "./pages/NotePage";
import GraphPage from "./pages/GraphPage";
import SearchPage from "./pages/SearchPage";
import SqlConsole from "./pages/SqlConsole";
import WorkflowsPage from "./pages/WorkflowsPage";
import ReviewPage from "./pages/ReviewPage";

interface AuthState {
  authenticated: boolean;
  brainName: string;
  server: string;
  pwaVersion: string;
  serverVersion: string | null;
  versionMismatch: boolean;
  demo: boolean;
  connect: (key: string, server: string) => Promise<void>;
  exploreDemo: () => void;
  disconnect: () => void;
}

const AuthCtx = createContext<AuthState>(null!);
export const useAuth = () => useContext(AuthCtx);
export const PWA_VERSION = __PWA_VERSION__;

export default function App() {
  const [loading, setLoading] = useState(true);
  const [authed, setAuthed] = useState(false);
  const [brainName, setBrainName] = useState("JBrain");
  const [serverVersion, setServerVersion] = useState<string | null>(null);

  // Load public server info (brain name + version) from the configured server.
  async function loadInfo() {
    const i = await get("/api/auth/info");
    setBrainName(i.brain_name || "JBrain");
    setServerVersion(i.version || null);
    return i;
  }

  // Validate a key (and server address) — throws on invalid.
  async function connect(key: string, srv: string) {
    setServer(srv);
    setAccessKey(key);
    await loadInfo();              // resolves the server (and surfaces bad URLs)
    await get("/api/auth/verify"); // 401 -> throws ApiError
    setAuthed(true);
  }

  function exploreDemo() {
    setDemo(true);
    setBrainName("Demo Brain");
    setAuthed(true);
  }

  function disconnect() {
    setDemo(false);
    clearAccessKey();
    setAuthed(false);
  }

  useEffect(() => {
    if (isDemo()) { setBrainName("Demo Brain"); setAuthed(true); setLoading(false); return; }
    loadInfo().catch(() => {});
    const stored = getAccessKey();
    if (stored) {
      get("/api/auth/verify")
        .then(() => setAuthed(true))
        .catch(() => clearAccessKey())
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, []);

  if (loading) return <div className="content muted">Loading…</div>;

  const versionMismatch = !!serverVersion && serverVersion !== PWA_VERSION;
  const auth: AuthState = {
    authenticated: authed, brainName, server: getServer(),
    pwaVersion: PWA_VERSION, serverVersion, versionMismatch, demo: isDemo(),
    connect, exploreDemo, disconnect,
  };

  return (
    <AuthCtx.Provider value={auth}>
      {!authed ? (
        <KeyEntry />
      ) : (
        <Shell>
          <Routes>
            <Route path="/" element={<Navigate to="/chat" replace />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/wiki" element={<Wiki />} />
            <Route path="/note/:slug" element={<NotePage />} />
            <Route path="/graph" element={<GraphPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/flows" element={<WorkflowsPage />} />
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/sql" element={<SqlConsole />} />
            <Route path="*" element={<Navigate to="/chat" replace />} />
          </Routes>
        </Shell>
      )}
    </AuthCtx.Provider>
  );
}
