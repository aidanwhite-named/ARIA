"""분석 실행에 덧붙이는 기계 판독 블록 출력 규칙.

ARIA 는 구성별 결과와 문헌 매핑을 사람이 읽는 Markdown 이 아니라 전용 JSON
블록으로 받는다(analysis_manifest, citation_mapping). 파서는 코드에 있는데 그
짝인 출력 규칙은 기본 Master Prompt 본문에만 있었다. 그래서 사용자가 프롬프트를
자기 것으로 바꾸면 파서는 그대로 기다리는데 규칙만 사라져서, 유사도 표도 번호
유지 후속 실행도 조용히 멈췄다. 프롬프트를 바꾼 사람은 자기가 무엇을 껐는지
알 방법이 없다.

계약의 두 짝을 같은 곳에 둔다. 규칙을 여기로 옮기고, 분석 조립이 선택된
프롬프트 뒤에 이 절을 붙인다.

이것은 "ARIA 는 Master Prompt 앞뒤로 업무 지시를 덧붙이지 않는다"는
prompt_assembly 의 원칙을 깨지 않는다. 무엇을 어떻게 분석할지 — 대비 기준,
유사도 판단, 보고서 구성 — 은 여전히 Master Prompt 하나에서 온다. 여기 있는
것은 "그 결론을 어느 형식으로 돌려 달라"는 프로토콜 한 절뿐이다.
citation_mapping 의 표현을 빌리면, 이건 분석이 아니라 프로토콜이다.

검색 실행에는 붙이지 않는다. 검색은 자기 출력 계약(search_manifest)이 따로
있고, 조립 경로도 다르다 — prompt_assembly.assemble_search 는 이 모듈을 부르지
않는다.
"""

from __future__ import annotations

from .analysis_manifest import DEFAULT_THRESHOLD

# 프롬프트가 자기 블록 규칙을 이미 갖고 있는지 판정하는 표지. 두 프로토콜의
# 여는 표지와 같아야 하므로 각 모듈에서 그대로 가져온다 — 여기서 다시 적으면
# 한쪽만 고쳤을 때 규칙이 두 번 들어간다.
from .analysis_manifest import _OPEN as _COMPONENT_OPEN
from .citation_mapping import _OPEN as _MAPPING_OPEN

# 기본 Master Prompt 에 있던 절을 그대로 옮겨 온 것이다. 유사도 기준값은
# 파서와 어긋나면 안 되므로 analysis_manifest 의 상수에서 채운다.
INSTRUCTIONS = f"""# ARIA 기계 판독 블록

종합 요약 뒤에 아래 두 블록을 출력한다. 화면에서는 제거되므로 본문에서 설명하지 않는다.

## 구성별 분석 블록

모든 구성요소를 보고서 순서로 한 번만 기록한다. `similarity`는 본문과 같은 정수, `status`는 {DEFAULT_THRESHOLD}% 이상 `matched`, 0~{DEFAULT_THRESHOLD - 1}% `below_threshold`, 판독 또는 검토 범위 제한으로 유사도를 생략한 경우는 `unreadable`이다(`not_found` 미사용). `difference`에는 미대응 기능을 간결히 쓴다. 한 줄 JSON이며 코드펜스를 쓰지 않는다.

{_COMPONENT_OPEN}
{{"items":[{{"claim":"청구항 1","symbol":"(A)","feature":"청구항 구성 내용","similarity":92,"status":"matched","difference":""}},{{"claim":"청구항 1","symbol":"(B)","feature":"청구항 구성 내용","similarity":0,"status":"below_threshold","difference":"확인 범위에서 대응 내용 없음"}}]}}
[/ARIA_COMPONENT_ANALYSIS_V1]

## 문헌 매핑 블록

보고서 맨 마지막에 한 번만 출력한다. 번호를 부여한 모든 문헌에 대해 `citation_number`는 표의 번호, `attachment`는 첨부의 `ATT-02`형 자료 번호, `document_number`는 확인된 고유 문헌번호를 쓴다. UUID·해시는 쓰지 않으며 문헌번호 미확인 시 블록 전체를 생략한다. 한 줄 JSON이며 코드펜스를 쓰지 않는다.

{_MAPPING_OPEN}
{{"items":[{{"citation_number":1,"attachment":"ATT-02","document_number":"KR10-1234567"}}]}}
[/ARIA_CITATION_MAPPING_V1]
"""


def declares_blocks(master_prompt: str) -> bool:
    """이 프롬프트가 블록 규칙을 이미 자기 본문에 갖고 있는가."""
    return _COMPONENT_OPEN in master_prompt or _MAPPING_OPEN in master_prompt


def apply(master_prompt: str) -> str:
    """분석 프롬프트 뒤에 출력 규칙을 붙인다.

    이미 갖고 있으면 그대로 둔다. 규칙이 두 벌 들어가면 모델이 블록을 두 번
    출력하고, 두 파서 모두 "블록이 2개 있습니다"로 실패한다 — 규칙을 성실히
    따르는 프롬프트일수록 깨지는 셈이다. 옛 프롬프트 파일과 사용자가 직접 적어
    둔 프롬프트가 여기에 해당한다.
    """
    if declares_blocks(master_prompt):
        return master_prompt
    return master_prompt.rstrip() + "\n\n" + INSTRUCTIONS
