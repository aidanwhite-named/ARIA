"""로컬 Agentic Retrieval.

대용량 인용발명 PDF 를 프롬프트에 통째로 넣지 않고, ARIA 가 페이지·문단 단위로
로컬 색인한 뒤 AI 가 구조화된 action 으로 검색하게 한다. 설계 근거와 라이브러리
선택 이유는 docs/adr-0001-local-retrieval.md 에 있다.

바깥에서 쓰는 것은 이 파일이 다시 내보내는 이름들뿐이다.
"""

from .agent import RetrievalBudget
from .evidence import (
    BUNDLE_STATUSES,
    PLACEHOLDER_KEY,
    STATUS_COVERAGE,
    STATUS_MATCHED,
    STATUS_NOT_FOUND_SCOPE,
    STATUS_UNREADABLE,
    STATUS_VISUAL_REVIEW,
    render,
    render_placeholder,
)
from .index import IndexUnavailable, probe_sqlite
from .prompts import NOT_FOUND_PHRASE
from .service import (
    RETRIEVAL_DIRNAME,
    RetrievalResult,
    budget_from_settings,
    build_corpus,
    close_documents,
    extraction_report,
    run_retrieval,
)
from .versions import EXTRACTOR_VERSION, INDEX_VERSION, library_versions

__all__ = [
    "BUNDLE_STATUSES",
    "EXTRACTOR_VERSION",
    "INDEX_VERSION",
    "IndexUnavailable",
    "NOT_FOUND_PHRASE",
    "PLACEHOLDER_KEY",
    "RETRIEVAL_DIRNAME",
    "RetrievalBudget",
    "RetrievalResult",
    "STATUS_COVERAGE",
    "STATUS_MATCHED",
    "STATUS_NOT_FOUND_SCOPE",
    "STATUS_UNREADABLE",
    "STATUS_VISUAL_REVIEW",
    "budget_from_settings",
    "build_corpus",
    "close_documents",
    "extraction_report",
    "library_versions",
    "probe_sqlite",
    "render",
    "render_placeholder",
    "run_retrieval",
]
