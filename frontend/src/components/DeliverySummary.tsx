/**
 * 이 실행이 인용발명을 어떤 폭으로, 왜, 얼마만큼 보냈는가.
 *
 * RunPage 안에 인라인으로 있던 것을 꺼냈다. 두 가지 이유다.
 *
 *   1. 렌더링 테스트가 가능해진다. 「좁혀 전달한 실행이 전체 전달로 표시되지
 *      않는가」는 표시 문자열을 만드는 함수만 봐서는 답할 수 없다 — JSX 구조가
 *      깨져도 그 함수는 그대로다.
 *   2. 섞지 말아야 할 두 한도를 한 군데서만 그린다. Provider 전송 하드 한도와
 *      모델 컨텍스트 입력 예산은 사용자가 할 일이 다르므로 같은 줄에 뭉뚱그리지
 *      않는다.
 */
import { DELIVERY_LABEL, isNarrowed, toDeliveryPlan } from "../lib/types";
import type { DeliveryManifest, RetrievalManifest } from "../lib/types";

export default function DeliverySummary({
  plan,
  provider,
  manifest,
  retrieval,
}: {
  plan: string;
  provider: string;
  manifest?: DeliveryManifest | null;
  retrieval?: RetrievalManifest | null;
}) {
  const resolved = toDeliveryPlan(plan);
  const narrowed = isNarrowed(resolved);
  const budget = manifest?.model_token_budget ?? null;

  return (
    <div data-testid="delivery-summary">
      <span className={`pill ${narrowed ? "accent" : "neutral"}`}>
        {DELIVERY_LABEL[resolved]}
      </span>
      {manifest?.selection_reason && (
        <div className="faint">{manifest.selection_reason}</div>
      )}
      {manifest && (
        <div className="faint">
          실제 전송 {manifest.actual_payload_chars.toLocaleString()}자 ·{" "}
          {manifest.actual_payload_bytes.toLocaleString()} bytes
        </div>
      )}
      {/* 전송 하드 한도. 그 CLI 가 자르는 지점이며 사용자가 끌 수 없다. */}
      {manifest?.provider_byte_limit != null && (
        <div className="faint">
          {provider} 전송 한도 {manifest.provider_byte_limit.toLocaleString()} bytes
          — 이 CLI 가 입력을 자르는 지점입니다.
        </div>
      )}
      {/* 모델 컨텍스트 예산. 위와 다른 축이다. 추정값이면 그 사실을 반드시 적는다. */}
      {budget && (
        <div className="faint">
          모델 입력 예산 {budget.input_tokens.toLocaleString()} 토큰 (컨텍스트{" "}
          {budget.context_tokens.toLocaleString()} − 출력·추론{" "}
          {budget.reserve_tokens.toLocaleString()})
          {budget.source === "fallback" && (
            <strong> · 모델 한도를 확인하지 못해 보수적 대체값을 썼습니다.</strong>
          )}
        </div>
      )}
      {narrowed && retrieval && (
        <div className="faint">
          색인 {retrieval.documents.length}건 · 라운드 {retrieval.rounds.length}회 ·
          읽은 페이지 {retrieval.pages_read}쪽 · 근거{" "}
          {retrieval.evidence_chars.toLocaleString()}자
        </div>
      )}
    </div>
  );
}
