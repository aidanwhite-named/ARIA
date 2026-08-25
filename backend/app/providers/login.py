"""각 CLI가 소유하는 인증 흐름을 ARIA UI에서 시작하고 관찰한다.

ARIA는 비밀번호, API Key, OAuth code/token, CLI 출력 원문을 저장하지 않는다.
브라우저 인증은 CLI 프로세스가 직접 수행하고 자격증명도 CLI의 기본 저장소에
남긴다. 이 모듈이 보관하는 것은 메모리상의 진행 상태와 프로세스 핸들뿐이다.

Claude와 Codex는 로그인도 로그아웃도 비대화식 자식 프로세스로 처리할 수 있다.
agy는 전용 login/logout 명령이 없고 대화형 TUI 안에서만 인증 상태를 바꿀 수
있으므로, Windows에서만 샌드박스가 켜진 별도 도우미 콘솔을 연다. 로그인과
로그아웃은 같은 세션 관리자를 쓰고 intent 로만 구분한다.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import PATHS
from ..enums import AuthState
from ..execution import process as proc
from ..execution.process import kill_process_tree
from .agy_cli import resolve_agy
from .env import build_child_env
from .registry import probe_one
from .resolver import ResolvedExecutable, resolve_claude, resolve_simple


TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
SUPPORTED_PROVIDERS = frozenset({"agy", "claude", "codex"})

LOGIN_INTENT = "login"
LOGOUT_INTENT = "logout"

# 전용 logout 명령이 없어서 대화형 도우미 창으로만 로그아웃할 수 있는 Provider.
# 실측(agy 1.1.19): agy --print /logout 은 CLI가 명시적으로 거부한다.
#   Error: /logout is not available in print mode (it clears stored
#   credentials, an effect that outlives the run)
# agy -p /help 가 알려주는 print mode 허용 명령 목록에도 /logout 은 없다.
HELPER_WINDOW_LOGOUT_PROVIDERS = frozenset({"agy"})

_LOGIN_TIMEOUT_SECONDS = 15 * 60
_LOGOUT_HELPER_TIMEOUT_SECONDS = 15 * 60
_LOGOUT_TIMEOUT_SECONDS = 60


class LoginError(RuntimeError):
    """사용자에게 그대로 보여도 되는 로그인/로그아웃 시작 오류."""


async def logout_provider(
    provider: str, *, executable_override: str | None = None
) -> dict:
    """CLI가 저장한 현재 계정 자격증명을 CLI 자체 logout 명령으로 지운다.

    전용 logout 명령을 가진 Provider(claude, codex)만 여기서 처리한다. agy는
    print mode에서 /logout 이 거부되므로 ``ProviderLoginManager.start_logout``
    이 여는 도우미 창 세션이 담당한다.

    로그아웃 명령의 stdout/stderr에는 계정 정보나 로컬 경로가 섞일 수 있으므로
    성공 여부 판정에만 사용하고 API 응답에는 넣지 않는다.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise LoginError("알 수 없는 Provider 입니다.")
    if provider in HELPER_WINDOW_LOGOUT_PROVIDERS:
        # 여기까지 왔다면 라우팅 실수다. 조용히 잘못된 명령을 실행하지 않는다.
        raise LoginError(
            f"{provider} 는 비대화식 로그아웃 명령이 없습니다. "
            "로그아웃 도우미 창을 사용하세요."
        )

    resolved = ProviderLoginManager._resolve(provider, executable_override)
    if resolved is None:
        raise LoginError(f"{provider} CLI를 찾을 수 없습니다. 먼저 설치 경로를 확인하세요.")

    args = ["auth", "logout"] if provider == "claude" else ["logout"]

    run = await proc.run_capture(
        resolved.command(args),
        cwd=ProviderLoginManager._login_dir(),
        env=build_child_env(),
        timeout_seconds=_LOGOUT_TIMEOUT_SECONDS,
    )

    # CLI가 로그아웃 뒤 인증 화면으로 전환하며 0이 아닌 코드로 끝나는 구현도
    # 있을 수 있으므로, 종료 코드보다 실제 인증 상태를 먼저 확인한다.
    result = await probe_one(
        provider,
        {provider: executable_override} if executable_override else None,
    )
    if result is not None and result.auth_state == AuthState.NOT_LOGGED_IN:
        return {
            "provider": provider,
            "mode": "immediate",
            "ok": True,
            "auth_state": AuthState.NOT_LOGGED_IN,
            "message": "로그아웃했습니다.",
        }

    if run.launch_error:
        raise LoginError("로그아웃 명령을 시작하지 못했습니다.")
    if run.timed_out:
        raise LoginError("로그아웃 확인 시간이 초과되었습니다.")
    if run.exit_code != 0:
        raise LoginError("CLI 로그아웃 명령이 완료되지 않았습니다.")
    raise LoginError("로그아웃 완료를 확인하지 못했습니다. CLI 상태를 다시 확인하세요.")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _success_message(intent: str) -> str:
    if intent == LOGOUT_INTENT:
        return "로그아웃했습니다."
    return "로그인이 완료되었습니다."


