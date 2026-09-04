"""로컬 Agentic Retrieval 의 색인·검색·근거 패키지 계층.

실제 CLI 를 부르지 않는다. Provider 호출이 필요한 경로는 tests/fake_provider 의
결정론적 대역을 쓰고, 여기서는 그 아래 계층(추출·청킹·인덱스·채널·게이트)을
직접 검증한다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import retrieval
from app.enums import AttachmentRole, DeliveryMode, ExtractionMethod
from app.ingestion.service import IngestedFile
from app.retrieval import chunking, evidence, extraction, index as index_module, search
from app.retrieval.agent import RetrievalBudget

from .fake_provider import DeterministicTestProvider, _round_payload
from .pdf_fixture import build_korean_pdf, build_pdf, build_scanned_like_pdf

# ------------------------------------------------------------------ 도우미

KOREAN_PAGES = [
    "[0001] 본 발명은 압력센서를 이용한 제어 장치에 관한 것이다.\n- 1 -",
    "[0015] 제1 센서와 제2 센서가 하우징에 결합되고, 도면부호 110 으로 표시된다.\n- 2 -",
    "[0032] 두 센서의 신호를 결합하여 제어부가 5V 로 구동한다.\n- 3 -",
    "[0048] 마지막 페이지에만 있는 고유문구 크로스체크지표 를 기재한다.\n- 4 -",
]


def _pdf_attachment(tmp_path, name: str, pages: list[str], sha: str = "sha-1"):
    """PDF 를 실제 파일로 쓰고 IngestedFile 을 만든다."""
    data = build_korean_pdf(pages)
    target = tmp_path / "input" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return IngestedFile(
        attachment_id=name.replace(".pdf", ""),
        original_filename=name,
        internal_filename=name,
        mime_type="application/pdf",
        size_bytes=len(data),
        sha256=sha,
        required=True,
        stored_path=str(target),
        role=AttachmentRole.CITATION,
        page_count=len(pages),
        char_count=sum(len(p) for p in pages),
        extraction_method=ExtractionMethod.PDF_TEXT_LAYER,
        delivery_mode=DeliveryMode.INLINE_CONTEXT,
        read_ok=True,
    )


def _corpus(tmp_path, items):
    documents, skipped = retrieval.build_corpus(items, tmp_path)
    return documents, skipped


# ------------------------------------------------------- 실행 환경 능력 확인


def test_sqlite_capabilities_are_probed_at_runtime() -> None:
    """FTS5/trigram 지원 여부를 가정하지 않고 실제로 만들어 본다."""
    caps = retrieval.probe_sqlite()
    assert caps.fts5 is True, caps.error
    assert caps.sqlite_version
    # trigram 이 없어도 실행은 가능해야 하지만, 그 사실이 기록에 남아야 한다.
    assert isinstance(caps.trigram, bool)
    assert "trigram" in caps.to_dict()


# ------------------------------------------------------------- 추출·완전성


def test_extraction_report_records_empty_and_failed_pages(tmp_path) -> None:
    """6. 빈 페이지와 추출 실패가 완전성 보고서에 기록된다."""
    data = build_scanned_like_pdf(3)
    target = tmp_path / "scan.pdf"
    target.write_bytes(data)

    result = extraction.extract_document(
        target, attachment_id="a", filename="scan.pdf", sha256="s"
    )
    report = result.report()

    assert report["source_page_count"] == 3
    assert report["processed_page_count"] == 3
    assert report["page_count_mismatch"] is False
    assert report["empty_or_low_text_pages"] == [1, 2, 3]
    assert report["ok_pages"] == 0
    assert report["status"] == extraction.DOC_UNUSABLE
    # OCR 을 했다는 표시는 어디에도 없어야 한다.
    assert "ocr" not in json.dumps(report).lower()


def test_extraction_report_marks_mixed_document_review_required(tmp_path) -> None:
    data = build_pdf(["Normal page with enough text to pass.", " "])
    target = tmp_path / "mixed.pdf"
    target.write_bytes(data)

    report = extraction.extract_document(
        target, attachment_id="a", filename="mixed.pdf", sha256="s"
    ).report()

    assert report["ok_pages"] == 1
    assert report["empty_or_low_text_pages"] == [2]
    assert report["status"] == extraction.DOC_REVIEW


def test_unopenable_pdf_is_reported_not_silently_empty(tmp_path) -> None:
    target = tmp_path / "broken.pdf"
    target.write_bytes(b"%PDF-1.4\nnot really a pdf")
    result = extraction.extract_document(
        target, attachment_id="a", filename="broken.pdf", sha256="s"
    )
    report = result.report()
    assert report["status"] == extraction.DOC_UNUSABLE
    assert report["open_error"]


def test_printed_page_number_is_detected() -> None:
    assert extraction.detect_printed_page("본문\n- 12 -") == "12"
    assert extraction.detect_printed_page("7 / 40\n본문") == "7"
    assert extraction.detect_printed_page("본문만 있고 번호는 없다") is None


# ------------------------------------------------------------------ 청킹


def test_chunks_never_cross_page_boundaries(tmp_path) -> None:
    """4. 문단번호와 PDF 페이지가 보존된다. 청크는 페이지를 넘지 않는다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = extraction.extract_document(
        tmp_path / "input" / "doc.pdf",
        attachment_id="doc",
        filename="doc.pdf",
        sha256="s",
    )
    chunks = chunking.chunk_document(result)

    assert chunks
    for chunk in chunks:
        assert 1 <= chunk.page_number <= len(KOREAN_PAGES)
    paragraphs = {chunk.paragraph for chunk in chunks if chunk.paragraph}
    assert {"[0001]", "[0015]", "[0032]", "[0048]"} <= paragraphs
    # 문단번호와 페이지가 짝을 이룬다.
    by_paragraph = {c.paragraph: c.page_number for c in chunks if c.paragraph}
    assert by_paragraph["[0001]"] == 1
    assert by_paragraph["[0048]"] == 4
    assert item.page_count == 4


def test_long_paragraph_is_split_with_overlap_only_when_needed() -> None:
    short = "가" * 100
    assert chunking.split_page(short) == [short]

    long_block = "나" * (chunking.MAX_CHUNK_CHARS * 2 + 50)
    pieces = chunking.split_page(long_block)
    assert len(pieces) > 1
    assert all(len(piece) <= chunking.MAX_CHUNK_CHARS for piece in pieces)


# ------------------------------------------------------------------ 검색


def test_exact_phrase_on_last_page_is_found(tmp_path) -> None:
    """2. 문서 마지막 페이지의 고유 문구를 정확히 검색한다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    documents, _ = _corpus(tmp_path, [item])
    result = documents[0].index.search_phrase(["크로스체크지표"])

    assert result.executed
    assert [row.page_number for row in result.rows] == [4]
    assert result.rows[0].paragraph == "[0048]"
    retrieval.close_documents(documents)


def test_korean_substring_survives_compounds_and_particles(tmp_path) -> None:
    """3. 합성어("압력센서")와 조사("센서를") 차이에서도 후보를 찾는다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    documents, _ = _corpus(tmp_path, [item])
    document = documents[0]

    # 2자 검색어. trigram 은 3자 미만을 색인하지 못하므로 부분문자 채널이 맡는다.
    found = search.search_document(document, queries=["센서"], limit=10)
    pages = {hit.row.page_number for hit in found.hits}
    # 1쪽의 "압력센서를"(합성어 + 조사), 2·3쪽의 "센서와"/"센서의"가 모두 걸린다.
    assert {1, 2, 3} <= pages
    channels = {channel for hit in found.hits for channel in hit.channels}
    assert search.CHANNEL_SUBSTRING in channels

    # 3자 이상이면 trigram 채널도 실제로 실행된다.
    trigram = document.index.search_trigram(["압력센서"])
    assert trigram.executed
    assert [row.page_number for row in trigram.rows] == [1]

    # 접두 질의(BM25)가 조사 차이를 흡수한다.
    bm25 = document.index.search_bm25(["제어"])
    assert bm25.executed and bm25.rows
    retrieval.close_documents(documents)


