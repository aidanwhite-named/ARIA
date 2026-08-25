"""Provider 계층: 환경변수 격리, 실행 파일 해석, 잠금 플래그, probe."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from app.enums import AuthState
from app.execution import process as proc
from app.providers.base import ExecutionRequest, ProbeResult
from app.providers.agy_cli import AgyCliProvider
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.env import build_child_env, describe_filtering, is_blocked
from app.providers.registry import PROVIDER_ORDER, build_provider, probe_all
from app.providers.resolver import resolve_claude, resolve_simple


# ------------------------------------------------------------------ env 격리


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "CODEX_HOME",
        "AWS_SECRET_ACCESS_KEY",
    ],
)
def test_provider_env_vars_are_blocked(name: str) -> None:
    assert is_blocked(name)


def test_parent_agent_env_does_not_leak_to_child(monkeypatch) -> None:
    """실측으로 확인된 실패를 막는 테스트.

    ARIA 를 Claude Code 세션 안에서 실행하면 부모가 ANTHROPIC_BASE_URL 등을
    환경에 심는다. 그대로 상속하면 자식 claude.exe 가 "Not logged in" 으로
    실패한다.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://host-only.invalid")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-py")

    env = build_child_env()
    assert "ANTHROPIC_BASE_URL" not in env
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env


def test_essential_env_vars_survive() -> None:
    env = build_child_env()
    assert "PATH" in env
    if sys.platform == "win32":
        # 저장된 로그인 세션을 찾으려면 사용자 프로필 경로가 필요하다.
        assert "USERPROFILE" in env
        assert "APPDATA" in env
    else:
        assert "HOME" in env


