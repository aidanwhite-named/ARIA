"""Claude Code CLI Provider.

v0.1 정책: 도구를 전부 끈다.

에이전트에게 로컬 파일을 Read 시키면 (1) 모델이 파일을 얼마나 읽을지
보장할 수 없어 실행마다 입력이 달라지고, (2) 파일시스템 접근이라는 보안
표면이 생기며, (3) 권한 거부/도구 오류라는 실패 유형이 추가된다.

ARIA 는 대신 첨부 자료를 미리 텍스트로 정규화해서 메시지 안에 직접 넣는다.
넣은 것은 반드시 들어간 것이므로 "필수 첨부를 못 읽었다"는 실패가 아예
발생하지 않는다.

플래그는 설치된 버전의 --help 로 검증한 것만 쓴다. 2.1.156 기준으로
아래 조합을 확인했다. CLI 옵션이 바뀌면 여기만 고치면 된다.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..enums import AuthState
from ..execution import process as proc
from .base import EmitFn, ExecutionOutcome, ExecutionRequest, ProbeResult, Provider
from .claude_stream import ClaudeStreamParser
from .env import build_child_env
from .resolver import ResolvedExecutable, resolve_claude

# MCP 를 완전히 비우는 설정. `--mcp-config "{}"` 는 유효하지 않다.
# (실측: mcpServers: Invalid input: expected record, received undefined)
_EMPTY_MCP = json.dumps({"mcpServers": {}})

_CHILD_ENV_EXTRA = {
    # 사용자 레벨 auto-memory 가 실행에 섞이지 않게 한다.
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


class ClaudeCliProvider(Provider):
    id = "claude"
    display_name = "Claude (Claude Code CLI)"
    install_hint = (
        "npm install -g @anthropic-ai/claude-code 로 설치한 뒤, 별도 터미널에서 "
        "claude setup-token 또는 claude auth login 으로 로그인하십시오. "
        "ARIA 는 API Key 를 입력받지 않고 CLI 에 저장된 로그인 세션만 사용합니다."
    )

    def __init__(self, executable_override: str | None = None) -> None:
        self._override = executable_override or None
        self._resolved: ResolvedExecutable | None = None

    # ------------------------------------------------------------------ probe

    def _resolve(self) -> ResolvedExecutable | None:
        self._resolved = resolve_claude(self._override)
        return self._resolved

    async def probe(self) -> ProbeResult:
        result = ProbeResult(
            provider=self.id,
            display_name=self.display_name,
            install_hint=self.install_hint,
            capabilities={
                "non_interactive": True,
                "stream_json": True,
                "stdin_prompt": True,
                "system_prompt_override": True,
                "tools_disabled": True,
                "model_select": True,
                "cancellable": True,
                "native_pdf": False,
            },
        )

        resolved = self._resolve()
        if resolved is None:
            result.notes.append("PATH, npm 전역 패키지, 네이티브 설치 경로에서 찾지 못했습니다.")
            return result

        result.installed = True
        result.executable_path = resolved.path
        result.executable_kind = resolved.kind
        result.notes.append(f"발견 위치: {resolved.source}")

        env = build_child_env(_CHILD_ENV_EXTRA)

        version_run = await proc.run_capture(
            resolved.command(["--version"]), env=env, timeout_seconds=45
        )
        if version_run.launch_error:
            result.notes.append(f"실행 실패: {version_run.launch_error}")
            return result
        if version_run.exit_code != 0:
            result.notes.append(
                f"--version 이 exit code {version_run.exit_code} 로 종료했습니다."
            )
            return result

        result.executable_ok = True
        result.version = (version_run.stdout or "").strip().splitlines()[0] if version_run.stdout else None

        # 인증 확인. 모델을 호출하지 않으므로 사용량이 발생하지 않는다.
        auth_run = await proc.run_capture(
            resolved.command(["auth", "status"]), env=env, timeout_seconds=45
        )
        result.auth_state, auth_note = self._interpret_auth(auth_run)
        if auth_note:
            result.notes.append(auth_note)

        return result

    def _interpret_auth(self, run: proc.ProcessResult) -> tuple[str, str]:
        if run.launch_error or run.timed_out:
            return AuthState.UNKNOWN, "인증 상태를 확인하지 못했습니다."
        raw = (run.stdout or "").strip()
        if not raw:
            return AuthState.UNKNOWN, "auth status 가 빈 응답을 반환했습니다."
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            lowered = raw.lower()
            if "not logged in" in lowered:
                return AuthState.NOT_LOGGED_IN, "로그인되어 있지 않습니다."
            return AuthState.UNKNOWN, f"auth status 응답을 해석하지 못했습니다: {raw[:120]}"

        if payload.get("loggedIn"):
            method = payload.get("authMethod") or "unknown"
            return AuthState.OK, f"로그인됨 (authMethod: {method})"
        return (
            AuthState.NOT_LOGGED_IN,
            "로그인되어 있지 않습니다. 별도 터미널에서 `claude setup-token` 을 실행하십시오.",
        )

    # ---------------------------------------------------------------- execute

    def build_args(self, request: ExecutionRequest) -> list[str]:
        args = [
            "-p",
            "--input-format",
            "text",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            # 도구를 전부 제거한다. 파일 접근/셸/네트워크 표면이 사라진다.
            "--tools",
            "",
            # 코딩 에이전트 기본 시스템 프롬프트를 ARIA 규칙으로 교체한다.
            # append 가 아니라 replace 인 이유: 도구가 없는데 코딩 에이전트
            # 지침을 남겨둘 이유가 없다.
            "--system-prompt",
            request.system_prompt,
            # 호스트 설정(user/project/local) 로드 차단.
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            _EMPTY_MCP,
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
        ]
        if request.model:
            args += ["--model", request.model]
        return args

    async def execute(self, request: ExecutionRequest, emit: EmitFn) -> ExecutionOutcome:
        outcome = ExecutionOutcome()

        resolved = self._resolved or self._resolve()
        if resolved is None:
            outcome.is_error = True
            outcome.error_message = "Claude CLI 실행 파일을 찾지 못했습니다."
            outcome.errors.append(outcome.error_message)
            return outcome

        args = self.build_args(request)
        argv = resolved.command(args)
        outcome.cli_path = resolved.path
        outcome.cli_args = self._redact_args(args)

        version_run = await proc.run_capture(
            resolved.command(["--version"]),
            env=build_child_env(_CHILD_ENV_EXTRA),
            timeout_seconds=45,
        )
        if version_run.exit_code == 0 and version_run.stdout:
            outcome.cli_version = version_run.stdout.strip().splitlines()[0]

        parser = ClaudeStreamParser()

        async def on_stdout(line: str) -> None:
            for event_type, payload in parser.feed(line):
                await emit(event_type, payload)

        async def on_stderr(line: str) -> None:
            if line.strip():
                await emit("stderr", {"line": line[:500]})

        await emit("provider_start", {"provider": self.id, "message": "Claude CLI 실행"})

        run = await proc.run_streaming(
            job_id=request.job_id,
            argv=argv,
            cwd=request.work_dir,
            env=build_child_env(_CHILD_ENV_EXTRA),
            stdin_data=request.user_message,
            on_stdout_line=on_stdout,
            on_stderr_line=on_stderr,
            timeout_seconds=request.timeout_seconds,
        )

        state = parser.state
        outcome.raw_stdout = run.stdout
        outcome.raw_stderr = run.stderr
        outcome.exit_code = run.exit_code
        outcome.timed_out = run.timed_out
        outcome.cancelled = run.cancelled
        outcome.result_text = state.final_text
        outcome.terminal_reason = state.terminal_reason or (
            "cancelled" if run.cancelled else "timeout" if run.timed_out else None
        )
        outcome.permission_denials = state.permission_denials
        outcome.usage = state.usage
        outcome.is_error = state.is_error
        outcome.auth_required = state.auth_required
        outcome.rate_limited = state.rate_limited
        # 이 Provider 는 --tools "" 로 도구를 완전히 끄고 실행한다.
        # 따라서 도구가 하나라도 보이면 정책이 깨진 것이다.
        outcome.tools_must_be_disabled = True
        outcome.tools_advertised = list(state.tool_names)
        outcome.tool_uses = list(state.tool_uses)

        if run.launch_error:
            outcome.is_error = True
            outcome.error_message = run.launch_error
            outcome.errors.append(f"프로세스를 시작하지 못했습니다: {run.launch_error}")

        if state.is_error and state.result_text:
            outcome.error_message = state.result_text[:500]
            outcome.errors.append(state.result_text[:500])

        if not state.saw_result and not run.cancelled and not run.timed_out:
            outcome.warnings.append(
                "최종 result 이벤트를 받지 못했습니다. 스트림이 중간에 끊겼을 수 있습니다."
            )

        if state.parse_errors:
            outcome.warnings.append(
                f"스트림 {len(state.parse_errors)}줄을 해석하지 못했습니다. 원문은 보존됩니다."
            )
        for detail in state.tool_errors:
            outcome.warnings.append(f"도구 오류: {detail}")

        return outcome

    async def cancel(self, job_id: str) -> bool:
        return await proc.cancel_job(job_id)

    def _redact_args(self, args: list[str]) -> list[str]:
        """system prompt 본문 등 긴 값은 요약해서 기록한다."""
        redacted: list[str] = []
        skip_next = False
        for i, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if arg == "--system-prompt":
                redacted.append(arg)
                nxt = args[i + 1] if i + 1 < len(args) else ""
                redacted.append(f"<{len(nxt)} chars>")
                skip_next = True
                continue
            redacted.append(arg)
        return redacted

    async def smoke_test(self, emit: EmitFn | None = None) -> ExecutionOutcome:
        """실제 모델을 호출한다. 사용량이 발생한다."""
        import tempfile

        async def noop(_type: str, _payload: dict) -> None:
            return None

        with tempfile.TemporaryDirectory(prefix="aria-smoke-") as tmp:
            request = ExecutionRequest(
                job_id=f"smoke-{id(self)}",
                work_dir=Path(tmp),
                system_prompt="You are a connectivity test. Answer with exactly one short line.",
                user_message="Reply with exactly: ARIA_SMOKE_OK",
                timeout_seconds=120,
            )
            return await self.execute(request, emit or noop)


def node_available() -> bool:
    return shutil.which("node") is not None
