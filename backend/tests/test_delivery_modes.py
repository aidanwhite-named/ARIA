"""전달 방식 판정과 근거 패키지의 페이지 확장.

폭은 둘이다.

  full_inline     원문 전체
  local_retrieval 로컬 색인 후 검색으로 확인한 구간 + 그 페이지 전문과 앞뒤 페이지

한때 그 사이에 「페이지 단위」를 독립 모드로 두었는데, 같은 검색을 돌리고 담는
단위만 다른 것이라 **전달 방식이 아니라 근거 패키지의 확장 방식**이 맞았다.
지금은 retrieval 안에서 예산이 허락하는 만큼 페이지를 담고, 모자라면 중요도가
낮은 주변 페이지부터 줄인다.

그리고 세 축을 섞지 않는다.

  - agy 의 180,000 bytes 는 그 **CLI** 가 자르는 지점이다. 사용자가 못 끈다.
  - codex/claude 의 한계는 **모델** 컨텍스트 토큰이다. 모르면 보수적 대체값.
  - 사건 규모 기준은 조정 가능한 품질 정책이고 기본은 꺼짐이다.
"""

from __future__ import annotations

import pytest

from app import job_assembly
from app.enums import (
    AttachmentRole,
    DeliveryMode,
    DeliveryPlan,
    ExtractionMethod,
    RetrievalMode,
)
from app.ingestion.service import IngestedFile
from app.job_assembly import DeliveryScale, decide_delivery
from app.providers import model_limits
from app.providers.agy_cli import AgyCliProvider
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.codex_cli import CodexCliProvider
from app.retrieval import pages as pages_module
from app.retrieval.agent import RetrievalBudget

from .pdf_fixture import build_korean_pdf

AGY_LIMIT = AgyCliProvider.max_input_bytes


def _budget(context: int, reserve: int = 32_000, source=model_limits.SOURCE_CONFIGURED):
    return model_limits.TokenBudget(
        context_tokens=context, reserve_tokens=reserve, source=source, model="m"
    )


def _decide(**kwargs):
    base = {
        "retrieval_mode": RetrievalMode.AUTO,
        "full_inline_bytes": 0,
        "provider_byte_budget": None,
    }
    base.update(kwargs)
    return decide_delivery(**base)


# ------------------------------------------------ agy: 전송 하드 한도만


def test_agy_within_the_byte_limit_sends_everything() -> None:
    decision = _decide(full_inline_bytes=AGY_LIMIT, provider_byte_budget=AGY_LIMIT)
    assert decision.plan == DeliveryPlan.FULL_INLINE
    assert f"{AGY_LIMIT:,} bytes 안에" in decision.reason


def test_agy_over_the_byte_limit_goes_straight_to_retrieval() -> None:
    """중간 단계가 없다. 한 바이트만 넘어도 로컬 검색이다."""
    decision = _decide(full_inline_bytes=AGY_LIMIT + 1, provider_byte_budget=AGY_LIMIT)
    assert decision.plan == DeliveryPlan.LOCAL_RETRIEVAL
    assert f"{AGY_LIMIT:,} bytes 를 넘습니다" in decision.reason
    assert "종료 코드 0" in decision.reason


