"""CLI 로그인 연동: 입력 경계, 상태 API, Codex 인증 판정."""

from __future__ import annotations

import asyncio
import sys

import pytest

from app.enums import AuthState
from app.execution.process import ProcessResult
from app.providers.base import ProbeResult
from app.providers.login import (
    LOGIN_INTENT,
    LOGOUT_INTENT,
    LoginError,
    LoginSession,
    ProviderLoginManager,
)
from app.providers.codex_cli import CodexCliProvider
from app.providers.resolver import ExecutableKind, ResolvedExecutable


def _session(**updates) -> dict:
    data = {
        "session_id": "login-1",
        "provider": "claude",
        "intent": "login",
        "method": "subscription",
        "mode": "browser",
        "state": "WAITING_FOR_USER",
        "message": "브라우저에서 로그인하세요.",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": None,
        "can_cancel": True,
    }
    data.update(updates)
    return data


def test_login_start_endpoint_never_accepts_credentials(client, monkeypatch) -> None:
    from app.api import providers as providers_api

    captured: dict = {}

    async def fake_start(provider, *, method=None, executable_override=None):
        captured.update(
            provider=provider, method=method, executable_override=executable_override
        )
        return _session(provider=provider, method=method)

    monkeypatch.setattr(providers_api.LOGIN_MANAGER, "start", fake_start)
    response = client.post(
        "/api/providers/claude/login", json={"method": "subscription"}
    )
    assert response.status_code == 202
    assert captured["provider"] == "claude"
    assert captured["method"] == "subscription"
    assert "token" not in response.text.lower()
    assert "password" not in response.text.lower()

    # 임의의 credential 필드는 받았다는 인상을 주지 않도록 요청 자체를 거부한다.
    extra = client.post(
        "/api/providers/claude/login",
        json={"method": "subscription", "password": "must-not-be-used"},
    )
    assert extra.status_code == 400
    assert "must-not-be-used" not in extra.text


def test_login_endpoint_requires_csrf_header(client) -> None:
    response = client.post(
        "/api/providers/claude/login",
        json={"method": "subscription"},
        headers={"X-ARIA-Client": ""},
    )
    assert response.status_code == 403


def test_login_status_and_cancel_endpoints(client, monkeypatch) -> None:
    from app.api import providers as providers_api

    async def fake_get(provider, session_id, *, intent=None):
        if session_id != "login-1" or intent not in (None, "login"):
            return None
        return _session(provider=provider)

    async def fake_cancel(provider, session_id, *, intent=None):
        if session_id != "login-1" or intent not in (None, "login"):
            return None
        return _session(
            provider=provider,
            state="CANCELLED",
            message="로그인을 취소했습니다.",
            can_cancel=False,
        )

    monkeypatch.setattr(providers_api.LOGIN_MANAGER, "get", fake_get)
    monkeypatch.setattr(providers_api.LOGIN_MANAGER, "cancel", fake_cancel)

    status = client.get("/api/providers/claude/login/login-1")
    assert status.status_code == 200
    assert status.json()["state"] == "WAITING_FOR_USER"
    cancelled = client.delete("/api/providers/claude/login/login-1")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"
    assert client.get("/api/providers/claude/login/missing").status_code == 404


@pytest.mark.parametrize(
    ("provider", "method", "expected"),
    [
        ("claude", None, "subscription"),
        ("claude", "console", "console"),
        ("codex", None, "chatgpt"),
        ("agy", None, "google"),
    ],
)
def test_login_methods_are_narrowly_validated(provider, method, expected) -> None:
    assert ProviderLoginManager._normalize_method(provider, method) == expected


def test_api_key_login_method_is_not_exposed() -> None:
    with pytest.raises(LoginError):
        ProviderLoginManager._normalize_method("codex", "api-key")


def test_public_login_state_contains_no_process_or_output() -> None:
    login = LoginSession(
        session_id="s",
        provider="codex",
        method="chatgpt",
        mode="browser",
    )
    public = login.public()
    assert "process" not in public
    assert "task" not in public
    assert "stdout" not in public
    assert "stderr" not in public


