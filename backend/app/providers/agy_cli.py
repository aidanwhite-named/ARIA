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

   --mode plan 은 쓰지 않는다. 실측하면 agy 가 직접 이렇게 경고한다.

     warning: --mode plan has no effect while slash command expansion
              is disabled.

   ARIA 는 항상 --disable-slash-commands 로 실행하므로 plan 모드는 무효다.
   슬래시 명령/프롬프트 수준 기능이지 권한 경계가 아니다. 같은 실행에서
   tools 는 여전히 57개, permission_mode 는 여전히 request-review 였다.
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
from .base import (
    AGY_WEB_SEARCH,
    EmitFn,
    ExecutionOutcome,
    ExecutionRequest,
    ProbeResult,
    Provider,
)
from .env import build_child_env
from .resolver import ExecutableKind, ResolvedExecutable, resolve_simple

# 이 Provider 를 켜기 전에 사용자가 알아야 할 것. Settings 에 그대로 표시된다.
RISKS = (
    "도구를 끄는 플래그가 없습니다. run_command, write_to_file 을 포함해 수십 개 "
    "도구가 활성 상태로 실행됩니다.",
    "ARIA 는 도구 호출을 '탐지'해서 실패로 기록할 뿐, 호출 자체를 '차단'하지 "
    "못합니다. 실패로 표시되는 시점에는 이미 파일 쓰기나 명령 실행이 끝난 뒤일 수 "
    "있습니다. 이건 fail-closed 가 아니라 사후 탐지입니다.",
    "실측(agy 1.1.15): 파일 쓰기와 셸 명령을 요청했을 때 도구 호출이 시도됐고 "
    "ARIA 가 탐지해 실패 처리했으며 디스크에는 변화가 없었습니다. 다만 이는 세 "
    "가지 시나리오를 확인한 것일 뿐이고, 차단은 agy 자신의 승인 정책에 의존하며 "
    "ARIA 가 보장하는 경계가 아닙니다.",
    "도구 호출 탐지는 이벤트 이름에 기반합니다. 관찰하지 못한 이름이 있으면 "
    "놓칠 수 있습니다.",
    "read_url_content 는 가져온 페이지를 파일로만 돌려줍니다. 그 파일을 읽는 "
    "view_file 호출은 경로가 이번 대화의 산출물이고 단계 번호가 성공한 "
    "read_url_content 와 일치할 때만 정상 열람으로 인정합니다. 다른 경로·다른 "
    "대화·일반 로컬 파일은 위반으로 남습니다. 이 판정 역시 호출이 끝난 뒤에 "
    "이뤄지는 사후 감사이며 읽기 자체를 막지는 못합니다.",
    "시스템 프롬프트를 분리할 수 없어 ARIA 런타임 컨텍스트가 사용자 메시지에 "
    "포함됩니다. 첨부 문서와 같은 층위라 프롬프트 인젝션 방어가 약합니다.",
    "신뢰할 수 없는 출처의 문서 분석에는 사용하지 마십시오.",
)


_KNOWN_INSTALL_DIRS = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin",
    Path.home() / "AppData" / "Local" / "agy" / "bin",
    Path.home() / ".agy" / "bin",
)

# agy 가 가져온 페이지를 저장하는 경로 규칙.
#
#   <brain>/<conversation_id>/.system_generated/steps/<n>/content.md
#
# read_url_content 는 본문을 돌려주지 않고 이 경로만 알려준다. 그래서 본문을
# 읽으려면 view_file 을 부르는 수밖에 없고, 그 호출이 "가져온 페이지를 확인한
# 것"인지 "임의의 로컬 파일을 읽은 것"인지는 경로로만 구분된다.
#
# 마지막 다섯 조각만 검사한다. brain 루트가 어디인지 알 필요가 없고, 대화 ID 는
# 실행마다 새로 생기는 값이라 다른 실행의 파일을 가리키면 그 조각에서 걸린다.
_ARTIFACT_MARKER = ".system_generated"
_ARTIFACT_STEPS = "steps"
_ARTIFACT_FILE = "content.md"
# agy 의 페이지 열람 도구. 이 이름의 호출만 산출물 경로와 대조한다.
_FETCH_TOOL = "read_url_content"


def content_artifact_step(path: str, conversation_id: str | None) -> int | None:
    """이 경로가 이번 대화의 read_url_content 산출물이면 그 단계 번호.

    아니면 None. 판정은 사전 차단이 아니라 사후 감사다 — 이 함수가 None 을
    돌려주는 시점에는 파일이 이미 읽힌 뒤다. agy 에는 도구 호출을 실행 전에
    가로챌 지점이 없다. 그래도 기록에 남는 판정만은 정확해야 한다.
    """
    if not path or not conversation_id:
        return None
    try:
        # 심볼릭 링크·재분석 지점·`..` 을 먼저 푼다. 링크로 우회하면 풀린 경로의
        # 꼬리가 규칙과 어긋나 걸린다. 파일이 지워진 뒤에도 정규화는 된다.
        resolved = Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    parts = resolved.parts
    if len(parts) < 5:
        return None
    conversation, marker, steps, step, name = parts[-5:]
    if marker != _ARTIFACT_MARKER or steps != _ARTIFACT_STEPS:
        return None
    if name.casefold() != _ARTIFACT_FILE:
        return None
    if conversation.casefold() != conversation_id.casefold():
        return None
    if not step.isdigit():
        return None
    return int(step)