def test_agy_forced_full_one_byte_over_is_left_for_the_final_gate() -> None:
    """같은 180,001 bytes라도 full 고정만 INPUT_TOO_LARGE 계약이다.

    auto는 위 테스트처럼 검색으로 전환한다. full은 선택을 유지하되 실행 직전
    provider 바이트 게이트가 호출 없이 막는다.
    """

    decision = _decide(
        retrieval_mode=RetrievalMode.FULL,
        full_inline_bytes=AGY_LIMIT + 1,
        provider_byte_budget=AGY_LIMIT,
    )
    assert decision.plan == DeliveryPlan.FULL_INLINE
    assert "INPUT_TOO_LARGE" in decision.reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"full_inline_tokens": 10_000_000, "token_budget": _budget(1_000)},
        {
            "scale": DeliveryScale(documents=50),
            "scale_limits": DeliveryScale(documents=5),
        },
    ],
)
def test_agy_ignores_the_other_two_axes(kwargs) -> None:
    """하드 한도를 선언한 Provider 에는 모델 예산도 규모 기준도 걸지 않는다.

    겹쳐 걸면 한도 안에 들어오는 입력까지 조용히 좁아지고, 판정 사유가 "이
    Provider 는 전송 하드 한도를 선언하지 않으므로"라는 거짓을 담게 된다.
    """
    decision = _decide(full_inline_bytes=1_000, provider_byte_budget=AGY_LIMIT, **kwargs)
    assert decision.plan == DeliveryPlan.FULL_INLINE
    assert decision.scale_downgraded is False
    assert "전송 하드 한도를 선언하지 않" not in decision.reason
    assert "품질 정책" not in decision.reason


def test_only_agy_declares_a_transport_cap() -> None:
    assert AgyCliProvider.max_input_bytes == 180_000
    assert CodexCliProvider.max_input_bytes is None
    assert ClaudeCliProvider.max_input_bytes is None


# ------------------------------- codex / claude: 모델 컨텍스트 토큰 예산


def test_input_budget_subtracts_the_output_reserve() -> None:
    assert _budget(200_000, reserve=32_000).input_tokens == 168_000
    # 예약이 컨텍스트보다 크면 0 이다. 음수 예산으로 판정하지 않는다.
    assert _budget(10_000, reserve=50_000).input_tokens == 0


def test_within_the_token_budget_sends_everything() -> None:
    decision = _decide(full_inline_tokens=100_000, token_budget=_budget(200_000))
    assert decision.plan == DeliveryPlan.FULL_INLINE
    assert "168,000 토큰 안에" in decision.reason


def test_over_the_token_budget_switches_to_retrieval() -> None:
    decision = _decide(full_inline_tokens=200_000, token_budget=_budget(200_000))
    assert decision.plan == DeliveryPlan.LOCAL_RETRIEVAL
    assert "168,000 토큰을 넘습니다" in decision.reason


def test_zero_token_budget_never_falls_through_to_full_inline() -> None:
    """예약 >= 컨텍스트인 잘못된 설정이 안전장치를 꺼서는 안 된다."""

    decision = _decide(
        full_inline_bytes=5_000_000,
        full_inline_tokens=2_500_000,
        token_budget=_budget(32_000, reserve=32_000),
    )
    assert decision.plan == DeliveryPlan.LOCAL_RETRIEVAL
    assert "입력 예산이 0" in decision.reason


def test_unknown_model_uses_the_conservative_fallback_and_says_so() -> None:
    """모델 한도를 모르면 추측하지 않는다. 대체값을 쓰고 그 사실을 적는다."""
    budget = model_limits.token_budget(
        provider_id="codex",
        model="처음 보는 모델",
        overrides={},
        reserve_tokens=32_000,
        fallback_context_tokens=128_000,
    )
    assert budget.source == model_limits.SOURCE_FALLBACK
    assert budget.is_estimated is True
    assert budget.input_tokens == 96_000

    decision = _decide(full_inline_tokens=200_000, token_budget=budget)
    assert decision.plan == DeliveryPlan.LOCAL_RETRIEVAL
    assert "확인하지 못해 보수적 대체값" in decision.reason
    assert "모델 컨텍스트 한도" in decision.reason


def test_configured_limit_wins_over_the_fallback() -> None:
    budget = model_limits.token_budget(
        provider_id="codex",
        model="gpt-5-codex",
        overrides={"codex:gpt-5-codex": 400_000},
        reserve_tokens=32_000,
        fallback_context_tokens=128_000,
    )
    assert budget.source == model_limits.SOURCE_CONFIGURED
    assert budget.is_estimated is False
    assert budget.context_tokens == 400_000


