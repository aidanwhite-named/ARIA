"""작업 하나의 최종 프롬프트 조립.

runner 와 preflight 가 **같은 함수**를 부른다.

두 곳이 각자 조립하면 준비 화면이 안내한 크기와 실제로 나가는 크기가 어긋나고,
그 어긋남은 실행이 실패한 뒤에야 드러난다. 2026-08-25 실행이 그랬다 — 화면은
「허용 800,000자」라고 안내했는데 agy 는 210,743 바이트에서 막았다. 문자수와
바이트가 다른 축인 데다, 화면이 세던 것은 조립 전 원본이었고 실제로 나간 것은
런타임 컨텍스트·경계 표시·명세서 절이 모두 붙은 최종 본문이었다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import retrieval, search_manifest, search_prompt
from .config import (
    AGY_SEARCH_RUNTIME_CONTEXT,
    CODEX_SEARCH_RUNTIME_CONTEXT,
    SEARCH_RUNTIME_CONTEXT,
)
from .enums import AttachmentRole, DeliveryPlan, JobKind, RetrievalMode
from .providers import model_limits
from .ingestion.service import IngestedFile, read_normalized
from .prompt_assembly import (
    AssembledPrompt,
    assemble,
    assemble_search,
    char_gate,
    ordered_attachments,
)
from .prompt_assembly import included_attachments as prompt_assembly_included

# 검색 실행의 런타임 규칙은 Provider 가 실제로 가진 도구에 맞춰야 한다.
# 도구 이름이 다를 뿐 아니라, Codex 는 web_search 하나로 검색과 URL 조회를
# 겸하고(성공 여부는 ARIA 가 확인할 수 없다) agy 는
# 가져온 페이지를 파일로만 돌려준다. 정책 이름으로 고르므로 Provider 가 늘어도
# 이 표만 채우면 된다.
SEARCH_CONTEXT_BY_POLICY = {
    "agy_web_search": AGY_SEARCH_RUNTIME_CONTEXT,
    "codex_web_search": CODEX_SEARCH_RUNTIME_CONTEXT,
}

# 분석 경로에는 레인이 없다. 하나뿐인 조립본을 담는 이름.
LANE_SINGLE = "single"

# 체크된 자료가 하나도 없을 때 세 경로가 함께 쓰는 문구. 화면 안내(preflight),
# 작업 생성 거절(API), 실행 실패(runner)가 같은 말을 해야 한다.
NO_INCLUDED_MATERIAL = (
    "분석에 포함할 인용발명 문헌이 하나도 없습니다. 「분석에 포함」을 체크한 "
    "PDF 가 최소 1건 있어야 구성대비 분석을 실행할 수 있습니다."
)


# 이 실행의 분석 자료를 고르는 단 하나의 계약. 정의는 prompt_assembly 에 있고
# (조립 마지막 층에서도 같은 함수를 쓴다) 여기서는 이름만 다시 내보낸다.
# preflight 와 runner 는 job_assembly 를 통해 부른다.
included_attachments = prompt_assembly_included


class SpecUnreadable(Exception):
    """출원발명 문서를 넣었는데 본문을 읽지 못했다.

    그냥 지나치면 사용자는 명세서를 반영한 검색을 받았다고 믿게 된다.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        super().__init__(filename)


class ModelInputTooLarge(Exception):
    """최종 조립본이 모델 입력 예산에 들어가지 않는다."""

    def __init__(
        self,
        *,
        actual_tokens: int,
        budget: model_limits.TokenBudget,
    ) -> None:
        self.actual_tokens = actual_tokens
        self.budget = budget
        if budget.input_tokens <= 0:
            message = (
                f"모델 {budget.model or '(미지정)'} 의 컨텍스트 "
                f"{budget.context_tokens:,} 토큰에서 출력·추론 예약 "
                f"{budget.reserve_tokens:,} 토큰을 빼면 입력 예산이 0입니다. "
                "Provider 를 호출하지 않았고 토큰도 소모되지 않았습니다. "
                "예약 토큰을 컨텍스트 토큰보다 작게 설정하십시오."
            )
        else:
            message = (
                f"최종 조립 입력은 약 {actual_tokens:,} 토큰으로 모델 "
                f"{budget.model or '(미지정)'} 의 입력 예산 "
                f"{budget.input_tokens:,} 토큰을 넘습니다. Provider 를 호출하지 "
                "않았고 토큰도 소모되지 않았습니다. 로컬 검색의 근거 패키지 "
                "예산을 줄이거나 모델 컨텍스트 설정을 확인하십시오."
            )
        super().__init__(message)


