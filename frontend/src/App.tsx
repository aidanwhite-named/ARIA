import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { api } from "./lib/api";

type IconName = "run" | "prompts" | "history" | "settings";

const NAV: Array<{
  to: string;
  label: string;
  note: string;
  icon: IconName;
}> = [
  { to: "/run", label: "분석 실행", note: "Analyze", icon: "run" },
  { to: "/prompts", label: "프롬프트", note: "Directives", icon: "prompts" },
  { to: "/history", label: "실행 기록", note: "Audit trail", icon: "history" },
  { to: "/settings", label: "환경 설정", note: "Runtime", icon: "settings" },
];

function NavIcon({ name }: { name: IconName }) {
  const paths: Record<IconName, ReactNode> = {
    run: (
      <>
        <path d="M6.8 4.8 17.2 12 6.8 19.2V4.8Z" />
        <path d="M18.5 5.5v13" />
      </>
    ),
    prompts: (
      <>
        <path d="M6.5 3.8h8l3 3v13.4H6.5z" />
        <path d="M14.5 3.8v3h3M9.5 11h5M9.5 14.5h5" />
      </>
    ),
    history: (
      <>
        <path d="M5.2 8.2A7.5 7.5 0 1 1 4.8 15" />
        <path d="M4.8 4.8v3.8h3.8M12 7.7v4.7l3 1.8" />
      </>
    ),
    settings: (
      <>
        <circle cx="12" cy="12" r="3.2" />
        <path d="M12 2.8v2M12 19.2v2M21.2 12h-2M4.8 12h-2M18.5 5.5l-1.4 1.4M6.9 17.1l-1.4 1.4M18.5 18.5l-1.4-1.4M6.9 6.9 5.5 5.5" />
      </>
    ),
  };

  return (
    <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
      {paths[name]}
    </svg>
  );
}

function BrandEmblem() {
  return (
    <svg
      className="brand-mark"
      viewBox="0 0 72 72"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id="aria-blue" x1="8" y1="12" x2="43" y2="63">
          <stop stopColor="#9bb8c3" />
          <stop offset="0.45" stopColor="#456f83" />
          <stop offset="1" stopColor="#24495d" />
        </linearGradient>
        <linearGradient id="aria-sage" x1="34" y1="6" x2="61" y2="63">
          <stop stopColor="#c2d0c5" />
          <stop offset="0.48" stopColor="#809985" />
          <stop offset="1" stopColor="#506a5a" />
        </linearGradient>
        <linearGradient id="aria-amber" x1="13" y1="40" x2="53" y2="58">
          <stop stopColor="#f0d3a6" />
          <stop offset="0.52" stopColor="#c38a4d" />
          <stop offset="1" stopColor="#8f5f34" />
        </linearGradient>
        <linearGradient id="aria-stone" x1="18" y1="39" x2="50" y2="58">
          <stop stopColor="#fffdf4" />
          <stop offset="1" stopColor="#d9d7c9" />
        </linearGradient>
        <filter id="aria-depth" x="-30%" y="-30%" width="160%" height="170%">
          <feDropShadow dx="0" dy="4" stdDeviation="3.2" floodColor="#24383c" floodOpacity="0.24" />
        </filter>
      </defs>
      <g filter="url(#aria-depth)">
        <path d="M35.8 5 7.5 55.5 22.7 63 41.4 26.5Z" fill="url(#aria-blue)" />
        <path d="M35.8 5 64.5 55.2 49.4 63 30.4 26.2Z" fill="url(#aria-sage)" />
        <path d="m11.2 49.1 9.6-9.2h34l9.7 15.3-15.1 7.9-5.9-11.4H27.8L22.7 63 7.5 55.5Z" fill="url(#aria-amber)" />
        <path d="m20.8 39.9 7 11.8h15.7l6.6-11.8Z" fill="url(#aria-stone)" />
        <path d="m35.8 5 5.6 21.5-5.7 7.9-5.3-8.2Z" fill="#dfe9e7" opacity="0.78" />
      </g>
    </svg>
  );
}

export default function App() {
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    api
      .health()
      .catch(() => setOffline(true));
  }, []);

  return (
    <div className="app">
      <aside className="sidebar no-print">
        <div className="brand">
          <div className="brand-lockup">
            <div className="brand-mark-wrap">
              <BrandEmblem />
            </div>
            <div className="brand-name">
              <strong>ARIA</strong>
              <span>Auditable Runtime</span>
              <span>for Invention Analysis</span>
            </div>
          </div>
          <p className="brand-thesis">
            Evidence enters whole.<br />
            Every decision leaves a trace.
          </p>
        </div>

        <div className="nav-caption">Workspace</div>
        <nav className="nav" aria-label="주요 메뉴">
          {NAV.map((item, index) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              <span className="nav-index">0{index + 1}</span>
              <NavIcon name={item.icon} />
              <span className="nav-copy">
                <b>{item.label}</b>
                <small>{item.note}</small>
              </span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className={`system-state ${offline ? "offline" : ""}`}>
            <span className="state-dot" />
            <div>
              <strong>{offline ? "Runtime offline" : "Local runtime"}</strong>
              <span>{offline ? "백엔드 연결 확인 필요" : "Private · loopback only"}</span>
            </div>
          </div>
        </div>
      </aside>

      <main className="main">
        <div className="workspace-bar no-print">
          <span className="workspace-kicker">Patent intelligence / evidence workspace</span>
          <span className="workspace-signal">
            <i /> Input integrity preserved
          </span>
        </div>
        <div className="main-inner">
          <Outlet />
        </div>
        <footer className="app-copyright">All right reserved by Aidan</footer>
      </main>
    </div>
  );
}
