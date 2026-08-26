"""「분석에 포함」 체크박스가 실제로 자료를 빼는지 고정한다.

지키려는 성질은 하나다. **화면이 세는 크기 · preflight 가 안내하는 크기 ·
Provider 에게 실제로 나가는 본문이 같은 포함 목록에서 나온다.**

셋이 갈라지면 사용자는 체크를 풀어 크기를 줄였다고 믿는데 실행에는 그대로
들어간다. 그 어긋남은 실행이 끝난 뒤에야 드러나고, 최악의 경우 Provider 가
입력을 조용히 잘라 앞부분만 분석한 보고서가 '성공'으로 남는다.

화면 쪽 계산은 frontend/src/lib/attachmentSelection.test.ts 가 따로 고정한다.
"""

from __future__ import annotations

import json

import pytest

from app.config import PROMPT_DIR

from .conftest import wait_for_job
from .fake_provider import RECEIVED


CLAIM = "청구항 1. 제1 센서와 제2 센서를 포함하는 장치."

CAPABLE_PROMPT = """<!-- ARIA_PROMPT_METADATA
{
  "name": "포함 여부 테스트 프롬프트",
  "output_mode": "markdown",
  "capabilities": ["citation_mapping_v1"],
  "enabled": true
}
-->
청구항과 인용발명을 대비하십시오."""


@pytest.fixture()
def prompt(client):
    """문헌 매핑을 선언한 프롬프트. 매핑 대상까지 함께 검증한다."""
    path = PROMPT_DIR / "inclusion-test.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CAPABLE_PROMPT, encoding="utf-8")
    yield client.get(f"/api/prompts/{path.name}").json()
    path.unlink(missing_ok=True)


def upload_two(client) -> dict:
    """길이가 뚜렷이 다른 인용발명 PDF 2건. 어느 쪽이 빠졌는지 크기로 구분한다."""
    response = client.post(
        "/api/uploads",
        files=[
            ("files", ("keep.txt", ("킵문서고유표식 " + "가" * 500).encode(), "text/plain")),
            ("files", ("drop.txt", ("드롭문서고유표식 " + "나" * 5000).encode(), "text/plain")),
        ],
        data={"roles": json.dumps(["CITATION", "CITATION"])},
    )
    assert response.status_code == 200, response.text
    return response.json()


def ids_by_name(upload: dict) -> dict[str, str]:
    return {f["original_filename"]: f["attachment_id"] for f in upload["files"]}


def attachment_manifest(job_id: str) -> list[dict]:
    """이 실행이 조립 시점에 남긴 자료 목록. API 에는 노출되지 않는다."""
    from app.db import session_scope
    from app.models import ExecutionJob

    with session_scope() as session:
        return list(session.get(ExecutionJob, job_id).attachment_manifest or [])


