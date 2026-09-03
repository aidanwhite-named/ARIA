"""정규화된 내부 검색 계획.

이 계획은 모델을 부르지 않는다. 청구항 문언과 사용자 전략 본문에 **실제로 있는**
것만 뽑는다. 그래서 여기서 확인할 것은 두 가지다.

  1. 무엇을 재료로 검색에 들어갔는지가 구조화되어 남는가.
  2. 그 스키마가 사용자 프롬프트에 노출되거나 의존하지 않는가.
"""

from __future__ import annotations

from app import search_plan

CLAIM = (
    "청구항 1. 제1 센서와 제2 센서를 포함하고, 상기 제1 센서는 진동 신호를 "
    "검출하며; 제어부는 두 신호를 결합하여 이상을 판정한다."
)

STRATEGY = (
    "vibration sensor fusion 을 중시해줘. G06F 3/041 과 H04N19/00 분류도 확인해줘."
)


def test_the_plan_structures_terms_classifications_and_components() -> None:
    plan = search_plan.build(claim_text=CLAIM, strategy_body=STRATEGY)
    payload = plan.as_dict()

    assert payload["version"] == search_plan.PLAN_VERSION
    assert payload["generator"] == "aria_deterministic_v1"

    terms = {row["text"] for row in payload["terms"]}
    # 청구항에서 온 낱말은 조사를 떼어 검색어 모양으로 남긴다.
    assert {"센서", "진동", "신호", "제어부"} <= terms
    # 전략에 적힌 영문어도 재료다. 다만 출처가 구분된다.
    sources = {row["text"]: row["source"] for row in payload["terms"]}
    assert sources["센서"] == search_plan.SOURCE_CLAIM
    assert sources["vibration"] == search_plan.SOURCE_STRATEGY

    # 분류코드는 표기가 달라도 같은 모양으로 정규화한다.
    assert payload["classifications"] == ["G06F 3/041", "H04N 19/00"]

    # 검색 대상 구성은 청구항을 구분자로 나눈 어림값이다.
    assert [row["id"] for row in payload["components"]] == ["C1", "C2"]

    purposes = {row["purpose"] for row in payload["queries"]}
    assert search_plan.PURPOSE_COMBINED in purposes
    assert search_plan.PURPOSE_COMPONENT in purposes


def test_stopwords_do_not_become_search_terms() -> None:
    plan = search_plan.build(claim_text=CLAIM)
    terms = {row.text for row in plan.terms}
    for noise in ("청구항", "포함", "상기", "구성"):
        assert noise not in terms


def test_a_gap_search_plan_targets_only_the_selected_components() -> None:
    """미대응 구성 검색은 대상이 다르다. 그 사실이 계획에 남아야 한다."""
    focus = {
        "components": [
            {"id": "C002", "symbol": "(B)", "claim": "청구항 1", "feature": "신호 결합 제어"},
            {"id": "C003", "symbol": "(C)", "claim": "청구항 1", "feature": "이상 판정부"},
        ]
    }
    plan = search_plan.build(claim_text=CLAIM, search_focus=focus)

    assert [row["id"] for row in plan.as_dict()["components"]] == ["C002", "C003"]
    assert all(
        row["source"] == search_plan.SOURCE_FOCUS
        for row in plan.as_dict()["components"]
    )
    assert any("미대응" in note for note in plan.notes)


def test_the_plan_never_asks_the_model() -> None:
    """계획 단계에는 Provider 인자가 없다.

    모델에게 계획을 물으면 그 답을 검증할 방법이 없고, 검증할 수 없는 것을
    계획이라고 저장하면 "ARIA 가 관측한 사실"과 "모델이 해석한 내용"의 경계가
    무너진다. 그래서 이 함수는 순수 함수여야 한다.
    """
    import inspect

    signature = inspect.signature(search_plan.build)
    assert "provider" not in signature.parameters
    assert "model" not in signature.parameters

    # 같은 입력이면 언제나 같은 계획이다.
    first = search_plan.build(claim_text=CLAIM, strategy_body=STRATEGY).as_dict()
    second = search_plan.build(claim_text=CLAIM, strategy_body=STRATEGY).as_dict()
    assert first == second


def test_an_empty_claim_produces_an_honest_empty_plan() -> None:
    plan = search_plan.build(claim_text="   ")
    assert plan.terms == ()
    assert plan.queries == ()
    assert any("비어 있습니다" in note for note in plan.notes)


def test_the_plan_records_which_strategy_made_it() -> None:
    plan = search_plan.build(
        claim_text=CLAIM,
        strategy_body=STRATEGY,
        strategy_prompt_id="my-strategy.md",
        strategy_prompt_sha256="b" * 64,
    )
    payload = plan.as_dict()
    assert payload["strategy_prompt_id"] == "my-strategy.md"
    assert payload["strategy_prompt_sha256"] == "b" * 64