def test_provider_scoped_key_beats_the_bare_model_key() -> None:
    """같은 모델 이름을 여러 Provider 가 노출할 수 있다(agy 가 claude 계열을 준다)."""
    overrides = {"claude-sonnet-4-6": 200_000, "agy:claude-sonnet-4-6": 100_000}
    agy = model_limits.token_budget(
        provider_id="agy",
        model="claude-sonnet-4-6",
        overrides=overrides,
        reserve_tokens=0,
        fallback_context_tokens=1,
    )
    claude = model_limits.token_budget(
        provider_id="claude",
        model="claude-sonnet-4-6",
        overrides=overrides,
        reserve_tokens=0,
        fallback_context_tokens=1,
    )
    assert agy.context_tokens == 100_000
    assert claude.context_tokens == 200_000


@pytest.mark.parametrize("value", [0, -1, "많이", None, True])
def test_bad_override_values_fall_back_instead_of_crashing(value) -> None:
    budget = model_limits.token_budget(
        provider_id="codex",
        model="m",
        overrides={"m": value},
        reserve_tokens=0,
        fallback_context_tokens=999,
    )
    assert budget.context_tokens == 999
    assert budget.source == model_limits.SOURCE_FALLBACK


def test_token_estimate_is_deliberately_high() -> None:
    """적게 세면 「들어간다」고 판정한 입력이 모델에 거절된다. 많게 센다."""
    korean = "가" * 1_000  # UTF-8 3,000 bytes
    assert model_limits.estimate_tokens(korean) == 1_500
    assert model_limits.estimate_tokens("") == 0
    assert model_limits.estimate_tokens("a", "b") == 1


def test_no_token_budget_means_no_narrowing() -> None:
    """예산을 정하지 못했으면 좁히지 않는다. 모르는 것을 이유로 좁히지 않는다."""
    decision = _decide(full_inline_bytes=9_000_000, token_budget=None)
    assert decision.plan == DeliveryPlan.FULL_INLINE
    assert "정해지지 않아" in decision.reason


# --------------------------------------------- 사건 규모 품질 기준


def test_scale_rules_are_off_unless_configured() -> None:
    decision = _decide(
        full_inline_tokens=1,
        token_budget=_budget(200_000),
        scale=DeliveryScale(documents=50, pages=5_000, claim_elements=90),
        scale_limits=DeliveryScale(),
    )
    assert decision.plan == DeliveryPlan.FULL_INLINE
    assert decision.scale_downgraded is False


@pytest.mark.parametrize(
    "scale,label",
    [
        (DeliveryScale(documents=8), "문헌 8건"),
        (DeliveryScale(pages=400), "총 400페이지"),
        (DeliveryScale(claim_elements=20), "청구항 구성 20개"),
    ],
)
def test_scale_rule_switches_to_retrieval_when_enabled(scale, label) -> None:
    decision = _decide(
        full_inline_tokens=1,
        token_budget=_budget(200_000),
        scale=scale,
        scale_limits=DeliveryScale(documents=5, pages=300, claim_elements=15),
    )
    assert decision.plan == DeliveryPlan.LOCAL_RETRIEVAL
    assert decision.scale_downgraded is True
    assert label in decision.reason
    assert "품질 정책" in decision.reason


# ------------------------------------------------------- 고정 모드와 옛 값


@pytest.mark.parametrize(
    "mode,expected",
    [
        (RetrievalMode.FULL, DeliveryPlan.FULL_INLINE),
        (RetrievalMode.RETRIEVAL, DeliveryPlan.LOCAL_RETRIEVAL),
    ],
)
def test_explicit_modes_ignore_size(mode, expected) -> None:
    decision = _decide(
        retrieval_mode=mode,
        full_inline_bytes=9_000_000,
        provider_byte_budget=AGY_LIMIT,
        full_inline_tokens=9_000_000,
        token_budget=_budget(1_000),
    )
    assert decision.plan == expected