def test_numbers_and_symbols_channel(tmp_path) -> None:
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    documents, _ = _corpus(tmp_path, [item])
    document = documents[0]

    figure = document.index.search_literal(["110"])
    assert [row.page_number for row in figure.rows] == [2]

    voltage = document.index.search_literal(["5V"])
    assert [row.page_number for row in voltage.rows] == [3]
    retrieval.close_documents(documents)


def test_trigram_absence_is_reported_not_silent(tmp_path, monkeypatch) -> None:
    """trigram 이 없는 환경에서도 실패가 아니라 '수행하지 않음'으로 남는다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    caps = index_module.SqliteCapabilities(
        fts5=True, trigram=False, sqlite_version="3.0.0"
    )
    documents, _ = retrieval.build_corpus(tmp_path and [item], tmp_path, capabilities=caps)
    result = documents[0].index.search_trigram(["압력센서"])
    assert result.executed is False
    assert "trigram" in result.skipped_reason
    retrieval.close_documents(documents)


def test_no_document_monopolizes_results(tmp_path) -> None:
    """5. 여러 인용문헌이 있을 때 한 문헌이 전역 top-k 를 독점하지 않는다."""
    packed = ["[%04d] 센서 신호를 제어한다. 반복 %d." % (i, i) for i in range(1, 41)]
    items = [
        _pdf_attachment(tmp_path, "big.pdf", packed, sha="s-big"),
        _pdf_attachment(tmp_path, "small1.pdf", KOREAN_PAGES, sha="s-1"),
        _pdf_attachment(tmp_path, "small2.pdf", KOREAN_PAGES, sha="s-2"),
    ]
    documents, _ = _corpus(tmp_path, items)

    results = search.search_corpus(documents, queries=["센서"], per_document_limit=4)
    assert len(results) == 3
    for result in results:
        assert len(result.hits) >= search.MIN_HITS_PER_DOCUMENT

    merged = search.interleave(results, total_limit=6)
    aliases = {hit.alias for hit in merged}
    # 상한이 작아도 세 문헌이 모두 남는다.
    assert len(aliases) == 3
    retrieval.close_documents(documents)


def test_rrf_fusion_prefers_multi_channel_hits(tmp_path) -> None:
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    documents, _ = _corpus(tmp_path, [item])
    found = search.search_document(
        documents[0], queries=["제어"], phrases=["신호를 결합하여"], limit=5
    )
    top = found.hits[0]
    # 두 채널이 함께 올린 구간이 앞에 온다.
    assert len(top.channels) >= 2
    assert top.row.page_number == 3
    retrieval.close_documents(documents)


# ------------------------------------------------------------- 인덱스 재사용


def test_index_is_reused_when_hash_and_versions_match(tmp_path) -> None:
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES, sha="abc")
    first, _ = _corpus(tmp_path, [item])
    assert first[0].rebuilt is True
    retrieval.close_documents(first)

    second, _ = _corpus(tmp_path, [item])
    assert second[0].rebuilt is False
    retrieval.close_documents(second)


def test_index_is_rebuilt_when_pdf_sha256_changes(tmp_path) -> None:
    """8. PDF sha256 이 바뀌면 인덱스를 재생성한다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES, sha="abc")
    first, _ = _corpus(tmp_path, [item])
    retrieval.close_documents(first)

    changed = _pdf_attachment(
        tmp_path,
        "doc.pdf",
        [*KOREAN_PAGES, "[0060] 새로 추가된 페이지의 신규문구 이다.\n- 5 -"],
        sha="def",
    )
    second, _ = _corpus(tmp_path, [changed])
    assert second[0].rebuilt is True
    assert second[0].index.sha256 == "def"
    found = second[0].index.search_phrase(["신규문구"])
    assert [row.page_number for row in found.rows] == [5]
    retrieval.close_documents(second)


def test_index_is_rebuilt_when_index_version_changes(tmp_path, monkeypatch) -> None:
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES, sha="abc")
    first, _ = _corpus(tmp_path, [item])
    retrieval.close_documents(first)

    monkeypatch.setattr(index_module, "INDEX_VERSION", 999)
    second, _ = _corpus(tmp_path, [item])
    assert second[0].rebuilt is True
    retrieval.close_documents(second)


# ------------------------------------------------- 역할 분리와 별칭 일치


def test_application_document_is_never_a_search_target(tmp_path) -> None:
    """출원발명 문서를 인용발명처럼 검색하면 자기 발명이 근거가 된다.

    구성대비에서 이보다 나쁜 오류는 없다. 색인 대상에서 빼고, 대신 본문은
    프롬프트에 그대로 남긴다.
    """
    citation = _pdf_attachment(tmp_path, "citation.pdf", KOREAN_PAGES, sha="s-cit")
    spec = _pdf_attachment(tmp_path, "spec.pdf", KOREAN_PAGES, sha="s-spec")
    spec.role = AttachmentRole.APPLICATION

    documents, skipped = _corpus(tmp_path, [citation, spec])
    assert [document.filename for document in documents] == ["citation.pdf"]
    assert skipped == []
    # 검색 인덱스 파일 자체가 만들어지지 않는다.
    index_dir = tmp_path / retrieval.RETRIEVAL_DIRNAME / "index"
    assert not (index_dir / f"{spec.attachment_id}.sqlite3").exists()
    retrieval.close_documents(documents)


def test_application_body_stays_inline_in_retrieval_mode(tmp_path) -> None:
    """검색 대상이 아니라고 본문까지 빼면 청구항 해석 기준이 사라진다."""
    from app.prompt_assembly import assemble

    citation = _pdf_attachment(tmp_path, "citation.pdf", KOREAN_PAGES, sha="s-cit")
    spec = _pdf_attachment(tmp_path, "spec.pdf", KOREAN_PAGES, sha="s-spec")
    spec.role = AttachmentRole.APPLICATION
    # 정규화 텍스트를 실제로 만들어 둔다. 인라인 전달은 이 파일에서 읽는다.
    for item, text in (
        (citation, "인용발명 전체 본문 고유표식 CITBODY"),
        (spec, "출원발명 명세서 전체 본문 고유표식 SPECBODY"),
    ):
        path = tmp_path / "normalized" / f"{item.attachment_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        item.normalized_text_path = str(path)

    assembled = assemble(
        master_prompt="마스터",
        attachments=[citation, spec],
        runtime_context="",
        runtime_context_enabled=False,
        max_chars=None,
        claim_text="청구항 1.",
        evidence_bundle={"documents": [], "components": []},
    )
    message = assembled.user_message
    # 출원발명 본문은 그대로 들어간다.
    assert "SPECBODY" in message
    # 인용발명 본문은 근거 패키지로 대체된다.
    assert "CITBODY" not in message


