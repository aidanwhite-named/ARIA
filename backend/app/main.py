"""ARIA FastAPI 앱.

localhost 전용. 외부 네트워크에 서버를 공개하지 않는다.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, settings_service
from .api import history, jobs, prompts, providers, settings
from .config import HOST, PATHS, PORT
from .db import init_engine
from .prompt_store import PROMPT_STORE

# Windows 에서 asyncio 서브프로세스는 Proactor 이벤트 루프에서만 동작한다.
# Selector 루프면 create_subprocess_exec 이 NotImplementedError 를 던진다.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

# 변경 요청에 요구하는 전용 헤더. 교차 출처에서는 preflight 없이 붙일 수 없다.
CLIENT_HEADER = "X-ARIA-Client"
CLIENT_HEADER_VALUE = "1"

@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    PATHS.ensure()
    PROMPT_STORE.ensure()
    init_engine()
    # agy 권장 열람 허용 목록의 **일회성** 적용. 여기 한 곳에서만 자동으로
    # 병합하고, Provider 검사는 읽기 전용이다. 사용자가 지운 호스트가 다음
    # 검사에서 되살아나지 않게 하는 것이 이 배치의 이유다.
    settings_service.run_agy_allowlist_migration()
    try:
        yield
    finally:
        # ARIA가 종료될 때 브라우저 로그인 대기 프로세스나 agy 도우미 창을
        # 고아 프로세스로 남기지 않는다.
        await providers.LOGIN_MANAGER.shutdown()


app = FastAPI(
    title="ARIA",
    description="분석·검색 전용 프롬프트를 선택한 AI CLI에서 안전하게 실행하는 로컬 프로그램",
    version=__version__,
    lifespan=lifespan,
)

# 개발 중 Vite dev server(5173)만 허용한다. 프로덕션은 동일 출처라 CORS 가 필요 없다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", CLIENT_HEADER],
)


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """localhost 서버도 CSRF 표적이 된다.

    CORS 는 다른 사이트가 *응답을 읽는 것*을 막을 뿐, 요청이 전송되는 것
    자체를 항상 막지는 않는다. 본문 없는 POST(예: smoke-test)는 preflight
    없이 전송되는 단순 요청이라, 외부 웹페이지가 사용자의 계정 사용량을
    발생시킬 수 있다.

    두 겹으로 막는다.
      1) Origin 이 있으면 loopback 이어야 한다.
      2) 변경 요청에는 전용 헤더를 요구한다. 커스텀 헤더는 preflight 를
         강제하므로 교차 출처에서는 붙일 수 없다.
    """
    if request.method in _MUTATING:
        origin = request.headers.get("origin")
        if origin:
            host = urlsplit(origin).hostname or ""
            if host not in _LOOPBACK_HOSTS:
                return JSONResponse(
                    {"detail": "교차 출처 요청이 차단되었습니다."}, status_code=403
                )
        if request.headers.get(CLIENT_HEADER.lower()) != CLIENT_HEADER_VALUE:
            return JSONResponse(
                {
                    "detail": (
                        f"변경 요청에는 {CLIENT_HEADER} 헤더가 필요합니다. "
                        "ARIA UI 밖에서 호출한 경우 헤더를 추가하십시오."
                    )
                },
                status_code=403,
            )
    return await call_next(request)

app.include_router(prompts.router)
app.include_router(providers.router)
app.include_router(jobs.router)
app.include_router(history.router)
app.include_router(settings.router)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "data_dir": str(PATHS.data_dir),
        "host": HOST,
        "port": PORT,
    }


if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"detail": "프론트엔드가 빌드되지 않았습니다."}, status_code=404)
