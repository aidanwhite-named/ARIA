import DOMPurify from "dompurify";
import { marked, type TokenizerAndRendererExtension } from "marked";

/**
 * 모델 출력은 끝까지 비신뢰 데이터다.
 *
 * 첨부 문서 안에 프롬프트 인젝션이 들어 있었다면 그 영향이 출력에 남을 수
 * 있다. 런타임 컨텍스트의 방어 문구는 완화책이지 보안 경계가 아니므로,
 * 렌더링 단계에서 반드시 sanitize 한다.
 */
marked.setOptions({ gfm: true, breaks: false });

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => HTML_ESCAPES[character]);
}

// $$…$$ 는 여러 줄을 허용한다. 인라인 $…$ 는 통화 표기를 삼키지 않도록 좁게
// 잡는다. 여는 $ 뒤와 닫는 $ 앞에 공백이 없어야 하고, 닫는 $ 뒤에 숫자가
// 오면 안 된다. 그래서 "$100 and $200" 은 수식으로 잡히지 않는다.
const DISPLAY_MATH = /^\$\$[\s\S]+?\$\$/;
const INLINE_MATH = /^\$(?![\s$])[^\n$]*[^\s$]\$(?!\d)/;

/**
 * 수식 구간은 마크다운 문법으로 해석하지 않고 원문 그대로 둔다.
 *
 * 인용발명 문헌에서 옮겨 온 LaTeX 는 마크다운과 문법이 겹친다.
 * `$\{I_i\}_{i=1}^N$` 를 그냥 통과시키면 `\{` 의 백슬래시가 이스케이프로
 * 소비되고 `}_{` 의 밑줄 두 개가 이탤릭 한 쌍으로 묶여서, 화면과 인쇄물에는
 * `${I_i}{i=1}^N$` 만 남는다. 인용발명의 인용문이 조용히 바뀌는 것이므로
 * 표시상의 문제로 넘길 수 없다.
 *
 * 수식을 렌더링하지는 않는다. 원문 문자를 그대로 보존할 뿐이다. 그래서 경계를
 * 잘못 잡아도 결과 텍스트는 입력과 같고, 그 구간의 마크다운 서식만 생략된다.
 */
const mathSource: TokenizerAndRendererExtension = {
  name: "mathSource",
  level: "inline",
  start(src) {
    return src.indexOf("$");
  },
  tokenizer(src) {
    const match = DISPLAY_MATH.exec(src) ?? INLINE_MATH.exec(src);
    if (!match) return undefined;
    return { type: "mathSource", raw: match[0], text: match[0] };
  },
  renderer(token) {
    return escapeHtml(token.text);
  },
};

marked.use({ extensions: [mathSource] });

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
