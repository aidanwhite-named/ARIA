import { useEffect, useMemo, useRef } from "react";

import { hardenLinks, renderMarkdown } from "../lib/markdown";

interface Props {
  text: string;
  outputMode: "markdown" | "text";
  streaming?: boolean;
}

export default function ResultView({ text, outputMode, streaming }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  const html = useMemo(
    () => (outputMode === "markdown" ? renderMarkdown(text) : ""),
    [text, outputMode],
  );

  useEffect(() => {
    hardenLinks(ref.current);
  }, [html]);

  return (
    <div>
      {outputMode === "text" ? (
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
