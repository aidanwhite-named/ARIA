"""공통 Provider 인터페이스.

세 Provider 의 분석 결과를 하나의 업무 스키마로 강제하지 않는다.
공통화하는 것은 실행 메타데이터뿐이다.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..enums import AuthState


@dataclass
class ProbeResult:
    """모델 호출 없이 확인 가능한 Provider 상태."""

    provider: str
    display_name: str
    installed: bool = False
    executable_path: str | None = None
    executable_kind: str | None = None
    executable_ok: bool = False
    version: str | None = None
    auth_state: str = AuthState.UNKNOWN
    capabilities: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    install_hint: str = ""
    # 설치·인증 상태와 별개로 ARIA가 이 Provider의 실행 경로를 구현했는가.
    execution_supported: bool = True

    # 제한된 안전성 Provider: 기술적으로는 동작하지만 ARIA 의 안전
    # 원칙(도구 없는 실행)을 충족하지 못한다.
    #
    # 이 표시는 '고지'이지 '관문'이 아니다. 예전에는 사용자가 체크박스로
    # 동의해야 실행할 수 있었지만, 매번 같은 화면을 넘기게 만들 뿐이라
    # 걷어냈다. 위험 목록은 Settings 의 Provider 상세에 그대로 남고, 도구
    # 호출에 대한 사후 판정(tools_uncontrollable)도 그대로다.
    experimental: bool = False
    risks: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        """설치/실행/인증 상태."""
        return (
            self.execution_supported
            and self.installed
            and self.executable_ok
            and self.auth_state
            in (
                AuthState.OK,
                AuthState.NOT_APPLICABLE,
            )
        )

    @property
    def usable(self) -> bool:
        """실행 허용 여부. 지금은 설치·인증이 전부다."""
        return self.runnable


@dataclass(frozen=True)
class ToolPolicy:
    """이 실행에서 허용되는 도구. 작업 종류마다 하나씩 고정된다.

    v0.1 은 '도구 없음'을 불리언 하나로 표현했다. 검색 작업이 생기면서
    '허용 목록'이라는 세 번째 상태가 필요해졌는데, 전역 설정
    (fail_on_tool_use) 을 느슨하게 푸는 방식은 쓰지 않는다. 그렇게 하면
    기존 PDF 분석의 fail-closed 까지 같이 풀린다.

    대신 실행마다 정책을 명시적으로 붙이고, 판정은 그 정책에 대해서만 한다.
    기본값은 도구 없음이다. 정책을 지정하지 않은 호출 경로는 예전과 똑같이
    도구가 전부 꺼진 채 실행된다(fail-closed).

      allowed_tools  : 이 실행이 광고해도 되고 호출해도 되는 도구. 비어 있으면
                       도구 전면 금지.
      required_tools : 최소 한 번은 실제로 호출되어야 하는 도구. 하나도 부르지
                       않았으면 실행을 성공으로 두지 않는다.
      max_tool_calls : 도구 호출 총 횟수 상한. 0 이면 상한 없음. 넘으면
                       Provider 가 프로세스를 끊는다.
      enforce_advertised_allowlist : Provider 가 모델에게 노출한 도구 목록까지
                       allowed_tools 와 일치해야 하는가. Claude 는 --tools 로 이를
                       강제할 수 있다. agy 는 모든 도구를 항상 노출하므로 False 이며,
                       이 경우 실제 호출만 사후 검사한다.

    도메인 제한은 여기에 없다. Claude CLI 는 WebFetch 에만 도메인 규칙을 걸 수
    있고 WebSearch 에는 걸 수 없으므로, ARIA 가 '검색 도메인을 제한한다'고
    주장할 근거가 없다. 없는 보증을 필드로 만들지 않는다.
    """

    name: str
    allowed_tools: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    max_tool_calls: int = 0
    enforce_advertised_allowlist: bool = True

    @property
    def tools_disabled(self) -> bool:
        return not self.allowed_tools

    def unexpected(self, names) -> list[str]:
        """허용 목록 밖의 도구 이름만 순서대로 돌려준다."""
        allowed = set(self.allowed_tools)
        seen: list[str] = []
        for name in names:
            if name not in allowed and name not in seen:
                seen.append(name)
        return seen


# 도구를 전부 끈 실행. 기존 PDF/문헌 분석의 정책이며 기본값이다.
NO_TOOLS = ToolPolicy(name="no_tools")

# 유사 문헌 검색 실행. WebSearch/WebFetch 만 허용한다.
#
# Bash/Read/Write/Edit/Task 등은 목록에 없으므로 광고되기만 해도 정책 위반이다.
# WebSearch 를 한 번도 부르지 않으면 검색을 수행한 것이 아니므로 실패로 본다.
WEB_SEARCH = ToolPolicy(
    name="web_search",
    allowed_tools=("WebSearch", "WebFetch"),
    required_tools=("WebSearch",),
    max_tool_calls=40,
)

# agy 검색 실행. agy 는 search_web/read_url_content 를 실제로 제공하지만
# --tools 같은 노출 제한 플래그가 없다. 따라서 이 정책은 허용 도구의 사전
# allowlist 가 아니라 실제 호출에 대한 사후 탐지 계약이다. --sandbox 와 agy 의
# request-review 권한 모드를 함께 쓰지만, ARIA 가 호출 자체를 차단한다고 주장하지
# 않는다.
AGY_WEB_SEARCH = ToolPolicy(
    name="agy_web_search",
    allowed_tools=("search_web", "read_url_content"),
    required_tools=("search_web",),
    max_tool_calls=40,
    enforce_advertised_allowlist=False,
)

# Codex 검색 실행. Codex 는 `[tools]` 설정으로 web_search 를 켜고 끌 수 있지만
# 셸·파일 도구를 끄는 수단은 없다. 따라서 이것도 사전 allowlist 가 아니라 실제
# 호출에 대한 사후 탐지 계약이다. 도구 이름은 CLI 가 내보내는 항목 종류
# (item.type) 를 그대로 쓴다 — codex_stream.TOOL_ITEM_TYPES 를 보라.
CODEX_WEB_SEARCH = ToolPolicy(
    name="codex_web_search",
    allowed_tools=("web_search",),
    required_tools=("web_search",),
    max_tool_calls=40,
    enforce_advertised_allowlist=False,
)

POLICIES = {
    policy.name: policy
    for policy in (NO_TOOLS, WEB_SEARCH, AGY_WEB_SEARCH, CODEX_WEB_SEARCH)
}


@dataclass
class ExecutionRequest:
    job_id: str
    work_dir: Path
    system_prompt: str
    user_message: str
    model: str | None = None
    timeout_seconds: int = 900
    # 지정하지 않으면 도구 없음. 새 호출 경로가 실수로 도구를 여는 일이 없도록
    # 기본값을 닫힌 쪽에 둔다.
    tool_policy: ToolPolicy = NO_TOOLS


@dataclass
class ExecutionOutcome:
    """Provider 가 돌려주는 원시 실행 결과.

    성공/실패 판정은 여기서 하지 않는다. ResultEvaluator 가 첨부 정보까지
    합쳐서 판정한다.
    """

    result_text: str = ""
    exit_code: int | None = None
    terminal_reason: str | None = None
    is_error: bool = False
    error_message: str | None = None
    usage: dict | None = None
    permission_denials: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""
    cli_path: str | None = None
    cli_version: str | None = None
    cli_args: list[str] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    auth_required: bool = False
    rate_limited: bool = False

    # 도구 정책. v0.1 에서 '도구 없음'은 편의 설정이 아니라 보안 불변조건이다.
    # tools_must_be_disabled 인 Provider 가 도구를 광고하거나 실제로 호출하면
    # 경고가 아니라 실패로 처리한다.
    tools_advertised: list[str] = field(default_factory=list)
    tool_uses: list[str] = field(default_factory=list)
    tools_must_be_disabled: bool = False
    # 도구를 끌 수단이 아예 없는 Provider. 이 경우 도구 호출은
    # 설정과 무관하게 항상 실패로 처리한다(사용자가 완화할 수 없다).
    tools_uncontrollable: bool = False
    # 이 실행에 적용한 도구 정책. Provider 가 채운다. None 이면 정책을 선언하지
    # 않은 Provider 이며, 위의 두 불리언으로만 판정한다.
    tool_policy: ToolPolicy | None = None
    # 도구 호출 감사 기록. 이름·시각·요약된 입력·성공 여부.
    # 검색 작업의 "실제 검색어"는 모델의 자기 보고가 아니라 여기서 온다.
    tool_calls: list[dict] = field(default_factory=list)
    # 정책의 max_tool_calls 를 넘겨서 ARIA 가 프로세스를 끊었다.
    tool_budget_exceeded: bool = False


# 실행 중 진행 상황을 밖으로 흘려보내는 콜백.
EmitFn = Callable[[str, dict], Awaitable[None]]


class Provider(abc.ABC):
    id: str = ""
    display_name: str = ""
    install_hint: str = ""

    # 이 Provider 가 실제로 강제할 수 있는 도구 정책. 기본은 '도구 없음' 뿐이다.
    # 도구를 목록으로 제한하는 플래그가 있는 Provider 만 넓힌다.
    supported_tool_policies: frozenset[str] = frozenset({NO_TOOLS.name})
    # 유사 문헌 검색에 사용할 정책. None 이면 검색 미지원이다.
    search_tool_policy: ToolPolicy | None = None

    # 이 Provider 가 한 번의 메시지로 받을 수 있는 입력 바이트 상한(UTF-8).
    # None 이면 상한을 강제하지 않는다. 자체적으로 큰 입력을 조용히 잘라 버리는
    # Provider 만 값을 선언한다 — ARIA 의 문자수 한도(max_inline_chars)는 바이트로
    # 재는 이 한도를 대신하지 못한다.
    max_input_bytes: int | None = None

    def supports_tool_policy(self, policy: ToolPolicy) -> bool:
        return policy.name in self.supported_tool_policies

    @abc.abstractmethod
    async def probe(self) -> ProbeResult:
        """설치/실행 가능/버전/인증 상태를 확인한다. 모델 호출은 하지 않는다."""

    @abc.abstractmethod
    async def execute(self, request: ExecutionRequest, emit: EmitFn) -> ExecutionOutcome:
        """프롬프트를 실행한다."""

    @abc.abstractmethod
    async def cancel(self, job_id: str) -> bool:
        """실행 중인 작업의 프로세스 트리를 종료한다."""

    async def smoke_test(self, emit: EmitFn | None = None) -> ExecutionOutcome:
        """실제 모델을 호출하는 검증. 사용량이 발생한다."""
        raise NotImplementedError