def test_retired_focused_mode_is_read_as_retrieval() -> None:
    """폐기한 값이 저장돼 있어도 화면이 열리고, 좁혀 둔 뜻이 유지된다.

    auto 로 되돌리면 사용자가 명시적으로 좁혀 두었던 설정이 조용히 넓어진다.
    """
    from app import settings_service

    assert RetrievalMode.coerce("focused") is RetrievalMode.RETRIEVAL
    assert settings_service._coerce("retrieval_mode", "focused") == "retrieval"
    assert _decide(retrieval_mode="focused").plan == DeliveryPlan.LOCAL_RETRIEVAL


def test_retired_focused_plan_is_read_as_retrieval() -> None:
    """옛 실행 기록의 focused_pages 를 전체 인라인으로 읽으면 거짓이 된다."""
    assert DeliveryPlan.coerce("focused_pages") is DeliveryPlan.LOCAL_RETRIEVAL
    assert DeliveryPlan.coerce("") is DeliveryPlan.FULL_INLINE
    assert DeliveryPlan.coerce(None) is DeliveryPlan.FULL_INLINE
    assert DeliveryPlan.coerce("local_retrieval") is DeliveryPlan.LOCAL_RETRIEVAL


def test_retired_threshold_setting_is_gone() -> None:
    """retrieval_auto_threshold_bytes 는 폐기했다. 스키마 어디에도 없어야 한다."""
    from app import settings_service
    from app.config import DEFAULTS

    key = "retrieval_auto_threshold_bytes"
    assert key not in DEFAULTS
    assert key not in settings_service.EDITABLE_KEYS
    assert key not in settings_service._INT_KEYS
    assert key not in settings_service._LIMITS


def test_settings_schema_is_internally_consistent() -> None:
    """바꿀 수 있다고 선언한 키는 기본값이 있어야 하고, 정수 키는 한계가 있어야 한다."""
    from app import settings_service
    from app.config import DEFAULTS

    missing = set(settings_service.EDITABLE_KEYS) - set(DEFAULTS)
    assert missing == set(), f"DEFAULTS 에 없는 편집 가능 키: {sorted(missing)}"
    assert set(settings_service._INT_KEYS) <= set(settings_service._LIMITS)


def test_delivery_policy_reads_settings_once() -> None:
    policy = job_assembly.delivery_policy_from_settings(
        {
            "model_context_tokens": {"codex:x": 1},
            "model_output_reserve_tokens": 111,
            "unknown_model_context_tokens": 222,
            "delivery_scale_documents": 3,
            "delivery_scale_pages": 4,
            "delivery_scale_claim_elements": 5,
        }
    )
    assert policy["model_context_overrides"] == {"codex:x": 1}
    assert policy["model_output_reserve_tokens"] == 111
    assert policy["unknown_model_context_tokens"] == 222
    assert policy["delivery_scale_limits"] == DeliveryScale(
        documents=3, pages=4, claim_elements=5
    )
    junk = job_assembly.delivery_policy_from_settings(
        {"model_context_tokens": "표가 아님", "delivery_scale_pages": "?"}
    )
    assert junk["model_context_overrides"] == {}
    assert junk["delivery_scale_limits"] == DeliveryScale()


# ------------------------------------------------- 조립 경로와 전달 기록


PAGES = [
    f"[{i:04d}] 제{i} 실시예에서 압력센서와 제어부가 하우징에 결합되어 신호를 "
    f"처리한다. 도면부호 {100 + i} 로 표시된다.\n- {i} -"
    for i in range(1, 41)
]


def _attachment(tmp_path, name="citation.pdf", pages=None, role=AttachmentRole.CITATION):
    pages = pages or PAGES
    data = build_korean_pdf(pages)
    target = tmp_path / "input" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    normalized = target.with_suffix(".txt")
    normalized.write_text("\n".join(pages), encoding="utf-8")
    return IngestedFile(
        attachment_id=name.replace(".pdf", ""),
        original_filename=name,
        internal_filename=name,
        mime_type="application/pdf",
        size_bytes=len(data),
        sha256=f"sha-{name}",
        required=True,
        stored_path=str(target),
        role=role,
        page_count=len(pages),
        char_count=sum(len(p) for p in pages),
        extraction_method=ExtractionMethod.PDF_TEXT_LAYER,
        delivery_mode=DeliveryMode.INLINE_CONTEXT,
        read_ok=True,
        normalized_text_path=str(normalized),
    )