def model_input_gate(
    assembled: AssembledPrompt, budget: model_limits.TokenBudget | None
) -> int:
    """실제로 보낼 최종 조립본을 모델 토큰 예산과 다시 비교한다."""

    if budget is None:
        return 0
    actual = model_limits.estimate_tokens(
        assembled.system_prompt, assembled.user_message
    )
    if budget.input_tokens <= 0 or actual > budget.input_tokens:
        raise ModelInputTooLarge(actual_tokens=actual, budget=budget)
    return actual


@dataclass
class AssemblyResult:
    """조립 결과. 레인이 하나든 둘이든 같은 모양으로 돌려준다."""

    lanes: dict[str, AssembledPrompt]
    spec_document: dict | None = None
    # 명세서 본문. 웹 레인은 이미 렌더된 프롬프트 안에 들고 있지만, EPO 레인은
    # 자기 프롬프트를 따로 만들므로 본문 자체가 필요하다. 없으면 빈 문자열이고,
    # 그때 EPO 는 청구항 단독 레인만 돈다.
    spec_text: str = ""
    search_prompt_sha: str = ""
    # 런타임 컨텍스트의 해시. 템플릿이 그대로여도 이것이 바뀌면 모델이 받은
    # 프롬프트가 달라진다 — Provider 별 파생 컨텍스트가 여기에 들어간다.
    search_runtime_context_sha: str = ""
    claim_boundary_neutralized: bool = False
    spec_boundary_neutralized: bool = False
    focus_boundary_neutralized: bool = False
    notes: list[str] = field(default_factory=list)
    # 인용발명 문헌을 최종 분석 모델에게 어떻게 전달하는가. 분석 경로에서만
    # 의미가 있다. 검색 작업은 첨부 본문을 애초에 넣지 않으므로 항상 기본값이다.
    delivery_plan: str = DeliveryPlan.FULL_INLINE
    # auto 판정에 쓴 값. 화면이 "왜 로컬 검색으로 갔는가"를 설명할 수 있게 남긴다.
    full_inline_bytes: int = 0
    full_inline_chars: int = 0
    # 판정 전체(사유 포함). 화면·manifest·실행 기록이 같은 문장을 쓴다.
    decision: DeliveryDecision | None = None
    # 이 조립본이 자리표(preflight)인가 실제 근거 패키지인가.
    # True 면 크기는 예산 상한이고, 실행이 그 상한을 넘지 못한다.
    evidence_placeholder: bool = False

    @property
    def selection_reason(self) -> str:
        return self.decision.reason if self.decision is not None else ""

    def delivery_manifest(self, provider=None) -> dict:
        """이 실행이 무엇을 어떻게 전달했는지의 한 벌 기록.

        화면·History·감사 기록이 **같은 값**을 쓴다. 세 곳이 각자 계산하면
        같은 실행이 세 가지로 설명된다.

        섞지 말아야 할 것을 필드 이름으로 갈라 둔다.

          provider_byte_limit      Provider 전송 하드 한도(agy). 사용자가 못 끈다.
          model_token_budget       모델 컨텍스트 입력 예산(codex, claude).
                                   추정값이면 source 가 fallback 이다.
          full_inline_bytes/chars/tokens  전체를 넣었다면 얼마였는가.
          actual_payload_*         실제로 나가는 크기.
          selection_reason         왜 이 폭을 골랐는가.

        전송 하드 한도와 모델 컨텍스트를 같은 칸에 두지 않는다. 앞쪽은 CLI 가
        자르는 지점이고 뒤쪽은 모델이 거절하는 지점이라, 사용자가 할 일이 다르다.
        """
        lane = self.representative
        measure = getattr(provider, "payload_bytes", None)
        decision = self.decision
        return {
            "provider": getattr(provider, "id", "") or "",
            "selected_delivery_mode": self.delivery_plan,
            "selection_reason": self.selection_reason,
            "full_inline_chars": self.full_inline_chars,
            "full_inline_bytes": self.full_inline_bytes,
            "actual_payload_chars": lane.total_chars,
            "actual_payload_bytes": _payload_bytes(lane, measure),
            "full_inline_tokens": decision.full_inline_tokens if decision else 0,
            "provider_byte_limit": getattr(provider, "max_input_bytes", None),
            "model_token_budget": (
                decision.token_budget.to_dict()
                if decision is not None and decision.token_budget is not None
                else None
            ),
            # 이 크기가 실측인가 예산 상한인가. preflight 는 상한을 보여 준다.
            "payload_is_budget_ceiling": self.evidence_placeholder,
            "scale_downgraded": bool(decision.scale_downgraded) if decision else False,
        }

    @property
    def representative(self) -> AssembledPrompt:
        """저장 메타데이터와 공통 코드가 쓰는 대표 조립본.

        명세서가 있으면 전체 입력을 기록하는 보조 조립본을 쓴다.
        """
        return (
            self.lanes.get(search_manifest.ORIGIN_SPEC_ASSISTED)
            or self.lanes.get(search_manifest.ORIGIN_CLAIM_ONLY)
            or self.lanes[LANE_SINGLE]
        )

    def lane_bytes(self, provider=None) -> dict[str, int]:
        """레인마다 Provider 에게 실제로 나갈 UTF-8 바이트 수.

        Provider 의 바이트 한도는 레인마다 따로 걸린다. 두 레인을 합쳐서 재면
        각각은 통과하는 입력이 초과로 잡힌다.

        provider 를 주면 그쪽 계산(Provider.payload_bytes)을 쓴다. 화면이 안내하는
        크기와 실행이 검사하는 크기는 같은 함수에서 나와야 한다 — 한쪽만 감싸기
        이후를 재면 준비 화면이 "여유 있음"이라고 안내한 실행이 실행 직전에 막힌다.
        """
        measure = getattr(provider, "payload_bytes", None) if provider else None
        if not callable(measure):
            def measure(system_prompt: str, user_message: str) -> int:
                return len(system_prompt.encode("utf-8")) + len(
                    user_message.encode("utf-8")
                )

        return {
            name: measure(lane.system_prompt, lane.user_message)
            for name, lane in self.lanes.items()
        }


