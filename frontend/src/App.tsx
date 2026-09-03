import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api } from "./lib/api";
import { useRunSession } from "./lib/runSession";
import type { Job, JobKind } from "./lib/types";
import { WORKSPACES } from "./lib/workspaces";

type IconName = "compare" | "search" | "prompts" | "history" | "settings";

const WORKSPACE_ICON: Record<JobKind, IconName> = {
  patent_analysis: "compare",
  similarity_search: "search",
};

/** 두 작업을 거들 뿐 그 자체가 작업은 아닌 것들. 위계를 다르게 둔다 — 같은
 *  목록에 섞어 번호를 매기면 다섯 개가 한 줄기 순서로 읽힌다. */
const TOOLS: Array<{ to: string; label: string; icon: IconName }> = [
  { to: "/prompts", label: "프롬프트", icon: "prompts" },
  { to: "/history", label: "실행 기록", icon: "history" },
  { to: "/settings", label: "환경 설정", icon: "settings" },
];

function NavIcon({ name }: { name: IconName }) {
  const paths: Record<IconName, ReactNode> = {
    compare: (
      <>
        <path d="M3.6 5.2h6.2v13.6H3.6zM14.2 5.2h6.2v13.6h-6.2z" />
        <path d="M10.4 9.6h3.2M10.4 14.4h3.2" />
      </>
    ),
    search: (
      <>
        <circle cx="10.7" cy="10.7" r="6" />
        <path d="m19.4 19.4-4.3-4.3" />
        <path d="M8.2 9.8h5M8.2 12.4h3.2" />
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

/** 그 작업 칸이 지금 무엇을 하고 있는지.
 *
 *  두 작업이 서로를 기다리지 않는다는 것은 말로 적어 두는 것보다 이렇게 보이는
 *  편이 빠르다 — 한쪽에서 검색이 도는 동안 다른 쪽 칸에 「실행 중」이 켜져
 *  있으면, 순서대로 하는 것이 아니라는 사실이 화면에 그대로 있다.
 *
 *  아무것도 하지 않은 칸에는 아무 말도 적지 않는다. 「비어 있음」을 양쪽에 달아
 *  두면 처음 화면이 경고 두 줄로 시작한다.
 */
function laneState(job: Job | null): { text: string; tone: string } | null {
  if (!job) return null;
  if (job.status === "QUEUED" || job.status === "RUNNING") {
    return { text: "실행 중", tone: "busy" };
  }
  if (job.status === "SUCCEEDED") return { text: "결과 있음", tone: "done" };
  if (job.status === "FAILED") return { text: "실패", tone: "fail" };
  if (job.status === "CANCELLED") return { text: "중단됨", tone: "fail" };
  return null;
}

export default function App() {
  const [offline, setOffline] = useState(false);
  const location = useLocation();
  // 상단 전환기는 주소를 따라간다. 세션의 jobKind 도 함께 맞춰 두면 링크를 누른
  // 그 순간부터 아래 화면이 같은 축을 본다(주소로 직접 들어온 경우와 뒤로 가기는
  // 각 작업 화면이 스스로 맞춘다).
  const { jobs, setJobKind } = useRunSession();

  useEffect(() => {
    // 한 번 실패하면 영영 offline 으로 남아 있었다. 성공했을 때 되돌리지
    // 않았기 때문이다. 백엔드를 다시 띄우면 화면도 따라 붙어야 한다.
    let alive = true;
    const check = () => {
      api
        .health()
        .then(() => alive && setOffline(false))
        .catch(() => alive && setOffline(true));
    };
    check();
    const timer = window.setInterval(check, 15_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const openWorkspace = WORKSPACES.find(
    (workspace) => workspace.path === location.pathname,
  );

  return (
    <div className="app" data-mode={openWorkspace?.id ?? "none"}>
      <header className="topbar no-print">
        <div className="topbar-inner">
          <NavLink className="brand" to="/" aria-label="ARIA 첫 화면">
            <img
              className="brand-mark"
              src="/assets/aria-favicon.svg"
              alt=""
              aria-hidden="true"
              draggable="false"
            />
            <span className="brand-name">
              <strong>ARIA</strong>
              <small>Analysis &amp; Retrieval for Invention Art</small>
            </span>
          </NavLink>

          {/* 두 작업은 나란히 놓인다. 위아래로 세우면 위엣것을 먼저 해야 하는
              것처럼 읽히고, 번호를 붙이면 그 인상이 굳는다. 폴더 탭처럼 아래
              작업 화면에 붙여 두어, 고른 쪽이 곧 지금 열려 있는 화면이라는
              것을 형태로 말한다. */}
          <nav className="lane-switch" aria-label="작업">
            {WORKSPACES.map((workspace) => {
              const state = laneState(jobs[workspace.id]);
              return (
                <NavLink
                  key={workspace.id}
                  to={workspace.path}
                  className="lane-tab"
                  data-kind={workspace.id}
                  onClick={() => setJobKind(workspace.id)}
                >
                  <NavIcon name={WORKSPACE_ICON[workspace.id]} />
                  <b>{workspace.label}</b>
                  <small>{workspace.note}</small>
                  {state && (
                    <span className={`lane-state ${state.tone}`}>
                      {state.tone === "busy" ? (
                        <span className="spinner" aria-hidden="true" />
                      ) : (
                        <i aria-hidden="true" />
                      )}
                      {state.text}
                    </span>
                  )}
                </NavLink>
              );
            })}
          </nav>

          <div className="topbar-side">
            {/* 좁은 화면에서는 글자를 접고 아이콘만 남긴다. 그때도 무엇인지
                알 수 있도록 title 을 붙여 둔다. */}
            <nav className="tool-nav" aria-label="도구">
              {TOOLS.map((tool) => (
                <NavLink
                  key={tool.to}
                  to={tool.to}
                  className={({ isActive }) => (isActive ? "active" : "")}
                  title={tool.label}
                >
                  <NavIcon name={tool.icon} />
                  <span>{tool.label}</span>
                </NavLink>
              ))}
            </nav>
            <span
              className={`link-state ${offline ? "offline" : ""}`}
              title={
                offline
                  ? "백엔드 연결을 확인해 주세요"
                  : "내 컴퓨터 안에서만 돌아요"
              }
            >
              <i aria-hidden="true" />
              <span>{offline ? "offline" : "local only"}</span>
            </span>
          </div>
        </div>
      </header>

      <main className="main">
        <div className="main-inner">
          <Outlet />
        </div>
        <footer className="app-copyright">All rights reserved by Aidan</footer>
      </main>
    </div>
  );
}