def test_corpus_aliases_match_the_final_prompt(tmp_path) -> None:
    """근거 패키지의 ATT-01 과 프롬프트 첨부 헤더의 ATT-01 은 같은 문헌이어야 한다.

    조립은 역할 순으로 정렬한 뒤 별칭을 붙인다. 검색 corpus 가 정렬 없이
    별칭을 붙이면 같은 실행 안에서 번호가 어긋나고, 모델은 그 사실을 알
    방법이 없다.
    """
    from app.prompt_assembly import assemble

    # 일부러 어긋난 순서로 넣는다: 기타 → 인용 → 출원.
    supplemental = _pdf_attachment(tmp_path, "misc.pdf", KOREAN_PAGES, sha="s-misc")
    supplemental.role = AttachmentRole.SUPPLEMENTAL
    citation = _pdf_attachment(tmp_path, "citation.pdf", KOREAN_PAGES, sha="s-cit")
    spec = _pdf_attachment(tmp_path, "spec.pdf", KOREAN_PAGES, sha="s-spec")
    spec.role = AttachmentRole.APPLICATION
    attachments = [supplemental, citation, spec]

    assembled = assemble(
        master_prompt="마스터",
        attachments=attachments,
        runtime_context="",
        runtime_context_enabled=False,
        max_chars=None,
        claim_text="청구항 1.",
        evidence_bundle={"documents": [], "components": []},
    )
    documents, _ = _corpus(tmp_path, attachments)

    for document in documents:
        assert (
            assembled.aliases[document.alias].attachment_id == document.attachment_id
        ), f"{document.alias} 가 서로 다른 문헌을 가리킵니다"
    # 출원발명이 ATT-01 을 가져가고 인용발명은 ATT-02 다.
    assert assembled.aliases["ATT-01"].original_filename == "spec.pdf"
    assert {document.alias for document in documents} == {"ATT-02", "ATT-03"}
    retrieval.close_documents(documents)


