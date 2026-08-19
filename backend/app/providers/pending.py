"""아직 실행이 확인되지 않은 Provider (Codex).

v0.1 에서는 실행 경로를 구현하지 않는다. 이 PC 에서 발견되지 않아 검증할
방법이 없기 때문이다. 다만 감지는 제대로 한다.

(Gemini 는 agy CLI 로 실제 구현되어 있다. agy_cli.py 를 보라.)

감지 원칙: PATH 에 있는지 / 파일이 있는지로 판단하지 않고, 실제로
`--version` 을 실행해서 성공하는지로 판단한다. Windows 에서는 실행 파일이
존재해도 WindowsApps 권한 때문에 외부 프로세스에서 호출하면 거부되는
경우가 있어서, 파일 존재 여부와 실행 가능 여부가 다르다.

CLI 가 설치되어 실제로 실행되면 probe 는 installed/executable_ok 를 참으로
보고하고, 실행은 여전히 거부하면서 어떤 작업이 남았는지 알려준다.
"""

from __future__ import annotations

from ..enums import AuthState
from ..execution import process as proc
from .base import EmitFn, ExecutionOutcome, ExecutionRequest, ProbeResult, Provider
from .env import build_child_env
from .resolver import resolve_simple


class PendingCliProvider(Provider):
    """probe 는 하되 실행은 아직 지원하지 않는 Provider."""

    command: str = ""
    pending_note: str = ""

    def __init__(self, executable_override: str | None = None) -> None:
        self._override = executable_override or None

    async def probe(self) -> ProbeResult:
        result = ProbeResult(
            provider=self.id,
            display_name=self.display_name,
            install_hint=self.install_hint,
            capabilities={
                "non_interactive": None,
                "stream_json": None,
                "stdin_prompt": None,
                "system_prompt_override": None,
                "tools_disabled": None,
                "model_select": None,
                "cancellable": None,
                "native_pdf": None,
            },
        )

        resolved = resolve_simple(self.command, self._override)
        if resolved is None:
            result.notes.append(
                f"`{self.command}` 를 PATH 및 지정 경로에서 찾지 못했습니다."
            )
            return result

        result.installed = True
        result.executable_path = resolved.path
        result.executable_kind = resolved.kind
        result.notes.append(f"발견 위치: {resolved.source}")

        run = await proc.run_capture(
            resolved.command(["--version"]),
            env=build_child_env(),
            timeout_seconds=45,
        )
        if run.launch_error:
            result.notes.append(
                f"실행 파일은 있으나 호출할 수 없습니다: {run.launch_error}"
            )
            return result
        if run.timed_out:
            result.notes.append("--version 이 시간 내에 응답하지 않았습니다.")
            return result
        if run.exit_code != 0:
            detail = (run.stderr or run.stdout or "").strip()[:200]
            result.notes.append(
                f"--version 이 exit code {run.exit_code} 로 종료했습니다. {detail}"
            )
            return result

        result.executable_ok = True
        result.version = (run.stdout or "").strip().splitlines()[0] if run.stdout else None
        result.auth_state = AuthState.UNKNOWN
        result.notes.append(self.pending_note)
        return result

    async def execute(self, request: ExecutionRequest, emit: EmitFn) -> ExecutionOutcome:
        outcome = ExecutionOutcome()
        outcome.is_error = True
        outcome.error_message = self.pending_note
        outcome.errors.append(self.pending_note)
        await emit("provider_error", {"message": self.pending_note})
        return outcome

    async def cancel(self, job_id: str) -> bool:
        return False


class CodexCliProvider(PendingCliProvider):
    id = "codex"
    display_name = "GPT (Codex CLI)"
    command = "codex"
    install_hint = (
        "외부에서 호출 가능한 Codex CLI 가 필요합니다. Codex 데스크톱 앱에 번들된 "
        "실행 파일은 WindowsApps 권한 때문에 외부 프로세스에서 호출할 수 없습니다. "
        "설치 후 Settings 에서 절대 경로를 지정하고 다시 검사하십시오."
    )
    pending_note = (
        "CLI 는 감지되었지만 ARIA v0.1 은 Codex 실행 경로를 아직 지원하지 않습니다. "
        "app/providers/pending.py 를 참고해 CodexCliProvider 를 구현하십시오."
    )
