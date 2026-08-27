/**
 * 전달 방식 표시 규칙.
 *
 * 이 로직이 틀리면 사용자는 **좁혀서 전달한 실행을 전체 전달로 읽는다.**
 * 실제로 그런 상태가 한 번 있었다 — 전달 방식을 늘렸는데 화면이 두 값으로
 * 하드코딩돼 있어서, 좁혀 전달한 실행이 「전체 인라인 전달」로 표시됐다.
 *
 * 지금은 폭이 둘뿐이고 「페이지 단위」는 로컬 검색 안의 근거 확장 방식이 됐다.
 * 그래서 여기서 고정하는 것은 **옛 값을 어떻게 읽는가**까지 포함한다.
 */
import { describe, expect, it } from "vitest";

import { DELIVERY_LABEL, isNarrowed, toDeliveryPlan } from "./types";
import type { DeliveryPlan } from "./types";

// 백엔드 enums.DeliveryPlan 과 같은 목록. 여기에 값을 더하면서 라벨을 빠뜨리면
// 아래 테스트가 잡는다.
const ALL_PLANS: DeliveryPlan[] = ["full_inline", "local_retrieval"];

describe("전달 방식 라벨", () => {
  it("모든 전달 방식에 이름이 있다", () => {
    for (const plan of ALL_PLANS) {
      expect(DELIVERY_LABEL[plan]).toBeTruthy();
    }
  });

  it("서로 다른 전달 방식이 같은 이름을 쓰지 않는다", () => {
    const labels = ALL_PLANS.map((plan) => DELIVERY_LABEL[plan]);
    expect(new Set(labels).size).toBe(ALL_PLANS.length);
  });

  it("좁힌 전달이 「전체」로 표시되지 않는다", () => {
    // 이 회귀가 핵심이다. 로컬 검색이 전체 전달로 보이면 사용자는 문헌 전체를
    // 모델이 봤다고 믿는다.
    expect(DELIVERY_LABEL.local_retrieval).not.toBe(DELIVERY_LABEL.full_inline);
    expect(DELIVERY_LABEL.local_retrieval).not.toContain("전체");
  });
});

describe("isNarrowed", () => {
  it("전체 인라인만 좁히지 않은 전달이다", () => {
    expect(isNarrowed("full_inline")).toBe(false);
    expect(isNarrowed("local_retrieval")).toBe(true);
  });

  it("좁힌 전달은 하나도 빠짐없이 검색 기록 패널을 연다", () => {
    // RunPage 와 HistoryPage 가 이 함수 하나로 갈린다. 여기서 false 가 나오는
    // 좁힌 전달이 생기면 그 실행은 검색 기록 없이 표시된다.
    const narrowed = ALL_PLANS.filter((plan) => plan !== "full_inline");
    expect(narrowed.every(isNarrowed)).toBe(true);
  });
});

describe("옛 값 읽기", () => {
  it("폐기한 focused_pages 를 전체 전달로 읽지 않는다", () => {
    // 그 실행도 검색을 돌린 실행이다. 전체 인라인으로 읽으면 「문헌 전체를
    // 모델이 봤다」가 되어 거짓이 된다.
    expect(toDeliveryPlan("focused_pages")).toBe("local_retrieval");
    expect(isNarrowed("focused_pages")).toBe(true);
  });

  it("값이 없는 과거 실행은 전체 인라인이다", () => {
    expect(toDeliveryPlan("")).toBe("full_inline");
    expect(toDeliveryPlan(null)).toBe("full_inline");
    expect(toDeliveryPlan(undefined)).toBe("full_inline");
  });

  it("모르는 값은 좁은 쪽으로 읽는다", () => {
    expect(toDeliveryPlan("무슨 값인지 모름")).toBe("local_retrieval");
  });
});
