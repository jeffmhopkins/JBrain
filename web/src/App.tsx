import { createContext, useContext, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { clearAccessKey, get, getAccessKey, getServer, setAccessKey, setServer } from "./api";
import { isDemo, setDemo } from "./demo";
import Shell from "./components/Shell";
import ErrorBoundary from "./components/ErrorBoundary";
import KeyEntry from "./pages/KeyEntry";
import OwnerOnboarding from "./pages/OwnerOnboarding";
import Chat from "./pages/Chat";
import Wiki from "./pages/Wiki";
import ListsPage from "./pages/ListsPage";
import NotePage from "./pages/NotePage";
import GraphPage from "./pages/GraphPage";
import MapPage from "./pages/MapPage";
import SearchPage from "./pages/SearchPage";
import PeoplePage from "./pages/PeoplePage";
import EntitiesPage from "./pages/EntitiesPage";
import SqlConsole from "./pages/SqlConsole";
import WorkflowsPage from "./pages/WorkflowsPage";
import ReviewPage from "./pages/ReviewPage";
import MedicalPage from "./pages/MedicalPage";
import LabsPage from "./pages/LabsPage";
import NotificationHistoryPage from "./pages/NotificationHistoryPage";
import AdvancedHome from "./pages/AdvancedHome";
import ActionsPage from "./pages/ActionsPage";
import SystemPage from "./pages/SystemPage";
import PromptsPanel from "./components/PromptsPanel";
import SharePage from "./pages/SharePage";
import SharesPage from "./pages/SharesPage";

interface AuthState {
  authenticated: boolean;
  brainName: string;
  server: string;
  pwaVersion: string;
  serverVersion: string | null;
  versionMismatch: boolean;
  hasLlm: boolean;
  appTz: string;
  vapidPublicKey: string;
  demo: boolean;
  connect: (key: string, server: string) => Promise<void>;
  exploreDemo: () => void;
  disconnect: () => void;
}

const AuthCtx = createContext<AuthState>(null!);
export const useAuth = () => useContext(AuthCtx);
export const PWA_VERSION = __PWA_VERSION__;

// Did the owner dismiss first-run setup on this device? (They can still set their name
// in Settings.) Keeps the onboarding gate from reappearing every load after a skip.
const ownerSetupSkipped = () => {
  try { return localStorage.getItem("jbrain_owner_setup_skipped") === "1"; }
  catch { return false; }
};

export default function App() {
  const [loading, setLoading] = useState(true);
  const [authed, setAuthed] = useState(false);
  const [ownerSet, setOwnerSet] = useState(false);
  const [brainName, setBrainName] = useState("JBrain");
  const [serverVersion, setServerVersion] = useState<string | null>(null);
  const [hasLlm, setHasLlm] = useState(false);
  const [appTz, setAppTz] = useState<string>("");
  const [vapidPublicKey, setVapidPublicKey] = useState<string>("");

  // Load public server info (brain name) from the configured server. The version
  // is authed-only now, so it comes from /verify instead.
  async function loadInfo() {
    const i = await get("/api/auth/info");
    setBrainName(i.brain_name || "JBrain");
    return i;
  }

  // Validate a key (and server address) — throws on invalid.
  async function connect(key: string, srv: string) {
    setServer(srv);
    setAccessKey(key);
    await loadInfo();                       // resolves the server (surfaces bad URLs)
    const v = await get("/api/auth/verify"); // 401 -> throws ApiError
    setServerVersion(v.version || null);
    setHasLlm(!!v.has_llm);
    setAppTz(v.app_tz || "");
    setVapidPublicKey(v.vapid_public_key || "");
    setOwnerSet(!!v.owner_set);
    setAuthed(true);
  }

  function exploreDemo() {
    setDemo(true);
    setBrainName("Demo Brain");
    setHasLlm(true);   // demo stubs the analysis calls, so the toggle can show
    setOwnerSet(true); // no onboarding in the demo
    setAuthed(true);
  }

  function disconnect() {
    setDemo(false);
    clearAccessKey();
    // Clear cached API responses too, so a shared device leaves nothing behind.
    if ("caches" in window) caches.keys().then((ks) => ks.forEach((k) => caches.delete(k))).catch(() => {});
    setAuthed(false);
  }

  useEffect(() => {
    if (isDemo()) { setBrainName("Demo Brain"); setOwnerSet(true); setAuthed(true); setLoading(false); return; }
    loadInfo().catch(() => {});
    const stored = getAccessKey();
    if (stored) {
      get("/api/auth/verify")
        .then((v) => { setServerVersion(v?.version || null); setHasLlm(!!v?.has_llm); setAppTz(v?.app_tz || ""); setVapidPublicKey(v?.vapid_public_key || ""); setOwnerSet(!!v?.owner_set); setAuthed(true); })
        // Only a real 401 means the key is bad/rotated — forget it and re-prompt.
        // A network error or 5xx (offline, server restarting) must NOT log the
        // user out: stay authed so cached pages still work.
        .catch((e: any) => { if (e?.status === 401) clearAccessKey(); else setAuthed(true); })
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, []);

  const versionMismatch = !!serverVersion && serverVersion !== PWA_VERSION;
  const auth: AuthState = {
    authenticated: authed, brainName, server: getServer(),
    pwaVersion: PWA_VERSION, serverVersion, versionMismatch, hasLlm, appTz, vapidPublicKey, demo: isDemo(),
    connect, exploreDemo, disconnect,
  };

  return (
    <AuthCtx.Provider value={auth}>
      <Routes>
        {/* Ungated, standalone public route — no key, no Shell. */}
        <Route path="/share/:token" element={<SharePage />} />
        <Route path="*" element={
          loading ? <div className="content muted">Loading…</div> :
          !authed ? <KeyEntry /> :
          (!ownerSet && !ownerSetupSkipped()) ? <OwnerOnboarding brainName={brainName} onDone={() => setOwnerSet(true)} /> : (
            <Shell>
              <ErrorBoundary>
              <Routes>
                <Route path="/" element={<Navigate to="/chat" replace />} />
                <Route path="/chat" element={<Chat />} />
                <Route path="/advanced" element={<AdvancedHome />} />
                <Route path="/wiki" element={<Wiki />} />
                <Route path="/lists" element={<ListsPage />} />
                <Route path="/shares" element={<SharesPage />} />
                <Route path="/note/:slug" element={<NotePage />} />
                <Route path="/graph" element={<GraphPage />} />
                <Route path="/map" element={<MapPage />} />
                <Route path="/people" element={<PeoplePage />} />
                <Route path="/entities" element={<EntitiesPage />} />
                <Route path="/medical" element={<MedicalPage />} />
                <Route path="/labs" element={<LabsPage />} />
                <Route path="/search" element={<SearchPage />} />
                <Route path="/prompts" element={<PromptsPanel />} />
                <Route path="/actions" element={<ActionsPage />} />
                <Route path="/flows" element={<WorkflowsPage />} />
                <Route path="/system" element={<SystemPage />} />
                <Route path="/review" element={<ReviewPage />} />
                <Route path="/notifications" element={<NotificationHistoryPage />} />
                <Route path="/sql" element={<SqlConsole />} />
                <Route path="*" element={<Navigate to="/chat" replace />} />
              </Routes>
              </ErrorBoundary>
            </Shell>
          )
        } />
      </Routes>
    </AuthCtx.Provider>
  );
}