def _assemble(tmp_path, **kwargs):
    from app.enums import JobKind

    base = dict(
        job_kind=JobKind.PATENT_ANALYSIS,
        master_prompt="마스터 프롬프트",
        attachments=kwargs["attachments"] if "attachments" in kwargs else [_attachment(tmp_path)],
        runtime_context="런타임 컨텍스트",
        runtime_context_enabled=True,
        max_chars=None,
        claim_text="청구항 1. 압력센서를 포함하는 장치.",
    )
    base.update(kwargs)
    return job_assembly.assemble_job(**base)


def test_manifest_keeps_the_two_limit_axes_apart(tmp_path) -> None:
    provider = AgyCliProvider()
    assembly = _assemble(
        tmp_path,
        provider_byte_budget=provider.max_input_bytes,
        provider_measure=provider.payload_bytes,
        provider_id="agy",
        model="gemini",
    )
    record = assembly.delivery_manifest(provider)
    assert record["provider"] == "agy"
    assert record["provider_byte_limit"] == provider.max_input_bytes
    # 하드 한도가 있는 Provider 에는 모델 예산을 계산하지 않는다.
    assert record["model_token_budget"] is None
    assert record["selection_reason"] == assembly.selection_reason
    assert record["actual_payload_bytes"] == provider.payload_bytes(
        assembly.representative.system_prompt, assembly.representative.user_message
    )


def test_manifest_records_the_token_budget_for_a_capless_provider(tmp_path) -> None:
    assembly = _assemble(
        tmp_path,
        provider_byte_budget=None,
        provider_id="codex",
        model="gpt-5-codex",
        model_context_overrides={"codex:gpt-5-codex": 400_000},
        model_output_reserve_tokens=32_000,
        unknown_model_context_tokens=128_000,
    )
    record = assembly.delivery_manifest(CodexCliProvider())
    assert record["provider_byte_limit"] is None
    budget = record["model_token_budget"]
    assert budget is not None
    assert budget["context_tokens"] == 400_000
    assert budget["input_tokens"] == 368_000
    assert budget["source"] == model_limits.SOURCE_CONFIGURED
    assert record["full_inline_tokens"] > 0


def test_full_inline_keeps_the_document_whole(tmp_path) -> None:
    assembly = _assemble(tmp_path, retrieval_mode=RetrievalMode.FULL)
    body = assembly.representative.user_message
    assert "제1 실시예" in body
    assert "제40 실시예" in body


def test_retrieval_does_not_inline_the_citation_body(tmp_path) -> None:
    """좁힌 경로에서는 본문을 자르는 것이 아니라 아예 넣지 않는다."""
    assembly = _assemble(tmp_path, retrieval_mode=RetrievalMode.RETRIEVAL)
    body = assembly.representative.user_message
    assert "제40 실시예" not in body
    assert "로컬 색인" in body


@pytest.mark.parametrize("claim_size", [463, 5_000, 20_000])
def test_retrieval_budget_subtracts_actual_overhead(tmp_path, claim_size) -> None:
    from app import retrieval
    provider = AgyCliProvider()
    options = dict(
        attachments=[_attachment(tmp_path)],
        retrieval_mode=RetrievalMode.RETRIEVAL,
        retrieval_budget=RetrievalBudget(max_evidence_chars=100_000),
        provider_byte_budget=provider.max_input_bytes,
        provider_measure=provider.payload_bytes,
        claim_text="한" * claim_size,
        followup_instruction="추가 지시" * 200,
    )
    assembly = _assemble(tmp_path, **options)
    budget = assembly.evidence_budget
    assert budget.max_evidence_chars == 100_000
    assert assembly.lane_bytes(provider)["single"] <= provider.max_input_bytes
    empty = _assemble(tmp_path, **{
        **options, "evidence_bundle": {retrieval.PLACEHOLDER_KEY: "a"},
    })
    assert budget.max_evidence_bytes == provider.max_input_bytes - (empty.lane_bytes(provider)["single"] - 1)
    # ASCII 는 같은 바이트 안에서 54,000자보다 많이 담을 수 있다.
    actual = _assemble(tmp_path, **{
        **options, "evidence_bundle": {retrieval.PLACEHOLDER_KEY: "a" * min(100_000, budget.max_evidence_bytes)},
    })
    assert actual.lane_bytes(provider)["single"] <= provider.max_input_bytes