def preflight_documents(attachments: list[IngestedFile]) -> list[dict]:
    """근거 패키지 자리표에 넣을 문헌 목록.

    아직 색인하기 전이라 추출 상태는 알 수 없다. 페이지 수와 파일명만으로
    골격을 만들고, 채움은 render_placeholder 가 예산만큼 붙인다.
    """
    from .citation_mapping import assign_aliases

    aliases = assign_aliases(ordered_attachments(attachments))
    alias_by_id = {item.attachment_id: alias for alias, item in aliases.items()}
    return [
        {
            "attachment": alias_by_id.get(item.attachment_id, ""),
            "filename": item.original_filename,
            "pdf_pages": item.page_count or 1,
            "extraction_status": "(실행 시 확인)",
            "empty_or_low_text_pages": [],
            "extraction_failed_pages": [],
            "visual_review_required_pages": [],
            "extraction_divergence_pages": [],
        }
        for item in attachments
    ]


def _payload_bytes(assembled: AssembledPrompt, measure=None) -> int:
    """이 조립본이 실제 전송선에 올릴 바이트 수.

    measure 는 Provider.payload_bytes 다. 주지 않으면 두 문자열의 단순 합인데,
    그것은 감싸기 없이 그대로 보내는 Provider 에서만 맞다.
    """
    if callable(measure):
        return measure(assembled.system_prompt, assembled.user_message)
    return len(assembled.system_prompt.encode("utf-8")) + len(
        assembled.user_message.encode("utf-8")
    )


@dataclass
class DeliveryScale:
    """사건 규모. 문자 수만으로는 보이지 않는 축이다.

    같은 20만 자라도 문헌 1건 30페이지와 문헌 8건 400페이지는 다른 작업이다.
    뒤쪽은 전체를 넣어도 모델이 구성마다 문헌을 오가며 대조해야 하고, 그
    왕복에서 놓치는 것이 발췌로 놓치는 것보다 크다.
    """

    documents: int = 0
    pages: int = 0
    claim_elements: int = 0