async def test_duplicate_active_login_reuses_the_same_session(monkeypatch, tmp_path) -> None:
    manager = ProviderLoginManager()
    gate = asyncio.Event()
    resolved = ResolvedExecutable(
        path=str(tmp_path / "claude.exe"), kind=ExecutableKind.NATIVE_EXE
    )

    async def wait_forever(login, executable, override):
        del executable, override
        login.state = "WAITING_FOR_USER"
        login.message = "waiting"
        await gate.wait()

    monkeypatch.setattr(manager, "_resolve", lambda provider, override: resolved)
    monkeypatch.setattr(manager, "_run", wait_forever)

    first = await manager.start("claude")
    await asyncio.sleep(0)
    second = await manager.start("claude")
    assert first["session_id"] == second["session_id"]
    await manager.cancel("claude", first["session_id"])
    gate.set()


async def test_browser_login_process_output_is_discarded(monkeypatch, tmp_path) -> None:
    from app.providers import login as login_module

    script = tmp_path / "fake_login.py"
    script.write_text(
        "import sys\n"
        "print('https://login.example.invalid/?code=one-time-secret')\n"
        "print('authorization-code: never-return-this', file=sys.stderr)\n",
        encoding="utf-8",
    )
    resolved = ResolvedExecutable(
        path=str(script),
        kind=ExecutableKind.NODE_ENTRY,
        argv_prefix=[sys.executable],
    )
    manager = ProviderLoginManager()

    async def fake_probe(provider, overrides=None):
        del overrides
        return ProbeResult(
            provider=provider,
            display_name="Claude",
            installed=True,
            executable_ok=True,
            auth_state=AuthState.OK,
        )

    monkeypatch.setattr(manager, "_resolve", lambda provider, override: resolved)
    monkeypatch.setattr(login_module, "probe_one", fake_probe)
    started = await manager.start("claude")
    task = manager._sessions[started["session_id"]].task
    assert task is not None
    await task
    finished = await manager.get("claude", started["session_id"])
    assert finished is not None
    assert finished["state"] == "SUCCEEDED"
    assert "one-time-secret" not in str(finished)
    assert "never-return-this" not in str(finished)


