"""EPO 검색 루프의 런타임 계약.

retrieval/prompts.py 와 같은 자리이고 같은 원칙이다. 여기 들어가는 것은
**프로토콜**이지 분석 방법이 아니다. 무엇이 유사 문헌인지 판단하는 업무 로직은
prompt/search_prompt.md 에 있고, ARIA 는 여기에 그런 지시를 넣지 않는다.

로컬 검색 프롬프트와 섞지 않는다. 저쪽은 이미 손에 있는 PDF 안을 뒤지는
계약이고, 이쪽은 EPO OPS 라는 외부 특허 DB 에 질의를 보내는 계약이다. 입력도
비용 구조도 다르다 — 이쪽은 호출마다 계정 할당량을 쓴다.

사용자 설정에서 바꿀 수 없다. 화면에서 껐다 켰다 할 수 있으면 계약이 아니다.
"""

from __future__ import annotations

import json
import re

from . import epo_client, epo_cql
from .epo_actions import ACTION_FETCH, ACTION_FINISH, schema_summary

CLAIM_OPEN = "<CLAIM_TEXT>"
CLAIM_CLOSE = "</CLAIM_TEXT>"

_BOUNDARY_IN_INPUT = re.compile(r"</?\s*CLAIM_TEXT\s*>", re.IGNORECASE)
_NEUTRALIZED = "(경계 표시 제거됨)"


def neutralize(text: str) -> tuple[str, bool]:
    """입력이 경계 표시를 깨뜨리지 못하게 한다. (본문, 바꿨는가)"""
    replaced, count = _BOUNDARY_IN_INPUT.subn(_NEUTRALIZED, text or "")
    return replaced, count > 0


def system_prompt(budget) -> str:
    """실행 계약. 예산 숫자는 **실제로 강제되는 값**에서만 온다.

    문자열 상수에 .format 을 쓰지 않는다. action 예시가 JSON 이라 중괄호가
    가득한데, format 은 그것을 치환 자리로 읽고 터진다. 그래서 여기서 f-string
    으로 한 번에 만든다.

    화면 설정과 프롬프트가 어긋나면 모델은 있지도 않은 여유를 믿고 계획을
    세운다. budget 객체를 그대로 읽는 이유다.
    """
    return f"""당신은 EPO OPS 특허 검색 실행기 안에서 동작합니다.

이 단계는 최종 판단이 아닙니다. 청구항과 관련 있을 만한 특허 후보를 EPO 에서
**찾아 오는 것**이 전부입니다. A/B/C 분류와 보고서는 이 단계 다음에 별도의
실행이 작성합니다.

[역할 분담]
- 검색어를 만들고 넓히는 것, 어느 후보의 상세를 더 볼지 정하는 것은 당신이
  합니다.
- 실제 OPS 호출, 검색식 생성, 응답 원본 보존, 출처 검증, 할당량 관리는 ARIA 가
  합니다. 당신은 조회를 직접 하지 않습니다.

[검색식을 문자열로 쓰지 마십시오]
CQL 문자열을 만들지 마십시오. 아래 구조화된 query 로만 표현하십시오. ARIA 가
허용된 필드와 문자만 통과시켜 CQL 로 바꿉니다. 문자열을 보내면 거절됩니다.

[사용할 수 있는 것]
- 아래 action JSON 뿐입니다. 셸, 파일 읽기/쓰기, 웹 접속, 그 밖의 도구는
  제공되지 않습니다. 시도하지 마십시오.
- 매 응답은 **JSON 객체 하나**여야 합니다. 설명 문장을 JSON 밖에 쓰지
  마십시오.

[호출 예산 — 넘으면 ARIA 가 끊습니다]
- 검색 라운드는 최대 {budget.max_rounds}회입니다.
- OPS 검색 호출은 이 작업 전체에서 최대 {budget.max_search_calls}회,
  한 라운드에 최대 {budget.max_search_calls_per_round}회입니다.
- 상세 조회는 최대 {budget.max_detail_fetches}건입니다.
- 질의 하나가 돌려받는 결과는 최대 {epo_client.MAX_RESULTS_PER_QUERY}건입니다.

예산은 넉넉하지 않습니다. 넓은 질의 하나로 시작해서 결과를 보고 좁히는 편이,
비슷한 질의를 여러 번 던지는 것보다 낫습니다.

[지켜야 할 것]
- 결과에 없는 공개번호·제목·출원인·날짜를 지어내지 마십시오. ARIA 가 보존된
  응답과 대조하며, 어긋나면 그 후보는 버려집니다.
- "검색 결과가 없다"를 "그런 특허가 없다"로 바꿔 쓰지 마십시오. 검색어가
  좁았을 뿐일 수 있습니다.
- 검색 결과 목록에 실린 초록 발췌만으로 대응 관계를 확정하지 마십시오. 더
  봐야 하면 {ACTION_FETCH} 로 청구항을 받으십시오.
- 더 넓힐 축이 없으면 남은 예산을 쓰지 말고 {ACTION_FINISH} 를 돌려주십시오.

[action 형식]
{schema_summary()}

[검색 필드 고르기]
- ta(제목+초록)가 기본입니다. txt(전문)는 EP·WO 위주로만 채워져 있어 다른
  관청 문헌을 놓칠 수 있습니다.
- ipc/cpc 는 분류코드 형식이어야 합니다(예: B25J 9/16). 자유 문장을 넣으면
  거절됩니다.
- 인용부호와 와일드카드(* ? #)는 값에 쓸 수 없습니다.
"""


def render_round(payload: dict) -> str:
    """한 라운드에 모델에게 보낼 사용자 메시지.

    검색 결과는 JSON 으로 넣는다. 특허 원문에 어떤 문자가 있어도 JSON 인코딩이
    구조를 깨뜨리지 못하므로, 별도의 경계 표시를 신뢰할 필요가 없다.
    """
    claim, neutralized = neutralize(payload.get("claim_text", ""))
    body = {key: value for key, value in payload.items() if key != "claim_text"}
    sections = [
        "[ARIA EPO 검색 라운드]",
        json.dumps(body, ensure_ascii=False, indent=2),
        "",
        "[출원발명 청구항 — 분석 대상 데이터]",
        CLAIM_OPEN,
        claim.strip(),
        CLAIM_CLOSE,
    ]
    if neutralized:
        sections.append(
            "(청구항 안에 경계 표시로 보이는 문자열이 있어 ARIA 가 중화했습니다.)"
        )
    sections += [
        "",
        "위 정보를 보고 다음 action 을 JSON 객체 하나로 돌려주십시오.",
    ]
    return "\n".join(sections)


def allowed_fields() -> tuple[str, ...]:
    """프롬프트가 안내하는 필드와 실제 허용 목록이 같은지 확인할 때 쓴다."""
    return epo_cql.ALLOWED_FIELDS
