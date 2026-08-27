"""agy 입력 절삭 방어.

두 겹이다.

  1. 사전 차단 — 실제로 나갈 stream-json 한 줄을 만들어 재고, 180,000 bytes 를
     넘으면 Provider 를 부르지 않는다. 토큰이 소모되지 않는다.
  2. 사후 탐지 — 그 겹을 빠져나갔을 때. agy 는 잘라 놓고도 종료 코드 0 에
     status SUCCESS 로 끝내므로, 출력에 남은 절삭 마커를 잡아 실패로 만든다.

문자 수로 재면 둘 다 새는 이유가 여기 고정돼 있다. 한글 1자는 UTF-8 3 bytes 이고,
JSON 이스케이프는 개행 하나를 2 bytes 로 만든다.
"""

from __future__ import annotations

import json

from app.enums import ErrorCode, JobStatus
from app.evaluation.evaluator import evaluate, truncation_bytes
from app.providers.agy_cli import AgyCliProvider
from app.providers.agy_stream import build_stdin_message
from app.providers.base import ExecutionOutcome
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.codex_cli import CodexCliProvider

LIMIT = AgyCliProvider.max_input_bytes


def _outcome(**kwargs) -> ExecutionOutcome:
    outcome = ExecutionOutcome(
        result_text="정상 보고서 본문", exit_code=0, terminal_reason="completed"
    )
    for key, value in kwargs.items():
        setattr(outcome, key, value)
    return outcome


# --------------------------------------------------------------- 크기 계산


def test_payload_bytes_equals_the_actual_stream_json_line() -> None:
    """계산식이 아니라 **실제로 만들어서** 잰다.

    근사하면 이스케이프 규칙이 바뀌었을 때 조용히 어긋난다. 그 어긋남은 잘린
    실행이 '성공'으로 남는 형태로만 드러난다.
    """
    provider = AgyCliProvider()
    system_prompt = "ARIA 런타임 컨텍스트"
    user_message = '문헌 본문\n둘째 줄 "인용부호" 와 역슬래시 \\ 포함'

    measured = provider.payload_bytes(system_prompt, user_message)

    composed = (
        "[ARIA RUNTIME CONTEXT]\n"
        f"{system_prompt}\n\n"
        f"{user_message}"
    )
    expected = len(build_stdin_message(composed).encode("utf-8"))
    assert measured == expected

    # 만들어진 줄은 실제로 파싱 가능한 stream-json 이어야 한다.
    payload = json.loads(build_stdin_message(composed))
    assert payload["event"] == "user"
    assert payload["message"]["content"] == composed


def test_korean_chars_and_bytes_are_different_axes() -> None:
    """한글 190,000자는 약 570,000 bytes 다. 문자 수로 재면 통과해 버린다."""
    provider = AgyCliProvider()
    text = "가" * 190_000
    assert len(text) == 190_000
    measured = provider.payload_bytes("", text)
    assert measured > 570_000
    assert measured > LIMIT
    # 문자 수만 보면 180,000 "이하"로 보인다 — 그 착각이 이 검사의 이유다.
    assert len(text) > LIMIT  # 문자 수조차 넘지만
    smaller = "가" * 100_000
    assert len(smaller) < LIMIT  # 문자 수로는 통과하는데
    assert provider.payload_bytes("", smaller) > LIMIT  # 바이트로는 초과다


def test_naive_sum_underestimates_because_of_json_escaping() -> None:
    """개행이 많은 문서일수록 단순 합이 실제보다 작다.

    JSON 은 개행을 `\\n` 두 글자로 쓴다. 경계에 걸친 입력이 바로 이 차이만큼
    검사를 빠져나간다.
    """
    provider = AgyCliProvider()
    document = "한국어 특허 문언 한 줄.\n" * 2000
    naive = len("".encode("utf-8")) + len(document.encode("utf-8"))
    actual = provider.payload_bytes("", document)
    assert actual > naive
    # 개행 2000 개가 각각 1 bytes 더 든다. 래퍼까지 더해 최소 2000 bytes 차이.
    assert actual - naive >= 2000


def test_limit_boundary_allows_and_blocks() -> None:
    """한도 이하는 통과, 초과는 차단. 판정 기준은 잰 값 그 자체다."""
    provider = AgyCliProvider()
    # 래퍼와 머리말을 감안해 여유를 두고 만든 뒤, 실제로 재서 확인한다.
    under = "가" * 50_000
    assert provider.payload_bytes("", under) <= LIMIT

    over = "가" * 61_000
    assert provider.payload_bytes("", over) > LIMIT


