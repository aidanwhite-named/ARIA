import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { api } from "./lib/api";

const NAV = [
  { to: "/run", label: "실행" },
  { to: "/prompts", label: "Prompt Library" },
  { to: "/history", label: "History" },
  { to: "/settings", label: "Settings" },
];

export default function App() {
  const [version, setVersion] = useState<string>("");
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    api
      .health()
      .then((h) => setVersion(h.version))
      .catch(() => setOffline(true));
  }, []);

  return (
    <div className="app">
      <aside className="sidebar no-print">
        <div className="brand">
          <strong>ARIA</strong>
          <span>로컬 프롬프트 실행기</span>
        </div>
        <nav className="nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          {offline ? (
            <span style={{ color: "var(--danger)" }}>백엔드에 연결할 수 없습니다</span>
          ) : (
            <>
              <div>v{version || "…"}</div>
              <div>127.0.0.1 전용</div>
              <div>API Key 사용 안 함</div>
            </>
          )}
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
