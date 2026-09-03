import React from "react";
import ReactDOM from "react-dom/client";
import {
  HashRouter,
  Navigate,
  Route,
  Routes,
  useSearchParams,
} from "react-router-dom";

import App from "./App";
import { RunSessionProvider, useRunSession } from "./lib/runSession";
import { workspacePath } from "./lib/workspaces";
import HistoryPage from "./pages/HistoryPage";
import PromptsPage from "./pages/PromptsPage";
import RunPage from "./pages/RunPage";
import SettingsPage from "./pages/SettingsPage";
import "./styles.css";

/** 첫 화면은 마지막으로 열어 두었던 작업이다.
 *
 *  둘 중 하나를 언제나 먼저 여는 것은 그쪽이 시작점이라는 뜻이 된다. 두 작업은
 *  각자 들르는 자리이므로, 나갔던 그 자리로 돌아온다.
 */
function LastWorkspace() {
  const { jobKind } = useRunSession();
  const [params] = useSearchParams();
  const query = params.toString();
  return (
    <Navigate to={`${workspacePath(jobKind)}${query ? `?${query}` : ""}`} replace />
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* 실행 상태는 라우터 바깥에 둔다. 메뉴를 옮겨도 결과가 남아야 한다. */}
    <RunSessionProvider>
      <HashRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/" element={<App />}>
            <Route index element={<LastWorkspace />} />
            {/* 두 작업은 각자의 주소를 갖는다. 즐겨찾기도 뒤로 가기도 어느
                작업이었는지 기억한다. */}
            <Route
              path="analysis"
              element={<RunPage kind="patent_analysis" />}
            />
            <Route
              path="search"
              element={<RunPage kind="similarity_search" />}
            />
            <Route path="prompts" element={<PromptsPage />} />
            <Route path="history" element={<HistoryPage />} />
            <Route path="settings" element={<SettingsPage />} />
            {/* 주소를 나누기 전의 즐겨찾기와 세션 캐시. ?job= 이 붙어 있으면
                작업 화면이 그 실행의 종류를 읽고 제 주소로 다시 보낸다. */}
            <Route path="run" element={<LastWorkspace />} />
            <Route path="*" element={<LastWorkspace />} />
          </Route>
        </Routes>
      </HashRouter>
    </RunSessionProvider>
  </React.StrictMode>,
);