@dataclass
class DeliveryDecision:
    """어떤 전달 방식으로 갈 것인가와 **왜 그렇게 정했는가**.

    사유를 값으로 들고 다닌다. 화면과 manifest 가 각자 문장을 만들면 같은
    실행을 두 가지로 설명하게 된다.
    """

    plan: str
    reason: str = ""
    full_inline_bytes: int = 0
    full_inline_chars: int = 0
    # 모델 컨텍스트 축. 바이트와 다른 축이므로 같은 칸에 두지 않는다.
    full_inline_tokens: int = 0
    provider_byte_limit: int | None = None
    token_budget: "model_limits.TokenBudget | None" = None
    # 규모 때문에 한 단계 좁혔는가. 화면이 "작은데 왜 좁혔나"에 답할 수 있어야 한다.
    scale_downgraded: bool = False


def decide_delivery(
    *,
    retrieval_mode: str,
    full_inline_bytes: int,
    provider_byte_budget: int | None,
    full_inline_tokens: int = 0,
    token_budget: model_limits.TokenBudget | None = None,
    scale: DeliveryScale | None = None,
    scale_limits: DeliveryScale | None = None,
) -> DeliveryDecision:
    """인용발명 문헌을 전체로 넣을 것인가, 로컬 검색으로 넣을 것인가.

    preflight 와 runner 가 **이 함수 하나**를 부른다. 두 곳이 각자 판정하면
    화면은 "전체 인라인"이라고 안내하고 실행은 로컬 검색으로 도는 상태가 되고,
    그 어긋남은 보고서를 받은 뒤에야 드러난다.

    **세 축을 섞지 않는다.**

      1. Provider 전송 하드 한도 (provider_byte_budget)
         그 CLI 가 모델에 넘기기 전에 자르는 지점. agy 만 선언한다. 넘겨 보내면
         뒷부분이 사라진 채 종료 코드 0 으로 끝난다. 사용자가 끌 수 없다.
      2. 모델 컨텍스트 입력 예산 (token_budget)
         CLI 가 자르지 않는 Provider(codex, claude)의 실제 한계. 모델이 받을 수
         있는 토큰에서 출력·추론 자리를 뺀 값이다. 넘기면 조용히 잘리지는 않고
         거절당한다 — 그래도 검색 비용은 이미 나간 뒤다.
      3. 사건 규모 품질 기준 (scale / scale_limits)
         "이 정도면 좁혀 읽는 편이 낫다"는 판단. 조정할 수 있고 기본은 꺼짐이다.
         1번을 선언한 Provider 에는 적용하지 않는다 — 그쪽은 하드 한도가 이미
         좁힐 시점을 정한다.

    어느 축으로 좁히든 문서를 자르지 않는다. 넣지 못한 범위는 미확인으로
    기록된다.
    """
    scale = scale or DeliveryScale()
    scale_limits = scale_limits or DeliveryScale()
    decision = DeliveryDecision(
        plan=DeliveryPlan.FULL_INLINE,
        full_inline_bytes=full_inline_bytes,
        full_inline_tokens=full_inline_tokens,
        provider_byte_limit=provider_byte_budget,
        token_budget=token_budget,
    )

    mode = RetrievalMode.coerce(retrieval_mode)
    if mode is RetrievalMode.FULL:
        decision.reason = (
            "전달 방식이 「전체 인라인 고정」입니다. 크기와 무관하게 원문 전체를 "
            "넣으며, 전송 한도를 넘으면 자르지 않고 INPUT_TOO_LARGE 로 "
            "중단합니다."
        )
        return decision
    if mode is RetrievalMode.RETRIEVAL:
        decision.plan = DeliveryPlan.LOCAL_RETRIEVAL
        decision.reason = (
            "전달 방식이 「로컬 검색 고정」입니다. 작은 문헌도 전체 본문 대신 "
            "근거 패키지만 전달되므로, 검색어에 걸리지 않은 구간은 최종 분석 "
            "모델이 보지 못합니다."
        )
        return decision

    # ---- auto ----------------------------------------------------------
    # 하드 한도를 선언한 Provider 에는 그 한도만 건다. 모델 컨텍스트와 규모
    # 기준을 겹쳐 걸면 한도 안에 들어오는 입력까지 좁아지고, 판정 사유가 "이
    # Provider 는 전송 하드 한도를 선언하지 않으므로"라는 거짓을 담게 된다.
    has_hard_cap = provider_byte_budget is not None

    if has_hard_cap:
        if full_inline_bytes > provider_byte_budget:
            decision.plan = DeliveryPlan.LOCAL_RETRIEVAL
            decision.reason = (
                f"전체 인라인은 {full_inline_bytes:,} bytes 로 이 Provider 의 "
                f"전송 한도 {provider_byte_budget:,} bytes 를 넘습니다. 한도를 "
                "넘겨 보내면 CLI 가 뒷부분을 잘라 놓고 종료 코드 0 으로 "
                "끝내므로, 로컬 검색으로 전환했습니다."
            )
        else:
            decision.reason = (
                f"전체 인라인이 {full_inline_bytes:,} bytes 로 이 Provider 의 "
                f"전송 한도 {provider_byte_budget:,} bytes 안에 들어갑니다. "
                "원문 전체를 넣습니다."
            )
        return decision

    # 하드 한도가 없는 Provider: 모델 컨텍스트 입력 예산으로 판정한다.
    if token_budget is not None:
        if token_budget.input_tokens <= 0:
            decision.plan = DeliveryPlan.LOCAL_RETRIEVAL
            decision.reason = (
                f"모델 {token_budget.model or '(미지정)'} 의 컨텍스트 "
                f"{token_budget.context_tokens:,} 토큰에서 출력·추론 예약 "
                f"{token_budget.reserve_tokens:,} 토큰을 빼면 입력 예산이 0입니다. "
                "전체 전달로 안전장치를 우회하지 않고 로컬 검색 경로로 "
                "전환했으며, 최종 조립 단계에서 유효한 입력 예산이 없으면 "
                "Provider 호출 전에 중단합니다."
            )
        elif full_inline_tokens > token_budget.input_tokens:
            decision.plan = DeliveryPlan.LOCAL_RETRIEVAL
            decision.reason = (
                f"전체 인라인은 약 {full_inline_tokens:,} 토큰으로 입력 예산 "
                f"{token_budget.input_tokens:,} 토큰을 넘습니다. "
                + model_limits.describe(token_budget)
            )
        else:
            decision.reason = (
                f"전체 인라인이 약 {full_inline_tokens:,} 토큰으로 입력 예산 "
                f"{token_budget.input_tokens:,} 토큰 안에 들어갑니다. "
                + model_limits.describe(token_budget)
            )
    else:
        decision.reason = (
            f"전체 인라인이 {full_inline_bytes:,} bytes 입니다. 이 Provider 는 "
            "전송 하드 한도를 선언하지 않았고 모델 입력 예산도 정해지지 않아 "
            "원문 전체를 넣습니다."
        )

    # ---- 규모에 따른 전환 ------------------------------------------------
    exceeded = []
    if scale_limits.documents and scale.documents > scale_limits.documents:
        exceeded.append(f"문헌 {scale.documents}건")
    if scale_limits.pages and scale.pages > scale_limits.pages:
        exceeded.append(f"총 {scale.pages}페이지")
    if scale_limits.claim_elements and scale.claim_elements > scale_limits.claim_elements:
        exceeded.append(f"청구항 구성 {scale.claim_elements}개")
    if exceeded and decision.plan != DeliveryPlan.LOCAL_RETRIEVAL:
        decision.plan = DeliveryPlan.LOCAL_RETRIEVAL
        decision.scale_downgraded = True
        decision.reason += (
            f" 또한 사건 규모({', '.join(exceeded)})가 기준을 넘어 로컬 검색으로 "
            "전환했습니다. 이것은 전송 한도가 아니라 조정 가능한 품질 정책입니다."
        )
    return decision


