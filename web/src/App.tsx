import { createContext, useContext, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { get, post } from "./api";
import Shell from "./components/Shell";
import Login from "./pages/Login";
import Chat from "./pages/Chat";
import Wiki from "./pages/Wiki";
import NotePage from "./pages/NotePage";
import GraphPage from "./pages/GraphPage";
import SearchPage from "./pages/SearchPage";
import SqlConsole from "./pages/SqlConsole";

interface AuthState {
  authenticated: boolean;
  username?: string;
  brainName: string;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthCtx = createContext<AuthState>(null!);
export const useAuth = () => useContext(AuthCtx);

export default function App() {
  const [loading, setLoading] = useState(true);
  const [authed, setAuthed] = useState(false);
  const [username, setUsername] = useState<string>();
  const [brainName, setBrainName] = useState("JBrain");

  async function refresh() {
    const me = await get("/api/auth/me");
    setAuthed(me.authenticated);
    setUsername(me.username);
    setBrainName(me.brain_name || "JBrain");
  }

  async function logout() {
    await post("/api/auth/logout");
    setAuthed(false);
  }

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="content muted">Loading…</div>;

  const auth: AuthState = { authenticated: authed, username, brainName, refresh, logout };

  return (
    <AuthCtx.Provider value={auth}>
      {!authed ? (
        <Login />
      ) : (
        <Shell>
          <Routes>
            <Route path="/" element={<Navigate to="/chat" replace />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/wiki" element={<Wiki />} />
            <Route path="/note/:slug" element={<NotePage />} />
            <Route path="/graph" element={<GraphPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/sql" element={<SqlConsole />} />
            <Route path="*" element={<Navigate to="/chat" replace />} />
          </Routes>
        </Shell>
      )}
    </AuthCtx.Provider>
  );
}