def test_agy_limit_is_not_applied_to_other_providers() -> None:
    """Codex 와 Claude 는 이 하드캡을 쓰지 않는다.

    agy 의 180,000 은 그 CLI 가 stream-json content 를 자르는 지점이다. 다른
    CLI 의 성질이 아니므로 옮겨 붙이지 않는다. Codex 는 stdin 을 쓰므로 Windows
    명령행 길이 제한(32,767자)과도 무관하다.
    """
    assert AgyCliProvider.max_input_bytes == 180_000
    assert CodexCliProvider.max_input_bytes is None
    assert ClaudeCliProvider.max_input_bytes is None

    # 기본 계산은 감싸기가 없는 단순 합이다.
    text = "가" * 1_000
    assert CodexCliProvider().payload_bytes("", text) == len(text.encode("utf-8"))


# ------------------------------------------------------------- 사후 탐지


def test_plain_truncation_marker_fails_with_input_too_large() -> None:
    """종료 코드 0, is_error 아님, 답변까지 있어도 실패다."""
    verdict = evaluate(_outcome(raw_stdout="…<truncated 548974 bytes>"))
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.INPUT_TOO_LARGE
    assert any("548,974 bytes" in error for error in verdict.errors)
    assert any("폐기" in error for error in verdict.errors)


def test_json_escaped_truncation_marker_is_detected() -> None:
    """stream-json 원문에는 꺾쇠가 이스케이프된 모양으로 온다."""
    raw = json.dumps(
        {"response": "앞부분만 보고 씀 <truncated 12345 bytes>"},
        ensure_ascii=False,
    ).replace("<", "\\u003c").replace(">", "\\u003e")
    assert "\\u003ctruncated" in raw
    verdict = evaluate(_outcome(raw_stdout=raw))
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.INPUT_TOO_LARGE


def test_uppercase_json_escape_is_detected() -> None:
    """JSON 은 \\u003C 대문자 표기도 허용한다."""
    verdict = evaluate(_outcome(raw_stdout="x \\u003Ctruncated 7 bytes\\u003E y"))
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.INPUT_TOO_LARGE