def split_tool_calls(calls, content_read_tools) -> tuple[int, int]:
    """(검색 호출 수, 본문 읽기 호출 수).

    한 예산에 섞으면 본문을 성실히 읽을수록 검색 예산이 마른다. 2026-08-25
    실행이 그랬다 — 검색 4회·열람 3회에 본문 읽기 14회가 더해져 상한 20을 넘겼고,
    사용자에게는 "검색 범위를 좁히라"는 엉뚱한 지시가 나갔다.

    범위를 벗어난 열람 호출도 본문 읽기로 센다. 그 호출은 어차피 정책 위반으로
    따로 잡히며, 위반을 검색 예산에 얹어서 두 번 벌줄 이유가 없다.
    """
    scoped = set(content_read_tools or ())
    content = sum(1 for call in calls if call.get("name") in scoped)
    return len(calls) - content, content


def audit_content_reads(state, policy) -> None:
    """아직 판정하지 않은 콘텐츠 열람 호출에 scope_ok 를 붙인다.

    통과 조건은 두 가지다. 경로가 이번 대화의 산출물 규칙에 맞아야 하고, 그
    단계 번호가 같은 실행에서 **성공한** read_url_content 의 단계와 일치해야
    한다. 하나라도 어긋나면 위반으로 남는다.

    통과한 호출은 그 페이지를 실제로 읽었다는 유일한 증거이기도 하다. 그래서
    대응하는 read_url_content 호출에 content_read 를 함께 표시한다 — 감사
    기록에서 '포인터를 받았다'와 '본문을 읽었다'를 가르는 값이다.
    """
    scoped = set(getattr(policy, "content_read_tools", ()) or ())
    if not scoped:
        return
    for call in state.tool_calls:
        if call.get("name") not in scoped or "scope_ok" in call:
            continue
        summary = call.get("input")
        if not isinstance(summary, dict):
            summary = {}
        step = content_artifact_step(
            str(summary.get("path") or ""), state.conversation_id
        )
        source = (
            state.tool_calls_by_step.get(str(step)) if step is not None else None
        )
        in_scope = bool(
            source is not None
            and source.get("name") == _FETCH_TOOL
            and source.get("ok") is True
        )
        call["scope_ok"] = in_scope
        call["scope"] = f"{_FETCH_TOOL}:{step}" if in_scope else "out_of_scope"
        if in_scope:
            source["content_read"] = True


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
    # agy 는 도구 노출 목록을 제한하지 못한다. 이 정책은 search_web 와
    # read_url_content 이외의 *실제 호출*을 사후 탐지하는 제한된 안전성 정책이다.
    supported_tool_policies = frozenset({AGY_WEB_SEARCH.name})
    search_tool_policy = AGY_WEB_SEARCH
    # agy 1.1.19 는 stream-json 메시지 content 를 약 192 KiB(≈196,608 bytes)에서
    # 조용히 자르고 뒷부분을 `<truncated N bytes>` 로 대체한다. 실측(run
    # c7a0ab27): 745 KB 입력 중 앞 ~196 KB 만 모델에 전달돼 첨부 절반이 통째로
    # 빠진 채 종료 코드 0 으로 "성공"했다. 여유(약 16 KB)를 둔 값으로 실행 전에
    # 막아 그 낭비를 없앤다.
    #
    # 이 값은 *모델 컨텍스트 한도가 아니다*. agy CLI 가 모델에 넘기기 전에
    # 입력을 자르는 지점이다. 그러므로 "모델 컨텍스트가 크다", "Provider 가
    # 알아서 압축한다"는 이유로 올리거나 없애면 안 된다 — 그 어느 쪽도 CLI 가
    # 자르는 것을 막지 못하고, 잘린 실행은 종료 코드 0 으로 "성공"해 버린다.
    # CLI 자체의 잘림 지점이 실측으로 바뀌었을 때만 이 값을 조정한다.
    max_input_bytes = 180_000
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
            experimental=True,
            risks=list(RISKS),
            capabilities={
                "non_interactive": True,
                "stream_json": True,
                "stdin_prompt": True,
                # 시스템 프롬프트 분리 불가 → 사용자 메시지에 합쳐서 보낸다.
                "system_prompt_override": False,
                # 도구를 끄는 플래그가 없다.
                "tools_disabled": False,
                # 실측(1.1.17): search_web 는 request-review 모드의 비대화형
                # 실행에서도 정상 완료된다. 다른 도구 노출은 제한할 수 없다.
                "web_search": True,
                "search_tool_control": "detect_only",
                "model_select": True,
                "cancellable": True,
                # 전용 login 명령이 없어 Windows 별도 도우미 창을 사용한다.
                "guided_login": sys.platform == "win32",
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

    def payload_bytes(self, system_prompt: str, user_message: str) -> int:
        """agy 에 실제로 나가는 stream-json 한 줄의 UTF-8 바이트 수.

        두 문자열을 그냥 더하면 실제보다 작게 잡힌다. 자르는 주체는 이 CLI 이고
        그것이 보는 것은 **직렬화된 메시지**이기 때문이다. 더해지는 것:

          - `compose_message` 가 앞에 붙이는 [ARIA RUNTIME CONTEXT] 머리말
            (agy 는 시스템 프롬프트를 분리할 수단이 없다)
          - JSON 이스케이프. 개행 하나가 `
` 2 bytes 가 되므로, 줄이 많은
            문서일수록 차이가 커진다. 따옴표·역슬래시도 마찬가지다.
          - `{"event":"user","message":{...}}` 래퍼

        그래서 여기서는 재지 않고 **실제로 만들어서 잰다.** 계산식으로 근사하면
        이스케이프 규칙이 바뀌었을 때 조용히 어긋난다.
        """
        request = ExecutionRequest(
            job_id="",
            work_dir=Path("."),
            system_prompt=system_prompt,
            user_message=user_message,
        )
        return len(build_stdin_message(self.compose_message(request)).encode("utf-8"))

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
        policy = request.tool_policy
        search_policy = (
            policy if policy is not None and policy.name == AGY_WEB_SEARCH.name else None
        )
        budget_exceeded = False
        content_budget_exceeded = False

        async def on_stdout(line: str) -> None:
            nonlocal budget_exceeded, content_budget_exceeded
            for event_type, payload in parser.feed(line):
                await emit(event_type, payload)
            if search_policy is None:
                return

            # 판정을 먼저 붙인다. 아래 예산 계산이 "검색 호출"과 "본문 읽기"를
            # 나누려면 각 호출이 어느 쪽인지 정해져 있어야 한다.
            audit_content_reads(parser.state, search_policy)
            search_calls, content_calls = split_tool_calls(
                parser.state.tool_calls, search_policy.content_read_tools
            )

            if (
                search_policy.max_tool_calls
                and not budget_exceeded
                and search_calls > search_policy.max_tool_calls
            ):
                budget_exceeded = True
                await emit(
                    "tool_budget_exceeded",
                    {
                        "limit": search_policy.max_tool_calls,
                        "message": (
                            f"검색 도구 호출이 상한({search_policy.max_tool_calls}회)을 "
                            "넘어 실행을 중단합니다."
                        ),
                    },
                )
                await proc.cancel_job(request.job_id)
                return

            if (
                search_policy.max_content_read_calls
                and not content_budget_exceeded
                and content_calls > search_policy.max_content_read_calls
            ):
                content_budget_exceeded = True
                await emit(
                    "content_read_budget_exceeded",
                    {
                        "limit": search_policy.max_content_read_calls,
                        "message": (
                            "페이지 본문 읽기 호출이 상한"
                            f"({search_policy.max_content_read_calls}회)을 넘어 "
                            "실행을 중단합니다."
                        ),
                    },
                )
                await proc.cancel_job(request.job_id)

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
        if search_policy is not None:
            # 마지막 줄에서 들어온 호출이나 프로세스를 끊으면서 남은 호출까지
            # 판정한다. 판정이 빠진 호출은 아래 evaluator 에서 위반으로 잡히므로
            # 여기서 빠뜨리면 정상 열람이 위반으로 기록된다.
            audit_content_reads(state, search_policy)
            for call in state.tool_calls:
                if call.get("name") == _FETCH_TOOL:
                    # 이 Provider 는 본문을 돌려주지 않는다. 아무도 읽지 않은
                    # 열람은 '포인터를 받았다'까지가 전부이므로 명시적으로
                    # 거짓이다 — 표시가 없으면 감사 쪽이 예전 계약대로
                    # '성공했으면 읽은 것'으로 읽는다.
                    call.setdefault("content_read", False)

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
        outcome.tools_advertised = list(state.tools_advertised)
        outcome.tool_calls = list(state.tool_calls)
        outcome.tool_budget_exceeded = budget_exceeded
        outcome.content_read_budget_exceeded = content_budget_exceeded
        # 기존 분석은 예전과 같은 사후 탐지 경로(None)를 유지한다. 검색에만 agy
        # 전용 정책을 붙여 search_web/read_url_content 호출을 정상 처리한다.
        outcome.tool_policy = search_policy

        if run.launch_error:
            outcome.is_error = True
            outcome.error_message = run.launch_error
            outcome.errors.append(f"프로세스를 시작하지 못했습니다: {run.launch_error}")

        if state.error_message:
            outcome.error_message = state.error_message[:500]
            outcome.errors.append(state.error_message[:500])

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
