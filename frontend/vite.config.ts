import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = process.env.ARIA_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: false,
        // SSE 응답이 버퍼링되지 않도록 한다.
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
              proxyRes.headers["cache-control"] = "no-cache, no-transform";
            }
          });
        },
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  // 렌더링 테스트는 DOM 이 필요하다. 순수 로직 테스트만 있을 때는 node 환경으로
  // 충분했지만, 「설정 화면에 이 옵션이 실제로 그려지는가」는 컴포넌트를 실제로
  // 그려 봐야만 답할 수 있다 — 표시 문자열을 만드는 함수만 검증하면 JSX 구조가
  // 깨져도 테스트는 통과한다.
  test: {
    environment: "jsdom",
    globals: false,
    restoreMocks: true,
  },
});
