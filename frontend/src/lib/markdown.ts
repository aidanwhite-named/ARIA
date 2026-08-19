import DOMPurify from "dompurify";
import { marked } from "marked";

/**
 * 모델 출력은 끝까지 비신뢰 데이터다.
 *
 * 첨부 문서 안에 프롬프트 인젝션이 들어 있었다면 그 영향이 출력에 남을 수
 * 있다. 런타임 컨텍스트의 방어 문구는 완화책이지 보안 경계가 아니므로,
 * 렌더링 단계에서 반드시 sanitize 한다.
 */
marked.setOptions({ gfm: true, breaks: false });

const ALLOWED_TAGS = [
  "h1", "h2", "h3", "h4", "h5", "h6",
  "p", "br", "hr",
  "ul", "ol", "li",
  "strong", "em", "del", "code", "pre",
  "blockquote",
  "table", "thead", "tbody", "tr", "th", "td",
  "a", "span",
];

export function renderMarkdown(source: string): string {
  const html = marked.parse(source ?? "", { async: false }) as string;
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR: ["href", "title", "colspan", "rowspan", "align"],
    // javascript:, data: 등 위험한 스킴 차단
    ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form", "input"],
    FORBID_ATTR: ["style", "onerror", "onload", "onclick"],
  });
}

/** 외부 링크가 새 탭에서 열리도록 후처리한다. */
export function hardenLinks(container: HTMLElement | null): void {
  if (!container) return;
  container.querySelectorAll("a[href]").forEach((node) => {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer nofollow");
  });
}
