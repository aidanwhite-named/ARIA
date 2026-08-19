"""agy CLI Provider.

이 PC 의 Gemini CLI 는 `agy` 라는 이름의 네이티브 실행 파일이다.
agy 1.1.15 를 실제로 실행해서 계약을 확인했다.

  실행 : agy --input-format stream-json --output-format stream-json
             --disable-slash-commands [--model M]
  입력 : {"event":"user","message":{"role":"user","content":"..."}}  (stdin)
  출력 : {"event":"init"|"step_update"|"result", ...}

Claude 와 다른 두 가지 제약이 있다.

1. 시스템 프롬프트를 분리할 수단이 없다.
   `--system-prompt` 같은 플래그가 없으므로 ARIA 런타임 컨텍스트를 사용자
   메시지 맨 앞에 붙인다. 첨부 본문과 같은 층위에 놓이므로 프롬프트 인젝션
   방어가 Claude 쪽보다 약하다.

2. 도구를 끌 수 없다.
   `--tools` 에 해당하는 플래그가 없고, init 이벤트가 run_command,
   write_to_file 을 포함해 57개 도구를 광고한다.

   여기서 분명히 해둘 것: ARIA 는 도구 호출을 **탐지**할 뿐 **차단**하지
   못한다. 실패로 표시되는 시점에는 이미 파일 쓰기나 명령 실행이 끝난
   뒤일 수 있다. 이건 fail-closed 가 아니라 사후 탐지다.

   --sandbox 를 붙이지만 그것을 안전 경계로 취급하지 않는다.
   --dangerously-skip-permissions 는 절대 쓰지 않는다.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from ..enums import AuthState
from ..execution import process as proc
from .agy_stream import AgyStreamParser, build_stdin_message
from .base import EmitFn, ExecutionOutcome, ExecutionRequest, ProbeResult, Provider
from .env import build_child_env
from .resolver import ExecutableKind, ResolvedExecutable, resolve_simple

_KNOWN_INSTALL_DIRS = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin",
    Path.home() / "AppData" / "Local" / "agy" / "bin",
    Path.home() / ".agy" / "bin",
)


def resolve_agy(override: str | None = None) -> ResolvedExecutable | None:
    """`agy` 만 찾는다. 구형 `gemini` 로 폴백하지 않는다."""
    if override:
        path = Path(override)
        if path.is_file():
            kind = (
                ExecutableKind.NATIVE_EXE
                if path.suffix.lower() == ".exe"
                else ExecutableKind.POSIX_BIN
            )
            return ResolvedExecutable(str(path), kind, source="사용자 지정")
        return None

    # `gemini` 로 폴백하지 않는다. 구형 gemini CLI 는 stdin/출력 계약이
    # 전혀 달라서, 여기 구현으로 호출하면 조용히 오작동한다. 그 CLI 를
    # 쓰려면 별도 Adapter 를 만들어야 한다.
    resolved = resolve_simple("agy")
    if resolved is not None:
        return resolved

    exe_name = "agy.exe" if sys.platform == "win32" else "agy"
    for directory in _KNOWN_INSTALL_DIRS:
        candidate = directory / exe_name
        try:
            if candidate.is_file():
                kind = (
                    ExecutableKind.NATIVE_EXE
                    if sys.platform == "win32"
                    else ExecutableKind.POSIX_BIN
                )
                return ResolvedExecutable(str(candidate), kind, source="기본 설치 경로")
        except OSError:
            continue
    return None


class AgyCliProvider(Provider):
    id = "agy"
    display_name = "agy"
    install_hint = (
        "agy CLI 를 설치하고 로그인하십시오. 설치되어 있으면 `agy models` 가 "
        "모델 목록을 반환합니다. ARIA 는 API Key 를 입력받지 않고 CLI 에 저장된 "
        "로그인 세션만 사용합니다."
    )

    def __init__(self, executable_override: str | None = None) -> None:
        self._override = executable_override or None
        self._resolved: ResolvedExecutable | None = None

    # ------------------------------------------------------------------ probe

    def _resolve(self) -> ResolvedExecutable | None:
        self._resolved = resolve_agy(self._override)
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
                # 시스템 프롬프트 분리 불가 → 사용자 메시지에 합쳐서 보낸다.
                "system_prompt_override": False,
                # 도구를 끄는 플래그가 없다.
                "tools_disabled": False,
                "model_select": True,
                "cancellable": True,
                "native_pdf": False,
            },
        )

        resolved = self._resolve()
        if resolved is None:
            result.notes.append("`agy` 를 찾지 못했습니다.")
            return result

        result.installed = True
        result.executable_path = resolved.path
        result.executable_kind = resolved.kind
        result.notes.append(f"발견 위치: {resolved.source}")

        env = build_child_env()
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
        result.version = (version_run.stdout or "").strip().splitlines()[0] or None

        # 인증 확인. 모델 추론을 돌리지 않으므로 토큰 사용량이 발생하지 않는다.
        models_run = await proc.run_capture(
            resolved.command(["models"]), env=env, timeout_seconds=60
        )
        if models_run.exit_code == 0 and models_run.stdout.strip():
            result.auth_state = AuthState.OK
            names = [
                line.split("\t")[0].strip()
                for line in models_run.stdout.splitlines()
                if "\t" in line
            ]
            result.capabilities["models"] = names[:20]
            result.notes.append(f"로그인됨. 사용 가능한 모델 {len(names)}개.")
        else:
            result.auth_state = AuthState.NOT_LOGGED_IN
            detail = (models_run.stderr or models_run.stdout or "").strip()[:160]
            result.notes.append(f"`agy models` 가 실패했습니다. 로그인이 필요합니다. {detail}")

        return result

    # ---------------------------------------------------------------- execute

    def build_args(self, request: ExecutionRequest) -> list[str]:
        args = [
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            # 방어 심화용. 터미널 제한을 켜지만 이것만으로 도구가
            # 차단되지는 않는다. 안전 경계로 취급하지 않는다.
            "--sandbox",
        ]
        if request.model:
            args += ["--model", request.model]
        return args

    def compose_message(self, request: ExecutionRequest) -> str:
        """시스템 프롬프트를 분리할 수 없으므로 맨 앞에 붙인다."""
        if not request.system_prompt.strip():
            return request.user_message
        return (
            "[ARIA RUNTIME CONTEXT]\n"
            f"{request.system_prompt.strip()}\n\n"
            f"{request.user_message}"
        )

    async def execute(self, request: ExecutionRequest, emit: EmitFn) -> ExecutionOutcome:
        outcome = ExecutionOutcome()

        resolved = self._resolved or self._resolve()
        if resolved is None:
            outcome.is_error = True
            outcome.error_message = "agy 실행 파일을 찾지 못했습니다."
            outcome.errors.append(outcome.error_message)
            return outcome

        args = self.build_args(request)
        outcome.cli_path = resolved.path
        outcome.cli_args = list(args)

        env = build_child_env()
        version_run = await proc.run_capture(
            resolved.command(["--version"]), env=env, timeout_seconds=45
        )
        if version_run.exit_code == 0 and version_run.stdout:
            outcome.cli_version = version_run.stdout.strip().splitlines()[0]

        parser = AgyStreamParser()

        async def on_stdout(line: str) -> None:
            for event_type, payload in parser.feed(line):
                await emit(event_type, payload)

        async def on_stderr(line: str) -> None:
            if line.strip():
                await emit("stderr", {"line": line[:500]})

        await emit("provider_start", {"provider": self.id, "message": "agy CLI 실행"})

        run = await proc.run_streaming(
            job_id=request.job_id,
            argv=resolved.command(args),
            cwd=request.work_dir,
            env=env,
            stdin_data=build_stdin_message(self.compose_message(request)),
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
        outcome.usage = state.usage
        outcome.is_error = state.is_error
        outcome.auth_required = state.auth_required
        outcome.rate_limited = state.rate_limited
        outcome.terminal_reason = state.status or (
            "cancelled" if run.cancelled else "timeout" if run.timed_out else None
        )

        # 도구를 끌 수 없는 Provider 다. 광고된 목록은 정보로만 남기고,
        # 실제 호출만 정책 위반으로 다룬다.
        outcome.tools_must_be_disabled = False
        # 도구를 끌 수단이 없다. 호출이 감지되면 사용자가 설정으로
        # 완화할 수 없게 항상 실패 처리한다.
        outcome.tools_uncontrollable = True
        outcome.tool_uses = list(state.tool_uses)
        if state.tools_advertised:
            outcome.warnings.append(
                f"agy 는 도구를 끌 수 없어 {len(state.tools_advertised)}개 도구가 "
                f"활성 상태로 실행됐습니다 (permission_mode: {state.permission_mode})."
            )

        if run.launch_error:
            outcome.is_error = True
            outcome.error_message = run.launch_error
            outcome.errors.append(f"프로세스를 시작하지 못했습니다: {run.launch_error}")

        if state.error_message:
            outcome.error_message = state.error_message[:500]
            outcome.errors.append(state.error_message[:500])

        if not state.saw_result and not run.cancelled and not run.timed_out:
            outcome.warnings.append(
                "최종 result 이벤트를 받지 못했습니다. 스트림이 중간에 끊겼을 수 있습니다."
            )

        if state.parse_errors:
            outcome.warnings.append(
                f"스트림 {len(state.parse_errors)}줄을 해석하지 못했습니다. 원문은 보존됩니다."
            )

        return outcome

    async def cancel(self, job_id: str) -> bool:
        return await proc.cancel_job(job_id)

    async def smoke_test(self, emit: EmitFn | None = None) -> ExecutionOutcome:
        """실제 모델을 호출한다. 사용량이 발생한다."""

        async def noop(_type: str, _payload: dict) -> None:
            return None

        with tempfile.TemporaryDirectory(prefix="aria-smoke-") as tmp:
            request = ExecutionRequest(
                job_id=f"smoke-agy-{id(self)}",
                work_dir=Path(tmp),
                system_prompt="You are a connectivity test. Answer with exactly one short line.",
                user_message="Reply with exactly: ARIA_SMOKE_OK",
                timeout_seconds=180,
            )
            return await self.execute(request, emit or noop)


def agy_available() -> bool:
    return shutil.which("agy") is not None