def test_retrieval_budget_respects_model_tokens_and_transport_wrapping(tmp_path) -> None:
    cap = 50_000
    provider = AgyCliProvider()
    def wrapped(system, user):
        return provider.payload_bytes(system, user) + 700
    wrapped_result = _assemble(
        tmp_path, retrieval_mode=RetrievalMode.RETRIEVAL,
        provider_byte_budget=cap, provider_measure=wrapped,
    )
    lane = wrapped_result.representative
    assert wrapped(lane.system_prompt, lane.user_message) == cap
    model_result = _assemble(
        tmp_path, retrieval_mode=RetrievalMode.RETRIEVAL,
        provider_id="codex", model="small",
        model_context_overrides={"codex:small": 25_000},
        model_output_reserve_tokens=5_000, unknown_model_context_tokens=128_000,
    )
    lane = model_result.representative
    assert model_limits.estimate_tokens(lane.system_prompt, lane.user_message) == 20_000


def test_claims_alone_exceed_transport_limit_before_retrieval(tmp_path) -> None:
    with pytest.raises(job_assembly.TransportInputTooLarge):
        _assemble(
            tmp_path, retrieval_mode=RetrievalMode.RETRIEVAL,
            provider_byte_budget=180_000, claim_text="한" * 65_000,
        )


def test_final_retrieval_prompt_is_checked_against_the_model_budget(tmp_path) -> None:
    """근거 패키지를 넣은 최종 조립본도 Provider 호출 전에 다시 잰다."""

    with pytest.raises(job_assembly.ModelInputTooLarge, match="최종 조립 입력"):
        _assemble(
            tmp_path,
            retrieval_mode=RetrievalMode.RETRIEVAL,
            retrieval_budget=RetrievalBudget(max_evidence_chars=2_000),
            provider_id="codex",
            model="tiny-model",
            model_context_overrides={"codex:tiny-model": 1_100},
            model_output_reserve_tokens=1_000,
            unknown_model_context_tokens=128_000,
        )


def test_zero_model_input_budget_fails_before_provider_call(tmp_path) -> None:
    with pytest.raises(job_assembly.ModelInputTooLarge, match="입력 예산이 0"):
        _assemble(
            tmp_path,
            retrieval_mode=RetrievalMode.RETRIEVAL,
            provider_id="codex",
            model="broken-budget",
            model_context_overrides={"codex:broken-budget": 32_000},
            model_output_reserve_tokens=32_000,
            unknown_model_context_tokens=128_000,
        )


# --------------------------------------------------- 근거 패키지 페이지 확장


class _FakeIndex:
    def __init__(self, pages: dict[int, str]) -> None:
        self._pages = pages
        self.page_count = max(pages) if pages else 0

    def page_rows(self, page_number: int):
        text = self._pages.get(page_number, "")
        return [type("Row", (), {"text": text})()] if text else []

    def page_status(self, page_number: int):
        return {
            "pdf_page": page_number,
            "printed_page": str(page_number),
            "status": "ok",
            "extraction_method": "pdf_text_layer",
        }


class _FakeDocument:
    def __init__(self, pages: dict[int, str], alias="ATT-01", attachment_id="doc"):
        self.alias = alias
        self.attachment_id = attachment_id
        self.filename = "citation.pdf"
        self.index = _FakeIndex(pages)