def preflight(client, prompt, batch_id, selected):
    body = {
        "job_kind": "patent_analysis",
        "prompt_id": prompt["id"],
        "provider": "test",
        "claim_text": CLAIM,
        "batch_id": batch_id,
    }
    if selected is not None:
        body["selected_attachment_ids"] = selected
    response = client.post("/api/jobs/preflight", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def create(client, prompt, batch_id, selected):
    body = {
        "prompt_id": prompt["id"],
        "provider": "test",
        "claim_text": CLAIM,
        "batch_id": batch_id,
    }
    if selected is not None:
        body["selected_attachment_ids"] = selected
    return client.post("/api/jobs", json=body)


# ------------------------------------------------------------------ 업로드


def test_upload_checks_only_successfully_processed_files(client) -> None:
    """전처리 직후의 초기 체크 상태를 서버가 알려준다.

    화면이 read_ok 를 다시 해석하지 않는다. 판단 지점이 둘이면 갈라진다.
    """
    from .pdf_fixture import build_scanned_like_pdf

    upload = client.post(
        "/api/uploads",
        files=[
            ("files", ("good.txt", b"usable content", "text/plain")),
            ("files", ("scan.pdf", build_scanned_like_pdf(2), "application/pdf")),
        ],
    ).json()
    included = {f["original_filename"]: f["included"] for f in upload["files"]}
    assert included == {"good.txt": True, "scan.pdf": False}


# ---------------------------------------------------------------- preflight


def test_preflight_shrinks_when_a_file_is_unchecked(client, prompt) -> None:
    """2건을 처리한 뒤 하나를 해제하면 preflight 크기가 줄어든다."""
    upload = upload_two(client)
    ids = ids_by_name(upload)

    both = preflight(client, prompt, upload["batch_id"], list(ids.values()))
    only_keep = preflight(client, prompt, upload["batch_id"], [ids["keep.txt"]])

    assert only_keep["chars"] < both["chars"]
    assert only_keep["bytes"] < both["bytes"]
    # 줄어든 폭은 뺀 자료의 본문 크기와 같은 자릿수여야 한다. 헤더만 빠진 것이
    # 아니라 본문이 통째로 빠졌다는 뜻이다.
    dropped_chars = next(
        f["char_count"] for f in upload["files"] if f["original_filename"] == "drop.txt"
    )
    assert both["chars"] - only_keep["chars"] > dropped_chars


def test_preflight_without_selection_keeps_every_file(client, prompt) -> None:
    """selected_attachment_ids 를 보내지 않으면 예전과 똑같이 전부 포함이다."""
    upload = upload_two(client)
    ids = ids_by_name(upload)

    legacy = preflight(client, prompt, upload["batch_id"], None)
    explicit = preflight(client, prompt, upload["batch_id"], list(ids.values()))

    assert legacy["chars"] == explicit["chars"]
    assert legacy["bytes"] == explicit["bytes"]
    assert legacy["blocked"] is False


def test_preflight_blocks_when_nothing_is_checked(client, prompt) -> None:
    upload = upload_two(client)
    result = preflight(client, prompt, upload["batch_id"], [])
    assert result["blocked"] is True
    assert "분석에 포함" in result["message"]


# ------------------------------------------------------------------- 실행


def test_unchecked_file_is_absent_from_the_final_prompt(client, prompt) -> None:
    """해제한 자료는 파일명도 본문도 최종 프롬프트에 없다."""
    upload = upload_two(client)
    ids = ids_by_name(upload)

    created = create(client, prompt, upload["batch_id"], [ids["keep.txt"]])
    assert created.status_code == 201, created.text
    job = wait_for_job(client, created.json()["id"])
    assert job["status"] == "SUCCEEDED", job["errors"]

    text = client.get(f"/api/jobs/{job['id']}/final-prompt").text
    assert "킵문서고유표식" in text
    assert "keep.txt" in text
    assert "드롭문서고유표식" not in text
    assert "drop.txt" not in text
    # 첨부 개수 안내도 포함된 자료만 센다.
    assert "총 1개 중 1개의 본문이 아래에 포함되어 있습니다." in text

    # 실행 기록에는 두 건 다 남는다. 무엇을 올렸고 무엇을 뺐는지가 증거다.
    stored = {a["original_filename"]: a["included"] for a in job["attachments"]}
    assert stored == {"keep.txt": True, "drop.txt": False}

    # 조립 manifest 는 분석 자료의 목록이다. 뺀 자료는 여기에도 없다.
    assert [row["original_filename"] for row in attachment_manifest(job["id"])] == [
        "keep.txt"
    ]


def test_uploaded_batch_cannot_be_reused_after_a_partial_run(client, prompt) -> None:
    """일부만 체크해 실행해도 batch 는 통째로 그 실행에 귀속된다.

    체크를 푼 자료도 그 실행의 기록이므로 다른 작업이 같은 batch 를 다시 쓰면
    두 실행이 같은 폴더를 공유하게 된다.
    """
    upload = upload_two(client)
    ids = ids_by_name(upload)

    first = create(client, prompt, upload["batch_id"], [ids["keep.txt"]])
    assert first.status_code == 201, first.text
    wait_for_job(client, first.json()["id"])

    again = create(client, prompt, upload["batch_id"], [ids["drop.txt"]])
    assert again.status_code == 400
    assert "이미 다른 작업에 사용된 업로드입니다." in again.json()["detail"]


def test_only_checked_files_get_citation_numbers(client, prompt) -> None:
    """자료 번호도 문헌 매핑도 포함된 자료에만 붙는다."""
    upload = upload_two(client)
    ids = ids_by_name(upload)

    created = create(client, prompt, upload["batch_id"], [ids["keep.txt"]])
    job = wait_for_job(client, created.json()["id"])
    assert job["status"] == "SUCCEEDED", job["errors"]

    mapping = job["citation_mapping"]
    assert mapping is not None
    assert [item["filename"] for item in mapping["items"]] == ["keep.txt"]
    assert [item["attachment_id"] for item in mapping["items"]] == [ids["keep.txt"]]


def test_creating_a_job_with_nothing_checked_is_refused(client, prompt) -> None:
    """체크된 자료가 하나도 없으면 작업을 만들지 않는다. 토큰도 쓰지 않는다."""
    upload = upload_two(client)
    refused = create(client, prompt, upload["batch_id"], [])
    assert refused.status_code == 400
    assert "분석에 포함" in refused.json()["detail"]

    # 거절당한 업로드는 그대로 남아 있어야 한다. 체크를 다시 켜고 실행하면
    # 같은 batch 로 시작할 수 있다.
    retried = create(client, prompt, upload["batch_id"], [ids_by_name(upload)["keep.txt"]])
    assert retried.status_code == 201, retried.text
    job = wait_for_job(client, retried.json()["id"])
    assert job["status"] == "SUCCEEDED", job["errors"]


def test_default_run_includes_every_file(client, prompt) -> None:
    """모두 체크된 기본 동작은 이 변경 전과 같다."""
    upload = upload_two(client)
    created = create(client, prompt, upload["batch_id"], None)
    job = wait_for_job(client, created.json()["id"])
    assert job["status"] == "SUCCEEDED", job["errors"]

    text = client.get(f"/api/jobs/{job['id']}/final-prompt").text
    assert "킵문서고유표식" in text
    assert "드롭문서고유표식" in text
    assert all(a["included"] for a in job["attachments"])
    assert len(job["citation_mapping"]["items"]) == 2


# ------------------------------------------------- preflight ↔ 실제 전송 일치


@pytest.mark.parametrize("drop_one", [False, True])
def test_preflight_matches_what_the_provider_receives(client, prompt, drop_one) -> None:
    """안내한 chars/bytes 가 Provider 에게 실제로 나간 본문과 정확히 같아야 한다.

    화면과 실행이 다른 목록을 보면 여기서 갈라진다. 체크를 푼 경우와 전부
    포함한 경우를 모두 고정한다.
    """
    upload = upload_two(client)
    ids = ids_by_name(upload)
    selected = [ids["keep.txt"]] if drop_one else list(ids.values())

    measured = preflight(client, prompt, upload["batch_id"], selected)
    assert measured["blocked"] is False

    RECEIVED.clear()
    created = create(client, prompt, upload["batch_id"], selected)
    assert created.status_code == 201, created.text
    job = wait_for_job(client, created.json()["id"])
    assert job["status"] == "SUCCEEDED", job["errors"]

    assert len(RECEIVED) == 1
    request = RECEIVED[0]
    sent_chars = len(request.system_prompt) + len(request.user_message)
    sent_bytes = len(request.system_prompt.encode("utf-8")) + len(
        request.user_message.encode("utf-8")
    )

    assert measured["chars"] == sent_chars
    assert measured["bytes"] == sent_bytes
    # 저장되는 값도 같은 숫자다. 실행 기록과 화면 안내가 어긋나면 안 된다.
    assert job["final_prompt_chars"] == sent_chars


# ------------------------------------------------------------------ 후속 분석


def test_follow_up_does_not_resurrect_an_excluded_document(client, prompt) -> None:
    """원본에서 뺀 자료는 후속 실행에서도 분석 자료로 되살아나지 않는다.

    복제 자체는 한다 — 그 실행의 자료 목록이 원본과 같아야 나중에 다시 켤 수
    있다. 다만 포함 여부는 원본이 정한 대로 따라간다.
    """
    upload = upload_two(client)
    ids = ids_by_name(upload)
    first = create(client, prompt, upload["batch_id"], [ids["keep.txt"]])
    parent = wait_for_job(client, first.json()["id"])
    assert parent["status"] == "SUCCEEDED", parent["errors"]

    kept_parent_id = next(
        a["attachment_id"]
        for a in parent["attachments"]
        if a["original_filename"] == "keep.txt"
    )
    child_created = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": CLAIM,
            "source_job_id": parent["id"],
            "relation_type": "REANALYZED",
            # 화면은 원본에서 포함이었던 자료의 id 만 실어 보낸다.
            "selected_attachment_ids": [kept_parent_id],
        },
    )
    assert child_created.status_code == 201, child_created.text
    child = wait_for_job(client, child_created.json()["id"])
    assert child["status"] == "SUCCEEDED", child["errors"]

    # 두 건 다 복제되지만 분석 자료는 한 건뿐이다.
    stored = {a["original_filename"]: a["included"] for a in child["attachments"]}
    assert stored == {"keep.txt": True, "drop.txt": False}

    text = client.get(f"/api/jobs/{child['id']}/final-prompt").text
    assert "킵문서고유표식" in text
    assert "드롭문서고유표식" not in text


def test_follow_up_without_a_selection_keeps_the_parent_decision(client, prompt) -> None:
    """목록을 보내지 않으면 원본이 정한 포함 여부를 그대로 잇는다."""
    upload = upload_two(client)
    ids = ids_by_name(upload)
    first = create(client, prompt, upload["batch_id"], [ids["keep.txt"]])
    parent = wait_for_job(client, first.json()["id"])

    child_created = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": CLAIM,
            "source_job_id": parent["id"],
            "relation_type": "REANALYZED",
        },
    )
    assert child_created.status_code == 201, child_created.text
    child = wait_for_job(client, child_created.json()["id"])
    assert child["status"] == "SUCCEEDED", child["errors"]

    stored = {a["original_filename"]: a["included"] for a in child["attachments"]}
    assert stored == {"keep.txt": True, "drop.txt": False}
    assert "드롭문서고유표식" not in client.get(
        f"/api/jobs/{child['id']}/final-prompt"
    ).text
