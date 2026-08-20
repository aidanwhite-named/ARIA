"""문헌 매핑 프로토콜.

후속 분석에서 인용발명 번호를 유지하려고 보고서 전체를 다시 넣지 않는다.
검증된 매핑만 넘긴다. 여기서 지키려는 성질은 이렇다.

  1. 매핑은 보고서에서 읽어 첨부와 대조해 검증한다. 모델이 쓴 별칭만 믿고
     attachment_id 나 해시는 ARIA 가 채운다.
  2. 읽지 못해도 실행은 성공이다. 번호 유지 후속 실행만 막는다.
     보고서 전체 전달로 조용히 되돌아가지 않는다.
  3. MAPPED 는 번호와 이전 청구항만 넘기고 이전 보고서는 넘기지 않는다.
"""

from __future__ import annotations

import json

import pytest

from app.citation_mapping import (
    AliasedAttachment,
    MappingError,
    parse,
    rebind,
    render,
    strip_block,
)
from app.config import PROMPT_DIR

from .conftest import wait_for_job

CAPABLE_PROMPT = """<!-- ARIA_PROMPT_METADATA
{
  "name": "매핑 지원 테스트 프롬프트",
  "output_mode": "markdown",
  "capabilities": ["citation_mapping_v1"],
  "enabled": true
}
-->
청구항과 인용발명을 대비하십시오."""

PLAIN_PROMPT = """<!-- ARIA_PROMPT_METADATA
{
  "name": "매핑 미지원 테스트 프롬프트",
  "output_mode": "markdown",
  "enabled": true
}
-->
청구항과 인용발명을 대비하십시오."""


def _write_prompt(filename: str, content: str) -> str:
    path = PROMPT_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.name


@pytest.fixture()
def capable_prompt(client):
    prompt_id = _write_prompt("mapping-capable.md", CAPABLE_PROMPT)
    yield client.get(f"/api/prompts/{prompt_id}").json()
    (PROMPT_DIR / prompt_id).unlink(missing_ok=True)


@pytest.fixture()
def plain_prompt(client):
    prompt_id = _write_prompt("mapping-plain.md", PLAIN_PROMPT)
    yield client.get(f"/api/prompts/{prompt_id}").json()
    (PROMPT_DIR / prompt_id).unlink(missing_ok=True)


