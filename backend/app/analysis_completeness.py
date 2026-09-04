"""분석 완전성 점검.

프로세스가 정상 종료했다는 것, 기계 판독 블록이 파싱됐다는 것, 그리고 분석이
선언한 범위를 다 덮었다는 것은 서로 다른 세 가지다. 지금까지 ARIA 는 앞의 둘만
확인했고, 셋째는 아무도 보지 않았다.

실측(job d39dc2cc):

  - 로컬 검색은 청구항 12 의 구성을 6개(전제부, A~E)로 선언했다.
  - 검색은 5라운드에서 예산이 마르며 status "partial", 미처리 요청 20건을 남겼다.
  - 최종 보고서의 구성 목록에는 A~E 5개만 있었다. **전제부가 사라졌다.**
  - 그런데 프로세스는 SUCCESS 였고, 두 블록도 문제없이 파싱됐다.

즉 "성공한 실행"이라는 표시만 보고는 무엇이 빠졌는지 알 수 없었다. 이 모듈은
그 셋을 갈라서 기록하고 보고서 끝에 사실만 적는다.

여기서 하지 않는 것:

  - 점수를 바꾸지 않는다. 검색이 partial 이라는 이유로 유사도를 깎지 않는다.
    검색 예산과 개시 여부는 다른 문제이고, 그 둘을 코드에서 이어 붙이면 ARIA 가
    기술적 판단을 만들어 내는 것이 된다.
  - 판정(status)을 바꾸지 않는다. 실행의 성공/실패는 evaluator 가 정한다.
  - 빠진 구성을 대신 채워 넣지 않는다. 빠졌다는 사실만 적는다.
"""

from __future__ import annotations

import re
from typing import Any

_SPACES = re.compile(r"\s+")

# 검색 매니페스트가 "이 범위는 다 못 봤다"고 표시하는 값.
_LIMITED = {"limited", "partial", "insufficient", "coverage_insufficient"}


def _key(text: str) -> str:
    return _SPACES.sub(" ", str(text or "")).strip().casefold()


def _declared(retrieval: dict | None) -> list[dict]:
    if not isinstance(retrieval, dict):
        return []
    components = retrieval.get("components")
    return [row for row in components if isinstance(row, dict)] if isinstance(components, list) else []


def _reported(analysis: dict | None) -> list[dict]:
    if not isinstance(analysis, dict):
        return []
    items = analysis.get("items")
    return [row for row in items if isinstance(row, dict)] if isinstance(items, list) else []


def check(
    *,
    retrieval_manifest: dict | None,
    analysis_manifest: dict | None,
    analysis_error: str | None = None,
    process_succeeded: bool = True,
) -> dict[str, Any]:
    """세 층위를 나눠서 점검한 결과를 돌려준다."""
    declared = _declared(retrieval_manifest)
    reported = _reported(analysis_manifest)

    reported_keys = {
        _key(f"{row.get('claim', '')} {row.get('symbol', '')}") for row in reported
    }
    reported_keys.discard("")

    missing: list[str] = []
    comparable = False
    if declared and reported_keys:
        matched = 0
        for row in declared:
            label = str(row.get("label") or row.get("id") or "").strip()
            if not label:
                continue
            if _key(label) in reported_keys:
                matched += 1
            else:
                missing.append(label)
        # 하나도 안 맞으면 이름 붙이는 방식이 서로 다른 것이다. 그때는 전부
        # 누락으로 적지 않는다 — 틀린 경고가 진짜 누락을 묻어 버린다.
        comparable = matched > 0
        if not comparable:
            missing = []

    # 직접 근거 없이 대응으로 평가된 구성. 점수는 그대로 두고 사실만 적는다.
    inferred: list[str] = []
    for row in reported:
        if str(row.get("status") or "").casefold() != "matched":
            continue
        if str(row.get("basis") or "").casefold() != "inferred":
            continue
        label = _SPACES.sub(" ", f"{row.get('claim', '')} {row.get('symbol', '')}").strip()
        if label:
            inferred.append(label)

    scope: dict[str, Any] = {}
    limited_components: list[str] = []
    if isinstance(retrieval_manifest, dict):
        status = str(retrieval_manifest.get("status") or "")
        pending = retrieval_manifest.get("deferred_pending")
        pending_count = len(pending) if isinstance(pending, list) else 0
        rounds = retrieval_manifest.get("rounds")
        scope = {
            "status": status,
            "rounds": len(rounds) if isinstance(rounds, list) else 0,
            "max_rounds": int(
                (retrieval_manifest.get("budget") or {}).get("max_rounds") or 0
            )
            if isinstance(retrieval_manifest.get("budget"), dict)
            else 0,
            "budget_exhausted": bool(retrieval_manifest.get("budget_exhausted")),
            "pending_actions": pending_count,
            "pages_read": retrieval_manifest.get("pages_read"),
        }
        for row in declared:
            completeness = str(row.get("search_completeness") or "").casefold()
            if completeness in _LIMITED:
                label = str(row.get("label") or row.get("id") or "").strip()
                if label:
                    limited_components.append(label)
        scope["limited_components"] = limited_components
        scope["limited"] = bool(
            status.casefold() in _LIMITED
            or scope["budget_exhausted"]
            or pending_count
            or limited_components
        )

    return {
        # 프로세스가 끝까지 갔는가. evaluator 의 판정이다.
        "process_succeeded": bool(process_succeeded),
        # 기계 판독 블록을 읽었는가. 형식의 문제이지 내용의 문제가 아니다.
        "manifest_parsed": bool(reported) and not analysis_error,
        "manifest_error": analysis_error or None,
        "declared_components": len(declared),
        "reported_components": len(reported),
        "comparable": comparable,
        "missing_components": missing,
        "inferred_components": inferred,
        "scope": scope,
        "complete": bool(comparable and not missing) and not scope.get("limited", False),
    }


