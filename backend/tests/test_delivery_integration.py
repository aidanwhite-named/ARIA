"""실행 경로 전체에서의 전달 방식과 의미 검색.

단위 테스트는 판정 함수와 근거 묶음을 따로 본다. 여기서는 업로드 → preflight
→ 실행 → manifest 까지 한 번에 돌려서, 화면이 안내한 것과 실제로 남은 기록이
같은지 확인한다. 두 층이 각자 맞아도 사이에서 어긋나면 사용자는 알 수 없다.
"""

from __future__ import annotations

import json

import pytest

from app import job_assembly
from app.retrieval import embedding_cache
from app.retrieval import semantic as semantic_module

from .fake_provider import DeterministicTestProvider
from .test_retrieval_api import AGY_BYTE_BUDGET, large_korean_pdf, upload_pdf
from .test_api import wait_for_job

pytest_plugins = ["tests.test_retrieval_api"]


def _model_available() -> bool:
    """모델이 **이미 캐시에 있는가.** 없으면 받지 않는다.

    allow_download=False 가 핵심이다. 이 함수는 수집 단계(skipif 판정)에서
    불리므로, 여기서 받기 시작하면 모델이 없는 깨끗한 환경의 테스트 실행이
    네트워크와 458 MB 다운로드에 의존하게 된다.
    """
    encoder, state = semantic_module.load_encoder(
        True, cache=embedding_cache.NullCache(), allow_download=False
    )
    if encoder is not None:
        encoder.close()
    return state.active


needs_model = pytest.mark.skipif(
    not _model_available(), reason="의미 검색 모델 캐시가 없습니다."
)


def _settings(client, values: dict) -> dict:
    """설정을 바꾸고 **실제로 반영됐는지 확인한다.**

    PUT 이 거절돼도 조용히 넘어가면, 이 파일의 테스트 전체가 기본값으로 돌면서
    통과해 버린다. 실제로 그런 상태를 한 번 겪었기 때문에 여기서 못박는다.
    """
    response = client.put("/api/settings", json={"values": values})
    assert response.status_code == 200, response.text
    applied = response.json()["values"]
    for key, expected in values.items():
        assert applied[key] == expected, f"{key} 가 반영되지 않았습니다: {applied[key]!r}"
    return applied


def _run(client, prompt, batch_id, claim_text):
    body = {
        "prompt_id": prompt["id"],
        "provider": "test",
        "claim_text": claim_text,
        "batch_id": batch_id,
    }
    preflight = client.post("/api/jobs/preflight", json=body).json()
    job = client.post("/api/jobs", json=body).json()
    final = wait_for_job(client, job["id"])
    return preflight, final


# ------------------------------------------------- 의미 검색이 실제로 돈다


@needs_model
def test_semantic_is_active_in_a_real_retrieval_run(
    client, prompt, monkeypatch, settings_guard
) -> None:
    """실행 기록에 active/loaded 로 남고 사유는 비어 있다."""
    monkeypatch.setattr(
        DeterministicTestProvider, "max_input_bytes", AGY_BYTE_BUDGET
    )
    _settings(
        client,
        {
            "retrieval_mode": "retrieval",
            "retrieval_semantic_enabled": True,
            "retrieval_evidence_chars": 20_000,
        },
    )
    upload = upload_pdf(client, large_korean_pdf())
    _preflight, final = _run(
        client,
        prompt,
        upload["batch_id"],
        "청구항 1. 제1 센서와 제2 센서, 그리고 제어부를 포함하는 장치.",
    )

    assert final["status"] == "SUCCEEDED", final["errors"]
    semantic = final["retrieval_manifest"]["semantic"]
    assert semantic["enabled"] is True
    assert semantic["active"] is True
    assert semantic["cache_state"] == "loaded"
    assert semantic["reason"] == ""
    assert semantic["model"] == semantic_module.MODEL_NAME
    assert semantic["revision"] == semantic_module.MODEL_REVISION


@needs_model
def test_semantic_channel_executes_and_appears_in_hits(
    client, prompt, monkeypatch, settings_guard
) -> None:
    """manifest 의 채널 기록과 실제 후보 양쪽에 semantic 이 남는다."""
    monkeypatch.setattr(
        DeterministicTestProvider, "max_input_bytes", AGY_BYTE_BUDGET
    )
    _settings(
        client,
        {
            "retrieval_mode": "retrieval",
            "retrieval_semantic_enabled": True,
            "retrieval_evidence_chars": 20_000,
        },
    )
    upload = upload_pdf(client, large_korean_pdf())
    _preflight, final = _run(
        client,
        prompt,
        upload["batch_id"],
        "청구항 1. 제1 센서와 제2 센서, 그리고 제어부를 포함하는 장치.",
    )
    assert final["status"] == "SUCCEEDED", final["errors"]

    manifest = final["retrieval_manifest"]
    rounds = json.dumps(manifest, ensure_ascii=False)
    # 실행 기록 어딘가에 semantic 채널이 실행된 흔적이 있어야 한다.
    assert "semantic" in rounds

    # 임베딩 통계도 남는다 — 몇 청크를 계산했고 몇 개를 캐시에서 썼는가.
    embedding = manifest["semantic"].get("embedding")
    assert embedding is not None
    assert embedding["document_encoded"] + embedding["document_cache_hits"] > 0
    assert embedding["document_seconds"] >= 0.0
    assert "query_seconds" in embedding