def _unconfirmed_message(intent: str) -> str:
    if intent == LOGOUT_INTENT:
        return (
            "로그아웃 완료를 확인하지 못했습니다. 도우미 창에 /logout 을 "
            "입력했는지 확인하고 다시 시도하세요."
        )
    return "로그인 완료를 확인하지 못했습니다. 다시 시도하거나 CLI 상태를 확인하세요."


def _cancel_message(intent: str) -> str:
    if intent == LOGOUT_INTENT:
        return "로그아웃을 취소했습니다."
    return "로그인을 취소했습니다."


def _start_failure_message(intent: str) -> str:
    if intent == LOGOUT_INTENT:
        return "로그아웃 도우미를 시작하지 못했습니다."
    return "로그인 도우미를 시작하지 못했습니다."


@dataclass
class LoginSession:
    session_id: str
    provider: str
    method: str
    mode: str
    intent: str = LOGIN_INTENT
    state: str = "STARTING"
    message: str = "로그인을 준비하고 있습니다."
    started_at: str = field(default_factory=_now)
    completed_at: str | None = None
    # 재검사에 필요하다. 로컬 경로이므로 public() 에는 넣지 않는다.
    executable_override: str | None = field(default=None, repr=False)
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "session_id": self.session_id,
            "provider": self.provider,
            "intent": self.intent,
            "method": self.method,
            "mode": self.mode,
            "state": self.state,
            "message": self.message,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "can_cancel": self.state not in TERMINAL_STATES,
        }


