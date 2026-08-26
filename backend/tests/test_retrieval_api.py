"""로컬 검색이 실제 실행 경로에 붙어 있는지 — 업로드부터 보고서까지.

실제 CLI 를 부르지 않는다. 결정론적 대역이 검색 라운드에서는 action JSON 을,
최종 분석에서는 보고서를 돌려준다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import PATHS

from .conftest import wait_for_job
from .fake_provider import RECEIVED, DeterministicTestProvider
from .pdf_fixture import build_korean_pdf

# agy 의 실제 전송 한도. 이 숫자를 넘는 인용문헌이 이번 작업의 출발점이었다.
AGY_BYTE_BUDGET = 180_000


@pytest.fixture()
def prompt(client):
    return client.post(
        "/api/prompts",
        json={
            "name": "구성대비 테스트",
            "body": "청구항과 인용발명을 구성별로 대비하십시오.",
            "output_mode": "markdown",
        },
    ).json()


@pytest.fixture()
def settings_guard(client):
    """설정을 바꾼 테스트가 다른 테스트에 새지 않게 한다."""
    before = client.get("/api/settings").json()["values"]
    keys = (
        "retrieval_mode",
        "retrieval_evidence_chars",
        "retrieval_max_rounds",
        "retrieval_max_page_reads",
        "retrieval_hits_per_document",
        "retrieval_semantic_enabled",
        "retrieval_auto_threshold_bytes",
    )
    yield
    client.put(
        "/api/settings", json={"values": {key: before[key] for key in keys}}
    )


def large_korean_pdf(pages: int = 60, per_page: int = 1_700) -> bytes:
    """정규화 텍스트가 180,000 bytes 를 넘는 한글 PDF."""
    body: list[str] = []
    for page in range(1, pages + 1):
        filler = "본 발명의 실시예에 따른 장치의 동작을 상세히 설명한다. "
        text = f"[{page * 8:04d}] " + (filler * (per_page // len(filler) + 1))
        if page == pages:
            text += " 마지막페이지고유문구 를 여기에 기재한다."
        if page == 3:
            text += " 제1 센서와 제2 센서가 결합되어 제어부가 신호를 처리한다."
        body.append(text[:per_page] + f"\n- {page} -")
    return build_korean_pdf(body)


def upload_pdf(client, data: bytes, filename: str = "citation.pdf") -> dict:
    response = client.post(
        "/api/uploads",
        files=[("files", (filename, data, "application/pdf"))],
        data={"roles": json.dumps(["CITATION"])},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------- 1


def test_oversized_citation_runs_through_retrieval(
    client, prompt, monkeypatch, settings_guard
) -> None:
    """1. 180,000 bytes 를 넘는 인용문헌도 작은 근거 패키지로 조립된다."""
    monkeypatch.setattr(
        DeterministicTestProvider, "max_input_bytes", AGY_BYTE_BUDGET
    )
    client.put(
        "/api/settings",
        json={"values": {"retrieval_mode": "auto", "retrieval_evidence_chars": 20_000}},
    )

    data = large_korean_pdf()
    upload = upload_pdf(client, data)
    citation = upload["files"][0]
    assert citation["read_ok"] is True
    assert citation["char_count"] > 60_000

    body = {
        "prompt_id": prompt["id"],
        "provider": "test",
        "claim_text": "청구항 1. 제1 센서와 제2 센서, 그리고 제어부를 포함하는 장치.",
        "batch_id": upload["batch_id"],
    }

    preflight = client.post("/api/jobs/preflight", json=body).json()
    assert preflight["delivery_plan"] == "local_retrieval"
    assert preflight["full_inline_bytes"] > AGY_BYTE_BUDGET
    assert preflight["evidence_budget_chars"] == 20_000
    assert preflight["blocked"] is False

    RECEIVED.clear()
    job = client.post("/api/jobs", json=body).json()
    final = wait_for_job(client, job["id"])

    assert final["status"] == "SUCCEEDED", final["errors"]
    assert final["delivery_plan"] == "local_retrieval"
    assert final["retrieval_manifest"] is not None

    # 최종 분석 호출은 마지막 요청이다. 검색 라운드는 그 앞에 있다.
    analysis = RECEIVED[-1]
    payload_bytes = len(analysis.system_prompt.encode("utf-8")) + len(
        analysis.user_message.encode("utf-8")
    )
    assert payload_bytes <= AGY_BYTE_BUDGET
    # preflight 가 안내한 최댓값을 실제 실행이 넘지 않는다.
    assert payload_bytes <= preflight["bytes"]

    # 인용발명 본문 전체가 프롬프트에 들어가지 않았다.
    assert "ARIA 로컬 검색 근거 패키지" in analysis.user_message
    # 60쪽 문헌의 반복 문구가 최대 몇 번 나올 수 있는지로 잰다. 전체 인라인
    # 이었다면 쪽마다 수십 번씩 나온다.
    filler = "본 발명의 실시예에 따른 장치의 동작"
    inline_count = (
        (PATHS.runs_dir / upload["batch_id"] / "normalized")
        .glob("*.txt")
        .__next__()
        .read_text(encoding="utf-8")
        .count(filler)
    )
    delivered_count = analysis.user_message.count(filler)
    assert inline_count > 1_000, inline_count
    assert delivered_count < inline_count / 20, (delivered_count, inline_count)


# --------------------------------------------------------------------- 2


def test_small_document_still_uses_full_inline(client, prompt, settings_guard) -> None:
    """작은 문헌의 auto 모드는 예전과 똑같이 전체 인라인이다."""
    upload = upload_pdf(
        client, build_korean_pdf(["[0001] 짧은 인용문헌 본문이다.\n- 1 -"]), "small.pdf"
    )
    body = {
        "prompt_id": prompt["id"],
        "provider": "test",
        "claim_text": "청구항 1. 장치.",
        "batch_id": upload["batch_id"],
    }
    preflight = client.post("/api/jobs/preflight", json=body).json()
    assert preflight["delivery_plan"] == "full_inline"

    RECEIVED.clear()
    job = client.post("/api/jobs", json=body).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "SUCCEEDED", final["errors"]
    assert final["delivery_plan"] == "full_inline"
    assert final["retrieval_manifest"] is None
    assert "짧은 인용문헌 본문이다" in RECEIVED[-1].user_message


# --------------------------------------------------------------------- 3


def test_retrieval_mode_can_be_forced(client, prompt, settings_guard) -> None:
    """설정으로 로컬 검색을 강제할 수 있고, 그 사실이 기록에 남는다."""
    client.put("/api/settings", json={"values": {"retrieval_mode": "retrieval"}})
    upload = upload_pdf(
        client,
        build_korean_pdf(["[0001] 제1 센서와 제어부를 포함한다.\n- 1 -"]),
        "forced.pdf",
    )
    RECEIVED.clear()
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 센서와 제어부.",
            "batch_id": upload["batch_id"],
        },
    ).json()
    final = wait_for_job(client, job["id"])

    assert final["status"] == "SUCCEEDED", final["errors"]
    assert final["delivery_plan"] == "local_retrieval"
    manifest = final["retrieval_manifest"]
    assert manifest["ocr_performed"] is False
    assert manifest["documents"][0]["index"]["index_version"] >= 1
    assert manifest["libraries"]["pypdf"]
    assert manifest["semantic"]["active"] is False
    assert manifest["semantic"]["reason"]


# --------------------------------------------------------------------- 4


def test_excluded_attachment_is_absent_from_evidence(
    client, prompt, settings_guard
) -> None:
    """9. 분석 제외 첨부는 검색 결과와 근거 패키지에 나타나지 않는다."""
    client.put("/api/settings", json={"values": {"retrieval_mode": "retrieval"}})
    response = client.post(
        "/api/uploads",
        files=[
            (
                "files",
                (
                    "keep.pdf",
                    build_korean_pdf(["[0001] 유지문헌 의 센서 구성이다.\n- 1 -"]),
                    "application/pdf",
                ),
            ),
            (
                "files",
                (
                    "drop.pdf",
                    build_korean_pdf(["[0001] 제외문헌 의 센서 구성이다.\n- 1 -"]),
                    "application/pdf",
                ),
            ),
        ],
        data={"roles": json.dumps(["CITATION", "CITATION"])},
    )
    upload = response.json()
    keep = next(f for f in upload["files"] if f["original_filename"] == "keep.pdf")

    RECEIVED.clear()
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 센서.",
            "batch_id": upload["batch_id"],
            "selected_attachment_ids": [keep["attachment_id"]],
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "SUCCEEDED", final["errors"]

    manifest = final["retrieval_manifest"]
    names = {document["filename"] for document in manifest["documents"]}
    assert names == {"keep.pdf"}

    work_dir = PATHS.runs_dir / upload["batch_id"]
    bundle = json.loads(
        (work_dir / "retrieval" / "evidence_bundle.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(bundle, ensure_ascii=False)
    assert "제외문헌" not in serialized
    assert "drop.pdf" not in serialized
    # 인덱스 파일 자체가 만들어지지 않는다.
    indexes = {p.stem for p in (work_dir / "retrieval" / "index").glob("*.sqlite3")}
    assert indexes == {keep["attachment_id"]}
    # 최종 프롬프트에도 없다.
    assert "제외문헌" not in RECEIVED[-1].user_message


# --------------------------------------------------------------------- 5


def test_extraction_anomaly_blocks_absent_verdict_end_to_end(
    client, prompt, settings_guard
) -> None:
    """7. 추출 이상이 있으면 근거 패키지가 「없음」을 확정하지 못한다.

    전부 스캔본인 PDF 는 업로드 단계에서 이미 거절된다(OCR 을 하지 않는다).
    여기서 다루는 것은 그 앞 단계 — 본문은 읽히지만 **일부 페이지만** 비어
    있는 문헌이다. 이런 문헌은 전달에 성공하므로, 검토 범위 제한을 잡아내는
    책임이 완전성 보고서와 근거 패키지 게이트에 있다.
    """
    client.put("/api/settings", json={"values": {"retrieval_mode": "retrieval"}})
    mixed = build_korean_pdf(
        [
            "[0001] 센서 구성이 기재되어 있다.\n- 1 -",
            " ",
            "[0020] 제어부의 동작을 설명한다.\n- 3 -",
        ]
    )
    upload = upload_pdf(client, mixed, "mixed.pdf")
    citation = upload["files"][0]
    # 전달 자체는 된다. 비어 있는 페이지는 완전성 보고서가 잡는다.
    assert citation["read_ok"] is True

    RECEIVED.clear()
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. RETRIEVAL_NOTFOUND 없는구성.",
            "batch_id": upload["batch_id"],
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "SUCCEEDED", final["errors"]

    work_dir = PATHS.runs_dir / upload["batch_id"]
    bundle = json.loads(
        (work_dir / "retrieval" / "evidence_bundle.json").read_text(encoding="utf-8")
    )
    assert bundle["coverage_blockers"]
    for component in bundle["components"]:
        assert component["status"] != "not_found_in_reviewed_scope"

    message = RECEIVED[-1].user_message
    assert "문헌에 없음" not in message
    assert "설정된 검색어와 추출 텍스트의 검토 범위에서는" in message


# --------------------------------------------------------------------- 6


def test_cancel_stops_multi_stage_retrieval(client, prompt, settings_guard) -> None:
    """12. 취소가 다단계 실행 전체를 중단한다."""
    client.put("/api/settings", json={"values": {"retrieval_mode": "retrieval"}})
    upload = upload_pdf(
        client,
        build_korean_pdf(["[0001] 센서 구성이 기재되어 있다.\n- 1 -"]),
        "slow.pdf",
    )
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. RETRIEVAL_SLOW 센서.",
            "batch_id": upload["batch_id"],
        },
    ).json()

    import time

    deadline = time.time() + 20
    while time.time() < deadline:
        current = client.get(f"/api/jobs/{job['id']}").json()
        if current["status"] == "RUNNING":
            break
        time.sleep(0.1)

    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["cancelled"] is True
    final = wait_for_job(client, job["id"])
    assert final["status"] == "CANCELLED"
    assert final["error_code"] == "CANCELLED"
    # 취소해도 검색 감사 기록은 남는다.
    assert final["retrieval_manifest"] is not None


# --------------------------------------------------------------------- 7


def test_followup_clone_keeps_independent_index(client, prompt, settings_guard) -> None:
    """13. 후속 분석의 첨부 복제와 인덱스가 독립적으로 유지된다."""
    client.put("/api/settings", json={"values": {"retrieval_mode": "retrieval"}})
    upload = upload_pdf(
        client,
        build_korean_pdf(["[0001] 제1 센서와 제어부를 포함한다.\n- 1 -"]),
        "parent.pdf",
    )
    parent = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 센서.",
            "batch_id": upload["batch_id"],
        },
    ).json()
    parent_final = wait_for_job(client, parent["id"])
    assert parent_final["status"] == "SUCCEEDED", parent_final["errors"]

    child = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1과 청구항 2. 센서.",
            "source_job_id": parent["id"],
            "relation_type": "REANALYZED",
        },
    ).json()
    child_final = wait_for_job(client, child["id"])
    assert child_final["status"] == "SUCCEEDED", child_final["errors"]
    assert child_final["delivery_plan"] == "local_retrieval"

    parent_dir = PATHS.runs_dir / upload["batch_id"] / "retrieval" / "index"
    child_dir = PATHS.runs_dir / child["id"] / "retrieval" / "index"
    assert parent_dir.exists() and child_dir.exists()
    assert parent_dir != child_dir

    parent_ids = {p.stem for p in parent_dir.glob("*.sqlite3")}
    child_ids = {p.stem for p in child_dir.glob("*.sqlite3")}
    # 복제로 attachment_id 가 바뀌므로 인덱스 파일도 별개다.
    assert parent_ids and child_ids and parent_ids.isdisjoint(child_ids)

    # 같은 자료이므로 sha256 은 같고, 자식 인덱스는 새로 만들어졌다.
    child_manifest = child_final["retrieval_manifest"]
    parent_manifest = parent_final["retrieval_manifest"]
    assert (
        child_manifest["documents"][0]["pdf_sha256"]
        == parent_manifest["documents"][0]["pdf_sha256"]
    )
    assert child_manifest["documents"][0]["index_rebuilt"] is True

    # 원본 실행 폴더를 지워도 자식의 인덱스는 남는다.
    assert client.delete(f"/api/history/{parent['id']}").status_code == 204
    assert child_dir.exists()
    assert {p.stem for p in child_dir.glob("*.sqlite3")} == child_ids


# --------------------------------------------------------------------- 8


def test_retrieval_artifacts_are_readable_from_history(
    client, prompt, settings_guard
) -> None:
    """UI 가 retrieval trace 와 evidence bundle 을 열 수 있어야 한다."""
    client.put("/api/settings", json={"values": {"retrieval_mode": "retrieval"}})
    upload = upload_pdf(
        client,
        build_korean_pdf(["[0001] 제1 센서와 제어부를 포함한다.\n- 1 -"]),
        "artifact.pdf",
    )
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 센서.",
            "batch_id": upload["batch_id"],
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "SUCCEEDED", final["errors"]

    bundle = client.get(f"/api/jobs/{job['id']}/retrieval?which=evidence")
    assert bundle.status_code == 200
    assert "components" in bundle.json()

    trace = client.get(f"/api/jobs/{job['id']}/retrieval?which=trace")
    assert trace.status_code == 200
    assert "llm_input" in trace.text

    report = client.get(f"/api/jobs/{job['id']}/retrieval?which=extraction")
    assert report.status_code == 200
    assert report.json()["documents"]


# --------------------------------------------------------------------- 9


def test_index_is_rebuilt_when_stored_pdf_changes(
    client, prompt, settings_guard
) -> None:
    """8. PDF 가 바뀌면 오래된 인덱스를 쓰지 않는다 (실행 경로 기준)."""
    client.put("/api/settings", json={"values": {"retrieval_mode": "retrieval"}})
    upload = upload_pdf(
        client,
        build_korean_pdf(["[0001] 최초문구 가 있는 문헌.\n- 1 -"]),
        "rev.pdf",
    )
    first = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 센서.",
            "batch_id": upload["batch_id"],
        },
    ).json()
    assert wait_for_job(client, first["id"])["status"] == "SUCCEEDED"

    work_dir = PATHS.runs_dir / upload["batch_id"]
    index_path = next((work_dir / "retrieval" / "index").glob("*.sqlite3"))
    stamp = index_path.stat().st_mtime_ns

    # 같은 자료로 다시 색인하면 재사용한다.
    from app import retrieval
    from app.execution.runner import attachments_for
    from app.db import session_scope

    with session_scope() as session:
        items = attachments_for(session, first["id"])
    documents, _ = retrieval.build_corpus(items, work_dir)
    assert documents[0].rebuilt is False
    retrieval.close_documents(documents)
    assert index_path.stat().st_mtime_ns == stamp

    # sha256 이 달라지면 재생성한다.
    items[0].sha256 = "0" * 64
    documents, _ = retrieval.build_corpus(items, work_dir)
    assert documents[0].rebuilt is True
    retrieval.close_documents(documents)
