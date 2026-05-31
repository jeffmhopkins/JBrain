import { useState } from "react";
import Wiki from "./Wiki";
import GraphPage from "./GraphPage";
import SearchPage from "./SearchPage";

type Tab = "notes" | "search" | "graph";

// Phone "Browse" group: Notes / Search / Graph under one screen.
export default function BrowsePage() {
  const [tab, setTab] = useState<Tab>("notes");
  return (
    <div>
      <div className="content" style={{ paddingBottom: 0 }}>
        <div className="row" style={{ gap: 6 }}>
          <button className={tab === "notes" ? "primary" : "ghost"} onClick={() => setTab("notes")}>Notes</button>
          <button className={tab === "search" ? "primary" : "ghost"} onClick={() => setTab("search")}>Search</button>
          <button className={tab === "graph" ? "primary" : "ghost"} onClick={() => setTab("graph")}>Graph</button>
        </div>
      </div>
      {tab === "notes" ? <Wiki /> : tab === "search" ? <SearchPage /> : <GraphPage />}
    </div>
  );
}
