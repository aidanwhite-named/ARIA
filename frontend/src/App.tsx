import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api } from "./lib/api";
import { useRunSession } from "./lib/runSession";
import type { JobKind } from "./lib/types";

type IconName = "compare" | "search" | "prompts" | "history" | "settings";

/** 이 프로그램의 두 축.
 *
 *  입력도 도구 정책도 결과물도 다른 작업이므로 메뉴에서부터 갈라 둔다. 둘 다
 *  실행 화면(/run)으로 들어가지만, 어느 축을 여는지는 여기서 정한다.
 */
const WORK_MODES: Array<{
  id: JobKind;
  label: string;
  note: string;
  kicker: string;
  icon: IconName;
}> = [
  {
    id: "patent_analysis",
    label: "구성대비 분석",
    note: "청구항 vs 인용발명",
    kicker: "compare",
    icon: "compare",
  },
  {
    id: "similarity_search",
    label: "유사문헌 검색",
    note: "닮은 특허 · 논문 찾기",
    kicker: "discover",
    icon: "search",
  },
];

const NAV: Array<{
  to: string;
  label: string;
  note: string;
  icon: IconName;
}> = [
  { to: "/prompts", label: "프롬프트", note: "내가 쓰는 지시문", icon: "prompts" },
  { to: "/history", label: "실행 기록", note: "지난 실행 다시 보기", icon: "history" },
  { to: "/settings", label: "환경 설정", note: "기본값 · 안전장치", icon: "settings" },
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

function BrandEmblem() {
  return (
    <img
      className="brand-mark"
      src="/assets/aria-favicon.svg"
      alt=""
      aria-hidden="true"
      draggable="false"
    />
  );
}

export default function App() {
  const [offline, setOffline] = useState(false);
  const location = useLocation();
  // 사이드바가 실행 화면의 작업 유형을 직접 정한다. 실행 상태는 라우터 밖
  // (RunSessionProvider)에 있으므로 메뉴에서도 읽고 쓸 수 있다.
  const { jobKind, setJobKind, setActiveTab, running } = useRunSession();
  const onRunPage = location.pathname === "/run";

  const pickMode = (id: JobKind) => {
    // 실행 중에는 유형을 고정한다. 준비 화면의 전환 카드와 같은 규칙이다.
    if (running || jobKind === id) return;
    setJobKind(id);
    setActiveTab("input");
  };

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

  const activeMode = WORK_MODES.find((mode) => mode.id === jobKind);

  return (
    <div className="app" data-mode={jobKind}>
      <aside className="sidebar no-print">
        <div className="brand">
          <div className="brand-lockup">
            <div className="brand-mark-wrap">
              <BrandEmblem />
            </div>
            <div className="brand-name">
              <strong>ARIA</strong>
              <span>Analysis &amp; Retrieval for Invention Art</span>
              <span className="brand-gloss">뜯어보고, 찾아보고</span>
            </div>
          </div>
        </div>

        <div className="nav-caption">pick your lane</div>
        <nav className="nav nav-modes" aria-label="작업 유형">
          {WORK_MODES.map((mode, index) => {
            const active = onRunPage && jobKind === mode.id;
            const locked = running && jobKind !== mode.id;
            return (
              <NavLink
                key={mode.id}
                to="/run"
                data-kind={mode.id}
                // 두 항목이 같은 주소를 가리키므로 라우터의 활성 판정을 쓰지
                // 않는다. 지금 열려 있는 작업 유형만 켠다.
                className={() =>
                  `${active ? "active" : ""}${locked ? " locked" : ""}`
                }
                aria-current={active ? "page" : "false"}
                title={
                  locked ? "실행 중에는 작업 유형을 바꿀 수 없습니다." : undefined
                }
                onClick={() => pickMode(mode.id)}
              >
                <span className="nav-index">0{index + 1}</span>
                <NavIcon name={mode.icon} />
                <span className="nav-copy">
                  <b>{mode.label}</b>
                  <small>{mode.note}</small>
                </span>
              </NavLink>
            );
          })}
        </nav>

        <div className="nav-caption">workspace</div>
        <nav className="nav" aria-label="관리 메뉴">
          {NAV.map((item, index) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              <span className="nav-index">0{index + 3}</span>
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
              <strong>{offline ? "offline" : "local only"}</strong>
              <span>
                {offline
                  ? "백엔드 연결을 확인해 주세요"
                  : "내 컴퓨터 안에서만 돌아요"}
              </span>
            </div>
          </div>
        </div>
      </aside>

      <main className="main">
        <div className="workspace-bar no-print">
          {/* 지금 무슨 축을 열고 있는지 본문 위에서 한 번 더 말해 준다.
              축을 바꾸면 이 줄의 색과 이름이 함께 바뀐다. */}
          {onRunPage && activeMode ? (
            <span className="workspace-lane">
              <i aria-hidden="true" />
              <b>{activeMode.label}</b>
              <em>{activeMode.kicker}</em>
            </span>
          ) : (
            <span className="workspace-kicker">patent workspace</span>
          )}
          <span className="workspace-signal">
            <i /> 원문 그대로 보관 중
          </span>
        </div>
        <div className="main-inner">
          <Outlet />
        </div>
        <footer className="app-copyright">All rights reserved by Aidan</footer>
      </main>
    </div>
  );
}