def decide_delivery_plan(
    *,
    retrieval_mode: str,
    full_inline_bytes: int,
    provider_byte_budget: int | None,
) -> str:
    """전달 방식만 필요한 호출부용. 판정은 decide_delivery 하나에 있다."""
    return decide_delivery(
        retrieval_mode=retrieval_mode,
        full_inline_bytes=full_inline_bytes,
        provider_byte_budget=provider_byte_budget,
    ).plan


def claim_element_count(claim_text: str) -> int:
    """청구항 구성 수의 **어림값**.

    정확한 분해는 검색 단계 AI 가 한다(retrieval.prompts 의 components). 조립
    시점에는 아직 그 결과가 없으므로 여기서는 구분자로만 센다.

    어림값이어도 되는 이유는 쓰이는 곳이 하나뿐이기 때문이다 — 사건 규모가
    커서 한 단계 좁힐지를 정하는 **품질 정책**이다. 전송 한도처럼 실행을 막는
    판정에는 쓰지 않는다. 틀려도 전달 폭이 한 칸 달라질 뿐이고, 그 사실과
    사유는 manifest 에 남는다.
    """
    text = str(claim_text or "").strip()
    if not text:
        return 0
    pieces: list[str] = []
    for line in text.replace(";", "\n").splitlines():
        cleaned = line.strip()
        # 너무 짧은 조각은 구성이 아니라 머리말이나 번호다.
        if len(cleaned) >= 8:
            pieces.append(cleaned)
    return len(pieces)


