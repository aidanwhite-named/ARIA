/**
 * 설정 화면이 **실제로 그려지는가.**
 *
 * 이 화면은 한 번 구조가 어긋난 적이 있다 — 패치가 앵커를 잘못 잡아 전달 카드가
 * 두 벌이 되고, 폐기한 선택지가 살아 있었다. 타입 검사는 그것을 잡지 못한다.
 *
 * 고정하는 것:
 *   - 전달 방식 선택지가 auto / full / retrieval 셋뿐이다 (폐기 값 없음)
 *   - 화면이 안내하는 「0 = 사용 안 함」이 실제로 있는 값에만 붙는다
 *   - 두 한도(전송 하드 / 모델 컨텍스트)가 각자 자기 절에서 설명된다
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const settingsResponse = {
  values: {
    max_file_size_bytes: 26214400,
    max_total_upload_bytes: 104857600,
    max_files_per_job: 20,
    max_inline_chars: 0,
    default_timeout_seconds: 900,
    max_concurrency_per_provider: 1,
    runtime_context: "런타임",
    runtime_context_enabled: true,
    default_prompt_id: "",
    default_provider: "agy",
    provider_paths: {},
    default_models: {},
    keep_raw_output: true,
    fail_on_tool_use: true,
    max_search_tool_calls: 40,
    retrieval_mode: "auto",
    retrieval_max_rounds: 6,
    retrieval_max_page_reads: 40,
    retrieval_evidence_chars: 40000,
    retrieval_hits_per_document: 6,
    retrieval_neighbor_pages: 1,
    model_context_tokens: {},
    model_output_reserve_tokens: 32000,
    unknown_model_context_tokens: 128000,
    delivery_scale_documents: 0,
    delivery_scale_pages: 0,
    delivery_scale_claim_elements: 0,
    embedding_cache_max_mb: 512,
    retrieval_semantic_enabled: true,
    kiwee_integration_enabled: false,
    epo_integration_enabled: false,
    epo_consumer_key: "",
    epo_consumer_secret: "",
  },
  warnings: [],
  data_dir: "C:/data",
  runs_dir: "C:/data/runs",
  env_filtering: {
    allowlist: [],
    blocked_prefixes: [],
    removed_count: 0,
    removed_sample: [],
  },
};

vi.mock("../lib/api", () => ({
  api: {
    settings: vi.fn(async () => settingsResponse),
    listPrompts: vi.fn(async () => []),
    listProviders: vi.fn(async () => []),
    updateSettings: vi.fn(async () => settingsResponse),
    probeProviders: vi.fn(async () => []),
  },
}));

let SettingsPage: typeof import("./SettingsPage").default;

beforeEach(async () => {
  SettingsPage = (await import("./SettingsPage")).default;
});
afterEach(cleanup);

async function renderPage() {
  const result = render(<SettingsPage />);
  await waitFor(() =>
    expect(screen.getByLabelText("전달 방식")).toBeTruthy(),
  );
  return result;
}

describe("대용량 인용발명 전달 방식", () => {
  it("선택지가 auto / full / retrieval 셋뿐이다", async () => {
    await renderPage();
    const select = screen.getByLabelText("전달 방식") as HTMLSelectElement;
    const values = [...select.options].map((option) => option.value);
    expect(values).toEqual(["auto", "full", "retrieval"]);
    // 폐기한 값이 살아 있으면 사용자가 저장할 수 없는 값을 고를 수 있다.
    expect(values).not.toContain("focused");
  });

  it("전달 카드가 한 벌만 있다", async () => {
    await renderPage();
    expect(screen.getAllByText("대용량 인용발명 전달 방식")).toHaveLength(1);
    expect(screen.getAllByLabelText("전달 방식")).toHaveLength(1);
  });

  it("두 한도를 각자 다른 축으로 설명한다", async () => {
    const { container } = await renderPage();
    const text = container.textContent ?? "";
    // 전송 하드 한도: 사용자가 끌 수 없다.
    expect(text).toContain("180,000 bytes");
    expect(text).toContain("사용자가 끌 수 없고");
    // 모델 컨텍스트: 별도 절에서 설명하고, 추측하지 않는다고 밝힌다.
    expect(text).toContain("모델 컨텍스트 입력 예산");
    expect(text).toContain("모델 한도를 추측하지 않습니다");
    // 사건 규모 기준: 전송 한도가 아니라고 못박는다.
    expect(text).toContain("전송 한도가 아닙니다");
  });

  it("폐기한 설정이 화면에 남아 있지 않다", async () => {
    const { container } = await renderPage();
    const text = container.textContent ?? "";
    expect(text).not.toContain("auto 전환 크기");
    expect(text).not.toContain("페이지 단위 전달 최대 문자 수");
    expect(text).not.toContain("대형 사건 기준");
  });

  it("「0 = 사용 안 함」 안내가 붙은 값은 실제로 0 을 담고 있다", async () => {
    await renderPage();
    // 백엔드가 0 을 거절하면서 화면만 0 을 안내하던 결함의 회귀.
    for (const label of [
      "문헌 수 기준 (0 = 사용 안 함)",
      "총 페이지 수 기준 (0 = 사용 안 함)",
      "청구항 구성 수 기준 (0 = 사용 안 함)",
    ]) {
      const field = screen.getByLabelText(label) as HTMLInputElement;
      expect(field.value).toBe("0");
    }
  });

  it("새 설정이 실제 값으로 그려진다", async () => {
    await renderPage();
    expect(
      (screen.getByLabelText("근거 페이지 앞뒤로 더 담을 페이지 수") as HTMLInputElement)
        .value,
    ).toBe("1");
    expect(
      (screen.getByLabelText("임베딩 캐시 상한 (MB, 0 = 정리 안 함)") as HTMLInputElement)
        .value,
    ).toBe("512");
    expect(
      (screen.getByLabelText("출력·추론 예약 토큰") as HTMLInputElement).value,
    ).toBe("32000");
  });

  it("등록된 모델 한도가 없으면 대체값을 쓴다고 알린다", async () => {
    const { container } = await renderPage();
    expect(container.textContent).toContain("없음 (전부 대체값 사용)");
  });

  it("내부 실행 한도는 설정 화면에 노출하지 않는다", async () => {
    const { container } = await renderPage();
    const text = container.textContent ?? "";
    expect(text).not.toContain("실행 한도");
    expect(text).not.toContain("파일 1개 최대 크기");
    expect(text).not.toContain("실행 제한 시간 (초)");
    expect(text).not.toContain("검색 1회당 최대 도구 호출 수");
    expect(text).not.toContain("raw stdout/stderr 를 파일로 보존");
  });

  it("특허 연동 카드를 전체 폭 대상으로 표시하고 설명을 간결하게 유지한다", async () => {
    const { container } = await renderPage();
    expect(container.querySelector(".settings-kiwee")).toBeTruthy();
    expect(container.querySelector(".settings-epo")).toBeTruthy();
    expect(container.textContent).toContain(
      "Kiwee 특허 DB를 유사문헌 검색 경로에 추가합니다.",
    );
    expect(container.textContent).toContain(
      "EPO OPS API로 특허를 검색하고 받은 XML과 결과를 대조합니다.",
    );
  });
});