def test_explicit_extras_override_allowlist() -> None:
    env = build_child_env({"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_utf8_forced() -> None:
    env = build_child_env()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_unrelated_vars_are_dropped(monkeypatch) -> None:
    monkeypatch.setenv("SOME_RANDOM_SECRET", "value")
    assert "SOME_RANDOM_SECRET" not in build_child_env()


def test_describe_filtering_shape() -> None:
    info = describe_filtering()
    assert "allowlist" in info
    assert "blocked_prefixes" in info
    assert isinstance(info["removed_count"], int)


# --------------------------------------------------------------- 실행 파일


def test_resolver_prefers_native_exe_over_cmd_wrapper() -> None:
    resolved = resolve_claude()
    if resolved is None:
        pytest.skip("이 환경에는 Claude CLI 가 설치되어 있지 않습니다.")
    if sys.platform == "win32":
        assert resolved.path.lower().endswith(".exe")
        assert resolved.kind == "native_exe"


def test_resolver_rejects_nonexistent_override() -> None:
    assert resolve_claude("C:\\definitely\\not\\here.exe") is None
    assert resolve_simple("gemini", "/definitely/not/here") is None


def test_resolver_accepts_valid_override(tmp_path) -> None:
    fake = tmp_path / "claude.exe"
    fake.write_bytes(b"stub")
    resolved = resolve_claude(str(fake))
    assert resolved is not None
    assert resolved.source == "사용자 지정"


def test_command_builds_argv_array(tmp_path) -> None:
    fake = tmp_path / "claude.exe"
    fake.write_bytes(b"stub")
    resolved = resolve_claude(str(fake))
    assert resolved is not None
    assert resolved.command(["--version"]) == [str(fake), "--version"]


# ------------------------------------------------------------ 잠금 플래그


def _args(model: str | None = None) -> list[str]:
    provider = ClaudeCliProvider()
    request = ExecutionRequest(
        job_id="j",
        work_dir=Path("."),
        system_prompt="RULES",
        user_message="MESSAGE",
        model=model,
    )
    return provider.build_args(request)


def test_tools_are_fully_disabled() -> None:
    args = _args()
    index = args.index("--tools")
    assert args[index + 1] == ""


def test_system_prompt_replaces_not_appends() -> None:
    """도구가 없는데 코딩 에이전트 기본 프롬프트를 남길 이유가 없다."""
    args = _args()
    assert "--system-prompt" in args
    assert "--append-system-prompt" not in args
    assert args[args.index("--system-prompt") + 1] == "RULES"


def test_host_settings_sources_disabled() -> None:
    args = _args()
    assert args[args.index("--setting-sources") + 1] == ""


def test_mcp_config_is_valid_empty_record() -> None:
    """`--mcp-config "{}"` 는 CLI 가 거부한다. mcpServers 키가 필요하다."""
    args = _args()
    payload = json.loads(args[args.index("--mcp-config") + 1])
    assert payload == {"mcpServers": {}}
    assert "--strict-mcp-config" in args


def test_session_and_integrations_disabled() -> None:
    args = _args()
    for flag in ("--no-session-persistence", "--no-chrome", "--disable-slash-commands"):
        assert flag in args


def test_stream_json_output() -> None:
    args = _args()
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "-p" in args


def test_bare_flag_never_used() -> None:
    """--bare 는 OAuth/keychain 을 읽지 않으므로 구독 로그인이 죽는다."""
    assert "--bare" not in _args()


def test_dangerous_flags_never_used() -> None:
    args = _args()
    for flag in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"):
        assert flag not in args


def test_model_flag_optional() -> None:
    assert "--model" not in _args()
    assert _args("sonnet")[_args("sonnet").index("--model") + 1] == "sonnet"


def test_system_prompt_redacted_in_stored_args() -> None:
    provider = ClaudeCliProvider()
    redacted = provider._redact_args(["--system-prompt", "x" * 500, "-p"])
    assert "x" * 500 not in redacted
    assert "<500 chars>" in redacted


# ----------------------------------------------------------------- probe


async def test_probe_all_covers_every_provider() -> None:
    results = await probe_all(force=True)
    assert {r.provider for r in results} == set(PROVIDER_ORDER)


async def test_probe_cache_expires_so_external_logout_is_noticed(monkeypatch) -> None:
    """캐시가 무기한이면 ARIA 밖에서 로그아웃했을 때 화면이 계속 거짓말한다.

    실측 사고: 터미널에서 agy /logout 을 마쳐 `agy models` 가 실패하는데도
    Settings 표는 "로그인됨. 사용 가능한 모델 14개" 를 계속 보여줬다.
    """
    from app.providers import registry

    calls = {"n": 0}
    states = iter([AuthState.OK, AuthState.NOT_LOGGED_IN])

    class FakeProvider:
        id = "agy"
        display_name = "agy"
        install_hint = ""

        async def probe(self):
            calls["n"] += 1
            return ProbeResult(
                provider="agy",
                display_name="agy",
                installed=True,
                executable_ok=True,
                auth_state=next(states),
            )

    now = {"t": 1000.0}
    monkeypatch.setattr(registry, "PROVIDER_ORDER", ["agy"])
    monkeypatch.setattr(registry, "all_providers", lambda overrides=None: [FakeProvider()])
    monkeypatch.setattr(registry.time, "monotonic", lambda: now["t"])
    registry.invalidate()

    first = await registry.probe_all()
    assert first[0].auth_state == AuthState.OK
    assert calls["n"] == 1

    # TTL 안에서는 캐시를 그대로 쓴다. CLI 를 매번 띄우지 않는다.
    now["t"] += registry._CACHE_TTL_SECONDS / 2
    again = await registry.probe_all()
    assert again[0].auth_state == AuthState.OK
    assert calls["n"] == 1

    # TTL 이 지나면 다시 검사해서 바깥에서 일어난 로그아웃을 반영한다.
    now["t"] += registry._CACHE_TTL_SECONDS
    fresh = await registry.probe_all()
    assert fresh[0].auth_state == AuthState.NOT_LOGGED_IN
    assert calls["n"] == 2
    registry.invalidate()


async def test_claude_probe_reports_auth_honestly() -> None:
    """설치돼 있어도 로그인 안 됐으면 usable 이 아니어야 한다."""
    result = await ClaudeCliProvider().probe()
    if not result.installed:
        pytest.skip("이 환경에는 Claude CLI 가 설치되어 있지 않습니다.")
    assert result.executable_ok
    assert result.version
    if result.auth_state == AuthState.NOT_LOGGED_IN:
        assert not result.usable
        assert any("setup-token" in note for note in result.notes)


def test_unknown_provider_returns_none() -> None:
    assert build_provider("nope") is None


# ---------------------------------------------------------------- 프로세스


async def test_run_capture_reads_stdout() -> None:
    result = await proc.run_capture(
        [sys.executable, "-c", "print('hello')"], env=build_child_env()
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout


async def test_run_capture_handles_missing_executable() -> None:
    result = await proc.run_capture(["definitely-not-a-real-binary-xyz"])
    assert result.launch_error is not None


async def test_stdin_write_and_stdout_read_do_not_deadlock(tmp_path) -> None:
    """긴 stdin 을 밀어넣으면서 동시에 stdout 을 읽어야 한다.

    stdout 을 안 빨아들이면 파이프 버퍼가 차서 교착에 빠진다.
    """
    script = tmp_path / "echo.py"
    script.write_text(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write(f'received {len(data)}\\n')\n"
        "sys.stdout.write('x' * 200000 + '\\n')\n",
        encoding="utf-8",
    )
    payload = "y" * 300_000
    lines: list[str] = []

    async def collect(line: str) -> None:
        lines.append(line)

    result = await proc.run_streaming(
        job_id="deadlock-test",
        argv=[sys.executable, str(script)],
        cwd=tmp_path,
        env=build_child_env(),
        stdin_data=payload,
        on_stdout_line=collect,
        timeout_seconds=60,
    )
    assert result.exit_code == 0
    assert not result.timed_out
    assert f"received {len(payload)}" in result.stdout


async def test_timeout_kills_process(tmp_path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    result = await proc.run_streaming(
        job_id="timeout-test",
        argv=[sys.executable, str(script)],
        cwd=tmp_path,
        env=build_child_env(),
        timeout_seconds=2,
    )
    assert result.timed_out


async def test_utf8_output_decoded(tmp_path) -> None:
    script = tmp_path / "kr.py"
    script.write_text(
        "import sys\nsys.stdout.reconfigure(encoding='utf-8')\nprint('한글 출력')\n",
        encoding="utf-8",
    )
    result = await proc.run_capture(
        [sys.executable, str(script)], cwd=tmp_path, env=build_child_env()
    )
    assert "한글 출력" in result.stdout


def test_kill_process_tree_on_missing_pid() -> None:
    # 존재하지 않는 PID 로 호출해도 예외를 던지지 않아야 한다.
    proc.kill_process_tree(999_999_999)


def test_data_dir_is_outside_project_tree() -> None:
    """실행 폴더가 프로젝트 안에 있으면 상위 CLAUDE.md 가 주입될 수 있다."""
    from app.config import PATHS

    project_root = Path(__file__).resolve().parents[2]
    assert project_root not in Path(os.environ["ARIA_DATA_DIR"]).resolve().parents
    assert PATHS.runs_dir.name == "runs"


def test_agy_declares_input_byte_budget() -> None:
    """agy 는 큰 입력을 조용히 자르므로 바이트 한도를 선언해야 한다.

    실측(run c7a0ab27): 745 KB 입력 중 앞 ~196 KB 만 모델에 전달됐다. runner 가
    이 값으로 실행 전에 막는다. 값이 사라지면 방어가 무력화되므로 고정한다.
    """
    budget = AgyCliProvider.max_input_bytes
    assert isinstance(budget, int)
    # 실측 잘림 지점(~196 KB) 아래의 안전한 값이어야 한다.
    assert 0 < budget < 196_000