def test_partial_index_failure_stops_the_run(tmp_path, monkeypatch) -> None:
    """인용문헌 하나라도 색인하지 못하면 좁아진 검색으로 계속 가지 않는다."""
    from app.retrieval import service as service_module

    first = _pdf_attachment(tmp_path, "d1.pdf", KOREAN_PAGES, sha="s-1")
    second = _pdf_attachment(tmp_path, "d2.pdf", KOREAN_PAGES, sha="s-2")

    real = service_module.ensure_index
    calls = {"n": 0}

    def flaky(path, factory, *, sha256, capabilities=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("디스크 오류 시뮬레이션")
        return real(path, factory, sha256=sha256, capabilities=capabilities)

    monkeypatch.setattr(service_module, "ensure_index", flaky)
    result = _run_agent(tmp_path, [first, second], "청구항 1. 센서.")

    assert not result.ok
    assert result.error_code == "RETRIEVAL_UNAVAILABLE"
    assert "d2.pdf" in result.error
    assert result.bundle is None
    # 실패해도 어떤 문헌이 빠졌는지는 기록에 남는다.
    report = json.loads(
        (tmp_path / retrieval.RETRIEVAL_DIRNAME / "extraction_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["not_indexed"]


def test_excluded_attachment_is_never_indexed(tmp_path) -> None:
    """9. 분석에서 제외한 첨부는 검색 인덱스에도 결과에도 없다."""
    from app import job_assembly

    included = _pdf_attachment(tmp_path, "keep.pdf", KOREAN_PAGES, sha="s-keep")
    excluded = _pdf_attachment(tmp_path, "drop.pdf", KOREAN_PAGES, sha="s-drop")
    excluded.included = False

    selected = job_assembly.included_attachments([included, excluded])
    documents, _ = _corpus(tmp_path, selected)

    aliases = {document.filename for document in documents}
    assert aliases == {"keep.pdf"}
    # 제외한 자료의 인덱스 파일 자체가 만들어지지 않는다.
    index_dir = tmp_path / retrieval.RETRIEVAL_DIRNAME / "index"
    assert not (index_dir / "drop.sqlite3").exists()

    results = search.search_corpus(documents, queries=["센서"])
    assert all(result.document.filename == "keep.pdf" for result in results)
    retrieval.close_documents(documents)


# ------------------------------------------------------------- Agent 루프


def _run_agent(tmp_path, items, claim: str, budget: RetrievalBudget | None = None):
    return asyncio.run(
        retrieval.run_retrieval(
            job_id="job-test",
            provider=DeterministicTestProvider(),
            model=None,
            timeout_seconds=60,
            work_dir=tmp_path,
            attachments=items,
            claim_text=claim,
            budget=budget or RetrievalBudget(),
        )
    )


def test_agent_loop_builds_verified_evidence_bundle(tmp_path) -> None:
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(tmp_path, [item], "청구항 1. 센서와 제어부를 포함한다.")

    assert result.ok, result.error
    bundle = result.bundle
    assert bundle["delivery_mode"] == "local_retrieval"
    assert bundle["ocr_performed"] is False
    assert bundle["components"]

    component = bundle["components"][0]
    assert component["status"] == evidence.STATUS_MATCHED
    finding = component["findings"][0]
    # 원문은 ARIA 가 인덱스에서 꺼낸 값이고, AI 메모와 칸이 분리되어 있다.
    assert finding["source_text"]
    assert finding["ai_relevance"] == "테스트 관련성 메모"
    assert finding["pdf_page"] in {1, 2, 3, 4}
    assert finding["channels"]

    # 감사 자료가 실행 폴더에 남는다.
    base = tmp_path / retrieval.RETRIEVAL_DIRNAME
    for name in (
        "extraction_report.json",
        "retrieval_manifest.json",
        "retrieval_trace.jsonl",
        "evidence_bundle.json",
    ):
        assert (base / name).exists(), name
    trace = [
        json.loads(line)
        for line in (base / "retrieval_trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = {entry["type"] for entry in trace}
    assert {"start", "llm_input", "llm_output", "search", "finalize"} <= kinds
    # 각 LLM 단계의 입력/출력 해시가 남는다.
    for record in result.manifest["rounds"]:
        assert len(record["input_sha256"]) == 64
        assert len(record["output_sha256"]) == 64


def test_evidence_text_cannot_be_invented_by_the_model(tmp_path) -> None:
    """모델이 없는 chunk_id 를 근거로 대면 ARIA 가 거절한다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(tmp_path, [item], "청구항 1. RETRIEVAL_FAKETEXT 센서.")

    assert result.ok
    bundle = result.bundle
    assert bundle["rejected_evidence"]
    assert all(not c["findings"] for c in bundle["components"])
    assert all(
        c["status"] != evidence.STATUS_MATCHED for c in bundle["components"]
    )


def test_invalid_attachment_and_page_are_rejected(tmp_path) -> None:
    """10. 잘못된 attachment_id·페이지 요청이 거절된다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(
        tmp_path, [item], "청구항 1. RETRIEVAL_BADATT RETRIEVAL_BADPAGE 센서."
    )

    assert result.ok
    reasons = " ".join(entry["reason"] for entry in result.manifest["action_errors"])
    assert "ATT-99" in reasons
    # 범위 밖 페이지는 읽지 않고 구조화된 오류로 돌려준다.
    read_events = [
        json.loads(line)
        for line in (
            tmp_path / retrieval.RETRIEVAL_DIRNAME / "retrieval_trace.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if '"read"' in line
    ]
    assert read_events
    assert read_events[0]["payload"]["rejected"]
    assert read_events[0]["payload"]["pages"] == []


def test_single_action_cannot_exceed_the_round_budget(tmp_path) -> None:
    """action 하나가 라운드 반환 예산을 통째로 넘길 수 없어야 한다.

    상한을 action 사이에서만 재면, 문헌 20개를 한 번에 검색하는 action 하나가
    문헌 수 × 후보 수 × 스니펫 길이만큼을 만들어 낸다. 기본 설정에서도 예산의
    네 배가 넘고, 그 결과가 다음 라운드 프롬프트에 그대로 실린다.
    """
    from app.retrieval import agent as agent_module

    documents = [
        _pdf_attachment(tmp_path, f"d{n:02d}.pdf", KOREAN_PAGES, sha=f"s-{n}")
        for n in range(20)
    ]
    budget = RetrievalBudget(max_round_result_chars=4_000, hits_per_document=6)
    captured: list[int] = []

    original = agent_module.RetrievalAgent._execute_actions

    async def spy(self, items, run, round_no):
        results = await original(self, items, run, round_no)
        # 본문 텍스트가 아니라 **다음 라운드에 실제로 실릴 JSON 전체**를 잰다.
        # 본문만 세면 파일명·채널 기록·청크 메타데이터가 빠지고,
        # get_document_status 처럼 본문이 없는 action 은 0 으로 잡힌다.
        captured.append(sum(agent_module.json_size(entry) for entry in results))
        return results

    monkey = pytest.MonkeyPatch()
    monkey.setattr(agent_module.RetrievalAgent, "_execute_actions", spy)
    try:
        result = _run_agent(tmp_path, documents, "청구항 1. 센서.", budget=budget)
    finally:
        monkey.undo()

    assert result.ok
    assert captured, "action 이 한 번도 실행되지 않았습니다"
    for size in captured:
        assert size <= budget.max_round_result_chars, size


def test_document_status_action_is_charged_to_the_round_budget(tmp_path) -> None:
    """본문이 없는 action 도 반환 JSON 만큼 예산을 쓴다.

    문헌 상태 조회는 페이지 목록이 여럿 실려 20개짜리 조회 하나가 예산의 두
    배가 된다(실측 8,318자). 본문이 없다는 이유로 0 으로 두면 이 action 만
    반복해서 라운드 예산을 통째로 우회할 수 있다.
    """
    from app.retrieval import agent as agent_module
    from app.retrieval.actions import GetDocumentStatus

    items = [
        _pdf_attachment(tmp_path, f"d{n:02d}.pdf", KOREAN_PAGES, sha=f"s-{n}")
        for n in range(20)
    ]
    corpus, _ = _corpus(tmp_path, items)
    try:
        budget = RetrievalBudget(max_round_result_chars=4_000)
        agent = agent_module.RetrievalAgent(
            job_id="job-status",
            provider=DeterministicTestProvider(),
            model=None,
            timeout_seconds=60,
            work_dir=tmp_path,
            corpus=corpus,
            claim_text="청구항 1.",
            budget=budget,
            trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
        )
        # 예산을 다 준 경우: 20개를 다 담으면 예산을 넘으므로 일부만 온다.
        entry, consumed = agent._document_status(
            corpus, GetDocumentStatus.__name__, budget.max_round_result_chars
        )
        assert consumed == agent_module.json_size(entry)
        assert consumed <= budget.max_round_result_chars
        assert len(entry["documents"]) < len(corpus)
        assert entry["omitted_by_budget"]
    finally:
        retrieval.close_documents(corpus)


def test_componentless_deferred_status_does_not_block_finalize(tmp_path) -> None:
    """전역 문헌 상태 조회는 어떤 구성의 미검토도 뜻하지 않는다."""
    from app.retrieval import agent as agent_module
    from app.retrieval.actions import GetDocumentStatus

    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    corpus, _ = _corpus(tmp_path, [item])
    try:
        agent = agent_module.RetrievalAgent(
            job_id="job-deferred-status",
            provider=DeterministicTestProvider(),
            model=None,
            timeout_seconds=60,
            work_dir=tmp_path,
            corpus=corpus,
            claim_text="청구항 1.",
            budget=RetrievalBudget(),
            trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
        )
        agent._deferred_actions.append(
            agent_module.DeferredAction(
                item=GetDocumentStatus(
                    action="get_document_status", attachment="ATT-01"
                ),
                first_round=1,
                reason="반환 예산 부족",
            )
        )

        assert agent._has_blocking_deferred() is False
    finally:
        retrieval.close_documents(corpus)


def test_last_valid_finalize_is_kept_when_round_limit_is_reached(tmp_path, monkeypatch) -> None:
    """이월 action 때문에 보류된 유효 finalize 는 빈 패키지로 잃지 않는다."""
    from app.providers.base import ExecutionOutcome
    from app.retrieval import agent as agent_module
    from app.retrieval.actions import SearchDocument

    class FallbackProvider(DeterministicTestProvider):
        async def execute(self, request, emit):
            outcome = ExecutionOutcome(cli_path="(test)", cli_version="0")
            # 전송 JSON 은 공백 없는 compact 형식이다. 문자열 모양이 아니라
            # 되읽은 payload 로 라운드를 가린다.
            if _round_payload(request.user_message).get("round") == 1:
                response = {
                    "components": [
                        {
                            "label": "청구항 1 (A)",
                            "feature": "센서 구성",
                            "importance": "high",
                            "importance_reasons": ["핵심 구성"],
                            "depends_on": [],
                        }
                    ],
                    "actions": [],
                }
            else:
                response = {
                    "actions": [
                        {
                            "action": "finalize_evidence",
                            "components": [
                                {
                                    "component_id": "R001",
                                    "status_claim": "not_found",
                                    "searched_terms": [],
                                    "evidence": [],
                                    "note": "테스트 확정",
                                }
                            ],
                        }
                    ]
                }
            outcome.result_text = json.dumps(response, ensure_ascii=False)
            outcome.exit_code = 0
            outcome.terminal_reason = "completed"
            return outcome

    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    corpus, _ = _corpus(tmp_path, [item])
    try:
        agent = agent_module.RetrievalAgent(
            job_id="job-finalize-fallback",
            provider=FallbackProvider(),
            model=None,
            timeout_seconds=60,
            work_dir=tmp_path,
            corpus=corpus,
            claim_text="청구항 1.",
            budget=RetrievalBudget(max_rounds=2),
            trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
        )

        async def defer_component_action(items, run, round_no):
            if not agent._deferred_actions:
                agent._deferred_actions.append(
                    agent_module.DeferredAction(
                        item=SearchDocument(
                            action="search_document",
                            component_id="R001",
                            attachment="ATT-01",
                            queries=["센서"],
                        ),
                        first_round=round_no,
                        reason="반환 예산 부족",
                    )
                )
            return []

        monkeypatch.setattr(agent, "_execute_actions", defer_component_action)
        run = asyncio.run(agent.run())

        assert run.finalize is not None
        assert run.finalize.components[0].component_id == "R001"
        assert any("예산 소진 상태로 채택" in note for note in run.notes)
    finally:
        retrieval.close_documents(corpus)


def test_read_page_action_is_charged_to_the_round_budget(tmp_path) -> None:
    """페이지 본문뿐 아니라 read_page 반환 JSON 전체가 예산 안에 있어야 한다."""
    from app.retrieval import agent as agent_module
    from app.retrieval.actions import ReadPage

    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    corpus, _ = _corpus(tmp_path, [item])
    try:
        budget = RetrievalBudget(max_round_result_chars=400)
        agent = agent_module.RetrievalAgent(
            job_id="job-read-budget",
            provider=DeterministicTestProvider(),
            model=None,
            timeout_seconds=60,
            work_dir=tmp_path,
            corpus=corpus,
            claim_text="청구항 1.",
            budget=budget,
            trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
        )
        run = agent_module.RetrievalRun()
        entry, consumed = asyncio.run(
            agent._read(
                ReadPage(
                    action="read_page",
                    component_id="",
                    attachment="ATT-01",
                    page=1,
                ),
                corpus,
                run,
                round_no=1,
                budget_left=budget.max_round_result_chars,
            )
        )

        assert consumed == agent_module.json_size(entry)
        assert consumed <= budget.max_round_result_chars
        # 페이지가 통째로 들어가지 않으면 보지 않은 청크를 노출해서는 안 된다.
        if not entry["pages"]:
            assert not run.exposed_chunks
            assert entry["skipped_by_result_budget_count"] == 1
    finally:
        retrieval.close_documents(corpus)


def test_omitted_hits_are_reported_and_block_absent_verdict(tmp_path) -> None:
    """예산 때문에 보여주지 못한 후보가 있으면 「없음」을 확정하지 못한다."""
    documents = [
        _pdf_attachment(tmp_path, f"d{n:02d}.pdf", KOREAN_PAGES, sha=f"s-{n}")
        for n in range(12)
    ]
    result = _run_agent(
        tmp_path,
        documents,
        "청구항 1. RETRIEVAL_NOTFOUND 센서.",
        budget=RetrievalBudget(max_round_result_chars=1_200),
    )
    assert result.ok
    assert result.manifest["budget_exhausted"] is True
    for component in result.bundle["components"]:
        assert component["status"] != evidence.STATUS_NOT_FOUND_SCOPE


def test_unreturned_hits_are_not_exposed_chunks(tmp_path) -> None:
    """예산 때문에 만들지 않은 후보는 근거로 쓸 수 없어야 한다.

    반환하지 않은 구간을 exposed_chunks 에 넣으면, AI 가 보지 못한 청크가
    근거로 통과한다 — 노출 게이트가 무의미해진다.
    """
    documents = [
        _pdf_attachment(tmp_path, f"d{n:02d}.pdf", KOREAN_PAGES, sha=f"s-{n}")
        for n in range(12)
    ]
    result = _run_agent(
        tmp_path,
        documents,
        "청구항 1. 센서.",
        budget=RetrievalBudget(max_round_result_chars=1_200),
    )
    assert result.ok
    # 근거로 채택된 구간은 전부 실제로 반환된 것이다.
    for component in result.bundle["components"]:
        for finding in component["findings"]:
            assert finding["found_by_search"] is True


def test_round_budget_is_enforced(tmp_path) -> None:
    """11. 검색 라운드 예산이 강제된다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(
        tmp_path,
        [item],
        "청구항 1. RETRIEVAL_NOFINALIZE 센서.",
        budget=RetrievalBudget(max_rounds=3),
    )

    assert result.ok
    assert len(result.manifest["rounds"]) == 3
    assert result.manifest["budget_exhausted"] is True
    assert result.bundle["budget_exhausted"] is True
    # 예산이 다 됐으면 대응 없음을 확정하지 않는다.
    assert all(
        component["status"] != evidence.STATUS_NOT_FOUND_SCOPE
        for component in result.bundle["components"]
    )


def test_evidence_char_budget_is_a_hard_upper_bound(tmp_path) -> None:
    """11. 반환 문자 예산이 강제된다 — 넘긴 뒤가 아니라 넘기 전에 막는다.

    남은 자리가 있는지만 보고 청크를 통째로 얹으면 마지막 하나가 상한을 훌쩍
    넘고, preflight 가 안내한 최댓값이 상한이 아니게 된다.
    """
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    for budget_chars in (8_000, 12_000):
        result = _run_agent(
            tmp_path,
            [item],
            "청구항 1. 센서.",
            budget=RetrievalBudget(max_evidence_chars=budget_chars),
        )
        assert result.ok
        assert result.bundle["evidence_chars"] <= budget_chars


def test_parse_error_returns_structured_error_not_shell(tmp_path) -> None:
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(tmp_path, [item], "청구항 1. RETRIEVAL_BADJSON 센서.")

    statuses = [record["status"] for record in result.manifest["rounds"]]
    assert "parse_error" in statuses
    # 형식 오류 뒤에도 루프는 계속되고 근거를 만든다.
    assert result.ok


def test_tool_call_in_no_tools_round_fails_the_run(tmp_path) -> None:
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(tmp_path, [item], "청구항 1. RETRIEVAL_TOOL 센서.")

    assert not result.ok
    assert result.error_code == "TOOL_POLICY_VIOLATION"


def test_cancellation_stops_the_whole_loop(tmp_path) -> None:
    """12. 취소가 다단계 실행 전체를 중단한다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)

    async def scenario():
        return await retrieval.run_retrieval(
            job_id="job-cancel",
            provider=DeterministicTestProvider(),
            model=None,
            timeout_seconds=60,
            work_dir=tmp_path,
            attachments=[item],
            claim_text="청구항 1. 센서.",
            budget=RetrievalBudget(),
            is_cancelled=lambda: True,
        )

    result = asyncio.run(scenario())
    assert result.cancelled is True
    assert result.error_code == "CANCELLED"
    assert result.bundle is None
    # 취소해도 감사 기록은 남는다.
    assert (tmp_path / retrieval.RETRIEVAL_DIRNAME / "retrieval_manifest.json").exists()


# --------------------------------------------------------- 「없음」 판정 제한


def test_unseen_chunk_cannot_become_evidence(tmp_path) -> None:
    """AI 가 본 적 없는 실재 청크를 지목해도 근거가 되지 않는다.

    chunk_id 형식은 action 스키마에 노출돼 있어 추측이 쉽다. 원문이 실재한다는
    것만으로 통과시키면 "AI 는 원문을 지어낼 수 없다"는 주장이 성립하지 않는다.
    """
    from .fake_provider import UNSEEN_CHUNK_ID

    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(tmp_path, [item], "청구항 1. RETRIEVAL_UNSEEN 센서.")

    assert result.ok
    bundle = result.bundle
    # 그 청크는 인덱스에 실재한다.
    documents, _ = _corpus(tmp_path, [item])
    assert documents[0].index.chunk(UNSEEN_CHUNK_ID) is not None
    retrieval.close_documents(documents)

    # 그런데도 근거로 채택되지 않는다.
    assert all(not c["findings"] for c in bundle["components"])
    assert bundle["rejected_evidence"]
    assert any(
        "반환된 적이 없습니다" in item["reason"]
        for item in bundle["rejected_evidence"]
    )
    assert all(
        c["status"] != evidence.STATUS_MATCHED for c in bundle["components"]
    )


def test_not_found_requires_every_document_searched(tmp_path) -> None:
    """한 문헌만 검색하고 나머지를 건너뛰면 「미발견」을 확정하지 못한다."""
    first = _pdf_attachment(tmp_path, "d1.pdf", KOREAN_PAGES, sha="s-1")
    second = _pdf_attachment(tmp_path, "d2.pdf", KOREAN_PAGES, sha="s-2")
    result = _run_agent(
        tmp_path,
        [first, second],
        "청구항 1. RETRIEVAL_ONEDOC RETRIEVAL_NOTFOUND 없는구성.",
    )

    assert result.ok
    for component in result.bundle["components"]:
        assert component["status"] == evidence.STATUS_COVERAGE
        assert any(
            "검색을 실행한 기록이 없습니다" in reason
            for reason in component["status_reasons"]
        )
        # 어느 문헌을 검색했고 어느 문헌을 건너뛰었는지가 기록에 남는다.
        assert component["unsearched_documents"]
        assert len(component["searched_documents"]) == 1


def test_zero_hit_search_still_counts_as_searched(tmp_path) -> None:
    """결과가 0건인 검색도 「찾아봤다」로 기록되어야 한다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(tmp_path, [item], "청구항 1. RETRIEVAL_NOTFOUND 센서.")

    assert result.ok
    for component in result.bundle["components"]:
        records = component["searched_documents"]
        assert len(records) == 1
        assert records[0]["attachment"] == "ATT-01"
        assert len(records[0]["queries"]) >= 3
        assert component["unsearched_documents"] == []


def test_empty_component_declaration_fails_the_run(tmp_path) -> None:
    """구성 분해가 없으면 빈 근거 패키지로 분석을 진행하지 않는다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(
        tmp_path,
        [item],
        "청구항 1. RETRIEVAL_NOCOMPONENT 센서.",
        budget=RetrievalBudget(max_rounds=2),
    )

    assert not result.ok
    assert result.error_code == "RETRIEVAL_FAILED"
    assert result.bundle is None
    statuses = [record["status"] for record in result.manifest["rounds"]]
    assert statuses == ["no_components", "no_components"]


def test_incomplete_finalize_is_rejected(tmp_path) -> None:
    """선언한 구성을 빠뜨린 마무리 요청은 받아 주지 않는다.

    받아 주면 그 구성은 근거도 상태 사유도 없이 보고서에서 사라진다.
    """
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(
        tmp_path,
        [item],
        "청구항 1. RETRIEVAL_PARTIAL 센서.",
        budget=RetrievalBudget(max_rounds=3),
    )

    # 마무리가 계속 거절되므로 라운드 예산까지 돌고 끝난다.
    assert result.ok
    assert result.manifest["budget_exhausted"] is True
    reasons = " ".join(
        entry["reason"] for entry in result.manifest["action_errors"]
    )
    assert "빠진 구성" in reasons
    # 빠뜨린 구성도 보고서에 남아 있고, 확정되지 않은 상태다.
    ids = {c["component_id"] for c in result.bundle["components"]}
    assert ids == {"R001", "R002"}
    for component in result.bundle["components"]:
        assert component["status"] != evidence.STATUS_NOT_FOUND_SCOPE


def test_not_found_requires_expansion_search(tmp_path) -> None:
    """AI 가 확장 검색을 하지 않으면 not_found 를 확정하지 못한다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(
        tmp_path, [item], "청구항 1. RETRIEVAL_NOEXPAND RETRIEVAL_NOTFOUND 없는구성."
    )

    assert result.ok
    for component in result.bundle["components"]:
        assert component["status"] == evidence.STATUS_COVERAGE
        assert any("확장 검색" in reason for reason in component["status_reasons"])


def test_extraction_anomaly_blocks_not_found(tmp_path) -> None:
    """7. 추출 이상이 있으면 「문헌에 없음」 판정을 차단한다."""
    good = _pdf_attachment(tmp_path, "good.pdf", KOREAN_PAGES, sha="s-good")
    scan_bytes = build_scanned_like_pdf(2)
    scan_path = tmp_path / "input" / "scan.pdf"
    scan_path.parent.mkdir(parents=True, exist_ok=True)
    scan_path.write_bytes(scan_bytes)
    scanned = IngestedFile(
        attachment_id="scan",
        original_filename="scan.pdf",
        internal_filename="scan.pdf",
        mime_type="application/pdf",
        size_bytes=len(scan_bytes),
        sha256="s-scan",
        required=True,
        stored_path=str(scan_path),
        role=AttachmentRole.CITATION,
        page_count=2,
        char_count=0,
        extraction_method=ExtractionMethod.PDF_TEXT_LAYER,
        delivery_mode=DeliveryMode.INLINE_CONTEXT,
        read_ok=True,
    )

    result = _run_agent(
        tmp_path, [good, scanned], "청구항 1. RETRIEVAL_NOTFOUND 없는구성."
    )
    assert result.ok
    bundle = result.bundle
    assert bundle["coverage_blockers"]
    for component in bundle["components"]:
        assert component["status"] != evidence.STATUS_NOT_FOUND_SCOPE
        assert component["needs_original_review"] is True

    rendered = retrieval.render(bundle)
    assert "문헌에 없음" not in rendered
    assert retrieval.NOT_FOUND_PHRASE in rendered


def test_clean_document_can_reach_not_found_in_reviewed_scope(tmp_path) -> None:
    """추출이 완전하고 확장 검색까지 했으면 검토 범위 한정 표현이 나온다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(tmp_path, [item], "청구항 1. RETRIEVAL_NOTFOUND 센서.")

    assert result.ok
    statuses = {c["status"] for c in result.bundle["components"]}
    assert statuses == {evidence.STATUS_NOT_FOUND_SCOPE}
    rendered = retrieval.render(result.bundle)
    assert retrieval.NOT_FOUND_PHRASE in rendered


def test_render_never_says_absent_from_document(tmp_path) -> None:
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(tmp_path, [item], "청구항 1. 센서.")
    rendered = retrieval.render(result.bundle)
    assert "문헌에 없음" not in rendered
    assert "OCR 은 수행하지 않았습니다" in rendered


# ------------------------------------------------------------------ 예산 계산


def test_budget_from_settings_uses_one_calculation() -> None:
    """preflight 와 runner 가 같은 함수로 예산을 만든다."""
    budget = retrieval.budget_from_settings(
        {
            "retrieval_max_rounds": 4,
            "retrieval_max_page_reads": 12,
            "retrieval_evidence_chars": 5_000,
            "retrieval_hits_per_document": 3,
        }
    )
    assert budget.max_rounds == 4
    assert budget.max_page_reads == 12
    assert budget.max_evidence_chars == 5_000
    assert budget.hits_per_document == 3

    # 범위 밖 값은 잘려서 들어온다. 예산이 없는 상태로 도는 경로가 없다.
    clamped = retrieval.budget_from_settings({"retrieval_max_rounds": 9999})
    assert clamped.max_rounds == 30


def test_index_identity_mismatch_blocks_not_found(tmp_path) -> None:
    """인덱스 해시·버전이 어긋나면 그 문헌으로 「없음」을 확정하지 못한다.

    정상 경로에서는 ensure_index 가 다시 만들기 때문에 도달하지 않는다. 그래도
    검사하는 이유는, 이 검사를 통과하지 못한 인덱스로 만든 「없음」 판정이
    조용히 나가는 것이 이 기능에서 가장 나쁜 실패이기 때문이다.
    """
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES, sha="abc")
    documents, _ = _corpus(tmp_path, [item])
    document = documents[0]
    assert evidence.document_gate(document) == []

    document.sha256 = "0" * 64
    reasons = evidence.document_gate(document)
    assert any("PDF 해시" in reason for reason in reasons)
    retrieval.close_documents(documents)


def test_bundle_carries_document_identity_excerpt(tmp_path) -> None:
    """문헌 매핑 계약이 로컬 검색에서도 깨지지 않아야 한다.

    검색 결과만 넣으면 최종 분석 모델이 그 문헌의 공개번호를 볼 수 없다.
    서지사항은 첫 페이지에 있으므로 그 앞부분을 원문 그대로 싣는다.
    """
    pages = [
        "공개번호 KR10-2020-0011111\n[0001] 본 발명은 센서 장치에 관한 것이다.\n- 1 -",
        *KOREAN_PAGES[1:],
    ]
    item = _pdf_attachment(tmp_path, "doc.pdf", pages)
    result = _run_agent(tmp_path, [item], "청구항 1. 센서.")

    document = result.bundle["documents"][0]
    assert "KR10-2020-0011111" in document["identity_excerpt"]
    assert "KR10-2020-0011111" in retrieval.render(result.bundle)


def test_evidence_budget_bounds_the_rendered_package(tmp_path) -> None:
    """예산은 '모델에게 나가는 크기'의 상한이다. 구조 문자까지 포함한다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    budget = RetrievalBudget(max_evidence_chars=3_000)
    result = _run_agent(tmp_path, [item], "청구항 1. 센서.", budget=budget)

    rendered = retrieval.render(result.bundle)
    placeholder = retrieval.render_placeholder(
        budget,
        [
            {
                "attachment": "ATT-01",
                "filename": "doc.pdf",
                "pdf_pages": 4,
                "extraction_status": "(실행 시 확인)",
            }
        ],
    )
    assert len(rendered.encode("utf-8")) <= len(placeholder.encode("utf-8"))


def _stress_bundle(
    components: int, label_chars: int, feature_chars: int, budget, corpus=None
):
    """구성 수와 문구 길이를 키운 근거 패키지."""
    from app.retrieval.agent import ComponentState, RetrievalRun

    corpus = corpus or []
    run = RetrievalRun()
    for index in range(1, components + 1):
        state = ComponentState(
            id=f"R{index:03d}",
            label="청구항 1 " + "가" * label_chars,
            feature="구성 내용 " + "나" * feature_chars,
            queries=[f"검색어{n}" for n in range(60)],
        )
        for document in corpus:
            state.record_search(
                attachment_id=document.attachment_id,
                alias=document.alias,
                queries=[f"검색어{n}" for n in range(60)],
                channels_used=["fts_bm25", "substring"],
                failed_channels=[],
                hits=0,
            )
        run.components.append(state)
    builder = evidence.EvidenceBuilder(
        corpus=corpus,
        run=run,
        budget=budget,
        claim_text="청구항 1.",
        semantic={},
        capabilities={"trigram": True},
        library_versions={},
    )
    bundle = builder.build()
    rendered = evidence.fit(bundle, budget)
    bundle["evidence_chars"] = len(rendered)
    return bundle, rendered


def test_package_stays_within_budget_under_stress(tmp_path) -> None:
    """문헌 20개 × 구성 20개 × 최대 길이 문구에서도 예산이 상한이어야 한다.

    항목별로 더하기 전에만 재면, 서지 발췌·구성 이름·구성 내용·검색어 목록·
    문헌별 검색 기록·상태 사유가 예산과 무관하게 늘어난다. 실측으로 예산의
    여섯 배가 나왔던 조합이다.
    """
    items = [
        _pdf_attachment(tmp_path, f"d{n:02d}.pdf", KOREAN_PAGES, sha=f"s-{n}")
        for n in range(20)
    ]
    corpus, _ = _corpus(tmp_path, items)
    # 예산을 명시한다. 이 시험이 재는 것은 "예산이 상한인가"와 "줄였으면
    # 기록하는가"이고, 뒤쪽은 예산이 실제로 모자라야 관측된다. 기본값을 쓰면
    # 기본값을 올리는 순간 압박이 사라져 시험이 아무것도 재지 않게 된다.
    budget = RetrievalBudget(max_evidence_chars=40_000)
    try:
        bundle, rendered = _stress_bundle(20, 180, 900, budget, corpus=corpus)
    finally:
        retrieval.close_documents(corpus)

    placeholder = retrieval.render_placeholder(budget, [])
    assert len(rendered) <= budget.max_evidence_chars
    assert len(rendered.encode("utf-8")) <= len(placeholder.encode("utf-8"))
    assert bundle["evidence_chars"] <= budget.max_evidence_chars
    assert bundle.get("package_over_budget") is not True
    # 줄인 사실이 기록되고 검토 범위 제한으로 올라간다.
    assert bundle["package_reductions"]
    assert bundle["coverage_blockers"]
    # 문헌도 구성도 통째로 없애지 않는다.
    assert len(bundle["components"]) == 20
    assert len(bundle["documents"]) == 20


def test_fit_returns_exactly_what_the_prompt_renders(tmp_path) -> None:
    """fit() 이 돌려준 문자열과 최종 프롬프트의 렌더링이 같아야 한다.

    fit() 은 축약 사유를 bundle 에 반영한다. 그 반영이 렌더링을 키우므로,
    반영 전에 잰 문자열을 돌려주면 최종 프롬프트가 안내한 크기보다 커진다.
    실행이 실제로 넘는 구간이 예산에 따라 생긴다.
    """
    for max_chars in (21_300, 27_500, 40_000, RetrievalBudget().max_evidence_chars):
        budget = RetrievalBudget(max_evidence_chars=max_chars)
        bundle, rendered = _stress_bundle(20, 180, 900, budget)
        # 최종 프롬프트가 하는 것과 같은 호출.
        assert retrieval.render(bundle) == rendered
        if not bundle.get("package_over_budget"):
            assert len(rendered) <= max_chars


@pytest.mark.parametrize("byte_limit", [0, 999, 1_000, 1_001, 1_999, 3_000])
def test_placeholder_covers_independent_char_and_byte_limits(byte_limit) -> None:
    budget = RetrievalBudget(max_evidence_chars=1_000, max_evidence_bytes=byte_limit)
    placeholder = retrieval.render_placeholder(budget, [])
    assert len(placeholder) == min(1_000, byte_limit)
    assert len(placeholder.encode("utf-8")) == byte_limit


def test_fit_enforces_bytes_even_when_the_char_budget_has_room() -> None:
    budget = RetrievalBudget(max_evidence_chars=100_000, max_evidence_bytes=10_000)
    bundle, rendered = _stress_bundle(1, 1_000, 4_000, budget)
    assert not bundle.get("package_over_budget")
    assert bundle["package_reductions"]
    assert len(rendered) < budget.max_evidence_chars
    assert len(rendered.encode("utf-8")) <= budget.max_evidence_bytes
    assert rendered == evidence.render(bundle)


@pytest.mark.parametrize("kind", ["read", "search"])
def test_partial_return_preserves_waiting_age_and_eventually_serves(tmp_path, kind) -> None:
    """실제 handler 가 일부 결과를 재이월해도 기다린 이력이 사라지지 않는다."""
    from dataclasses import replace
    from app.retrieval import agent as agent_module
    from app.retrieval.actions import ReadPage, SearchDocument

    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    corpus, _ = _corpus(tmp_path, [item])
    try:
        agent = agent_module.RetrievalAgent(
            job_id="age", provider=DeterministicTestProvider(), model=None,
            timeout_seconds=60, work_dir=tmp_path, corpus=corpus, claim_text="센서",
            budget=RetrievalBudget(max_round_result_chars=200),
            trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
        )
        agent._components["R001"] = agent_module.ComponentState("R001", "센서", "센서")
        action = (
            ReadPage(action="read_page", component_id="R001", attachment="ATT-01", page=1)
            if kind == "read" else
            SearchDocument(action="search_document", component_id="R001", attachment="ATT-01", queries=["센서"])
        )
        run = agent_module.RetrievalRun()
        for round_no in range(1, 9):
            asyncio.run(agent._execute_actions([action] if round_no == 1 else [], run, round_no))
            assert agent._deferred_actions
            assert all(entry.first_round == 1 for entry in agent._deferred_actions)
            assert min(entry.attempts for entry in agent._deferred_actions) >= round_no - 1
        assert not run.exposed_chunks
        agent._components["R001"].current_priority = "low"
        agent._components["R002"] = agent_module.ComponentState(
            "R002", "새 구성", "센서", current_priority="high"
        )
        urgent = SearchDocument(
            action="search_document", component_id="R002", attachment="ATT-01", queries=["센서"]
        )
        agent.budget = replace(agent.budget, max_round_result_chars=20_000)
        response = asyncio.run(agent._execute_actions([urgent], run, 9))
        assert response
        # 후보 0건 검색의 예약 몫을 준 뒤에도 오래 기다린 요청은 같은 라운드에 처리된다.
        if kind == "search":
            assert response[0].get("component_id") == "R001"
        else:
            assert any(entry.get("pages") for entry in response)
        assert run.exposed_chunks
        assert not agent._deferred_actions
    finally:
        retrieval.close_documents(corpus)


@pytest.mark.parametrize("kind", ["search", "read", "status"])
def test_unreturnable_envelope_does_not_enqueue_parent_and_children(tmp_path, kind) -> None:
    from app.retrieval import agent as agent_module
    from app.retrieval.actions import GetDocumentStatus, ReadPage, SearchDocument

    documents = [_pdf_attachment(tmp_path, f"d{i}.pdf", KOREAN_PAGES, sha=f"s{i}") for i in range(2)]
    corpus, _ = _corpus(tmp_path, documents)
    try:
        agent = agent_module.RetrievalAgent(
            job_id="envelope", provider=DeterministicTestProvider(), model=None,
            timeout_seconds=60, work_dir=tmp_path, corpus=corpus, claim_text="센서",
            budget=RetrievalBudget(max_round_result_chars=20),
            trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
        )
        agent._components["R001"] = agent_module.ComponentState("R001", "센서", "센서")
        action = {
            "search": SearchDocument(action="search_document", component_id="R001", queries=["센서"]),
            "read": ReadPage(action="read_page", component_id="R001", attachment="ATT-01", page=1),
            "status": GetDocumentStatus(action="get_document_status", attachment="*"),
        }[kind]
        run = agent_module.RetrievalRun()
        assert asyncio.run(agent._execute_actions([action], run, 1)) == []
        assert len(agent._deferred_actions) == 1
        assert agent._deferred_actions[0].item == action
        assert not run.exposed_chunks
        assert not agent._components["R001"].searched
    finally:
        retrieval.close_documents(corpus)


def test_package_reduction_downgrades_absent_verdict(tmp_path) -> None:
    """예산 때문에 줄였으면 「검토 범위에서 미발견」을 남겨 두지 않는다.

    구성 상태는 축약 이전에 확정된다. 축약 사유를 전역 blocker 에만 올리고
    구성 상태를 다시 내리지 않으면, 뺀 범위를 근거로 없음을 말하는 상태가
    남는다. 가장 위험한 조합이다.
    """
    budget = RetrievalBudget(max_evidence_chars=27_500)
    bundle, _rendered = _stress_bundle(20, 180, 900, budget)

    assert bundle["package_reductions"]
    for component in bundle["components"]:
        assert component["status"] != evidence.STATUS_NOT_FOUND_SCOPE
        assert component["needs_original_review"] is True
        # 사유가 전역뿐 아니라 구성별 기록에도 들어간다.
        assert any(
            reason in component["status_reasons"]
            for reason in bundle["package_reductions"]
        )

    # 줄일 필요가 없으면 상태를 건드리지 않는다.
    roomy = RetrievalBudget(max_evidence_chars=40_000)
    clean, _ = _stress_bundle(20, 180, 900, roomy)
    assert not clean["package_reductions"]
    assert all(
        component["status"] == evidence.STATUS_NOT_FOUND_SCOPE
        for component in clean["components"]
    )


def test_package_over_budget_fails_instead_of_overflowing(tmp_path) -> None:
    """도저히 안 들어가면 조용히 넘기지 않고 실패로 표시한다.

    구성 20개를 5,000자에 담을 수는 없다. 그때 넘겨 보내면 preflight 가 안내한
    크기가 거짓이 되고, 검색 비용을 다 쓴 뒤 Provider 호출 직전에 막힌다.
    """
    budget = RetrievalBudget(max_evidence_chars=5_000)
    bundle, _rendered = _stress_bundle(20, 180, 900, budget)

    assert bundle["package_over_budget"] is True
    assert bundle["package_required_chars"] > budget.max_evidence_chars
    # 사용자가 올려야 할 값이 기록에 있다.
    assert any(
        "들어가지 않습니다" in reason for reason in bundle["package_reductions"]
    )


def test_over_budget_package_fails_the_run(tmp_path) -> None:
    """예산에 못 맞춘 패키지로 최종 분석을 진행하지 않는다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    result = _run_agent(
        tmp_path,
        [item],
        "청구항 1. 센서.",
        # 안내문 골격만으로도 넘는 크기. 어떤 근거도 담을 수 없다.
        budget=RetrievalBudget(max_evidence_chars=600),
    )
    assert not result.ok
    assert result.error_code == "RETRIEVAL_FAILED"
    assert "근거 패키지 최대 문자 수" in result.error
    assert result.bundle is None


def test_documents_carry_role_in_the_package(tmp_path) -> None:
    """「기타 첨부 자료」에서 찾은 구간을 인용발명 개시로 읽으면 안 된다."""
    citation = _pdf_attachment(tmp_path, "citation.pdf", KOREAN_PAGES, sha="s-cit")
    misc = _pdf_attachment(tmp_path, "misc.pdf", KOREAN_PAGES, sha="s-misc")
    misc.role = AttachmentRole.SUPPLEMENTAL

    result = _run_agent(tmp_path, [citation, misc], "청구항 1. 센서.")
    assert result.ok
    roles = {d["attachment"]: d["role"] for d in result.bundle["documents"]}
    assert set(roles.values()) == {"CITATION", "SUPPLEMENTAL"}
    rendered = retrieval.render(result.bundle)
    assert "인용발명 문헌" in rendered
    assert "기타 첨부 자료" in rendered


def test_placeholder_size_is_an_upper_bound(tmp_path) -> None:
    """preflight 자리표는 실제 근거 패키지보다 작지 않다."""
    item = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    budget = RetrievalBudget(max_evidence_chars=4_000)
    result = _run_agent(tmp_path, [item], "청구항 1. 센서.", budget=budget)

    placeholder = retrieval.render_placeholder(
        budget,
        [
            {
                "attachment": "ATT-01",
                "filename": "doc.pdf",
                "pdf_pages": 4,
                "extraction_status": "(실행 시 확인)",
            }
        ],
    )
    actual = retrieval.render(result.bundle)
    assert len(placeholder.encode("utf-8")) >= len(actual.encode("utf-8"))


# --------------------------------------------------------------- OCR 금지


def test_no_ocr_dependency_is_added() -> None:
    """15. OCR 라이브러리나 외부 문서 업로드가 추가되지 않았다."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    banned = (
        "tesseract",
        "pytesseract",
        "ocrmypdf",
        "easyocr",
        "paddleocr",
        "pymupdf",
        "fitz",
        "llama-index",
        "haystack",
        "langchain",
        "qdrant",
        "chromadb",
        "elasticsearch",
        "openai",
    )
    for name in banned:
        assert name not in requirements, name

    sources = list((root / "app" / "retrieval").glob("*.py"))
    assert sources
    joined = "\n".join(path.read_text(encoding="utf-8") for path in sources).lower()
    for name in ("pytesseract", "ocrmypdf", "easyocr", "paddleocr", "import fitz"):
        assert name not in joined, name
    # 외부로 문서를 올리는 경로가 없다.
    for name in ("requests.post", "httpx.post", "urlopen", "upload_file"):
        assert name not in joined, name


@pytest.mark.parametrize(
    "status", [s for s in evidence.BUNDLE_STATUSES]
)
def test_every_bundle_status_has_a_label(status: str) -> None:
    assert evidence.STATUS_LABEL[status]
