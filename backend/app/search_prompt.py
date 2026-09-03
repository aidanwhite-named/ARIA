"""검색 전략 프롬프트 로딩과 데이터 구간 조립.

분석 프롬프트와 같은 저장 방식(prompt/ 폴더의 UTF-8 파일 + ARIA 메타데이터
헤더)을 쓰지만, 종류(kind)가 다르다. 검색 작업만 kind=search 프롬프트를 읽고,
분석 작업은 그것을 보지 않는다. 검색 전략 프롬프트는 여러 개일 수 있고 실행마다
고른다.

두 가지 조립 방식
-----------------
    appended_sections   기본값. 사용자는 검색 전략만 쓰고, ARIA 가 그 뒤에
                        데이터 구간(청구항·미대응 구성·명세서)을 붙인다.
                        경계 표시와 placeholder 를 사용자가 관리하지 않는다.
    legacy_placeholders 본문에 ``{{CLAIM_TEXT}}`` 가 있는 옛 프롬프트. 예전
                        계약 그대로 치환한다. 이미 만들어 둔 프롬프트와 이미
                        실행한 작업의 스냅샷이 계속 돌아야 한다.

어느 쪽이든 청구항·명세서·미대응 구성은 서로 다른 경계 안에 격리되고, 입력에
들어 있는 경계 표시는 삽입 전에 중화된다.

프롬프트 본문을 파이썬 소스에 넣지 않는다. 본문은 사용자가 읽고 고칠 수 있는
파일이어야 하고, 실행마다 어떤 본문으로 돌았는지 해시로 남아야 한다.

청구항은 지시문이 아니라 분석 대상 데이터다. 그래서 본문 안의
``<CLAIM_TEXT>`` … ``</CLAIM_TEXT>`` 경계 안에만 들어간다. 경계 표시가 없거나
placeholder 가 없으면 실행하지 않고 오류를 낸다 — 경계 없이 붙이면 청구항에
섞인 문장이 실행 지시로 읽힐 수 있다.

경계 자체를 청구항으로 깨뜨리는 것도 막는다. 사용자가 청구항 칸에
``</CLAIM_TEXT>`` 를 넣으면 그 뒤 내용이 경계 밖에 놓이므로, 삽입 전에
중화한다. 이건 완화책이지 보안 경계가 아니다. 실제 경계는 도구 허용 목록이다.

미대응 구성 보완 검색
--------------------
구성대비 결과에서 고른 미대응 구성은 ``<SEARCH_FOCUS>`` 경계 안에 별도 데이터로
넣는다. 일반 유사문헌 검색에는 이 절 전체가 없어야 하므로 명세서 절과 같은 방식의
선택 블록을 쓴다. 모델이 만든 구성 문구도 신뢰할 수 없는 입력이므로 경계 문자열을
중화한다.

출원발명 문서(명세서)
--------------------
청구항 문언이 중의적이거나 지나치게 포괄적일 때, 명세서의 용례·동의어·영문어가
검색어를 넓히는 데 도움이 된다. 그래서 명세서를 선택 입력으로 받는다.

명세서는 청구항과 같은 칸에 넣지 않는다. 두 자료는 역할이 다르다.

    청구항   검색 범위를 정하는 기준.
    명세서   격리된 보조 검색의 검색어를 넓히는 자료. 범위를 좁히는 근거가 아니다.

같은 모델 호출에 넣으면 별도 경계가 있어도 명세서가 청구항 단독 검색에 영향을
줄 수 있다. runner 는 명세서 없는 기본 프롬프트와 명세서가 있는 보조 프롬프트를
따로 렌더링하고 독립 실행한다. 여기서는 보조 프롬프트 안에서도 자료 역할이
섞이지 않게 ``<SPEC_TEXT>`` … ``</SPEC_TEXT>`` 경계를 둔다.

명세서 절 전체는 ``<!--ARIA_SPEC_BLOCK-->`` … ``<!--/ARIA_SPEC_BLOCK-->`` 로
감싼다. 명세서를 넣지 않은 실행에서는 이 구간을 통째로 걷어낸다. 빈 칸과
"명세서를 이렇게 쓰라"는 규칙만 남기면, 없는 자료에 대한 지시가 매 실행마다
모델 앞에 놓인다. 걷어내면 명세서를 쓰지 않는 실행의 최종 본문은 이 기능이
없던 때와 정확히 같다. 이 렌더링 결과가 청구항 단독 실행에 쓰인다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from . import search_contract
from .prompt_store import (
    KIND_SEARCH,
    PROMPT_STORE,
    PromptFile,
    PromptStoreError,
)

SEARCH_PROMPT_ID = "search_prompt.md"

# 조립 방식. 감사 기록에 남는다 — 같은 청구항이라도 어느 방식으로 조립했는지에
# 따라 모델이 받은 본문이 다르다.
MODE_APPENDED = "appended_sections"
MODE_LEGACY = "legacy_placeholders"

PLACEHOLDER = "{{CLAIM_TEXT}}"
OPEN_TAG = "<CLAIM_TEXT>"
CLOSE_TAG = "</CLAIM_TEXT>"

SPEC_PLACEHOLDER = "{{SPEC_TEXT}}"
SPEC_OPEN_TAG = "<SPEC_TEXT>"
SPEC_CLOSE_TAG = "</SPEC_TEXT>"
SPEC_BLOCK_OPEN = "<!--ARIA_SPEC_BLOCK-->"
SPEC_BLOCK_CLOSE = "<!--/ARIA_SPEC_BLOCK-->"

FOCUS_PLACEHOLDER = "{{SEARCH_FOCUS}}"
FOCUS_OPEN_TAG = "<SEARCH_FOCUS>"
FOCUS_CLOSE_TAG = "</SEARCH_FOCUS>"
FOCUS_BLOCK_OPEN = "<!--ARIA_GAP_BLOCK-->"
FOCUS_BLOCK_CLOSE = "<!--/ARIA_GAP_BLOCK-->"

# 입력 안에 들어 있으면 경계를 깨는 문자열. 대소문자를 가리지 않는다.
# 청구항 칸과 명세서 칸 양쪽에 같은 목록을 적용한다 — 명세서에 들어 있는
# ``</CLAIM_TEXT>`` 는 자기 경계는 못 깨도 앞 칸이 닫힌 것처럼 보이게 만든다.
_BOUNDARY_IN_INPUT = re.compile(
    r"</?\s*(?:CLAIM_TEXT|SPEC_TEXT|SEARCH_FOCUS)\s*>"
    r"|<!--\s*/?\s*ARIA_(?:SPEC|GAP)_BLOCK\s*-->",
    re.IGNORECASE,
)
_NEUTRALIZED = "(경계 표시 제거됨)"

# 두 placeholder 를 한 번에 바꾸기 위한 패턴.
_PLACEHOLDERS = re.compile(
    "|".join(
        re.escape(name)
        for name in (PLACEHOLDER, SPEC_PLACEHOLDER, FOCUS_PLACEHOLDER)
    )
)


class SearchPromptError(Exception):
    """검색 프롬프트를 읽지 못했거나 실행 계약을 만족하지 않는다."""


@dataclass(frozen=True)
class RenderedPrompt:
    """최종 본문과, 감사 기록에 남겨야 하는 사실."""

    body: str
    claim_boundary_neutralized: bool
    spec_boundary_neutralized: bool
    spec_included: bool
    focus_boundary_neutralized: bool
    focus_included: bool
    # 어느 조립 방식으로 만들었는가.
    mode: str = MODE_LEGACY
    # 사용자 전략 본문에 경계 표시가 들어 있어 중화했는가. 옛 방식에는 없던
    # 사실이다 — 그때는 경계가 본문의 일부였다.
    strategy_boundary_neutralized: bool = False


def load(prompt_id: str = SEARCH_PROMPT_ID) -> PromptFile:
    """검색 전략 프롬프트 하나를 읽고 조립 계약을 검사한다.

    id 를 주지 않으면 배포본을 읽는다. 옛 호출부(검색 프롬프트가 하나뿐이던
    시절)가 그대로 돌아야 하기 때문이다.
    """
    target = str(prompt_id or SEARCH_PROMPT_ID)
    try:
        prompt = PROMPT_STORE.get_for_kind(target, KIND_SEARCH)
    except PromptStoreError as exc:
        raise SearchPromptError(
            f"검색 전략 프롬프트를 읽지 못했습니다({target}): {exc}"
        ) from exc
    validate_strategy_body(prompt.body, prompt_id=target)
    return prompt


def is_legacy_template(body: str) -> bool:
    """이 본문이 placeholder 를 직접 관리하는 옛 프롬프트인가."""
    return PLACEHOLDER in body


def validate_strategy_body(body: str, *, prompt_id: str = SEARCH_PROMPT_ID) -> None:
    """조립 전에 본문이 성립하는지 본다.

    새 방식에는 요구하는 표시가 없다. 사용자는 전략만 쓰고 경계는 ARIA 가
    붙이므로, 검사할 계약이 "비어 있지 않다" 하나뿐이다. 옛 방식 본문만 예전
    계약(placeholder 하나 + 경계 안)을 그대로 통과해야 한다.
    """
    if not body.strip():
        raise SearchPromptError(f"{prompt_id} 의 검색 전략 본문이 비어 있습니다.")
    if is_legacy_template(body):
        validate_body(body, prompt_id=prompt_id)


def has_spec_section(body: str) -> bool:
    """이 본문이 명세서를 받을 수 있는가.

    작업 생성 시점에 확인한다. 명세서를 첨부했는데 본문에 넣을 자리가 없으면
    조용히 무시하지 않고 거절해야 한다.
    """
    return SPEC_BLOCK_OPEN in body and SPEC_BLOCK_CLOSE in body


def has_focus_section(body: str) -> bool:
    """이 검색 프롬프트가 미대응 구성 데이터를 받을 수 있는가."""
    return FOCUS_BLOCK_OPEN in body and FOCUS_BLOCK_CLOSE in body


def validate_body(body: str, *, prompt_id: str = SEARCH_PROMPT_ID) -> None:
    """본문이 실행 계약(placeholder 하나 + 경계 안)을 만족하는지 확인한다.

    파일에서 막 읽은 본문과 작업에 스냅샷된 본문 둘 다 이 검사를 거친다.
    검사가 갈라지면 큐에서 기다리는 사이 계약이 깨진 본문이 실행될 수 있다.
    """
    if body.count(PLACEHOLDER) != 1:
        raise SearchPromptError(
            f"{prompt_id} 에 {PLACEHOLDER} placeholder 가 정확히 한 번 "
            f"있어야 합니다(현재 {body.count(PLACEHOLDER)}개)."
        )

    open_at = body.find(OPEN_TAG)
    close_at = body.find(CLOSE_TAG)
    holder_at = body.find(PLACEHOLDER)
    if open_at < 0 or close_at < 0:
        raise SearchPromptError(
            f"{prompt_id} 에 청구항 경계 표시({OPEN_TAG} … {CLOSE_TAG})가 "
            "없습니다. 청구항을 경계 없이 넣지 않습니다."
        )
    if not open_at < holder_at < close_at:
        raise SearchPromptError(
            f"{prompt_id} 의 {PLACEHOLDER} 가 청구항 경계 안에 있지 "
            "않습니다."
        )

    _validate_spec_section(
        body, claim_open=open_at, claim_close=close_at, prompt_id=prompt_id
    )
    _validate_focus_section(
        body, claim_open=open_at, claim_close=close_at, prompt_id=prompt_id
    )

    if has_spec_section(body) and has_focus_section(body):
        spec_open = body.find(SPEC_BLOCK_OPEN)
        spec_close = body.find(SPEC_BLOCK_CLOSE) + len(SPEC_BLOCK_CLOSE)
        focus_open = body.find(FOCUS_BLOCK_OPEN)
        focus_close = body.find(FOCUS_BLOCK_CLOSE) + len(FOCUS_BLOCK_CLOSE)
        if spec_open < focus_close and focus_open < spec_close:
            raise SearchPromptError(
                f"{prompt_id} 의 명세서 절과 미대응 구성 절이 겹칩니다."
            )


def _validate_spec_section(
    body: str, *, claim_open: int, claim_close: int, prompt_id: str
) -> None:
    """명세서 절의 계약. 절 자체가 없는 본문도 유효하다.

    없으면 명세서를 받지 않는 프롬프트일 뿐이다. 다만 흔적만 남아 있는 상태
    (열림 표시만 있거나 placeholder 만 있는 편집 중 파일)는 거절한다. 그런
    본문으로 실행하면 명세서가 경계 밖에 놓이거나 아예 사라진다.
    """
    marks = {
        SPEC_BLOCK_OPEN: body.count(SPEC_BLOCK_OPEN),
        SPEC_BLOCK_CLOSE: body.count(SPEC_BLOCK_CLOSE),
        SPEC_OPEN_TAG: body.count(SPEC_OPEN_TAG),
        SPEC_CLOSE_TAG: body.count(SPEC_CLOSE_TAG),
        SPEC_PLACEHOLDER: body.count(SPEC_PLACEHOLDER),
    }
    if not any(marks.values()):
        return
    broken = [name for name, count in marks.items() if count != 1]
    if broken:
        raise SearchPromptError(
            f"{prompt_id} 의 명세서 절이 온전하지 않습니다. "
            f"{', '.join(broken)} 가 정확히 한 번씩 있어야 합니다."
        )

    block_open = body.find(SPEC_BLOCK_OPEN)
    spec_open = body.find(SPEC_OPEN_TAG)
    spec_holder = body.find(SPEC_PLACEHOLDER)
    spec_close = body.find(SPEC_CLOSE_TAG)
    block_close = body.find(SPEC_BLOCK_CLOSE)
    if not block_open < spec_open < spec_holder < spec_close < block_close:
        raise SearchPromptError(
            f"{prompt_id} 의 {SPEC_PLACEHOLDER} 가 명세서 경계"
            f"({SPEC_OPEN_TAG} … {SPEC_CLOSE_TAG}) 안에 있지 않습니다."
        )

    # 명세서를 넣지 않은 실행은 이 구간을 통째로 지운다. 청구항이 이 안에
    # 있으면 함께 사라진다.
    if block_open < claim_close and claim_open < block_close:
        raise SearchPromptError(
            f"{prompt_id} 의 명세서 절이 청구항 경계와 겹칩니다. "
            "두 자료는 서로 다른 경계에 있어야 합니다."
        )


def _validate_focus_section(
    body: str, *, claim_open: int, claim_close: int, prompt_id: str
) -> None:
    """미대응 구성 선택 절의 계약. 절 자체가 없는 본문도 유효하다."""
    marks = {
        FOCUS_BLOCK_OPEN: body.count(FOCUS_BLOCK_OPEN),
        FOCUS_BLOCK_CLOSE: body.count(FOCUS_BLOCK_CLOSE),
        FOCUS_OPEN_TAG: body.count(FOCUS_OPEN_TAG),
        FOCUS_CLOSE_TAG: body.count(FOCUS_CLOSE_TAG),
        FOCUS_PLACEHOLDER: body.count(FOCUS_PLACEHOLDER),
    }
    if not any(marks.values()):
        return
    broken = [name for name, count in marks.items() if count != 1]
    if broken:
        raise SearchPromptError(
            f"{prompt_id} 의 미대응 구성 절이 온전하지 않습니다. "
            f"{', '.join(broken)} 가 정확히 한 번씩 있어야 합니다."
        )

    block_open = body.find(FOCUS_BLOCK_OPEN)
    focus_open = body.find(FOCUS_OPEN_TAG)
    focus_holder = body.find(FOCUS_PLACEHOLDER)
    focus_close = body.find(FOCUS_CLOSE_TAG)
    block_close = body.find(FOCUS_BLOCK_CLOSE)
    if not block_open < focus_open < focus_holder < focus_close < block_close:
        raise SearchPromptError(
            f"{prompt_id} 의 {FOCUS_PLACEHOLDER} 가 미대응 구성 경계"
            f"({FOCUS_OPEN_TAG} … {FOCUS_CLOSE_TAG}) 안에 있지 않습니다."
        )
    if block_open < claim_close and claim_open < block_close:
        raise SearchPromptError(
            f"{prompt_id} 의 미대응 구성 절이 청구항 경계와 겹칩니다."
        )


def sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def neutralize_boundaries(text: str) -> tuple[str, bool]:
    """입력에서 경계 표시를 중화한다. (본문, 바뀌었는지) 를 돌려준다."""
    cleaned, count = _BOUNDARY_IN_INPUT.subn(_NEUTRALIZED, text)
    return cleaned, count > 0


def _drop_spec_section(body: str) -> str:
    """명세서 절을 여는 표시부터 닫는 표시까지 통째로 걷어낸다."""
    start = body.find(SPEC_BLOCK_OPEN)
    end = body.find(SPEC_BLOCK_CLOSE)
    if start < 0 or end < 0:
        return body
    return (body[:start].rstrip() + "\n\n" + body[end + len(SPEC_BLOCK_CLOSE):].lstrip()).strip()


def _unwrap_spec_section(body: str) -> str:
    """명세서 절을 감싼 표시만 없앤다. 안의 규칙과 경계 태그는 남는다."""
    marks = (
        SPEC_BLOCK_OPEN + "\n",
        SPEC_BLOCK_OPEN,
        "\n" + SPEC_BLOCK_CLOSE,
        SPEC_BLOCK_CLOSE,
    )
    for mark in marks:
        body = body.replace(mark, "", 1)
    return body


def _drop_focus_section(body: str) -> str:
    """미대응 구성 절을 통째로 걷어낸다."""
    start = body.find(FOCUS_BLOCK_OPEN)
    end = body.find(FOCUS_BLOCK_CLOSE)
    if start < 0 or end < 0:
        return body
    return (
        body[:start].rstrip()
        + "\n\n"
        + body[end + len(FOCUS_BLOCK_CLOSE) :].lstrip()
    ).strip()


def _unwrap_focus_section(body: str) -> str:
    """미대응 구성 절을 감싼 표시만 없앤다."""
    marks = (
        FOCUS_BLOCK_OPEN + "\n",
        FOCUS_BLOCK_OPEN,
        "\n" + FOCUS_BLOCK_CLOSE,
        FOCUS_BLOCK_CLOSE,
    )
    for mark in marks:
        body = body.replace(mark, "", 1)
    return body


def _boundary_section(open_tag: str, close_tag: str, text: str) -> str:
    return open_tag + chr(10) + text + chr(10) + close_tag


def compose(
    body: str,
    claim_text: str,
    spec_text: str = "",
    search_focus: str = "",
    *,
    prompt_id: str = SEARCH_PROMPT_ID,
) -> RenderedPrompt:
    """검색 전략 본문에 ARIA 의 데이터 구간을 붙여 최종 본문을 만든다.

    옛 프롬프트(placeholder 를 직접 든 본문)는 예전 경로로 보낸다. 두 경로가
    만드는 결과는 다르지만 불변조건은 같다 — 청구항·명세서·미대응 구성이 각자
    경계 안에 격리되고, 입력의 경계 표시는 삽입 전에 중화된다.

    새 경로에서는 **사용자 전략 본문 자체도** 중화 대상이다. 전략에 적힌
    ``</CLAIM_TEXT>`` 가 그대로 나가면 그 뒤의 진짜 청구항 구간이 이미 닫힌
    것처럼 보인다. 사용자가 경계를 관리하지 않게 만든 이상, 위조도 막는 쪽이
    ARIA 의 몫이다.
    """
    if is_legacy_template(body):
        return render(body, claim_text, spec_text, search_focus, prompt_id=prompt_id)

    validate_strategy_body(body, prompt_id=prompt_id)
    claim = claim_text.strip()
    if not claim:
        raise SearchPromptError("검색할 청구항이 비어 있습니다.")

    strategy, strategy_neutralized = neutralize_boundaries(body.strip())
    claim, claim_neutralized = neutralize_boundaries(claim)

    parts = [strategy, search_contract.preamble()]

    parts.append(search_contract.CLAIM_PREAMBLE)
    parts.append(_boundary_section(OPEN_TAG, CLOSE_TAG, claim))

    focus = search_focus.strip()
    focus_neutralized = False
    if focus:
        focus, focus_neutralized = neutralize_boundaries(focus)
        parts.append(search_contract.FOCUS_PREAMBLE)
        parts.append(_boundary_section(FOCUS_OPEN_TAG, FOCUS_CLOSE_TAG, focus))

    spec = spec_text.strip()
    spec_neutralized = False
    if spec:
        spec, spec_neutralized = neutralize_boundaries(spec)
        parts.append(search_contract.SPEC_PREAMBLE)
        parts.append(_boundary_section(SPEC_OPEN_TAG, SPEC_CLOSE_TAG, spec))

    return RenderedPrompt(
        body=(chr(10) * 2).join(parts),
        claim_boundary_neutralized=claim_neutralized,
        spec_boundary_neutralized=spec_neutralized,
        spec_included=bool(spec),
        focus_boundary_neutralized=focus_neutralized,
        focus_included=bool(focus),
        mode=MODE_APPENDED,
        strategy_boundary_neutralized=strategy_neutralized,
    )


def render(
    body: str,
    claim_text: str,
    spec_text: str = "",
    search_focus: str = "",
    *,
    prompt_id: str = SEARCH_PROMPT_ID,
) -> RenderedPrompt:
    """청구항과(있으면) 명세서를 각자의 경계 안에 넣은 최종 본문을 만든다.

    명세서가 비어 있으면 명세서 절을 걷어낸다. 그 처리를 치환보다 먼저 하는
    이유: 순서가 반대면 청구항 본문이 절 표시를 포함할 때 지우는 범위가 달라진다.

    두 placeholder 는 한 번의 훑기로 함께 바꾼다. 하나씩 replace 하면 먼저 넣은
    본문 안의 다른 placeholder 까지 두 번째 replace 가 건드린다 — 청구항 칸에
    ``{{SPEC_TEXT}}`` 를 적어 두면 명세서가 청구항 경계 안으로 한 벌 더 들어간다.
    """
    validate_body(body, prompt_id=prompt_id)
    claim = claim_text.strip()
    if not claim:
        raise SearchPromptError("검색할 청구항이 비어 있습니다.")

    focus = search_focus.strip()
    focus_neutralized = False
    if focus:
        if not has_focus_section(body):
            raise SearchPromptError(
                f"{prompt_id} 에 미대응 구성을 넣을 자리"
                f"({FOCUS_PLACEHOLDER})가 없습니다. 선택 구성을 무시한 채 검색하지 "
                "않습니다."
            )
        focus, focus_neutralized = neutralize_boundaries(focus)
        body = _unwrap_focus_section(body)
    else:
        body = _drop_focus_section(body)

    spec = spec_text.strip()
    spec_neutralized = False
    if spec:
        if not has_spec_section(body):
            # 첨부한 자료를 조용히 버리지 않는다.
            raise SearchPromptError(
                f"{prompt_id} 에 출원발명 문서를 넣을 자리"
                f"({SPEC_PLACEHOLDER})가 없습니다. 명세서를 무시한 채 검색하지 "
                "않습니다."
            )
        spec, spec_neutralized = neutralize_boundaries(spec)
        body = _unwrap_spec_section(body)
    else:
        body = _drop_spec_section(body)

    claim, claim_neutralized = neutralize_boundaries(claim)
    filled = {
        PLACEHOLDER: claim,
        SPEC_PLACEHOLDER: spec,
        FOCUS_PLACEHOLDER: focus,
    }
    rendered = _PLACEHOLDERS.sub(lambda m: filled.get(m.group(0), m.group(0)), body)

    return RenderedPrompt(
        body=rendered,
        claim_boundary_neutralized=claim_neutralized,
        spec_boundary_neutralized=spec_neutralized,
        spec_included=bool(spec),
        focus_boundary_neutralized=focus_neutralized,
        focus_included=bool(focus),
        mode=MODE_LEGACY,
    )