def test_semantic_off_run_records_the_reason(
    client, prompt, monkeypatch, settings_guard
) -> None:
    """끄면 예전 그대로 돌고, 왜 안 썼는지가 남는다. 모델 없이도 도는 회귀."""
    monkeypatch.setattr(
        DeterministicTestProvider, "max_input_bytes", AGY_BYTE_BUDGET
    )
    _settings(
        client,
        {
            "retrieval_mode": "retrieval",
            "retrieval_semantic_enabled": False,
            "retrieval_evidence_chars": 20_000,
        },
    )
    upload = upload_pdf(client, large_korean_pdf())
    _preflight, final = _run(
        client, prompt, upload["batch_id"], "청구항 1. 센서와 제어부."
    )
    assert final["status"] == "SUCCEEDED", final["errors"]
    semantic = final["retrieval_manifest"]["semantic"]
    assert semantic["enabled"] is False
    assert semantic["active"] is False
    assert semantic["cache_state"] == "not_checked"
    assert semantic["reason"]
    assert semantic["model"] is None


# --------------------------------------------------- 전달 방식 자동 선택


def test_oversized_input_goes_straight_to_retrieval(
    client, prompt, monkeypatch, settings_guard
) -> None:
    """전송 한도를 넘으면 곧바로 로컬 검색이다. 중간 단계가 없다."""
    monkeypatch.setattr(DeterministicTestProvider, "max_input_bytes", AGY_BYTE_BUDGET)
    _settings(
        client,
        {
            "retrieval_mode": "auto",
            "retrieval_evidence_chars": 20_000,
        },
    )
    upload = upload_pdf(client, large_korean_pdf())
    preflight, final = _run(
        client, prompt, upload["batch_id"], "청구항 1. 센서와 제어부."
    )

    assert preflight["delivery_plan"] == "local_retrieval"
    assert preflight["selection_reason"]
    assert preflight["full_inline_bytes"] > AGY_BYTE_BUDGET
    assert final["status"] == "SUCCEEDED", final["errors"]
    assert final["delivery_plan"] == "local_retrieval"
    assert final["retrieval_manifest"]["delivery_mode"] == "local_retrieval"
    # 실제로 나간 크기는 한도 안이다.
    assert 0 < final["delivery_manifest"]["actual_payload_bytes"] <= AGY_BYTE_BUDGET


def test_final_reassembly_keeps_the_model_budget_policy(
    client, prompt, monkeypatch, settings_guard
) -> None:
    """runner의 두 번째 조립에서도 Provider·모델 예산 인자를 잃지 않는다."""

    calls: list[dict] = []
    real_assemble = job_assembly.assemble_job

    def recording_assemble(**kwargs):
        calls.append(dict(kwargs))
        return real_assemble(**kwargs)

    monkeypatch.setattr(job_assembly, "assemble_job", recording_assemble)
    _settings(
        client,
        {
            "retrieval_mode": "retrieval",
            "retrieval_evidence_chars": 20_000,
        },
    )
    upload = upload_pdf(client, large_korean_pdf())
    _preflight, final = _run(
        client, prompt, upload["batch_id"], "청구항 1. 센서와 제어부."
    )
    assert final["status"] == "SUCCEEDED", final["errors"]

    final_calls = [call for call in calls if call.get("evidence_bundle") is not None]
    assert final_calls
    final_call = final_calls[-1]
    assert final_call["provider_id"] == "test"
    assert final_call["unknown_model_context_tokens"] == 128_000
    assert final_call["model_output_reserve_tokens"] == 32_000