def _upload(client, *names: str) -> str:
    files = [
        ("files", (name, f"{name} 본문".encode(), "text/plain")) for name in names
    ]
    response = client.post(
        "/api/uploads",
        files=files,
        data={"roles": json.dumps(["CITATION"] * len(names))},
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


# ------------------------------------------------------------------- 단위


def _aliases(*names: str) -> dict[str, AliasedAttachment]:
    return {
        f"ATT-{i:02d}": AliasedAttachment(
            alias=f"ATT-{i:02d}",
            attachment_id=f"id-{i}",
            sha256=f"sha-{i}",
            original_filename=name,
        )
        for i, name in enumerate(names, start=1)
    }


def _block(items: list[dict]) -> str:
    payload = json.dumps({"items": items}, ensure_ascii=False)
    return f"\n[ARIA_CITATION_MAPPING_V1]\n{payload}\n[/ARIA_CITATION_MAPPING_V1]\n"


def test_parse_fills_identifiers_that_the_model_never_wrote() -> None:
    aliases = _aliases("a.pdf", "b.pdf")
    report = "보고서 본문" + _block(
        [{"citation_number": 1, "attachment": "ATT-02", "document_number": "KR10-1"}]
    )
    mapping = parse(report, aliases)
    assert mapping == {
        "version": 1,
        "items": [
            {
                "citation_number": 1,
                "attachment_id": "id-2",
                "attachment_sha256": "sha-2",
                "filename": "b.pdf",
                "document_number": "KR10-1",
            }
        ],
    }


@pytest.mark.parametrize(
    "items, fragment",
    [
        ([], "items"),
        ([{"citation_number": 0, "attachment": "ATT-01", "document_number": "K"}], "1 이상"),
        (
            [
                {"citation_number": 1, "attachment": "ATT-01", "document_number": "K"},
                {"citation_number": 1, "attachment": "ATT-02", "document_number": "K2"},
            ],
            "중복",
        ),
        (
            [
                {"citation_number": 1, "attachment": "ATT-01", "document_number": "K"},
                {"citation_number": 2, "attachment": "ATT-01", "document_number": "K2"},
            ],
            "두 개의 인용발명 번호",
        ),
        ([{"citation_number": 1, "attachment": "ATT-99", "document_number": "K"}], "없는 자료 번호"),
        ([{"citation_number": 1, "attachment": "id-1", "document_number": "K"}], "형식이 아닙니다"),
        ([{"citation_number": 1, "attachment": "ATT-01", "document_number": "  "}], "문헌번호가 비어"),
    ],
)
def test_parse_rejects_malformed_mappings(items, fragment) -> None:
    with pytest.raises(MappingError) as exc:
        parse("본문" + _block(items), _aliases("a.pdf", "b.pdf"))
    assert fragment in str(exc.value)


def test_parse_rejects_more_than_one_block() -> None:
    one = _block([{"citation_number": 1, "attachment": "ATT-01", "document_number": "K"}])
    with pytest.raises(MappingError) as exc:
        parse("본문" + one + one, _aliases("a.pdf"))
    assert "하나만" in str(exc.value)


def test_strip_block_leaves_the_readable_report() -> None:
    report = "# 보고서\n\n본문입니다.\n" + _block(
        [{"citation_number": 1, "attachment": "ATT-01", "document_number": "K"}]
    )
    assert strip_block(report) == "# 보고서\n\n본문입니다.\n"


def test_strip_block_handles_a_fenced_block() -> None:
    payload = json.dumps(
        {"items": [{"citation_number": 1, "attachment": "ATT-01", "document_number": "K"}]}
    )
    report = (
        "본문\n\n```json\n[ARIA_CITATION_MAPPING_V1]\n"
        + payload
        + "\n[/ARIA_CITATION_MAPPING_V1]\n```\n"
    )
    assert "ARIA_CITATION_MAPPING" not in strip_block(report)
    assert parse(report, _aliases("a.pdf"))["items"][0]["citation_number"] == 1


def test_rebind_follows_content_not_identifiers() -> None:
    """복제하면 attachment_id 가 바뀐다. 같은 자료라는 근거는 sha256 이다."""

    class Row:
        def __init__(self, attachment_id: str, sha256: str) -> None:
            self.attachment_id = attachment_id
            self.sha256 = sha256

    mapping = {
        "version": 1,
        "items": [
            {
                "citation_number": 1,
                "attachment_id": "old-1",
                "attachment_sha256": "sha-1",
                "filename": "a.pdf",
                "document_number": "KR10-1",
            }
        ],
    }
    rebound = rebind(mapping, [Row("new-1", "sha-1")])
    assert rebound["items"][0]["attachment_id"] == "new-1"
    assert rebound["items"][0]["document_number"] == "KR10-1"

    with pytest.raises(MappingError):
        rebind(mapping, [Row("new-9", "sha-other")])


def test_render_uses_the_alias_of_this_run() -> None:
    aliases = _aliases("a.pdf", "b.pdf")
    mapping = {
        "version": 1,
        "items": [
            {
                "citation_number": 1,
                "attachment_id": "id-2",
                "attachment_sha256": "sha-2",
                "filename": "b.pdf",
                "document_number": "KR10-1",
            }
        ],
    }
    assert render(mapping, aliases) == "인용발명 1 = KR10-1 (ATT-02, b.pdf)"


# ------------------------------------------------------------------- 통합


def test_mapping_is_verified_and_removed_from_the_deliverable(
    client, capable_prompt
) -> None:
    job = _run(client, capable_prompt, batch_id=_upload(client, "c1.txt", "c2.txt"))
    assert job["status"] == "SUCCEEDED"

    mapping = job["citation_mapping"]
    assert mapping is not None
    assert [item["citation_number"] for item in mapping["items"]] == [1, 2]
    # 모델은 별칭만 썼다. 식별자와 해시는 ARIA 가 채운 것이라 첨부와 일치한다.
    by_hash = {a["sha256"]: a["attachment_id"] for a in job["attachments"]}
    for item in mapping["items"]:
        assert by_hash[item["attachment_sha256"]] == item["attachment_id"]

    # 사용자가 받아 가는 보고서에는 프로토콜 블록이 남지 않는다.
    assert "ARIA_CITATION_MAPPING" not in (job["result_text"] or "")
    assert "ARIA_CITATION_MAPPING" in client.get(f"/api/jobs/{job['id']}/raw").text


def test_mapping_is_not_expected_when_the_prompt_does_not_declare_it(
    client, plain_prompt
) -> None:
    job = _run(client, plain_prompt, batch_id=_upload(client, "c1.txt"))
    assert job["status"] == "SUCCEEDED"
    assert job["citation_mapping"] is None
    assert job["citation_mapping_error"] is None
    assert job["warnings"] == []
    # 선언하지 않은 프롬프트의 출력은 손대지 않는다.
    assert "ARIA_CITATION_MAPPING" in (job["result_text"] or "")


def test_unreadable_mapping_warns_but_keeps_the_run_successful(
    client, capable_prompt
) -> None:
    job = _run(
        client,
        capable_prompt,
        claim_text="TEST_BADMAP",
        batch_id=_upload(client, "c1.txt"),
    )
    assert job["status"] == "SUCCEEDED"
    assert job["citation_mapping"] is None
    assert job["citation_mapping_error"]
    assert any("문헌 매핑을 읽지 못했습니다" in w for w in job["warnings"])

    # 번호 유지 후속 실행만 막는다. 보고서 전체 전달로 폴백하지 않는다.
    refused = client.post(
        "/api/jobs",
        json={
            "prompt_id": capable_prompt["id"],
            "provider": "test",
            "source_job_id": job["id"],
            "relation_type": "MAPPED",
        },
    )
    assert refused.status_code == 400
    assert "번호를 이어받을 수 없습니다" in refused.json()["detail"]


def test_mapped_follow_up_carries_numbers_but_not_the_report(
    client, capable_prompt
) -> None:
    parent = _run(
        client,
        capable_prompt,
        claim_text="청구항 1. 독립항.",
        batch_id=_upload(client, "c1.txt", "c2.txt"),
    )
    child = _run(
        client,
        capable_prompt,
        claim_text="청구항 1. 독립항.\n청구항 2. 제1항에 있어서, 종속 구성.",
        source_job_id=parent["id"],
        relation_type="MAPPED",
    )

    assert child["relation_type"] == "MAPPED"
    assert child["prior_claim_text"] == "청구항 1. 독립항."
    assert child["prior_report"] == ""

    text = _final_prompt(client, child["id"])
    assert "[고정 문헌 매핑]" in text
    assert "[이전 청구항]" in text
    assert "[이전 분석 보고서]" not in text
    # 1차에서 부여한 번호와 문헌번호가 그대로 간다.
    for item in parent["citation_mapping"]["items"]:
        assert f"인용발명 {item['citation_number']} = {item['document_number']}" in text


def test_mapped_follow_up_rebinds_the_mapping_to_cloned_files(
    client, capable_prompt
) -> None:
    parent = _run(client, capable_prompt, batch_id=_upload(client, "c1.txt", "c2.txt"))
    child = _run(
        client, capable_prompt, source_job_id=parent["id"], relation_type="MAPPED"
    )

    bound = child["prior_citation_mapping"]
    child_ids = {a["attachment_id"] for a in child["attachments"]}
    parent_ids = {a["attachment_id"] for a in parent["attachments"]}
    assert child_ids.isdisjoint(parent_ids)

    # 복제로 id 가 바뀌었지만 매핑은 자식의 자료를 가리킨다.
    for item in bound["items"]:
        assert item["attachment_id"] in child_ids

    # 번호와 문헌번호는 그대로다.
    assert [i["citation_number"] for i in bound["items"]] == [
        i["citation_number"] for i in parent["citation_mapping"]["items"]
    ]
    assert [i["document_number"] for i in bound["items"]] == [
        i["document_number"] for i in parent["citation_mapping"]["items"]
    ]


def test_continued_still_sends_the_report_alongside_the_mapping(
    client, capable_prompt
) -> None:
    parent = _run(
        client, capable_prompt, claim_text="청구항 1.", batch_id=_upload(client, "c1.txt")
    )
    child = _run(
        client,
        capable_prompt,
        claim_text="청구항 1.",
        source_job_id=parent["id"],
        relation_type="CONTINUED",
    )
    text = _final_prompt(client, child["id"])
    assert "[고정 문헌 매핑]" in text
    assert "[이전 분석 보고서]" in text


def test_reanalyzed_carries_neither_numbers_nor_report(client, capable_prompt) -> None:
    parent = _run(client, capable_prompt, batch_id=_upload(client, "c1.txt"))
    child = _run(
        client, capable_prompt, source_job_id=parent["id"], relation_type="REANALYZED"
    )
    assert child["prior_citation_mapping"] is None
    text = _final_prompt(client, child["id"])
    assert "[고정 문헌 매핑]" not in text
    assert "[이전 분석 보고서]" not in text


def test_history_reports_whether_a_run_can_seed_numbers(client, capable_prompt) -> None:
    good = _run(client, capable_prompt, batch_id=_upload(client, "c1.txt"))
    bad = _run(
        client,
        capable_prompt,
        claim_text="TEST_NOMAP",
        batch_id=_upload(client, "c2.txt"),
    )
    listed = {item["id"]: item for item in client.get("/api/history").json()}
    assert listed[good["id"]]["has_citation_mapping"] is True
    assert listed[bad["id"]]["has_citation_mapping"] is False