class ProviderLoginManager:
    """한 Provider당 하나의 인증 프로세스만 실행하는 메모리 세션 관리자."""

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}
        self._active_by_provider: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize_method(provider: str, method: str | None) -> str:
        selected = (method or "").strip().lower()
        if provider == "claude":
            selected = selected or "subscription"
            if selected not in {"subscription", "console"}:
                raise LoginError("Claude 로그인 방식이 올바르지 않습니다.")
            return selected
        if provider == "codex":
            if selected not in {"", "chatgpt"}:
                raise LoginError("Codex는 ChatGPT 브라우저 로그인만 지원합니다.")
            return "chatgpt"
        if provider == "agy":
            if selected not in {"", "google"}:
                raise LoginError("agy 로그인 방식이 올바르지 않습니다.")
            return "google"
        raise LoginError("알 수 없는 Provider 입니다.")

    @staticmethod
    def _resolve(provider: str, override: str | None) -> ResolvedExecutable | None:
        if provider == "claude":
            return resolve_claude(override)
        if provider == "codex":
            return resolve_simple("codex", override)
        if provider == "agy":
            return resolve_agy(override)
        return None

    async def start(
        self,
        provider: str,
        *,
        method: str | None = None,
        executable_override: str | None = None,
    ) -> dict:
        if provider not in SUPPORTED_PROVIDERS:
            raise LoginError("알 수 없는 Provider 입니다.")
        selected_method = self._normalize_method(provider, method)
        resolved = self._resolve(provider, executable_override)
        if resolved is None:
            raise LoginError(f"{provider} CLI를 찾을 수 없습니다. 먼저 설치 경로를 확인하세요.")

        return await self._begin(
            provider,
            intent=LOGIN_INTENT,
            method=selected_method,
            mode="helper_window" if provider == "agy" else "browser",
            message="로그인을 준비하고 있습니다.",
            resolved=resolved,
            executable_override=executable_override,
        )

    async def start_logout(
        self, provider: str, *, executable_override: str | None = None
    ) -> dict:
        """전용 logout 명령이 없는 CLI를 위한 도우미 창 로그아웃 세션.

        로그인 도우미와 같은 수명주기를 쓴다. 창이 닫히면 인증 상태를 다시
        검사해서, CLI가 실제로 자격증명을 지웠을 때만 성공으로 표시한다.
        """
        if provider not in HELPER_WINDOW_LOGOUT_PROVIDERS:
            raise LoginError("이 Provider 는 도우미 창 로그아웃을 사용하지 않습니다.")
        if sys.platform != "win32":
            raise LoginError("현재 agy 로그아웃 도우미는 Windows에서만 지원합니다.")
        resolved = self._resolve(provider, executable_override)
        if resolved is None:
            raise LoginError(f"{provider} CLI를 찾을 수 없습니다. 먼저 설치 경로를 확인하세요.")

        return await self._begin(
            provider,
            intent=LOGOUT_INTENT,
            method="slash_command",
            mode="helper_window",
            message="로그아웃 도우미를 준비하고 있습니다.",
            resolved=resolved,
            executable_override=executable_override,
        )

    async def _begin(
        self,
        provider: str,
        *,
        intent: str,
        method: str,
        mode: str,
        message: str,
        resolved: ResolvedExecutable,
        executable_override: str | None,
    ) -> dict:
        async with self._lock:
            active_id = self._active_by_provider.get(provider)
            if active_id:
                active = self._sessions.get(active_id)
                if active is not None and active.state not in TERMINAL_STATES:
                    if active.intent != intent:
                        raise LoginError(
                            "같은 Provider 의 다른 인증 작업이 진행 중입니다. "
                            "먼저 그 작업을 끝내거나 취소하세요."
                        )
                    return active.public()

            session = LoginSession(
                session_id=str(uuid.uuid4()),
                provider=provider,
                method=method,
                mode=mode,
                intent=intent,
                message=message,
                executable_override=executable_override,
            )
            self._sessions[session.session_id] = session
            self._active_by_provider[provider] = session.session_id
            self._prune_finished()
            session.task = asyncio.create_task(
                self._run(session, resolved, executable_override),
                name=f"aria-{intent}-{provider}-{session.session_id}",
            )
            return session.public()

    async def get(
        self, provider: str, session_id: str, *, intent: str | None = None
    ) -> dict | None:
        session = self._sessions.get(session_id)
        if session is None or session.provider != provider:
            return None
        if intent is not None and session.intent != intent:
            return None
        return session.public()

    async def cancel(
        self,
        provider: str,
        session_id: str,
        *,
        intent: str | None = None,
        verify: bool = True,
    ) -> dict | None:
        session = self._sessions.get(session_id)
        if session is None or session.provider != provider:
            return None
        if intent is not None and session.intent != intent:
            return None
        if session.state in TERMINAL_STATES:
            return session.public()

        process = session.process
        if process is not None and process.returncode is None and process.pid is not None:
            await asyncio.to_thread(kill_process_tree, process.pid)
        if session.task is not None and not session.task.done():
            session.task.cancel()

        state, message = "CANCELLED", _cancel_message(session.intent)
        if (
            verify
            and session.mode == "helper_window"
            and await self._helper_reached_expected_auth(session)
        ):
            # agy TUI는 로그인/로그아웃을 끝내도 프로세스가 계속 열린다. 사용자가
            # '창 닫고 상태 확인'을 누르면 프로세스를 닫은 뒤 실제 인증 상태로
            # 성공 여부를 정한다. 확인 없이 CANCELLED로 끝내면 완료된 인증 작업이
            # 취소로 표시되고 Settings 캐시도 이전 상태에 머문다.
            state, message = "SUCCEEDED", _success_message(session.intent)
        self._finish(session, state, message)
        return session.public()

    async def _helper_reached_expected_auth(self, session: LoginSession) -> bool:
        overrides = (
            {session.provider: session.executable_override}
            if session.executable_override
            else None
        )
        try:
            result = await probe_one(session.provider, overrides)
        except (OSError, ValueError):
            return False
        expected = (
            AuthState.NOT_LOGGED_IN
            if session.intent == LOGOUT_INTENT
            else AuthState.OK
        )
        return result is not None and result.auth_state == expected

    async def shutdown(self) -> None:
        active = [
            session
            for session in self._sessions.values()
            if session.state not in TERMINAL_STATES
        ]
        for session in active:
            # 종료 중에는 CLI 를 새로 띄우지 않는다.
            await self.cancel(session.provider, session.session_id, verify=False)

    def _prune_finished(self) -> None:
        finished = [
            session
            for session in self._sessions.values()
            if session.state in TERMINAL_STATES
        ]
        if len(finished) <= 20:
            return
        for session in sorted(finished, key=lambda item: item.started_at)[:-20]:
            self._sessions.pop(session.session_id, None)

    def _finish(self, session: LoginSession, state: str, message: str) -> None:
        session.state = state
        session.message = message
        session.completed_at = _now()
        if self._active_by_provider.get(session.provider) == session.session_id:
            self._active_by_provider.pop(session.provider, None)

    async def _run(
        self,
        session: LoginSession,
        resolved: ResolvedExecutable,
        executable_override: str | None,
    ) -> None:
        try:
            if session.intent == LOGOUT_INTENT:
                await self._run_agy_logout_helper(session, resolved)
                expected = AuthState.NOT_LOGGED_IN
            elif session.provider == "agy":
                await self._run_agy_helper(session, resolved)
                expected = AuthState.OK
            else:
                await self._run_browser_login(session, resolved)
                expected = AuthState.OK

            if session.state in TERMINAL_STATES:
                return
            result = await probe_one(
                session.provider,
                {session.provider: executable_override} if executable_override else None,
            )
            if result is not None and result.auth_state == expected:
                self._finish(session, "SUCCEEDED", _success_message(session.intent))
            else:
                self._finish(session, "FAILED", _unconfirmed_message(session.intent))
        except asyncio.CancelledError:
            if session.state not in TERMINAL_STATES:
                self._finish(session, "CANCELLED", _cancel_message(session.intent))
        except (OSError, NotImplementedError, ValueError):
            # 예외 원문에는 로컬 경로나 CLI 출력 조각이 포함될 수 있으므로 UI로
            # 전달하거나 DB/로그에 남기지 않는다.
            self._finish(session, "FAILED", _start_failure_message(session.intent))
        finally:
            session.process = None

    async def _run_browser_login(
        self, session: LoginSession, resolved: ResolvedExecutable
    ) -> None:
        if session.provider == "claude":
            auth_flag = "--console" if session.method == "console" else "--claudeai"
            argv = resolved.command(["auth", "login", auth_flag])
            session.message = "브라우저에서 Anthropic 로그인을 완료하세요."
        else:
            argv = resolved.command(["login"])
            session.message = "브라우저에서 ChatGPT 로그인을 완료하세요."

        session.state = "WAITING_FOR_USER"
        session.process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self._login_dir()),
            env=build_child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            # 출력에는 일회용 코드나 인증 URL이 포함될 수 있다. 교착 방지를 위해
            # 읽되 보관하거나 반환하지 않는다.
            await asyncio.wait_for(
                session.process.communicate(), timeout=_LOGIN_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, TimeoutError):
            if session.process.pid is not None:
                await asyncio.to_thread(kill_process_tree, session.process.pid)
            self._finish(session, "FAILED", "로그인 제한 시간(15분)이 지났습니다.")
            return

        if session.process.returncode != 0:
            self._finish(
                session,
                "FAILED",
                "CLI 로그인 절차가 완료되지 않았습니다. 취소했다면 다시 시도하세요.",
            )

    async def _run_agy_helper(
        self, session: LoginSession, resolved: ResolvedExecutable
    ) -> None:
        if sys.platform != "win32":
            self._finish(
                session,
                "FAILED",
                "현재 agy 로그인 도우미는 Windows에서만 지원합니다.",
            )
            return

        session.state = "WAITING_FOR_USER"
        session.message = (
            "열린 agy 창에서 Google 로그인을 완료한 뒤 해당 창을 닫으세요. "
            "ARIA가 자동으로 로그인 상태를 다시 확인합니다."
        )
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        session.process = await asyncio.create_subprocess_exec(
            *resolved.command(["--sandbox"]),
            cwd=str(self._login_dir()),
            env=build_child_env(),
            creationflags=flags,
        )
        try:
            await asyncio.wait_for(
                session.process.wait(), timeout=_LOGIN_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, TimeoutError):
            if session.process.pid is not None:
                await asyncio.to_thread(kill_process_tree, session.process.pid)
            self._finish(session, "FAILED", "로그인 제한 시간(15분)이 지났습니다.")

    async def _run_agy_logout_helper(
        self, session: LoginSession, resolved: ResolvedExecutable
    ) -> None:
        """빈 agy TUI 를 새 콘솔로 띄우고 /logout 입력은 사용자가 한다.

        agy 로 로그아웃하는 방법은 대화형 TUI 의 /logout 하나뿐이다. 비대화식
        경로 두 개를 모두 실측으로 배제했다.

        1. print mode(--print /logout) — CLI 가 직접 거부한다(agy 1.1.19).

             Error: /logout is not available in print mode (it clears stored
             credentials, an effect that outlives the run); pass
             --disable-slash-commands to send /logout to the model as literal text

           CLI 가 함께 제안하는 --disable-slash-commands 는 절대 쓰지 않는다. 그
           플래그를 붙이면 /logout 이 슬래시 명령이 아니라 모델 프롬프트로
           전달돼서, 자격증명은 그대로인데 모델이 로그아웃했다고 대답한다.

        2. --prompt-interactive /logout — 초기 프롬프트는 슬래시 명령으로 확장되지
           않고 모델에게 그대로 전달된다. 실측하면 모델이 로그아웃 방법을
           조사하겠다며 문서를 읽고 `agy --help` 실행 권한을 요청하는 상태로
           끝난다. 로그아웃은 되지 않고 계정 사용량만 나간다.

        그래서 프롬프트를 아예 넘기지 않는다. 창이 닫힌 뒤의 인증 재검사가 성공
        여부를 결정하므로, 사용자가 /logout 을 입력하지 않았다면 실패로 남는다.
        """
        if sys.platform != "win32":
            self._finish(
                session,
                "FAILED",
                "현재 agy 로그아웃 도우미는 Windows에서만 지원합니다.",
            )
            return

        session.state = "WAITING_FOR_USER"
        session.message = (
            "열린 agy 창에 /logout 을 입력해 로그아웃한 뒤 창을 닫으세요. "
            "ARIA가 로그아웃 상태를 다시 확인합니다."
        )
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        session.process = await asyncio.create_subprocess_exec(
            *resolved.command(["--sandbox"]),
            cwd=str(self._login_dir()),
            env=build_child_env(),
            creationflags=flags,
        )
        try:
            await asyncio.wait_for(
                session.process.wait(), timeout=_LOGOUT_HELPER_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, TimeoutError):
            if session.process.pid is not None:
                await asyncio.to_thread(kill_process_tree, session.process.pid)
            self._finish(session, "FAILED", "로그아웃 제한 시간(15분)이 지났습니다.")

    @staticmethod
    def _login_dir() -> Path:
        path = PATHS.data_dir / "login-helper"
        path.mkdir(parents=True, exist_ok=True)
        return path


LOGIN_MANAGER = ProviderLoginManager()
