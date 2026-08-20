"""후속 분석: 계보, 첨부 복제, 삭제 정책.

여기서 지키려는 성질은 세 가지다.

  1. CONTINUED 는 이전 청구항과 이전 보고서를 프롬프트에 넣고,
     REANALYZED 는 같은 자료를 쓰되 이전 보고서를 넣지 않는다.
  2. 후속 실행은 첨부를 자기 폴더로 복제하므로 원본 이력을 지워도 온전하다.
  3. 스레드 일괄 삭제는 후속 실행까지 함께 지운다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import wait_for_job


@pytest.fixture()
def prompt(client):
    return client.post(
        "/api/prompts",
        json={
            "name": "후속 분석 테스트 프롬프트",
            "body": "청구항과 인용발명을 대비하십시오.",
            "output_mode": "markdown",
        },
    ).json()


def _upload(client, filename: str = "citation.txt", body: bytes = b"prior art body") -> str:
    response = client.post(
        "/api/uploads",
        files=[("files", (filename, body, "text/plain"))],
        data={"roles": json.dumps(["CITATION"])},
    )
    assert response.status_code == 200, response.text
    return response.json()["batch_id"]


def _run(client, prompt, **extra) -> dict:
    created = client.post(
        "/api/jobs", json={"prompt_id": prompt["id"], "provider": "test", **extra}
    )
    assert created.status_code == 201, created.text
    return wait_for_job(client, created.json()["id"])


def _final_prompt(client, job_id: str) -> str:
    return client.get(f"/api/jobs/{job_id}/final-prompt").text


def _stored_paths(job_id: str) -> list[Path]:
    from app.db import session_scope
    from app.models import Attachment

    with session_scope() as session:
        rows = session.query(Attachment).filter(Attachment.job_id == job_id).all()
        return [Path(row.stored_path) for row in rows]


# ------------------------------------------------------------ 프롬프트 조립


def test_continued_carries_prior_claim_and_report(client, prompt) -> None:
    parent = _run(
        client,
        prompt,
        claim_text="청구항 1. 독립항 본문.",
        batch_id=_upload(client),
    )
    assert parent["status"] == "SUCCEEDED"

    child = _run(
        client,
        prompt,
        claim_text="청구항 1. 독립항 본문.\n청구항 2. 제1항에 있어서, 종속 구성.",
        source_job_id=parent["id"],
        relation_type="CONTINUED",
    )
    assert child["relation_type"] == "CONTINUED"
    assert child["source_job_id"] == parent["id"]
    assert child["source_job_label"]
    assert child["prior_claim_text"] == "청구항 1. 독립항 본문."
    assert child["prior_report"] == parent["result_text"]

    text = _final_prompt(client, child["id"])
    assert "[이전 분석 이력]" in text
    assert "[이전 청구항]" in text
    assert "[이전 분석 보고서]" in text
    # 현재 청구항이 기준이고, 이전 청구항은 참고 자료로만 들어간다.
    assert "청구항 2. 제1항에 있어서, 종속 구성." in text
    assert "--- 이전 보고서 시작 ---" in text


def test_reanalyzed_reuses_files_without_the_report(client, prompt) -> None:
    parent = _run(client, prompt, claim_text="청구항 1.", batch_id=_upload(client))

    child = _run(
        client,
        prompt,
        claim_text="청구항 1.",
        source_job_id=parent["id"],
        relation_type="REANALYZED",
    )
    assert child["relation_type"] == "REANALYZED"
    assert child["prior_report"] == ""
    assert child["prior_claim_text"] == ""

    # 자료는 그대로 물려받는다.
    assert len(child["attachments"]) == 1

    text = _final_prompt(client, child["id"])
    assert "[이전 분석 이력]" not in text
    assert "[이전 분석 보고서]" not in text
    assert "prior art body" in text


def test_followup_instruction_is_passed_through_verbatim(client, prompt) -> None:
    parent = _run(client, prompt, claim_text="청구항 1.")
    instruction = "종속항 2~5 만 집중해서 보고, 인용발명 번호는 그대로 유지하십시오."

    child = _run(
        client,
        prompt,
        claim_text="청구항 1.\n청구항 2.",
        source_job_id=parent["id"],
        relation_type="CONTINUED",
        followup_instruction=instruction,
    )
    assert child["followup_instruction"] == instruction

    text = _final_prompt(client, child["id"])
    assert "[사용자 후속 지시]" in text
    assert instruction in text


# ---------------------------------------------------------------- 첨부 복제


def test_attachments_are_copied_not_shared(client, prompt) -> None:
    parent = _run(client, prompt, batch_id=_upload(client))
    child = _run(
        client,
        prompt,
        source_job_id=parent["id"],
        relation_type="REANALYZED",
    )

    parent_file = parent["attachments"][0]
    child_file = child["attachments"][0]

    # 새 행 · 새 파일이지만 내용은 같다는 것을 해시로 보인다.
    assert child_file["attachment_id"] != parent_file["attachment_id"]
    assert child_file["sha256"] == parent_file["sha256"]
    assert child_file["original_filename"] == parent_file["original_filename"]
    assert child_file["char_count"] == parent_file["char_count"]

    parent_paths = _stored_paths(parent["id"])
    child_paths = _stored_paths(child["id"])
    assert parent_paths and child_paths
    assert set(parent_paths).isdisjoint(child_paths)
    assert all(path.is_file() for path in parent_paths + child_paths)


def test_tampered_source_file_blocks_the_follow_up(client, prompt) -> None:
    parent = _run(client, prompt, batch_id=_upload(client))
    stored = _stored_paths(parent["id"])[0]
    stored.write_bytes(b"tampered content")

    response = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "source_job_id": parent["id"],
            "relation_type": "REANALYZED",
        },
    )
    assert response.status_code == 409
    assert "해시" in response.json()["detail"]


# ------------------------------------------------------------------- 검증


def test_lineage_fields_must_come_together(client, prompt) -> None:
    parent = _run(client, prompt)

    only_source = client.post(
        "/api/jobs",
        json={"prompt_id": prompt["id"], "provider": "test", "source_job_id": parent["id"]},
    )
    assert only_source.status_code == 400

    only_relation = client.post(
        "/api/jobs",
        json={"prompt_id": prompt["id"], "provider": "test", "relation_type": "CONTINUED"},
    )
    assert only_relation.status_code == 400

    unknown_source = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "source_job_id": "does-not-exist",
            "relation_type": "CONTINUED",
        },
    )
    assert unknown_source.status_code == 404

    bad_relation = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "source_job_id": parent["id"],
            "relation_type": "SOMETHING_ELSE",
        },
    )
    assert bad_relation.status_code == 422


def test_cannot_continue_from_a_run_without_a_report(client, prompt) -> None:
    failed = _run(client, prompt, claim_text="TEST_FAIL")
    assert failed["status"] == "FAILED"

    response = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "source_job_id": failed["id"],
            "relation_type": "CONTINUED",
        },
    )
    assert response.status_code == 400

    # 자료만 재사용하는 것은 실패한 실행에서도 허용한다.
    reanalyzed = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "source_job_id": failed["id"],
            "relation_type": "REANALYZED",
        },
    )
    assert reanalyzed.status_code == 201


# ------------------------------------------------------------------- 삭제


def test_deleting_the_source_leaves_the_follow_up_intact(client, prompt) -> None:
    parent = _run(client, prompt, claim_text="청구항 1.", batch_id=_upload(client))
    child = _run(
        client,
        prompt,
        claim_text="청구항 1.\n청구항 2.",
        source_job_id=parent["id"],
        relation_type="CONTINUED",
    )
    child_paths = _stored_paths(child["id"])

    assert client.delete(f"/api/history/{parent['id']}").status_code == 204
    assert client.get(f"/api/history/{parent['id']}").status_code == 404

    survivor = client.get(f"/api/history/{child['id']}")
    assert survivor.status_code == 200
    data = survivor.json()

    # 끊긴 참조는 남기고, 표시용 라벨과 이전 보고서 사본으로 계보를 보존한다.
    assert data["source_job_id"] == parent["id"]
    assert data["source_job_label"]
    assert data["prior_report"] == parent["result_text"]
    assert len(data["attachments"]) == 1
    assert all(path.is_file() for path in child_paths)
    assert client.get(f"/api/jobs/{child['id']}/final-prompt").status_code == 200


def test_thread_listing_and_bulk_delete(client, prompt) -> None:
    root = _run(client, prompt, claim_text="청구항 1.", batch_id=_upload(client))
    middle = _run(
        client,
        prompt,
        claim_text="청구항 1.\n청구항 2.",
        source_job_id=root["id"],
        relation_type="CONTINUED",
    )
    leaf = _run(
        client,
        prompt,
        claim_text="청구항 1.\n청구항 2.\n청구항 3.",
        source_job_id=middle["id"],
        relation_type="CONTINUED",
    )

    thread = client.get(f"/api/history/{root['id']}/thread").json()
    assert [item["id"] for item in thread] == [root["id"], middle["id"], leaf["id"]]
    assert thread[0]["descendant_count"] == 2
    assert thread[1]["descendant_count"] == 1
    assert thread[2]["descendant_count"] == 0

    listed = {item["id"]: item for item in client.get("/api/history").json()}
    assert listed[root["id"]]["descendant_count"] == 2
    assert listed[middle["id"]]["relation_type"] == "CONTINUED"
    assert listed[root["id"]]["relation_type"] is None

    # 중간에서 지우면 그 아래만 사라지고 위는 남는다.
    assert client.delete(f"/api/history/{middle['id']}/thread").json()["deleted"] == 2
    assert client.get(f"/api/history/{root['id']}").status_code == 200
    assert client.get(f"/api/history/{middle['id']}").status_code == 404
    assert client.get(f"/api/history/{leaf['id']}").status_code == 404


def test_thread_delete_removes_work_dirs(client, prompt) -> None:
    root = _run(client, prompt, batch_id=_upload(client))
    child = _run(
        client, prompt, source_job_id=root["id"], relation_type="REANALYZED"
    )
    paths = _stored_paths(root["id"]) + _stored_paths(child["id"])
    assert all(path.is_file() for path in paths)

    assert client.delete(f"/api/history/{root['id']}/thread").json()["deleted"] == 2
    assert not any(path.exists() for path in paths)