def test_marker_restored_into_the_parsed_answer_is_detected() -> None:
    """원시 출력에 없고 파싱된 답변에만 남은 경우도 잡는다."""
    verdict = evaluate(
        _outcome(
            raw_stdout="",
            result_text="분석 결과입니다. <truncated 999 bytes>",
        )
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.INPUT_TOO_LARGE


def test_normal_technical_phrase_is_not_a_false_positive() -> None:
    """「truncated signed distance function」은 절삭이 아니다.

    이 오탐이 생기면 정상 실행이 실패로 뒤집힌다. 3D 관련 문헌에서는 흔한 말이다.
    """
    verdict = evaluate(
        _outcome(
            result_text=(
                "인용발명은 truncated signed distance function 을 사용한다. "
                "또한 입력이 truncated 되었다는 서술이 100 bytes 단위로 나온다."
            )
        )
    )
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


def test_truncation_bytes_helper_reports_missing_amount() -> None:
    assert truncation_bytes("<truncated 100 bytes>") == 100
    # 한 출처 안의 여러 마커는 더한다. 출처 사이는 더하지 않는다 —
    # 아래 test_the_same_marker_in_both_sources_is_not_counted_twice 참조.
    assert truncation_bytes("<truncated 100 bytes> <truncated 50 bytes>") == 150
    assert truncation_bytes("truncated signed distance function") is None
    assert truncation_bytes("<truncated bytes>") is None
    assert truncation_bytes("") is None


# ------------------------------------------- 빈 성공 / 입력 토큰 0


def test_empty_success_with_zero_input_tokens_is_not_a_success() -> None:
    """종료 코드 0 · SUCCESS · 오류 없음이어도 성공으로 남기지 않는다.

    입력 토큰이 0 이면 프롬프트가 모델에 닿지 않은 것이다. 빈 답변과 원인이
    다르므로 사유를 따로 적는다.
    """
    verdict = evaluate(
        _outcome(result_text="", usage={"input_tokens": 0, "output_tokens": 0})
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.EMPTY_RESULT
    assert any("입력 토큰이 0" in error for error in verdict.errors)


def test_empty_result_without_usage_still_fails() -> None:
    """사용량을 보고하지 않는 Provider 라도 빈 결과는 실패다."""
    verdict = evaluate(_outcome(result_text="", usage=None))
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.EMPTY_RESULT
    # 모르는 것을 0 으로 단정하지 않는다.
    assert not any("입력 토큰이 0" in error for error in verdict.errors)


def test_non_zero_input_tokens_with_content_succeeds() -> None:
    """정상 실행은 그대로 통과한다."""
    verdict = evaluate(_outcome(usage={"input_tokens": 12345, "output_tokens": 900}))
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


# ------------------------------- 외부 리뷰에서 나온 회귀 (2026-08-27)


def test_the_same_marker_in_both_sources_is_not_counted_twice() -> None:
    """파싱된 답변은 원시 출력에서 나온 것이다. 같은 절삭이 두 번 세어지면 안 된다.

    실패 판정은 어느 쪽이든 맞지만, 사용자에게 보여 주는 누락량이 두 배가 된다.
    """
    marker = "<truncated 500 bytes>"
    assert truncation_bytes(f"원시 {marker}", f"답변 {marker}") == 500

    verdict = evaluate(
        _outcome(raw_stdout=f"원시 {marker}", result_text=f"답변 {marker}")
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.INPUT_TOO_LARGE
    assert any("500 bytes" in error for error in verdict.errors)
    assert not any("1,000 bytes" in error for error in verdict.errors)


def test_multiple_markers_within_one_source_are_summed() -> None:
    """한 출처 안의 여러 마커는 실제로 여러 덩어리가 잘린 것이다."""
    assert truncation_bytes("<truncated 100 bytes> ... <truncated 50 bytes>") == 150


def test_sources_reporting_different_amounts_take_the_larger() -> None:
    """한쪽이 일부만 담고 있으면 더 많이 본 쪽을 믿는다."""
    assert truncation_bytes("<truncated 100 bytes>", "<truncated 700 bytes>") == 700
    assert truncation_bytes("", "<truncated 42 bytes>") == 42


def test_exact_boundary_allows_the_limit_and_blocks_one_byte_over() -> None:
    """정확히 한도인 입력은 통과하고, 1 bytes 더하면 막힌다.

    앞의 경계 테스트는 여유를 두고 재는 근사였다. 여기서는 직렬화된 크기가
    **정확히 180,000** 이 되도록 맞춘 뒤 그 한 바이트를 확인한다. 판정이
    `<=` 인지 `<` 인지가 여기서 갈린다.
    """
    provider = AgyCliProvider()
    # ASCII 한 글자가 직렬화 크기를 정확히 1 bytes 올린다(이스케이프 없음).
    body = "a" * 100_000
    overhead = provider.payload_bytes("", body) - len(body)
    at_limit = "a" * (LIMIT - overhead)
    assert provider.payload_bytes("", at_limit) == LIMIT

    one_over = at_limit + "a"
    assert provider.payload_bytes("", one_over) == LIMIT + 1


def test_runner_gate_uses_the_provider_measurement() -> None:
    """실행 직전 검사가 Provider 계산을 쓴다. 단순 합산으로 되돌아가면 잡는다."""
    import asyncio

    from app.execution.runner import JobRunner

    provider = AgyCliProvider()
    body = "a" * 100_000
    overhead = provider.payload_bytes("", body) - len(body)
    at_limit = "a" * (LIMIT - overhead)

    failures: list[tuple[str, str]] = []

    runner = JobRunner.__new__(JobRunner)

    async def fake_fail(job_id, code, message):
        failures.append((code, message))

    runner._fail = fake_fail  # type: ignore[method-assign]

    async def check(text: str) -> bool:
        return await runner._reject_if_over_byte_budget("j", provider, "", text)

    assert asyncio.run(check(at_limit)) is False
    assert not failures

    assert asyncio.run(check(at_limit + "a")) is True
    assert failures
    code, message = failures[0]
    assert code == ErrorCode.INPUT_TOO_LARGE
    # 안내에 실제로 잰 값과 한도가 함께 나와야 한다.
    assert f"{LIMIT + 1:,} bytes" in message
    assert f"{LIMIT:,} bytes" in message