def render(result: dict[str, Any]) -> str:
    """보고서 끝에 붙일 절. 적을 것이 없으면 빈 문자열."""
    if not result:
        return ""
    lines: list[str] = []
    scope = result.get("scope") or {}

    if result.get("missing_components"):
        lines.append(
            "- 로컬 검색이 선언한 구성 "
            f"{result.get('declared_components')}개 중 "
            f"{len(result['missing_components'])}개가 최종 구성 목록에 없습니다: "
            + ", ".join(result["missing_components"])
            + ". 이 구성에 대한 대응 판단은 이 보고서에 없습니다."
        )
    elif result.get("declared_components") and not result.get("comparable"):
        lines.append(
            f"- 로컬 검색이 선언한 구성 {result.get('declared_components')}개와 "
            f"최종 구성 목록 {result.get('reported_components')}개의 이름이 서로 달라 "
            "구성 단위 대조를 하지 못했습니다."
        )

    if scope.get("limited"):
        detail = []
        rounds, max_rounds = scope.get("rounds"), scope.get("max_rounds")
        if rounds:
            detail.append(f"{rounds}라운드" + (f"/상한 {max_rounds}" if max_rounds else ""))
        if scope.get("pending_actions"):
            detail.append(f"미처리 검색 요청 {scope['pending_actions']}건")
        if scope.get("budget_exhausted"):
            detail.append("검색 예산 소진")
        limited_names = scope.get("limited_components") or []
        if limited_names:
            # 전부 제한이면 이름을 나열해도 알려 주는 것이 없다.
            detail.append(
                "모든 구성에서 검색 범위 제한"
                if len(limited_names) >= (result.get("declared_components") or 0) > 0
                else "검색 범위가 제한된 구성: " + ", ".join(limited_names)
            )
        lines.append(
            "- 로컬 검색이 선언 범위를 다 훑지 못한 상태로 끝났습니다"
            + (f" ({', '.join(detail)})" if detail else "")
            + ". 유사도와 대응 판단은 **실제로 확인한 범위** 기준이며, "
            "확인하지 못한 범위에 대한 판단이 아닙니다."
        )

    if result.get("inferred_components"):
        lines.append(
            "- 다음 구성은 핵심 한정 중 일부를 문헌 문언이 아니라 기재로부터의 "
            "추론으로 메운 채 대응으로 평가되었습니다: "
            + ", ".join(result["inferred_components"])
            + ". 해당 한정의 직접 근거는 본문의 차이점 항목에서 확인하십시오."
        )

    if not result.get("manifest_parsed"):
        lines.append(
            "- 구성별 분석 블록을 읽지 못해 구성 단위 대조를 하지 못했습니다"
            + (f": {result['manifest_error']}" if result.get("manifest_error") else "")
            + "."
        )

    if not lines:
        return ""

    return (
        "\n\n---\n\n## 분석 완전성 점검\n\n"
        "ARIA 가 실행 기록과 보고서를 대조해 자동으로 적은 절입니다. "
        "실행이 정상 종료했다는 것과 분석이 선언한 범위를 다 덮었다는 것은 다릅니다. "
        "아래 항목은 유사도 수치나 대응 판정을 바꾸지 않습니다.\n\n"
        + "\n".join(lines)
        + "\n"
    )
