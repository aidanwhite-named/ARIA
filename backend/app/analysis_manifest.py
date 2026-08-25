"""구성대비 결과의 기계 판독용 기록.

사람이 읽는 Markdown 보고서에서 유사도 문구를 정규식으로 다시 해석하지 않는다.
분석 프롬프트가 별도의 JSON 블록을 출력하면 ARIA 가 형식과 점수 범위를 검증하고,
검색 가능한 구성만 명시적으로 표시해 저장한다.

문헌 판독 제한은 "문헌에 구성이 없다"는 뜻이 아니므로 검색 대상으로 올리지
않는다. 반대로 유사도 80% 미만과 대응 문헌을 찾지 못한 구성은 보완 검색 대상이다.
"""

from __future__ import annotations

import json
import re

CAPABILITY = "claim_component_analysis_v1"
MANIFEST_VERSION = 1
DEFAULT_THRESHOLD = 80

STATUS_MATCHED = "matched"
STATUS_BELOW_THRESHOLD = "below_threshold"
STATUS_NOT_FOUND = "not_found"
STATUS_UNREADABLE = "unreadable"
STATUSES = frozenset(
    {
        STATUS_MATCHED,
        STATUS_BELOW_THRESHOLD,
        STATUS_NOT_FOUND,
        STATUS_UNREADABLE,
    }
)

_OPEN = "[ARIA_COMPONENT_ANALYSIS_V1]"
_CLOSE = "[/ARIA_COMPONENT_ANALYSIS_V1]"
_BLOCK = re.compile(
    r"(?:```[\w-]*\s*\n)?"
    + re.escape(_OPEN)
    + r"\s*(?P<payload>.*?)\s*"
    + re.escape(_CLOSE)
    + r"(?:\s*\n```)?",
    re.DOTALL,
)

_MAX_ITEMS = 300
_MAX_TEXT = 4000


class ComponentAnalysisError(Exception):
    """블록이 없거나 형식·값이 구성대비 계약과 맞지 않는다."""


def _text(value: object, limit: int = _MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def strip_block(text: str) -> str:
    """사용자 보고서에서 기계용 JSON 블록을 제거한다."""
    stripped = _BLOCK.sub("", text).rstrip()
    return stripped + ("\n" if text.endswith("\n") else "")


def parse(text: str, *, threshold: int = DEFAULT_THRESHOLD) -> dict:
    """구성별 결과 블록을 검증하고 검색용 스냅샷을 만든다."""
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise ComponentAnalysisError("유사도 기준값은 정수여야 합니다.")
    if threshold < 1 or threshold > 100:
        raise ComponentAnalysisError("유사도 기준값은 1~100 사이여야 합니다.")

    matches = _BLOCK.findall(text)
    if not matches:
        raise ComponentAnalysisError("보고서에서 구성별 분석 블록을 찾지 못했습니다.")
    if len(matches) > 1:
        raise ComponentAnalysisError(
            f"구성별 분석 블록이 {len(matches)}개 있습니다. 하나만 있어야 합니다."
        )

    try:
        payload = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise ComponentAnalysisError(
            f"구성별 분석 블록이 JSON 이 아닙니다: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise ComponentAnalysisError("구성별 분석 블록은 객체여야 합니다.")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ComponentAnalysisError("구성별 분석 블록에 items 배열이 없습니다.")
    if len(raw_items) > _MAX_ITEMS:
        raise ComponentAnalysisError(
            f"구성별 분석 항목이 너무 많습니다({len(raw_items)}개)."
        )

    items: list[dict] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ComponentAnalysisError(f"구성 {index} 항목이 객체가 아닙니다.")

        claim = _text(raw.get("claim"), 200)
        symbol = _text(raw.get("symbol"), 100)
        feature = _text(raw.get("feature"))
        difference = _text(raw.get("difference"))
        status = _text(raw.get("status"), 40).lower()
        if not claim:
            raise ComponentAnalysisError(f"구성 {index}의 청구항 표시가 비어 있습니다.")
        if not symbol:
            raise ComponentAnalysisError(f"구성 {index}의 구성 기호가 비어 있습니다.")
        if not feature:
            raise ComponentAnalysisError(f"구성 {index}의 구성 내용이 비어 있습니다.")
        if status not in STATUSES:
            raise ComponentAnalysisError(
                f"구성 {index}의 상태를 알 수 없습니다: {status!r}"
            )

        similarity = raw.get("similarity")
        if similarity is not None:
            if (
                isinstance(similarity, bool)
                or not isinstance(similarity, int)
                or similarity < 0
                or similarity > 100
            ):
                raise ComponentAnalysisError(
                    f"구성 {index}의 유사도는 0~100 정수 또는 null 이어야 합니다."
                )

        if status in {STATUS_NOT_FOUND, STATUS_UNREADABLE} and similarity is not None:
            raise ComponentAnalysisError(
                f"구성 {index}의 {status} 상태에는 유사도를 부여하지 않아야 합니다."
            )
        if status == STATUS_BELOW_THRESHOLD and (
            similarity is None or similarity >= threshold
        ):
            raise ComponentAnalysisError(
                f"구성 {index}의 below_threshold 상태와 유사도가 맞지 않습니다."
            )
        if status == STATUS_MATCHED and (
            similarity is None or similarity < threshold
        ):
            raise ComponentAnalysisError(
                f"구성 {index}의 matched 상태와 유사도가 맞지 않습니다."
            )

        search_eligible = status == STATUS_NOT_FOUND or (
            similarity is not None and similarity < threshold
        )
        items.append(
            {
                # 모델이 긴 식별자를 옮겨 적게 하지 않고 저장 순서로 짧게 붙인다.
                "id": f"C{index:03d}",
                "claim": claim,
                "symbol": symbol,
                "feature": feature,
                "similarity": similarity,
                "status": status,
                "difference": difference,
                "search_eligible": search_eligible,
            }
        )

    return {
        "version": MANIFEST_VERSION,
        "threshold": threshold,
        "items": items,
    }
