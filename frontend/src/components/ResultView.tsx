import { useEffect, useMemo, useRef, useState } from "react";

import { downloadUrl } from "../lib/api";
import { hardenLinks, renderMarkdown } from "../lib/markdown";

interface Props {
  jobId: string;
  text: string;
  outputMode: "markdown" | "text";
  streaming?: boolean;
}

export default function ResultView({ jobId, text, outputMode, streaming }: Props) {
  const [showRaw, setShowRaw] = useState(outputMode === "text");
  const [copied, setCopied] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const html = useMemo(
    () => (outputMode === "markdown" && !showRaw ? renderMarkdown(text) : ""),
    [text, outputMode, showRaw],
  );

  useEffect(() => {
    hardenLinks(ref.current);
  }, [html]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div>
      <div className="split no-print" style={{ marginBottom: 12 }}>
        <div className="btn-row">
          {outputMode === "markdown" && (
            <button className="btn small" onClick={() => setShowRaw((v) => !v)}>
              {showRaw ? "서식 보기" : "원문 보기"}
            </button>
          )}
          <button className="btn small" onClick={copy} disabled={!text}>
            {copied ? "복사됨" : "복사"}
          </button>
        </div>
        <div className="btn-row">
          <a className="btn small" href={downloadUrl(jobId, "md")}>
            Markdown
          </a>
          <a className="btn small" href={downloadUrl(jobId, "txt")}>
            TXT
          </a>
          <a className="btn small" href={downloadUrl(jobId, "json")}>
            JSON
          </a>
          <button className="btn small" onClick={() => window.print()}>
            인쇄 / PDF
          </button>
        </div>
      </div>

      {showRaw || outputMode === "text" ? (
        <div className="result-raw">{text || "(결과 없음)"}</div>
      ) : (
        <div
          className="result"
          ref={ref}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}

      {streaming && (
        <p className="faint no-print" style={{ marginTop: 8 }}>
          <span className="spinner" /> 결과 수신 중…
        </p>
      )}
    </div>
  );
}
