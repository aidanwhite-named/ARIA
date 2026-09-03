/**
 * 두 작업이 서로의 자리를 건드리지 않는가.
 *
 * 구성대비 분석과 유사문헌 검색은 순서가 아니라 갈래다. 화면이 그렇게 보이려면
 * 상태도 그래야 한다 — 한쪽에서 보고서를 펴 둔 채 다른 쪽으로 건너갔을 때,
 * 건너간 쪽이 「결과 보기」로 열리면 아직 아무것도 돌리지 않았는데 빈 결과
 * 화면이 뜬다. 그 순간 둘은 한 화면의 두 단계로 되돌아간다.
 *
 * 고정하는 것:
 *   - 보고 있던 탭을 축마다 따로 기억한다
 *   - kind 를 지정하면 보고 있지 않은 축의 탭을 바꾼다 (미대응 구성 검색)
 *   - 축마다 탭을 나누기 전에 저장된 캐시도 읽는다
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { RunSessionProvider, useRunSession, type RunSession } from "./runSession";

const STORAGE_KEY = "aria.run-session.v1";

let session: RunSession | null = null;

function Probe() {
  session = useRunSession();
  return <span data-testid="tab">{session.activeTab}</span>;
}

function openSession() {
  render(
    <RunSessionProvider>
      <Probe />
    </RunSessionProvider>,
  );
}

const tab = () => screen.getByTestId("tab").textContent;

describe("실행 세션", () => {
  beforeEach(() => {
    sessionStorage.clear();
    session = null;
  });

  afterEach(() => {
    cleanup();
  });

  it("보고 있던 탭을 축마다 따로 기억한다", () => {
    openSession();
    expect(tab()).toBe("input");

    act(() => session!.setActiveTab("result"));
    expect(tab()).toBe("result");

    // 검색으로 건너가면 검색은 제 자리에서 시작한다.
    act(() => session!.setJobKind("similarity_search"));
    expect(tab()).toBe("input");

    // 돌아오면 분석은 보던 자리 그대로다.
    act(() => session!.setJobKind("patent_analysis"));
    expect(tab()).toBe("result");
  });

  it("kind 를 주면 보고 있지 않은 축의 탭을 바꾼다", () => {
    openSession();

    // 분석 화면에서 검색 실행을 띄우는 경우(미대응 구성 검색). 결과는 검색
    // 화면에 열려야 하고, 지금 보고 있는 분석의 탭은 건드리지 않아야 한다.
    act(() => session!.setActiveTab("result", "similarity_search"));
    expect(tab()).toBe("input");

    act(() => session!.setJobKind("similarity_search"));
    expect(tab()).toBe("result");
  });

  it("축마다 탭을 나누기 전에 저장된 캐시는 그때 보던 축에만 얹는다", () => {
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ jobKind: "similarity_search", activeTab: "result" }),
    );

    openSession();
    // 그때 보고 있던 축은 검색이었다.
    expect(session!.jobKind).toBe("similarity_search");
    expect(tab()).toBe("result");

    // 분석은 그 캐시와 무관하다.
    act(() => session!.setJobKind("patent_analysis"));
    expect(tab()).toBe("input");
  });
});
