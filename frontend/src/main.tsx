import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";

import App from "./App";
import { RunSessionProvider } from "./lib/runSession";
import HistoryPage from "./pages/HistoryPage";
import PromptsPage from "./pages/PromptsPage";
import RunPage from "./pages/RunPage";
import SettingsPage from "./pages/SettingsPage";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* 실행 상태는 라우터 바깥에 둔다. 메뉴를 옮겨도 결과가 남아야 한다. */}
    <RunSessionProvider>
      <HashRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/" element={<App />}>
            <Route index element={<Navigate to="/run" replace />} />
            <Route path="run" element={<RunPage />} />
            <Route path="prompts" element={<PromptsPage />} />
            <Route path="history" element={<HistoryPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/run" replace />} />
          </Route>
        </Routes>
      </HashRouter>
    </RunSessionProvider>
  </React.StrictMode>,
);
