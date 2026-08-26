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
      content_read_tools : 이름만으로는 허용하지 않고, 인자 범위까지 봐야 허용
                       여부가 갈리는 도구. 가져온 페이지 본문을 파일로만 돌려주는
                       Provider 가 여기에 해당한다. Provider 가 인자를 검사해
                       call["scope_ok"] 를 True 로 표시한 호출만 허용된다.
      max_content_read_calls : 위 도구의 호출 상한. 검색 호출 상한과 따로 센다.
                       페이지 하나를 100줄씩 나눠 읽는 것과 검색을 100번 하는
                       것은 다른 행동이고, 한 예산에 섞으면 본문을 성실히 읽을수록
                       검색 예산이 말라 버린다.

    도메인 제한은 여기에 없다. Claude CLI 는 WebFetch 에만 도메인 규칙을 걸 수
    있고 WebSearch 에는 걸 수 없으므로, ARIA 가 '검색 도메인을 제한한다'고
    주장할 근거가 없다. 없는 보증을 필드로 만들지 않는다.
    """

    name: str
    allowed_tools: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    max_tool_calls: int = 0
    enforce_advertised_allowlist: bool = True
    content_read_tools: tuple[str, ...] = ()
    max_content_read_calls: int = 0

    @property
    def tools_disabled(self) -> bool:
        return not (self.allowed_tools or self.content_read_tools)

    def unexpected(self, names) -> list[str]:
        """허용 목록 밖의 도구 이름만 순서대로 돌려준다.

        이름만 보는 검사다. content_read_tools 는 이름만으로 허용되지 않으므로
        여기서는 위반으로 잡힌다 — 인자를 볼 수 없는 호출 경로가 이 함수를 쓰면
        닫힌 쪽으로 판정된다. 인자까지 보려면 unexpected_calls 를 쓴다.
        """
        allowed = set(self.allowed_tools)
        seen: list[str] = []
        for name in names:
            if name not in allowed and name not in seen:
                seen.append(name)
        return seen

    def unexpected_calls(self, calls) -> list[str]:
        """이름과 인자 범위를 함께 보고 허용 목록 밖의 호출을 돌려준다.

        content_read_tools 는 Provider 가 인자를 검사해 call["scope_ok"] 를
        True 로 표시한 호출만 통과시킨다. 표시가 없으면 위반이다(fail-closed) —
        인자를 검사할 줄 모르는 Provider 가 content_read_tools 를 선언하는 것만
        으로 조용히 열려서는 안 된다.

        이것도 사후 감사다. 판정이 나오는 시점에는 그 호출이 이미 끝나 있다.
        """
        allowed = set(self.allowed_tools)
        scoped = set(self.content_read_tools)
        seen: list[str] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            if name in allowed:
                continue
            if name in scoped and call.get("scope_ok") is True:
                continue
            if name not in seen:
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
#
# view_file 은 allowed_tools 가 아니라 content_read_tools 에 있다. agy 의
# read_url_content 는 가져온 페이지를 파일에 저장하고 경로만 돌려주므로, 본문을
# 읽는 유일한 통로가 view_file 이다. 그렇다고 이름만으로 열어 주면 임의의 로컬
# 파일 읽기가 함께 열린다. 그래서 "이번 대화의 read_url_content 산출물"이라는
# 인자 조건을 만족한 호출만 agy_cli 가 scope_ok 로 표시하고, 그것만 통과한다.
# 다른 경로·다른 대화·일반 파일은 그대로 위반이다.
AGY_WEB_SEARCH = ToolPolicy(
    name="agy_web_search",
    allowed_tools=("search_web", "read_url_content"),
    required_tools=("search_web",),
    max_tool_calls=40,
    enforce_advertised_allowlist=False,
    content_read_tools=("view_file",),
    max_content_read_calls=40,
)

# Codex 검색 실행. Codex 는 `[tools]` 설정으로 web_search 를 켜고 끌 수 있지만
# 셸·파일 도구를 끄는 수단은 없다. 따라서 이것도 사전 allowlist 가 아니라 실제
# 호출에 대한 사후 탐지 계약이다. 도구 이름은 CLI 가 내보내는 항목 종류
# (item.type) 를 그대로 쓴다 — codex_stream.TOOL_ITEM_TYPES 를 보라.
#
# 페이지 본문을 여는 도구가 없다. 그래서 이 정책으로 나온 후보는 검색 스니펫
# 수준을 넘지 못하고, 구성 대응표의 page_text 행은 만들어질 수 없다(대조할
# 열람 기록이 없으므로 search_manifest 가 snippet 으로 내린다). 근거 기반
# 대응표가 필요한 실행에는 쓰지 않는다 — 스니펫 기반 후보 탐색 전용이다.
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
    # 정책의 max_content_read_calls 를 넘겨서 ARIA 가 프로세스를 끊었다.
    # 검색 상한과 따로 센다 — 사용자에게 "검색을 줄여라"와 "본문 읽기를
    # 줄여라"는 다른 지시다.
    content_read_budget_exceeded: bool = False


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

    # 이 Provider 가 자료 전체를 손실 없이 모델에 전달할 수 있는 입력 바이트
    # 상한(UTF-8). 사용자 입력 제한이 아니라 전달 경로의 한계이므로 설정으로
    # 끄지 못한다. None 이면 상한을 강제하지 않는다 — 자체적으로 큰 입력을
    # 조용히 잘라 버리는 Provider 만 값을 선언한다.
    #
    # ARIA 의 글자 수 한도(max_inline_chars)는 이것을 대신하지 못한다. 그쪽은
    # 사용자가 스스로 거는 상한이라 0(제한 없음)으로 끌 수 있고, 애초에 문자로
    # 세는 다른 축이다. 한글 한 글자는 UTF-8 3 bytes 다.
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
