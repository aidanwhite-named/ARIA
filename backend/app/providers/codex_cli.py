"""Codex CLI Provider.

codex-cli 0.149.0 을 실제로 실행해서 계약을 확인했다.

  실행 : codex exec --json --color never --sandbox read-only
              --skip-git-repo-check --ephemeral --ignore-user-config
              --ignore-rules -C <work_dir> -o <파일> [-m 모델]
              -c tools.web_search=<bool> -
  입력 : 프롬프트 본문 (stdin). 마지막 인수 `-` 가 stdin 에서 읽으라는 뜻이다.
  출력 : JSONL 이벤트 (stdout) + 최종 본문 (-o 로 지정한 파일)

Claude 와 다른 두 가지 제약이 있다. agy 와 같은 종류의 제약이다.

1. 시스템 프롬프트를 분리할 수단이 없다.
   `--system-prompt` 같은 플래그가 없다. 실행 파일 안에 base_instructions
   라는 세션 필드가 있지만 그것을 지정하는 검증된 플래그가 없으므로 쓰지
   않는다. 그래서 ARIA 런타임 컨텍스트를 사용자 메시지 맨 앞에 붙인다.
   첨부 본문과 같은 층위에 놓이므로 인젝션 방어가 Claude 쪽보다 약하다.

2. 셸·파일 도구를 끌 수 없다.
   설정의 `[tools]` 표에는 web_search, experimental_request_user_input,
   update_plan 세 개뿐이다(실행 파일에서 확인). 셸 실행과 파일 수정은 Codex
   의 핵심 도구라 끄는 수단이 없다.

   여기서 분명히 해둘 것: ARIA 는 도구 호출을 **탐지**할 뿐 **차단**하지
   못한다. 실패로 표시되는 시점에는 이미 명령 실행이 끝난 뒤일 수 있다.
   이건 fail-closed 가 아니라 사후 탐지다.

   `--sandbox read-only` 를 붙이지만 그것을 ARIA 의 안전 경계로 취급하지
   않는다. 읽기는 여전히 되고, 무엇보다 그 경계는 Codex 자신의 것이지
   ARIA 가 보증하는 것이 아니다.
   `--dangerously-bypass-approvals-and-sandbox` 는 절대 쓰지 않는다.

agy 보다 나은 점이 하나 있다. `--ephemeral` 로 세션 파일을 디스크에 남기지
않고 실행할 수 있다. agy 는 실행 대화가 영구 저장되고 그것을 끄는 옵션이
없다.

최종 본문을 JSONL 스트림에서 조립하지 않고 `--output-last-message` 파일에서
읽는 이유는 codex_stream 모듈 설명에 적어 두었다.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..enums import AuthState
from ..execution import process as proc
from .base import (
    REASONING_EFFORTS,
    CODEX_WEB_SEARCH,
    EmitFn,
    ExecutionOutcome,
    ExecutionRequest,
    ProbeResult,
    Provider,
)
from .codex_stream import CodexStreamParser, split_call_kinds
from .env import build_child_env
from .resolver import ResolvedExecutable, resolve_simple

# 최종 본문을 받을 파일 이름. 작업 폴더는 실행별로 격리돼 있다.
_LAST_MESSAGE_FILE = "codex_last_message.txt"

# 이 Provider 를 켜기 전에 사용자가 알아야 할 것. Settings 에 그대로 표시된다.
RISKS = (
    "셸 실행과 파일 수정 도구를 끄는 수단이 없습니다. 설정의 [tools] 표에는 "
    "web_search 등 세 항목뿐이고 셸·파일 도구는 그 목록에 없습니다.",
    "ARIA 는 도구 호출을 '탐지'해서 실패로 기록할 뿐, 호출 자체를 '차단'하지 "
    "못합니다. 실패로 표시되는 시점에는 이미 명령 실행이 끝난 뒤일 수 있습니다. "
    "이건 fail-closed 가 아니라 사후 탐지입니다.",
    "`--sandbox read-only` 로 실행하지만 이는 Codex 자신의 경계이며 ARIA 가 "
    "보증하는 경계가 아닙니다. 읽기 접근은 여전히 열려 있습니다.",
    "도구 호출 탐지는 CLI 가 내보내는 항목 종류 이름에 기반합니다. 다음 버전에서 "
    "이름이 바뀌거나 도구가 늘면 놓칠 수 있습니다.",
    "시스템 프롬프트를 분리할 수 없어 ARIA 런타임 컨텍스트가 사용자 메시지에 "
    "포함됩니다. 첨부 문서와 같은 층위라 프롬프트 인젝션 방어가 약합니다.",
    "신뢰할 수 없는 출처의 문서 분석에는 사용하지 마십시오.",
)

# 실행 파일에서 확인한 모델 slug. 계정별 모델 목록을 반환하는 명령이 없어서
# CLI 가 아는 이름만 노출한다.
MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.6-pro",
    "gpt-5.5",
    "gpt-5.4",
)

# Codex CLI 0.149.0 이 로컬 모델 카탈로그에서 광고하는 모델별 추론강도다.
# API 의 reasoning.effort 목록과 CLI Agent 의 목록은 같지 않다. 예를 들어
# Codex Agent 의 ultra 는 자동 작업 분할까지 포함하는 CLI 단계이고, luna 에는
# 없다. UI 가 전역 합집합만 보여 주면 luna + ultra 같은 실행 불가능한 조합을
# 저장하게 되므로 모델별 목록을 함께 내린다.
MODEL_REASONING_EFFORTS: dict[str, tuple[str, ...]] = {
    "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-terra": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"),
    "gpt-5.5": ("low", "medium", "high", "xhigh"),
    "gpt-5.4": ("low", "medium", "high", "xhigh"),
}

MODEL_DEFAULT_REASONING_EFFORTS: dict[str, str] = {
    "gpt-5.6-sol": "low",
    "gpt-5.6-terra": "medium",
    "gpt-5.6-luna": "medium",
    "gpt-5.5": "medium",
    "gpt-5.4": "medium",
}


class CodexCliProvider(Provider):
    id = "codex"
    display_name = "Codex"
    # Codex 는 도구 노출 목록을 제한하지 못한다. 이 정책은 web_search 이외의
    # *실제 호출*을 사후 탐지하는 제한된 안전성 정책이다.
    supported_tool_policies = frozenset({CODEX_WEB_SEARCH.name})
    search_tool_policy = CODEX_WEB_SEARCH
    install_hint = (
        "npm install -g @openai/codex 로 설치한 뒤 `codex login` 으로 "
        "로그인하십시오. Codex 데스크톱 앱에 번들된 실행 파일은 WindowsApps 권한 "
        "때문에 외부 프로세스에서 호출하지 못할 수 있습니다. 그런 경우 Settings "
        "에서 절대 경로를 지정하고 다시 검사하십시오. ARIA 는 API Key 를 "
        "입력받지 않고 CLI 에 저장된 로그인 세션만 사용합니다."
    )

    def __init__(self, executable_override: str | None = None) -> None:
        self._override = executable_override or None
        self._resolved: ResolvedExecutable | None = None

    # ------------------------------------------------------------------ probe

    def _resolve(self) -> ResolvedExecutable | None:
        self._resolved = resolve_simple("codex", self._override)
        return self._resolved

    async def probe(self) -> ProbeResult:
        result = ProbeResult(
            provider=self.id,
            display_name=self.display_name,
            install_hint=self.install_hint,
            experimental=True,
            risks=list(RISKS),
            capabilities={
                "non_interactive": True,
                "stream_json": True,
                "stdin_prompt": True,
                # 시스템 프롬프트 분리 불가 → 사용자 메시지에 합쳐서 보낸다.
                "system_prompt_override": False,
                # 셸·파일 도구를 끄는 플래그가 없다.
                "tools_disabled": False,
                # 설정으로 켜고 끌 수 있는 유일한 도구.
                "web_search": True,
                "search_tool_control": "detect_only",
                "model_select": True,
                # 고를 수 있는 레벨. 비워 두면 모델 기본값이며, 그 값이 무엇인지
                # ARIA 는 알 수 없다 — CLI 가 명령으로 알려주지 않는다.
                "reasoning_effort_select": True,
                "reasoning_efforts": list(REASONING_EFFORTS),
                "reasoning_efforts_by_model": {
                    model: list(efforts)
                    for model, efforts in MODEL_REASONING_EFFORTS.items()
                },
                "reasoning_defaults_by_model": dict(
                    MODEL_DEFAULT_REASONING_EFFORTS
                ),
                "cancellable": True,
                "browser_login": True,
                # 세션 파일을 디스크에 남기지 않고 실행할 수 있다.
                "ephemeral_session": True,
                "native_pdf": False,
                "models": list(MODELS),
            },
        )

        resolved = self._resolve()
        if resolved is None:
            result.notes.append("`codex` 를 PATH 및 지정 경로에서 찾지 못했습니다.")
            return result

        result.installed = True
        result.executable_path = resolved.path
        result.executable_kind = resolved.kind
        result.notes.append(f"발견 위치: {resolved.source}")

        env = build_child_env()

        # 파일이 존재해도 Windows 에서는 권한 때문에 외부 프로세스에서 호출이
        # 거부될 수 있다. 그래서 존재 여부가 아니라 실제 실행으로 판정한다.
        version_run = await proc.run_capture(
            resolved.command(["--version"]), env=env, timeout_seconds=45
        )
        if version_run.launch_error:
            result.notes.append(
                f"실행 파일은 있으나 호출할 수 없습니다: {version_run.launch_error}"
            )
            return result
        if version_run.timed_out:
            result.notes.append("--version 이 시간 내에 응답하지 않았습니다.")
            return result
        if version_run.exit_code != 0:
            detail = (version_run.stderr or version_run.stdout or "").strip()[:200]
            result.notes.append(
                f"--version 이 exit code {version_run.exit_code} 로 종료했습니다. {detail}"
            )
            return result

        result.executable_ok = True
        result.version = (version_run.stdout or "").strip().splitlines()[0] or None

        # 인증 확인. 모델을 호출하지 않으므로 사용량이 발생하지 않는다.
        auth_run = await proc.run_capture(
            resolved.command(["login", "status"]), env=env, timeout_seconds=45
        )
        result.auth_state, note = self._interpret_auth(auth_run)
        if note:
            result.notes.append(note)
        return result

    def _interpret_auth(self, run: proc.ProcessResult) -> tuple[str, str]:
        if run.launch_error or run.timed_out:
            return AuthState.UNKNOWN, "Codex 인증 상태를 확인하지 못했습니다."
        raw = "\n".join(
            part.strip() for part in (run.stdout, run.stderr) if part and part.strip()
        )
        lowered = raw.lower()
        # 로그아웃 판정을 먼저 한다. "Not logged in" 은 "logged in" 을 부분
        # 문자열로 포함하므로, 순서를 뒤집으면 exit code 하나에만 기대게 된다.
        # 실측(codex-cli 0.149.0): 로그아웃 상태는 "Not logged in" + exit 1 이라
        # 지금은 걸러지지만, CLI 가 exit 0 으로 바꾸는 순간 로그아웃에 성공하고도
        # 로그인 상태로 오판해 로그아웃이 실패로 보고된다.
        if "not logged in" in lowered or "login required" in lowered:
            return (
                AuthState.NOT_LOGGED_IN,
                "Codex 에 로그인되어 있지 않습니다. Settings 의 로그인 버튼을 "
                "쓰거나 별도 터미널에서 `codex login` 을 실행하십시오.",
            )
        if run.exit_code == 0 and "logged in" in lowered:
            return AuthState.OK, raw.splitlines()[0][:160]
        return AuthState.UNKNOWN, "Codex 로그인 상태 응답을 해석하지 못했습니다."

    # ---------------------------------------------------------------- execute

    def build_args(self, request: ExecutionRequest) -> list[str]:
        """검증된 플래그만 쓴다. CLI 옵션이 바뀌면 여기만 고치면 된다."""
        policy = request.tool_policy
        wants_search = policy is not None and policy.name == CODEX_WEB_SEARCH.name
        args = [
            "exec",
            "--json",
            # ANSI 이스케이프가 결과 본문에 섞이지 않게 한다.
            "--color",
            "never",
            # 방어 심화용. 이것만으로 도구가 차단되지는 않으므로 안전 경계로
            # 취급하지 않는다.
            "--sandbox",
            "read-only",
            # 작업 폴더는 git 저장소가 아니다.
            "--skip-git-repo-check",
            # 실행 대화를 디스크에 남기지 않는다.
            "--ephemeral",
            # 호스트의 config.toml 과 execpolicy 규칙이 실행에 섞이지 않게 한다.
            # 인증은 그대로 CODEX_HOME 에서 읽는다.
            "--ignore-user-config",
            "--ignore-rules",
            "-C",
            str(request.work_dir),
            "-o",
            str(request.work_dir / _LAST_MESSAGE_FILE),
            # 끌 수 있는 유일한 도구다. 분석 작업에서는 반드시 끈다.
            "-c",
            f"tools.web_search={'true' if wants_search else 'false'}",
        ]
        if request.model:
            args += ["-m", request.model]
        # 사용자가 고르지 않았으면 **아무 것도 넘기지 않는다.** 그래야 모델
        # 카탈로그의 기본값이 그대로 적용된다. 빈 값을 어떤 레벨로 채우는 순간
        # ARIA 가 고르지도 않은 강도를 대신 정해 주는 셈이 된다.
        #
        # --ignore-user-config 때문에 ~/.codex/config.toml 의 값은 무시되지만,
        # -c 로 준 값은 그대로 적용된다. 모델 카탈로그(models_cache.json)는
        # config.toml 이 아니라서 --ignore-user-config 의 영향을 받지 않는다.
        if request.reasoning_effort:
            args += ["-c", f"model_reasoning_effort={request.reasoning_effort}"]
        # 마지막 인수. 프롬프트를 stdin 에서 읽는다 — Windows 의 명령행 길이
        # 제한(32,767자) 때문에 인수로는 긴 프롬프트를 넘길 수 없다.
        args.append("-")
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

    def _read_last_message(self, work_dir: Path) -> str:
        try:
            return (work_dir / _LAST_MESSAGE_FILE).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    async def execute(self, request: ExecutionRequest, emit: EmitFn) -> ExecutionOutcome:
        outcome = ExecutionOutcome()

        resolved = self._resolved or self._resolve()
        if resolved is None:
            outcome.is_error = True
            outcome.error_message = "codex 실행 파일을 찾지 못했습니다."
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

        parser = CodexStreamParser()
        policy = request.tool_policy
        search_policy = (
            policy
            if policy is not None and policy.name == CODEX_WEB_SEARCH.name
            else None
        )
        budget_exceeded = False

        async def on_stdout(line: str) -> None:
            nonlocal budget_exceeded
            for event_type, payload in parser.feed(line):
                await emit(event_type, payload)
            if search_policy is None or budget_exceeded:
                return
            # 1층: 시작 이벤트 기준 전체 hard cap. 시작 시점에는 query 가 비어
            # 있어 종류를 모르므로, 종류별 예산만으로는 폭주를 막을 수 없다.
            over: tuple[int, str] | None = None
            if (
                search_policy.max_tool_calls
                and len(parser.state.tool_uses) > search_policy.max_tool_calls
            ):
                over = (
                    search_policy.max_tool_calls,
                    f"도구 호출이 상한({search_policy.max_tool_calls}회)을 넘어 "
                    "실행을 중단합니다.",
                )
            else:
                # 2·3층: 완료 이벤트 기준. web_search 하나가 검색과 URL 조회를
                # 겸하므로 한 예산에 섞으면 URL 을 몇 개 열어 보는 것만으로
                # 검색 라운드가 마른다.
                searches, lookups = split_call_kinds(parser.state.tool_calls)
                if (
                    search_policy.max_search_calls
                    and searches > search_policy.max_search_calls
                ):
                    over = (
                        search_policy.max_search_calls,
                        f"검색 호출이 상한({search_policy.max_search_calls}회)을 "
                        "넘어 실행을 중단합니다.",
                    )
                elif (
                    search_policy.max_url_lookup_calls
                    and lookups > search_policy.max_url_lookup_calls
                ):
                    over = (
                        search_policy.max_url_lookup_calls,
                        "URL 조회가 상한("
                        f"{search_policy.max_url_lookup_calls}회)을 넘어 실행을 "
                        "중단합니다.",
                    )
            if over is not None:
                budget_exceeded = True
                await emit(
                    "tool_budget_exceeded",
                    {"limit": over[0], "message": over[1]},
                )
                await proc.cancel_job(request.job_id)

        async def on_stderr(line: str) -> None:
            if line.strip():
                await emit("stderr", {"line": line[:500]})

        await emit("provider_start", {"provider": self.id, "message": "Codex CLI 실행"})

        run = await proc.run_streaming(
            job_id=request.job_id,
            argv=resolved.command(args),
            cwd=request.work_dir,
            env=env,
            stdin_data=self.compose_message(request),
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
        outcome.result_text = self._read_last_message(request.work_dir) or state.fallback_text
        outcome.usage = state.usage
        outcome.is_error = state.is_error
        outcome.auth_required = state.auth_required
        outcome.rate_limited = state.rate_limited
        outcome.terminal_reason = state.status or (
            "cancelled" if run.cancelled else "timeout" if run.timed_out else None
        )

        # 도구를 끌 수 없는 Provider 다. 실제 호출만 정책 위반으로 다룬다.
        outcome.tools_must_be_disabled = False
        # 도구를 끌 수단이 없다. 호출이 감지되면 사용자가 설정으로 완화할 수
        # 없게 항상 실패 처리한다.
        outcome.tools_uncontrollable = True
        outcome.tool_uses = list(state.tool_uses)
        # 광고 목록을 알 수 없다. Codex 는 사용 가능한 도구를 미리 알려주지 않는다.
        outcome.tools_advertised = []
        outcome.tool_calls = list(state.tool_calls)
        outcome.tool_budget_exceeded = budget_exceeded
        # 분석은 예전과 같은 사후 탐지 경로(None)를 쓰고, 검색에만 Codex 전용
        # 정책을 붙여 web_search 호출을 정상 처리한다.
        outcome.tool_policy = search_policy

        if run.launch_error:
            outcome.is_error = True
            outcome.error_message = run.launch_error
            outcome.errors.append(f"프로세스를 시작하지 못했습니다: {run.launch_error}")

        if state.error_message:
            outcome.error_message = state.error_message[:500]
            outcome.errors.append(state.error_message[:500])

        if state.unknown_item_types:
            outcome.errors.append(
                "처음 보는 Codex 항목 종류가 있었습니다: "
                + ", ".join(state.unknown_item_types[:10])
                + ". 도구 탐지 목록을 확인해야 할 수 있습니다."
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
                job_id=f"smoke-codex-{id(self)}",
                work_dir=Path(tmp),
                system_prompt="You are a connectivity test. Answer with exactly one short line.",
                user_message="Reply with exactly: ARIA_SMOKE_OK",
                timeout_seconds=180,
            )
            return await self.execute(request, emit or noop)


def codex_available() -> bool:
    return shutil.which("codex") is not None