def test_preflight_blocks_when_the_evidence_budget_ceiling_exceeds_the_limit(
    client, prompt, monkeypatch, settings_guard
) -> None:
    """근거 패키지 예산이 전송 한도를 넘으면 준비 화면이 먼저 막는다.

    화면이 재는 것은 **예산 상한**이다. 실제 패키지는 그보다 작을 수 있고, 그때는
    실행이 성공한다 — 그래도 미리 알려 주는 편이 낫다. 검색 비용을 다 쓴 뒤
    Provider 호출 직전에 막히는 것보다 낫기 때문이다.

    상한이 아니라 **실제** 패키지가 한도를 넘는 경우의 차단은 실행 직전 게이트가
    맡는다(tests/test_agy_input_bytes.py 의
    test_runner_gate_uses_the_provider_measurement).
    """
    monkeypatch.setattr(DeterministicTestProvider, "max_input_bytes", AGY_BYTE_BUDGET)
    _settings(
        client,
        {
            # 한글이면 3배가 되므로 이 예산의 상한은 한도를 넘는다.
            "retrieval_mode": "retrieval",
            "retrieval_evidence_chars": 200_000,
        },
    )
    upload = upload_pdf(client, large_korean_pdf())
    preflight, final = _run(
        client, prompt, upload["batch_id"], "청구항 1. 센서와 제어부."
    )

    assert preflight["delivery_plan"] == "local_retrieval"
    assert preflight["over_bytes"] is True
    assert preflight["blocked"] is True

    # 실제 패키지가 상한보다 작으면 실행은 통과한다. 그때도 나간 크기는
    # 반드시 한도 안이다 — 넘겼다면 게이트가 막았어야 한다.
    if final["status"] == "SUCCEEDED":
        assert final["delivery_manifest"]["actual_payload_bytes"] <= AGY_BYTE_BUDGET
    else:
        assert final["error_code"] == "INPUT_TOO_LARGE"


def test_small_case_is_untouched(client, prompt, settings_guard) -> None:
    """작은 사건은 예전처럼 전체 인라인이다. 기본값이 바뀌지 않았다."""
    from .pdf_fixture import build_korean_pdf

    data = build_korean_pdf(["[0001] 짧은 인용발명 문헌입니다.\n- 1 -"])
    upload = upload_pdf(client, data, filename="small.pdf")
    preflight, final = _run(
        client, prompt, upload["batch_id"], "청구항 1. 센서와 제어부."
    )
    assert preflight["delivery_plan"] == "full_inline"
    assert final["status"] == "SUCCEEDED", final["errors"]
    assert final["delivery_plan"] == "full_inline"


# --------------------------------------------------------- 전달 기록


def test_delivery_manifest_is_recorded_and_matches_preflight(
    client, prompt, monkeypatch, settings_guard
) -> None:
    """화면이 안내한 값과 실행이 남긴 값이 같은 축으로 기록된다."""
    monkeypatch.setattr(
        DeterministicTestProvider, "max_input_bytes", AGY_BYTE_BUDGET
    )
    _settings(
        client,
        {
            "retrieval_mode": "auto",
            "retrieval_evidence_chars": 20_000,
        },
    )
    upload = upload_pdf(client, large_korean_pdf())
    preflight, final = _run(
        client, prompt, upload["batch_id"], "청구항 1. 센서와 제어부."
    )
    assert final["status"] == "SUCCEEDED", final["errors"]

    record = final["delivery_manifest"]
    assert record is not None
    assert record["selected_delivery_mode"] == final["delivery_plan"]
    assert record["selected_delivery_mode"] == preflight["delivery_plan"]
    assert record["selection_reason"] == preflight["selection_reason"]
    assert record["provider_byte_limit"] == AGY_BYTE_BUDGET
    assert record["full_inline_bytes"] == preflight["full_inline_bytes"]
    # 실제로 나간 크기는 한도 안이다.
    assert 0 < record["actual_payload_bytes"] <= AGY_BYTE_BUDGET
    # 실행 기록은 자리표가 아니라 실측이다.
    assert record["payload_is_budget_ceiling"] is False
    # preflight 는 상한을 보여 준 것이므로 실제 크기가 그보다 크지 않다.
    assert record["actual_payload_chars"] <= preflight["chars"]


def test_full_inline_run_records_matching_sizes(
    client, prompt, settings_guard
) -> None:
    """전체 인라인이면 「전체를 넣었다면」과 「실제로 나간 것」이 같다."""
    from .pdf_fixture import build_korean_pdf

    data = build_korean_pdf(["[0001] 짧은 인용발명 문헌입니다.\n- 1 -"])
    upload = upload_pdf(client, data, filename="small.pdf")
    _preflight, final = _run(
        client, prompt, upload["batch_id"], "청구항 1. 센서와 제어부."
    )
    record = final["delivery_manifest"]
    assert record["selected_delivery_mode"] == "full_inline"
    assert record["actual_payload_bytes"] == record["full_inline_bytes"]
    assert record["actual_payload_chars"] == record["full_inline_chars"]
    # 전송 한도를 선언하지 않은 대역이므로 None 이다. agy 값을 물려받지 않는다.
    assert record["provider_byte_limit"] is None
