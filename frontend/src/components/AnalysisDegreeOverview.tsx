import type { AnalysisComponent } from "../lib/types";

type DegreeTone = "exact" | "substantial" | "partial" | "none" | "unavailable";

export interface AnalysisDegree {
  icon: string;
  label: string;
  tone: DegreeTone;
  score: string;
}

/**
 * 모델의 연속 점수는 그대로 보존하되, 사람이 훑어볼 상태는 다섯 단계로 줄인다.
 * 80%는 미대응 구성 보완 검색의 경계와 같아야 한다.
 */
export function analysisDegree(item: AnalysisComponent): AnalysisDegree {
  if (item.status === "not_found") {
    return { icon: "×", label: "대응 없음", tone: "none", score: "0%" };
  }
  if (item.status === "unreadable" || item.similarity === null) {
    return {
      icon: "?",
      label: "판단 불가",
      tone: "unavailable",
      score: "유사도 없음",
    };
  }
  if (item.similarity === 0) {
    return { icon: "×", label: "대응 없음", tone: "none", score: "0%" };
  }
  if (item.similarity >= 95) {
    return {
      icon: "≡",
      label: "동일 대응",
      tone: "exact",
      score: `${item.similarity}%`,
    };
  }
  if (item.similarity >= 80) {
    return {
      icon: "●",
      label: "실질 대응",
      tone: "substantial",
      score: `${item.similarity}%`,
    };
  }
  return {
    icon: "≈",
    label: "부분 대응",
    tone: "partial",
    score: `${item.similarity}%`,
  };
}

interface Props {
  components: AnalysisComponent[];
}

export default function AnalysisDegreeOverview({ components }: Props) {
  if (components.length === 0) return null;

  return (
    <section className="analysis-degree-overview" aria-labelledby="analysis-degree-title">
      <div className="analysis-degree-title-row">
        <div>
          <h3 id="analysis-degree-title">구성별 대응 정도</h3>
          <p>상태가 핵심 판단이며, 유사도는 기술적 대응 정도를 보조하는 수치입니다.</p>
        </div>
      </div>

      <ul className="analysis-degree-grid">
        {components.map((item) => {
          const degree = analysisDegree(item);
          return (
            <li
              key={item.id}
              className={`analysis-degree-item degree-${degree.tone}`}
            >
              <div className="analysis-degree-item-head">
                <strong>
                  {item.claim} {item.symbol}
                </strong>
                <span className={`analysis-degree-badge degree-${degree.tone}`}>
                  <span aria-hidden="true">{degree.icon}</span>
                  {degree.label}
                </span>
                <span className="analysis-degree-score">{degree.score}</span>
              </div>
              <div className="analysis-degree-feature" title={item.feature}>
                {item.feature}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
