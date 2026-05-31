import { createContext, useContext, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { clearAccessKey, get, getAccessKey, setAccessKey } from "./api";
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
  connect: (key: string) => Promise<void>;
  disconnect: () => void;
}

const AuthCtx = createContext<AuthState>(null!);
export const useAuth = () => useContext(AuthCtx);

export default function App() {
  const [loading, setLoading] = useState(true);
  const [authed, setAuthed] = useState(false);
  const [brainName, setBrainName] = useState("JBrain");

  // Validate a key against the server (throws on invalid).
  async function connect(key: string) {
    setAccessKey(key);
    await get("/api/auth/verify"); // 401 -> throws ApiError
    setAuthed(true);
  }

  function disconnect() {
    clearAccessKey();
    setAuthed(false);
  }

  useEffect(() => {
    // Brain name for the key-entry screen (public endpoint).
    get("/api/auth/info").then((i) => setBrainName(i.brain_name || "JBrain")).catch(() => {});

    // If a key is already stored, verify it silently.
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

  const auth: AuthState = { authenticated: authed, brainName, connect, disconnect };

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