def _pages(finding_pages, page_count=10, neighbours=1, char_budget=1_000_000):
    body = {
        i: " ".join([f"{i}페이지 본문입니다."] * 10) for i in range(1, page_count + 1)
    }
    return pages_module.build(
        corpus=[_FakeDocument(body)],
        finding_pages={"doc": set(finding_pages)},
        neighbours=neighbours,
        char_budget=char_budget,
    )


def test_pages_include_the_finding_page_and_its_neighbours() -> None:
    built = _pages([5])
    assert built[0]["candidate_pages"] == [5]
    assert built[0]["included_pages"] == [4, 5, 6]
    marks = {page["pdf_page"]: page["candidate"] for page in built[0]["pages"]}
    assert marks == {4: False, 5: True, 6: False}


@pytest.mark.parametrize("neighbours,expected", [(0, [5]), (2, [3, 4, 5, 6, 7])])
def test_neighbour_count_is_configurable(neighbours, expected) -> None:
    assert _pages([5], neighbours=neighbours)[0]["included_pages"] == expected


def test_pages_do_not_run_past_the_document_edges() -> None:
    assert _pages([1, 10], page_count=10)[0]["included_pages"] == [1, 2, 9, 10]


def test_documents_without_findings_get_no_pages() -> None:
    """검색과 무관한 문헌이 문맥이라는 이름으로 딸려 오면 안 된다."""
    assert _pages([]) == []


def test_unverified_pages_are_derived_from_what_is_included() -> None:
    built = _pages([5], page_count=10)
    assert pages_module.unverified_pages(built[0]) == [1, 2, 3, 7, 8, 9, 10]
    text = "\n".join(pages_module.render(built))
    assert "미확인 페이지" in text
    assert "1-3, 7-10" in text


def test_context_pages_are_dropped_before_finding_pages() -> None:
    """중요도가 낮은 주변 페이지부터, 후보에서 먼 것부터 줄인다."""
    built = _pages([5], neighbours=2)
    removed = []
    while True:
        gone = pages_module.drop_one(built, only_context=True)
        if gone is None:
            break
        removed.append(gone["pdf_page"])
    assert removed == [7, 3, 6, 4]
    assert [page["pdf_page"] for page in built[0]["pages"]] == [5]
    # 그다음에야 근거 페이지가 빠진다.
    assert pages_module.drop_one(built, only_context=False)["pdf_page"] == 5
    assert pages_module.drop_one(built, only_context=False) is None


def test_dropped_pages_move_into_the_unverified_list() -> None:
    built = _pages([5], neighbours=1, page_count=10)
    pages_module.drop_one(built, only_context=True)
    assert 6 in pages_module.unverified_pages(built[0])


def test_dropped_page_list_is_capped_so_it_cannot_grow_unbounded() -> None:
    """뺀 페이지 목록이 길어지면 그 목록이 다시 예산을 먹고 fit() 이 수렴을 못 한다."""
    labels = [f"ATT-01 p.{n}" for n in range(1, 40)]
    line = pages_module._dropped_line(labels)
    assert "외 31쪽" in line
    assert line.count("ATT-01 p.") == pages_module.MAX_LISTED_DROPS


def test_page_build_respects_a_rough_char_budget() -> None:
    """조립 단계에서 미리 자른다. 300페이지를 만들었다가 하나씩 빼면 O(n²) 이다."""
    built = _pages([5], neighbours=4, char_budget=100)
    assert not built or sum(len(p["text"]) for p in built[0]["pages"]) <= 100


def test_tight_budget_keeps_the_candidate_before_its_neighbours() -> None:
    """유효한 ±5 설정에서도 주변 5쪽이 근거 쪽을 밀어내면 안 된다."""

    body = {page: "x" * 20 for page in range(1, 11)}
    built = pages_module.build(
        corpus=[_FakeDocument(body)],
        finding_pages={"doc": {10}},
        neighbours=5,
        char_budget=100,
    )

    assert built[0]["candidate_pages"] == [10]
    assert built[0]["included_pages"] == [6, 7, 8, 9, 10]
    assert any(
        page["pdf_page"] == 10 and page["candidate"]
        for page in built[0]["pages"]
    )