def delivery_policy_from_settings(values: dict) -> dict:
    """전달 판정에 필요한 설정을 한 번에 읽는다.

    preflight 와 runner 가 **이 함수 하나**를 쓴다. 두 곳이 각자 기본값을 적어
    두면 화면이 안내한 전달 방식과 실제로 도는 방식이 달라지고, 그 어긋남은
    보고서를 받은 뒤에야 드러난다. retrieval.budget_from_settings 와 같은 이유다.

    여기 있는 값 중 Provider 전송 하드 한도는 없다. 그쪽은 Provider 클래스가
    선언하며 설정으로 바꿀 수 없다.
    """

    def _int(key: str, fallback: int = 0) -> int:
        try:
            return max(0, int(values.get(key, fallback) or 0))
        except (TypeError, ValueError):
            return fallback

    overrides = values.get("model_context_tokens")
    return {
        "model_context_overrides": overrides if isinstance(overrides, dict) else {},
        "model_output_reserve_tokens": _int("model_output_reserve_tokens", 32_000),
        "unknown_model_context_tokens": _int("unknown_model_context_tokens", 128_000),
        "delivery_scale_limits": DeliveryScale(
            documents=_int("delivery_scale_documents"),
            pages=_int("delivery_scale_pages"),
            claim_elements=_int("delivery_scale_claim_elements"),
        ),
    }


def search_spec(attachments: list[IngestedFile]) -> IngestedFile | None:
    """검색 실행에 넣은 출원발명 문서. 없으면 None.

    검색 작업의 첨부는 이것 하나뿐이다. 여러 건이 들어오는 경우는 작업 생성
    단계에서 이미 거절된다.
    """
    for item in attachments:
        if item.role == AttachmentRole.APPLICATION:
            return item
    return None


