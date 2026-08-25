import type { AnalysisComponent, Job } from "../lib/types";

const STATUS_LABEL: Record<string, string> = {
  below_threshold: "80% 미만",
  not_found: "대응 문헌 미발견",
};

interface Props {
  job: Job;
  components: AnalysisComponent[];
  selectedIds: string[];
  providerLabel: string;
  searchAvailable: boolean;
  submitting: boolean;
  onSelectionChange: (ids: string[]) => void;
  onRun: () => void;
  onClose: () => void;
}

export default function GapSearchPanel({
  job,
  components,
  selectedIds,
  providerLabel,
  searchAvailable,
  submitting,
  onSelectionChange,
  onRun,
  onClose,
}: Props) {
  const selected = new Set(selectedIds);
  const toggle = (id: string) => {
    onSelectionChange(
      selected.has(id)
        ? selectedIds.filter((item) => item !== id)
        : [...selectedIds, id],
    );
  };

  return (
    <section className="gap-search-panel no-print" aria-label="미대응 구성 검색 설정">
      <div className="split">
        <div>
          <h3>미대응 구성 보완 검색</h3>
          <p className="faint">
            검색할 구성을 확인하십시오. 원 보고서와 인용 발췌문은 검색 모델에
            전달하지 않고, 원 청구항과 아래 선택 구성·차이점만 전달합니다.
          </p>
        </div>
        <button type="button" className="btn small" onClick={onClose}>
          닫기
        </button>
      </div>

      <div className="notice info gap-search-order">
        <strong>검색 순서</strong>
        <ol>
          <li>
            <strong>1차 조합 검색</strong> — 선택 구성이 하나의 문헌에 함께 개시된
            발명을 먼저 찾습니다.
          </li>
          <li>
            <strong>2차 개별 검색</strong> — 선택한 각 구성을 개별 검색식으로
            넓혀 찾습니다.
          </li>
        </ol>
      </div>

      <div className="btn-row gap-search-select-actions">
        <button
          type="button"
          className="btn small"
          onClick={() => onSelectionChange(components.map((item) => item.id))}
        >
          전체 선택
        </button>
        <button
          type="button"
          className="btn small"
          onClick={() => onSelectionChange([])}
        >
          선택 해제
        </button>
        <span className="faint">
          {components.length}개 중 {selectedIds.length}개 선택 · 기준 {job.analysis_manifest?.threshold ?? 80}%
          미만
        </span>
      </div>

      <div className="gap-component-list">
        {components.map((item) => (
          <label key={item.id} className="gap-component-item">
            <input
              type="checkbox"
              checked={selected.has(item.id)}
              onChange={() => toggle(item.id)}
            />
            <span className="gap-component-body">
              <span className="gap-component-head">
                <strong>
                  {item.claim} {item.symbol}
                </strong>
                <span className="pill warn">
                  {item.similarity === null
                    ? STATUS_LABEL[item.status] ?? item.status
                    : `${item.similarity}%`}
                </span>
              </span>
              <span>{item.feature}</span>
              {item.difference && (
                <span className="faint">검색할 차이: {item.difference}</span>
              )}
            </span>
          </label>
        ))}
      </div>

      {!searchAvailable && (
        <div className="notice danger">
          <strong>{providerLabel || "현재 실행 도구"}로는 웹 검색할 수 없습니다.</strong>
          <div>
            <a href="#/settings">환경 설정</a>에서 웹 검색을 지원하는 실행 도구를
            기본값으로 지정한 뒤 다시 시도하십시오.
          </div>
        </div>
      )}

      <div className="btn-row">
        <button
          type="button"
          className="btn primary"
          onClick={onRun}
          disabled={!searchAvailable || selectedIds.length === 0 || submitting}
        >
          {submitting ? "검색 작업 생성 중…" : "선택 구성으로 웹 검색"}
        </button>
        <span className="faint">검색 실행 도구: {providerLabel || "설정 필요"}</span>
      </div>
    </section>
  );
}
