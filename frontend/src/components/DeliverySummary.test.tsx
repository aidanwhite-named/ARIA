/**
 * 전달 요약이 **실제로 그려지는가.**
 *
 * 표시 문자열을 만드는 함수만 검증하면 JSX 구조가 깨져도 테스트는 통과한다.
 * 여기서는 컴포넌트를 진짜로 그려서 화면에 무엇이 나오는지 확인한다.
 *
 * 고정하는 것:
 *   - 로컬 검색 실행이 「전체」로 표시되지 않는다
 *   - 왜 그 폭을 골랐는지(사유)와 실제로 나간 바이트가 보인다
 *   - 전송 하드 한도와 모델 컨텍스트 예산이 **다른 줄**로 구분된다
 *   - 모델 한도를 추정한 경우 그 사실이 화면에 나온다
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import DeliverySummary from "./DeliverySummary";
import type { DeliveryManifest } from "../lib/types";

afterEach(cleanup);

const AGY_MANIFEST: DeliveryManifest = {
  provider: "agy",
  selected_delivery_mode: "local_retrieval",
  selection_reason:
    "전체 인라인은 251,159 bytes 로 이 Provider 의 전송 한도 180,000 bytes 를 넘습니다.",
  full_inline_chars: 90_000,
  full_inline_bytes: 251_159,
  full_inline_tokens: 125_580,
  actual_payload_chars: 41_234,
  actual_payload_bytes: 118_902,
  provider_byte_limit: 180_000,
  model_token_budget: null,
  payload_is_budget_ceiling: false,
  scale_downgraded: false,
};

const CODEX_MANIFEST: DeliveryManifest = {
  ...AGY_MANIFEST,
  provider: "codex",
  provider_byte_limit: null,
  selection_reason: "전체 인라인이 약 40,000 토큰으로 입력 예산 안에 들어갑니다.",
  selected_delivery_mode: "full_inline",
  model_token_budget: {
    model: "gpt-5-codex",
    context_tokens: 400_000,
    reserve_tokens: 32_000,
    input_tokens: 368_000,
    source: "configured",
  },
};

describe("전달 요약", () => {
  it("로컬 검색 실행을 「전체 인라인」으로 표시하지 않는다", () => {
    render(
      <DeliverySummary
        plan="local_retrieval"
        provider="agy"
        manifest={AGY_MANIFEST}
      />,
    );
    expect(screen.getByText("로컬 검색 전달")).toBeTruthy();
    expect(screen.queryByText("전체 인라인 전달")).toBeNull();
  });

  it("폐기한 focused_pages 기록도 전체 전달로 표시하지 않는다", () => {
    render(
      <DeliverySummary plan="focused_pages" provider="agy" manifest={null} />,
    );
    expect(screen.getByText("로컬 검색 전달")).toBeTruthy();
    expect(screen.queryByText("전체 인라인 전달")).toBeNull();
  });

  it("판정 사유와 실제로 나간 크기를 보여 준다", () => {
    const { container } = render(
      <DeliverySummary
        plan="local_retrieval"
        provider="agy"
        manifest={AGY_MANIFEST}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain(AGY_MANIFEST.selection_reason);
    expect(text).toContain("41,234");
    expect(text).toContain("118,902");
  });

  it("전송 하드 한도는 「CLI 가 자르는 지점」으로 설명한다", () => {
    const { container } = render(
      <DeliverySummary
        plan="local_retrieval"
        provider="agy"
        manifest={AGY_MANIFEST}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("180,000 bytes");
    expect(text).toContain("입력을 자르는 지점");
    // 하드 한도가 있는 Provider 에는 모델 예산 줄이 없다.
    expect(text).not.toContain("모델 입력 예산");
  });

  it("모델 컨텍스트 예산은 전송 한도와 다른 줄에 나온다", () => {
    const { container } = render(
      <DeliverySummary
        plan="full_inline"
        provider="codex"
        manifest={CODEX_MANIFEST}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("모델 입력 예산 368,000 토큰");
    expect(text).toContain("400,000");
    // 하드 한도를 선언하지 않았으므로 그 줄은 없다.
    expect(text).not.toContain("전송 한도");
  });

  it("모델 한도를 추정했으면 화면에 그 사실이 나온다", () => {
    const { container } = render(
      <DeliverySummary
        plan="local_retrieval"
        provider="codex"
        manifest={{
          ...CODEX_MANIFEST,
          model_token_budget: {
            model: "처음 보는 모델",
            context_tokens: 128_000,
            reserve_tokens: 32_000,
            input_tokens: 96_000,
            source: "fallback",
          },
        }}
      />,
    );
    expect(
      (container.textContent ?? "").includes("보수적 대체값"),
    ).toBe(true);
  });

  it("기록이 없는 과거 실행도 깨지지 않는다", () => {
    const { container } = render(
      <DeliverySummary plan="" provider="agy" manifest={null} />,
    );
    expect(screen.getByText("전체 인라인 전달")).toBeTruthy();
    expect(container.textContent).not.toContain("실제 전송");
  });
});