def assemble_job(
    *,
    job_kind: JobKind,
    master_prompt: str,
    attachments: list[IngestedFile],
    runtime_context: str,
    runtime_context_enabled: bool,
    # None(또는 0) = ARIA 자체 글자 수 한도 없음. 그래도 Provider 전송 한도와
    # 모델 컨텍스트 한도는 남는다 — 그 검사는 조립 뒤에 바이트로 이뤄진다.
    max_chars: int | None,
    claim_text: str = "",
    focus_text: str = "",
    followup_instruction: str = "",
    prior_claim_text: str = "",
    prior_report: str = "",
    prior_citation_mapping: dict | None = None,
    tool_policy_name: str = "",
    retrieval_mode: str = RetrievalMode.AUTO,
    provider_byte_budget: int | None = None,
    retrieval_budget: retrieval.RetrievalBudget | None = None,
    evidence_bundle: dict | None = None,
    # 모델 컨텍스트 입력 예산. 전송 하드 한도를 선언하지 않은 Provider
    # (codex, claude)에서만 판정에 쓰인다.
    provider_id: str = "",
    model: str = "",
    model_context_overrides: dict | None = None,
    model_output_reserve_tokens: int = 0,
    unknown_model_context_tokens: int = 0,
    # 사건 규모 품질 정책. 0 이면 쓰지 않는다.
    delivery_scale_limits: DeliveryScale | None = None,
    claim_element_count: int = 0,
    provider_measure=None,
) -> AssemblyResult:
    """이 작업이 Provider 에게 실제로 보낼 본문을 만든다.

    검색이면 청구항 단독 / 명세서 보조 두 레인을, 분석이면 하나를 돌려준다.
    InputTooLarge 와 SearchPromptError 는 그대로 올린다 — 호출부가 실행 실패로
    기록할지(runner) 화면에 안내할지(preflight) 정한다.

    호출부가 이미 걸렀더라도 여기서 한 번 더 included_attachments 를 통과시킨다.
    "제외한 자료는 프롬프트에 한 글자도 들어가지 않는다"는 불변조건을 조립
    바로 앞에서 지키기 위해서다.
    """
    attachments = included_attachments(attachments)
    if job_kind is not JobKind.SIMILARITY_SEARCH:
        common = {
            "master_prompt": master_prompt,
            "attachments": attachments,
            "runtime_context": runtime_context,
            "runtime_context_enabled": runtime_context_enabled,
            "claim_text": claim_text,
            "followup_instruction": followup_instruction,
            "prior_claim_text": prior_claim_text,
            "prior_report": prior_report,
            "prior_citation_mapping": prior_citation_mapping,
        }

        # auto 판정에는 전체 인라인 조립본의 실제 바이트가 필요하다. 이 조립에는
        # 글자 수 한도를 걸지 않는다 — 한도를 넘었다고 여기서 예외를 던지면
        # "너무 커서 로컬 검색으로 간다"는 판정 자체를 못 한다. 전체 인라인으로
        # 확정되면 아래에서 같은 조립본에 한도를 건다.
        probe: AssembledPrompt | None = None
        full_bytes = 0
        full_chars = 0
        full_tokens = 0
        if RetrievalMode.coerce(retrieval_mode) is RetrievalMode.AUTO:
            probe = assemble(max_chars=None, **common)
            # 바이트는 Provider 에게 묻는다. 감싸기·이스케이프 이후의 크기가
            # 전송 한도와 비교되는 값이다.
            full_bytes = _payload_bytes(probe, provider_measure)
            full_chars = probe.total_chars
            full_tokens = model_limits.estimate_tokens(
                probe.system_prompt, probe.user_message
            )
        budget_for_model = (
            model_limits.token_budget(
                provider_id=provider_id,
                model=model,
                overrides=model_context_overrides,
                reserve_tokens=model_output_reserve_tokens,
                fallback_context_tokens=unknown_model_context_tokens,
            )
            if provider_byte_budget is None and unknown_model_context_tokens
            else None
        )
        decision = decide_delivery(
            retrieval_mode=retrieval_mode,
            full_inline_bytes=full_bytes,
            provider_byte_budget=provider_byte_budget,
            full_inline_tokens=full_tokens,
            token_budget=budget_for_model,
            scale=DeliveryScale(
                documents=len(attachments),
                pages=sum(int(item.page_count or 0) for item in attachments),
                claim_elements=claim_element_count,
            ),
            scale_limits=delivery_scale_limits,
        )
        decision.full_inline_chars = full_chars
        plan = decision.plan

        if plan == DeliveryPlan.FULL_INLINE:
            assembled = probe if probe is not None else assemble(
                max_chars=None, **common
            )
            # 한도 검사는 조립본을 다시 만들지 않고 같은 값에 건다. 다시 만들면
            # 큰 첨부를 두 번 읽게 되고, 두 조립본이 미세하게 달라질 여지도 생긴다.
            char_gate(assembled.total_chars, max_chars)
            actual_tokens = model_input_gate(assembled, budget_for_model)
            if not decision.full_inline_tokens:
                decision.full_inline_tokens = actual_tokens
            measured = full_bytes or _payload_bytes(assembled, provider_measure)
            decision.full_inline_bytes = measured
            decision.full_inline_chars = full_chars or assembled.total_chars
            return AssemblyResult(
                lanes={LANE_SINGLE: assembled},
                delivery_plan=plan,
                full_inline_bytes=measured,
                full_inline_chars=decision.full_inline_chars,
                decision=decision,
            )

        # 로컬 검색. 실제 근거 패키지가 아직 없으면(preflight) 예산만큼의
        # 자리표로 크기를 잰다. 실행은 같은 예산을 넘지 못하므로 여기서 잰
        # 값이 실제 크기의 상한이 된다.
        budget = retrieval_budget or retrieval.RetrievalBudget()
        placeholder = evidence_bundle is None
        bundle = evidence_bundle
        if placeholder:
            # 실제 근거 패키지가 아직 없다(preflight). 예산만큼의 자리표로
            # 크기를 잰다. 실행은 같은 예산을 넘지 못하므로 여기서 잰 값이
            # 실제 크기의 상한이 된다.
            bundle = {
                retrieval.PLACEHOLDER_KEY: retrieval.render_placeholder(
                    budget, preflight_documents(attachments)
                )
            }
        lane = assemble(max_chars=max_chars, evidence_bundle=bundle, **common)
        model_input_gate(lane, budget_for_model)
        return AssemblyResult(
            lanes={LANE_SINGLE: lane},
            delivery_plan=plan,
            full_inline_bytes=full_bytes,
            full_inline_chars=full_chars,
            evidence_placeholder=placeholder,
            decision=decision,
        )

    # 검색 프롬프트는 실행 시점에 파일에서 다시 읽지 않는다. 작업 생성 시
    # 스냅샷한 본문으로 돈다 — 큐에서 기다리는 동안 파일이 바뀌어도 이 실행의
    # 계약은 흔들리지 않아야 한다. 해시는 그 스냅샷에 대해 계산한다.
    spec = search_spec(attachments)
    spec_text = read_normalized(spec) if spec is not None else ""
    if spec is not None and not spec_text.strip():
        raise SpecUnreadable(spec.original_filename)

    # 가장 중요한 불변조건: 기본 검색 프롬프트에는 명세서 본문이 단 한 글자도
    # 들어가지 않는다. 같은 호출 안에서 "먼저 청구항만 보라"고 부탁하는 대신
    # 컨텍스트 자체를 격리한다.
    claim_rendered = search_prompt.render(master_prompt, claim_text, "", focus_text)
    search_context = SEARCH_CONTEXT_BY_POLICY.get(
        tool_policy_name, SEARCH_RUNTIME_CONTEXT
    )
    lanes: dict[str, AssembledPrompt] = {
        search_manifest.ORIGIN_CLAIM_ONLY: assemble_search(
            search_prompt_body=claim_rendered.body,
            runtime_context=search_context,
            max_chars=max_chars,
            # 파일 신원조차 모델 컨텍스트에는 들어가지 않지만, 단독 실행의
            # 조립 기록도 입력 파일과 분리해 둔다.
            attachments=[],
        )
    }

    spec_document: dict | None = None
    spec_boundary_neutralized = False
    if spec is not None:
        spec_document = {
            "attachment_id": spec.attachment_id,
            "filename": spec.original_filename,
            "sha256": spec.sha256,
            "page_count": spec.page_count,
            "char_count": len(spec_text),
        }
        assisted_rendered = search_prompt.render(
            master_prompt, claim_text, spec_text, focus_text
        )
        spec_boundary_neutralized = assisted_rendered.spec_boundary_neutralized
        lanes[search_manifest.ORIGIN_SPEC_ASSISTED] = assemble_search(
            search_prompt_body=assisted_rendered.body,
            runtime_context=search_context,
            max_chars=max_chars,
            attachments=attachments,
        )

    return AssemblyResult(
        lanes=lanes,
        spec_document=spec_document,
        spec_text=spec_text,
        search_prompt_sha=search_prompt.sha256(master_prompt),
        search_runtime_context_sha=hashlib.sha256(
            search_context.encode("utf-8")
        ).hexdigest(),
        claim_boundary_neutralized=claim_rendered.claim_boundary_neutralized,
        spec_boundary_neutralized=spec_boundary_neutralized,
        focus_boundary_neutralized=claim_rendered.focus_boundary_neutralized,
    )