@pytest.mark.parametrize(
    ("provider", "expected_args"),
    [
        ("claude", ["auth", "logout"]),
        ("codex", ["logout"]),
    ],
)
async def test_logout_uses_cli_owned_command_and_discards_output(
    provider, expected_args, monkeypatch, tmp_path
) -> None:
    from app.providers import login as login_module

    resolved = ResolvedExecutable("provider.exe", ExecutableKind.NATIVE_EXE)
    captured: dict = {}

    async def fake_capture(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return ProcessResult(
            exit_code=0,
            stdout="account@example.invalid one-time-token",
            stderr="local credential path",
        )

    async def fake_probe(provider_id, overrides=None):
        del overrides
        return ProbeResult(
            provider=provider_id,
            display_name=provider_id,
            installed=True,
            executable_ok=True,
            auth_state=AuthState.NOT_LOGGED_IN,
        )

    monkeypatch.setattr(ProviderLoginManager, "_resolve", lambda *args: resolved)
    monkeypatch.setattr(login_module.proc, "run_capture", fake_capture)
    monkeypatch.setattr(login_module, "probe_one", fake_probe)

    result = await login_module.logout_provider(provider)
    assert captured["argv"] == ["provider.exe", *expected_args]
    assert captured["kwargs"]["cwd"] == login_module.PATHS.data_dir / "login-helper"
    assert result == {
        "provider": provider,
        "mode": "immediate",
        "ok": True,
        "auth_state": AuthState.NOT_LOGGED_IN,
        "message": "로그아웃했습니다.",
    }
    assert "account@example.invalid" not in str(result)
    assert "one-time-token" not in str(result)


def test_logout_endpoint_requires_csrf_and_returns_sanitized_result(
    client, monkeypatch
) -> None:
    from app.api import providers as providers_api

    async def fake_logout(provider, *, executable_override=None):
        del executable_override
        return {
            "provider": provider,
            "ok": True,
            "auth_state": "NOT_LOGGED_IN",
            "message": "로그아웃했습니다.",
        }

    monkeypatch.setattr(providers_api, "logout_provider", fake_logout)
    response = client.post("/api/providers/claude/logout")
    assert response.status_code == 200
    assert response.json()["auth_state"] == "NOT_LOGGED_IN"

    blocked = client.post(
        "/api/providers/claude/logout", headers={"X-ARIA-Client": ""}
    )
    assert blocked.status_code == 403


async def test_logout_fails_when_auth_state_is_still_logged_in(
    monkeypatch,
) -> None:
    from app.providers import login as login_module

    resolved = ResolvedExecutable("codex.exe", ExecutableKind.NATIVE_EXE)

    async def fake_capture(*args, **kwargs):
        del args, kwargs
        return ProcessResult(exit_code=0, stdout="done")

    async def fake_probe(*args, **kwargs):
        del args, kwargs
        return ProbeResult(
            provider="codex",
            display_name="Codex",
            installed=True,
            executable_ok=True,
            auth_state=AuthState.OK,
        )

    monkeypatch.setattr(ProviderLoginManager, "_resolve", lambda *args: resolved)
    monkeypatch.setattr(login_module.proc, "run_capture", fake_capture)
    monkeypatch.setattr(login_module, "probe_one", fake_probe)
    with pytest.raises(LoginError, match="완료를 확인하지 못했습니다"):
        await login_module.logout_provider("codex")


async def test_agy_never_uses_the_non_interactive_logout_path() -> None:
    """agy 는 print mode 로그아웃 명령이 없다.

    실측(agy 1.1.19): `agy --print /logout` 은 CLI 가 거부한다.
      "/logout is not available in print mode (it clears stored credentials...)"
    비대화식 경로로 새어 들어가면 조용히 실패하므로 시작 자체를 막는다.
    """
    from app.providers import login as login_module

    with pytest.raises(LoginError, match="도우미 창"):
        await login_module.logout_provider("agy")


def test_agy_logout_endpoint_starts_a_helper_window_session(client, monkeypatch) -> None:
    from app.api import providers as providers_api

    captured: dict = {}

    async def fake_start_logout(provider, *, executable_override=None):
        captured.update(provider=provider, executable_override=executable_override)
        return _session(
            session_id="logout-1",
            provider=provider,
            intent="logout",
            method="slash_command",
            mode="helper_window",
            message="로그아웃 도우미를 준비하고 있습니다.",
        )

    monkeypatch.setattr(
        providers_api.LOGIN_MANAGER, "start_logout", fake_start_logout
    )
    response = client.post("/api/providers/agy/logout")
    assert response.status_code == 200
    body = response.json()
    assert captured["provider"] == "agy"
    assert body["intent"] == "logout"
    assert body["mode"] == "helper_window"
    assert body["session_id"] == "logout-1"


def test_logout_session_endpoints_are_scoped_to_the_logout_intent(
    client, monkeypatch
) -> None:
    """로그인 세션 id 로 로그아웃 상태를 조회할 수 없어야 한다."""
    from app.api import providers as providers_api

    async def fake_get(provider, session_id, *, intent=None):
        if session_id != "logout-1" or intent != LOGOUT_INTENT:
            return None
        return _session(session_id=session_id, provider=provider, intent="logout")

    async def fake_cancel(provider, session_id, *, intent=None):
        if session_id != "logout-1" or intent != LOGOUT_INTENT:
            return None
        return _session(
            session_id=session_id,
            provider=provider,
            intent="logout",
            state="CANCELLED",
            message="로그아웃을 취소했습니다.",
            can_cancel=False,
        )

    monkeypatch.setattr(providers_api.LOGIN_MANAGER, "get", fake_get)
    monkeypatch.setattr(providers_api.LOGIN_MANAGER, "cancel", fake_cancel)

    ok = client.get("/api/providers/agy/logout/logout-1")
    assert ok.status_code == 200
    assert ok.json()["intent"] == "logout"
    # 같은 관리자에 들어 있어도 intent 가 다르면 다른 엔드포인트로 새지 않는다.
    assert client.get("/api/providers/agy/login/logout-1").status_code == 404
    cancelled = client.delete("/api/providers/agy/logout/logout-1")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"


class _FakeConsoleProcess:
    """사용자가 도우미 창을 닫은 상황을 흉내낸다."""

    def __init__(self) -> None:
        self.pid = 4321
        self.returncode = 0

    async def wait(self) -> int:
        return 0


async def test_agy_logout_helper_passes_no_prompt_to_the_model(
    monkeypatch, tmp_path
) -> None:
    from app.providers import login as login_module

    resolved = ResolvedExecutable(str(tmp_path / "agy.exe"), ExecutableKind.NATIVE_EXE)
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return _FakeConsoleProcess()

    async def fake_probe(provider, overrides=None):
        del overrides
        return ProbeResult(
            provider=provider,
            display_name="agy",
            installed=True,
            executable_ok=True,
            auth_state=AuthState.NOT_LOGGED_IN,
        )

    manager = ProviderLoginManager()
    monkeypatch.setattr(login_module.sys, "platform", "win32")
    monkeypatch.setattr(manager, "_resolve", lambda provider, override: resolved)
    monkeypatch.setattr(login_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(login_module, "probe_one", fake_probe)

    started = await manager.start_logout("agy")
    assert started["intent"] == LOGOUT_INTENT
    assert started["mode"] == "helper_window"
    task = manager._sessions[started["session_id"]].task
    assert task is not None
    await task

    argv = captured["argv"]
    # 프롬프트를 넘기지 않는다. 실측: --prompt-interactive /logout 은 슬래시
    # 명령으로 확장되지 않고 모델에게 텍스트로 전달돼서, 모델이 로그아웃 방법을
    # 조사하기 시작하고 로그아웃은 되지 않는다. --print /logout 은 CLI 가 거부하고,
    # --disable-slash-commands 는 모델이 로그아웃했다고 거짓 응답하게 만든다.
    assert argv == [resolved.path, "--sandbox"]
    assert "--prompt-interactive" not in argv
    assert "--disable-slash-commands" not in argv
    assert "--print" not in argv
    assert "/logout" not in argv

    finished = await manager.get("agy", started["session_id"], intent=LOGOUT_INTENT)
    assert finished is not None
    assert finished["state"] == "SUCCEEDED"
    assert finished["message"] == "로그아웃했습니다."


async def test_agy_logout_fails_when_credentials_survive(monkeypatch, tmp_path) -> None:
    """창이 닫혀도 여전히 로그인 상태면 성공으로 보고하지 않는다."""
    from app.providers import login as login_module

    resolved = ResolvedExecutable(str(tmp_path / "agy.exe"), ExecutableKind.NATIVE_EXE)

    async def fake_exec(*argv, **kwargs):
        del argv, kwargs
        return _FakeConsoleProcess()

    async def fake_probe(provider, overrides=None):
        del overrides
        return ProbeResult(
            provider=provider,
            display_name="agy",
            installed=True,
            executable_ok=True,
            auth_state=AuthState.OK,
        )

    manager = ProviderLoginManager()
    monkeypatch.setattr(login_module.sys, "platform", "win32")
    monkeypatch.setattr(manager, "_resolve", lambda provider, override: resolved)
    monkeypatch.setattr(login_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(login_module, "probe_one", fake_probe)

    started = await manager.start_logout("agy")
    task = manager._sessions[started["session_id"]].task
    assert task is not None
    await task

    finished = await manager.get("agy", started["session_id"], intent=LOGOUT_INTENT)
    assert finished is not None
    assert finished["state"] == "FAILED"
    assert "확인하지 못했습니다" in finished["message"]


async def test_cancelling_a_logout_reports_success_when_already_logged_out(
    monkeypatch, tmp_path
) -> None:
    """창에서 /logout 을 끝낸 뒤 '창 닫고 상태 확인'을 누르는 흐름.

    확인 없이 CANCELLED 로 끝내면 캐시에 남은 "로그인됨" 이 표에 그대로 남아
    실제 상태와 어긋난다.
    """
    from app.providers import login as login_module

    resolved = ResolvedExecutable(str(tmp_path / "agy.exe"), ExecutableKind.NATIVE_EXE)
    gate = asyncio.Event()
    auth_state = AuthState.OK

    async def fake_exec(*argv, **kwargs):
        del argv, kwargs
        return _FakeConsoleProcess()

    async def fake_probe(provider, overrides=None):
        del overrides
        return ProbeResult(
            provider=provider,
            display_name="agy",
            installed=True,
            executable_ok=True,
            auth_state=auth_state,
        )

    async def wait_forever(session, executable, override):
        del executable, override
        session.state = "WAITING_FOR_USER"
        await gate.wait()

    manager = ProviderLoginManager()
    monkeypatch.setattr(login_module.sys, "platform", "win32")
    monkeypatch.setattr(manager, "_resolve", lambda provider, override: resolved)
    monkeypatch.setattr(login_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(login_module, "probe_one", fake_probe)
    monkeypatch.setattr(manager, "_run", wait_forever)

    started = await manager.start_logout("agy")
    await asyncio.sleep(0)

    # 사용자가 창에서 /logout 을 끝낸 뒤 버튼을 눌렀다.
    auth_state = AuthState.NOT_LOGGED_IN
    cancelled = await manager.cancel(
        "agy", started["session_id"], intent=LOGOUT_INTENT
    )
    assert cancelled is not None
    assert cancelled["state"] == "SUCCEEDED"
    assert cancelled["message"] == "로그아웃했습니다."
    gate.set()


async def test_cancelling_a_logout_stays_cancelled_when_still_logged_in(
    monkeypatch, tmp_path
) -> None:
    from app.providers import login as login_module

    resolved = ResolvedExecutable(str(tmp_path / "agy.exe"), ExecutableKind.NATIVE_EXE)
    gate = asyncio.Event()

    async def fake_probe(provider, overrides=None):
        del overrides
        return ProbeResult(
            provider=provider,
            display_name="agy",
            installed=True,
            executable_ok=True,
            auth_state=AuthState.OK,
        )

    async def wait_forever(session, executable, override):
        del executable, override
        session.state = "WAITING_FOR_USER"
        await gate.wait()

    manager = ProviderLoginManager()
    monkeypatch.setattr(login_module.sys, "platform", "win32")
    monkeypatch.setattr(manager, "_resolve", lambda provider, override: resolved)
    monkeypatch.setattr(login_module, "probe_one", fake_probe)
    monkeypatch.setattr(manager, "_run", wait_forever)

    started = await manager.start_logout("agy")
    await asyncio.sleep(0)
    cancelled = await manager.cancel(
        "agy", started["session_id"], intent=LOGOUT_INTENT
    )
    assert cancelled is not None
    assert cancelled["state"] == "CANCELLED"
    gate.set()


@pytest.mark.parametrize(
    ("auth_state", "expected_state", "expected_message"),
    [
        (AuthState.OK, "SUCCEEDED", "로그인이 완료되었습니다."),
        (AuthState.NOT_LOGGED_IN, "CANCELLED", "로그인을 취소했습니다."),
    ],
)
async def test_closing_agy_login_helper_verifies_the_actual_auth_state(
    auth_state, expected_state, expected_message, monkeypatch, tmp_path
) -> None:
    """로그인 후에도 열린 agy TUI를 버튼으로 닫고 완료 상태를 확인한다."""
    from app.providers import login as login_module

    resolved = ResolvedExecutable(str(tmp_path / "agy.exe"), ExecutableKind.NATIVE_EXE)
    gate = asyncio.Event()

    async def fake_probe(provider, overrides=None):
        del overrides
        return ProbeResult(
            provider=provider,
            display_name="agy",
            installed=True,
            executable_ok=True,
            auth_state=auth_state,
        )

    async def wait_forever(session, executable, override):
        del executable, override
        session.state = "WAITING_FOR_USER"
        await gate.wait()

    manager = ProviderLoginManager()
    monkeypatch.setattr(manager, "_resolve", lambda provider, override: resolved)
    monkeypatch.setattr(login_module, "probe_one", fake_probe)
    monkeypatch.setattr(manager, "_run", wait_forever)

    started = await manager.start("agy")
    await asyncio.sleep(0)
    finished = await manager.cancel(
        "agy", started["session_id"], intent=LOGIN_INTENT
    )

    assert finished is not None
    assert finished["state"] == expected_state
    assert finished["message"] == expected_message
    gate.set()


async def test_login_and_logout_sessions_do_not_share_a_slot(
    monkeypatch, tmp_path
) -> None:
    manager = ProviderLoginManager()
    gate = asyncio.Event()
    resolved = ResolvedExecutable(str(tmp_path / "agy.exe"), ExecutableKind.NATIVE_EXE)

    async def wait_forever(session, executable, override):
        del executable, override
        session.state = "WAITING_FOR_USER"
        await gate.wait()

    from app.providers import login as login_module

    monkeypatch.setattr(login_module.sys, "platform", "win32")
    monkeypatch.setattr(manager, "_resolve", lambda provider, override: resolved)
    monkeypatch.setattr(manager, "_run", wait_forever)

    started = await manager.start("agy")
    assert started["intent"] == LOGIN_INTENT
    await asyncio.sleep(0)
    with pytest.raises(LoginError, match="다른 인증 작업"):
        await manager.start_logout("agy")
    await manager.cancel("agy", started["session_id"])
    gate.set()


async def test_codex_probe_reports_logged_in(monkeypatch) -> None:
    from app.providers import codex_cli

    resolved = ResolvedExecutable("codex.exe", ExecutableKind.NATIVE_EXE)
    replies = iter(
        [
            ProcessResult(exit_code=0, stdout="codex-cli 0.149.0\n"),
            ProcessResult(exit_code=0, stdout="Logged in using ChatGPT\n"),
        ]
    )

    async def fake_capture(*args, **kwargs):
        del args, kwargs
        return next(replies)

    monkeypatch.setattr(codex_cli, "resolve_simple", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(codex_cli.proc, "run_capture", fake_capture)
    result = await CodexCliProvider().probe()
    assert result.auth_state == AuthState.OK
    # 실행 경로가 구현됐으므로 설치·로그인만 끝나면 runnable 이다.
    assert result.runnable is True
    assert result.execution_supported is True
    assert result.capabilities["browser_login"] is True
    # 다만 도구를 끄지 못하므로 opt-in 없이는 쓸 수 없다.
    assert result.experimental is True


async def test_codex_probe_reports_logged_out(monkeypatch) -> None:
    from app.providers import codex_cli

    resolved = ResolvedExecutable("codex.exe", ExecutableKind.NATIVE_EXE)
    replies = iter(
        [
            ProcessResult(exit_code=0, stdout="codex-cli 0.149.0\n"),
            ProcessResult(exit_code=1, stderr="Not logged in\n"),
        ]
    )

    async def fake_capture(*args, **kwargs):
        del args, kwargs
        return next(replies)

    monkeypatch.setattr(codex_cli, "resolve_simple", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(codex_cli.proc, "run_capture", fake_capture)
    result = await CodexCliProvider().probe()
    assert result.auth_state == AuthState.NOT_LOGGED_IN
    assert result.runnable is False


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_codex_logged_out_wins_over_exit_code_zero(monkeypatch, stream) -> None:
    """"Not logged in" 은 "logged in" 을 부분 문자열로 포함한다.

    실측(codex-cli 0.149.0)에서는 로그아웃 상태가 exit 1 이라 지금은 걸러지지만,
    CLI 가 exit 0 으로 바뀌면 로그아웃에 성공하고도 로그인 상태로 오판해서
    로그아웃이 실패로 보고된다. 문자열 판정이 exit code 에 기대지 않게 고정한다.
    """
    from app.providers import codex_cli

    resolved = ResolvedExecutable("codex.exe", ExecutableKind.NATIVE_EXE)
    replies = iter(
        [
            ProcessResult(exit_code=0, stdout="codex-cli 0.149.0\n"),
            ProcessResult(exit_code=0, **{stream: "Not logged in\n"}),
        ]
    )

    async def fake_capture(*args, **kwargs):
        del args, kwargs
        return next(replies)

    monkeypatch.setattr(codex_cli, "resolve_simple", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(codex_cli.proc, "run_capture", fake_capture)
    result = await CodexCliProvider().probe()
    assert result.auth_state == AuthState.NOT_LOGGED_IN
    assert result.runnable is False
