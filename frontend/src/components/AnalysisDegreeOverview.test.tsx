import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import AnalysisDegreeOverview, { analysisDegree } from "./AnalysisDegreeOverview";
import type { AnalysisComponent } from "../lib/types";

afterEach(cleanup);

function component(
  similarity: number | null,
  status: AnalysisComponent["status"] = "matched",
  id = "R001",
): AnalysisComponent {
  return {
    id,
    claim: "청구항 1",
    symbol: "(A)",
    feature: "시험 구성",
    similarity,
    status,
    difference: "",
    search_eligible: similarity !== null && similarity < 80,
  };
}

describe("구성별 대응 정도", () => {
  it("연속 점수를 읽기 쉬운 다섯 상태로 줄인다", () => {
    expect(analysisDegree(component(98)).label).toBe("동일 대응");
    expect(analysisDegree(component(91)).label).toBe("실질 대응");
    expect(analysisDegree(component(75, "below_threshold")).label).toBe("부분 대응");
    expect(analysisDegree(component(0, "below_threshold")).label).toBe("대응 없음");
    expect(analysisDegree(component(null, "unreadable")).label).toBe("판단 불가");
  });

  it("색을 보지 않아도 상태명과 유사도를 읽을 수 있다", () => {
    render(
      <AnalysisDegreeOverview
        components={[
          component(92, "matched", "R001"),
          { ...component(null, "unreadable", "R002"), symbol: "(B)" },
        ]}
      />,
    );

    expect(screen.getByText("실질 대응")).toBeTruthy();
    expect(screen.getByText("92%")).toBeTruthy();
    expect(screen.getByText("판단 불가")).toBeTruthy();
    expect(screen.getByText("유사도 없음")).toBeTruthy();
  });
});