def test_page_expansion_is_off_when_there_is_no_room() -> None:
    assert _pages([5], char_budget=0) == []


def test_oversized_pages_are_partial_and_never_claimed_as_full() -> None:
    """페이지별 한도 초과는 부분 수록하며 누락량과 미확인 상태를 남긴다."""
    skipped: list[str] = []
    built = pages_module.build(
        corpus=[_FakeDocument({page: "x" * 500 for page in range(1, 11)})],
        finding_pages={"doc": {5}},
        neighbours=1,
        char_budget=100,
        skipped=skipped,
    )
    assert built[0]["included_pages"] == [4, 5, 6]
    assert all(page["text"] == "x" * 25 for page in built[0]["pages"])
    assert all(page["truncated"] and page["omitted_chars"] == 475 for page in built[0]["pages"])
    assert 5 in pages_module.unverified_pages(built[0])
    assert skipped == []


def test_zero_budget_still_names_the_pages_it_could_not_take() -> None:
    skipped: list[str] = []
    built = pages_module.build(
        corpus=[_FakeDocument({page: "본문" for page in range(1, 11)})],
        finding_pages={"doc": {2, 9}},
        neighbours=1,
        char_budget=0,
        skipped=skipped,
    )
    assert built == []
    assert skipped == ["ATT-01 p.2", "ATT-01 p.9"]


def test_page_expansion_turned_off_by_setting_is_not_a_budget_reduction() -> None:
    """설정으로 끈 것을 「예산 때문에 뺐다」고 적으면 원인을 잘못 가리킨다."""
    skipped: list[str] = []
    pages_module.build(
        corpus=[_FakeDocument({1: "본문"})],
        finding_pages={"doc": {1}},
        neighbours=-1,
        char_budget=100_000,
        skipped=skipped,
    )
    assert skipped == []


def test_render_says_so_when_no_page_made_it_in() -> None:
    text = "\n".join(pages_module.render([], ["ATT-01 p.5"]))
    assert "한 쪽도 담지 못했습니다" in text
    assert "ATT-01 p.5" in text
    # 담은 페이지가 없으니 위쪽 「미확인 페이지」 목록을 가리키면 안 된다.
    assert "위 미확인 페이지" not in text


def test_budget_shrinks_pages_before_touching_findings() -> None:
    """예산이 모자라면 페이지 확장이 먼저 사라지고 근거 구간은 남는다.

    그리고 **구성 판정은 흔들리지 않는다.** 페이지를 뺀 것은 근거를 뺀 것이
    아니다 — 근거 구간과 그 발췌는 그대로이고 빠진 것은 앞뒤 문맥뿐이다.
    """
    from app.retrieval import evidence

    bundle = {
        "documents": [],
        "components": [
            {
                "component_id": "R001",
                "claim_component": "구성 1",
                "feature": "내용",
                "status": "matched",
                "status_label": "대응",
                "queries_used": [],
                "search_channels_used": [],
                "findings": [
                    {
                        "attachment": "ATT-01",
                        "chunk_id": "P0005-001",
                        "pdf_page": 5,
                        "extraction_status": "ok",
                        "source_text": "근거 원문",
                        "channels": [],
                    }
                ],
                "searched": [],
                "status_reasons": [],
            }
        ],
        "evidence_pages": _pages([5], neighbours=2),
        "page_reductions": [],
        "package_reductions": [],
    }
    # 페이지가 전부 들어가면 넘고, 다 빠지면 들어가는 크기로 잡는다. 그래야
    # "무엇이 먼저 빠지는가"를 실제로 확인할 수 있다.
    budget = RetrievalBudget(max_evidence_chars=1_700)
    before = len("\n".join(pages_module.render(bundle["evidence_pages"])))
    assert before > 0
    text = evidence.fit(bundle, budget)
    assert len(text) <= budget.max_evidence_chars
    assert "근거 원문" in text
    assert bundle["page_reductions"]
    assert bundle["components"][0]["status"] == "matched"
    assert not bundle["package_reductions"]
