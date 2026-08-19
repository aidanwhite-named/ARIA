"""64KB 를 넘는 단일 JSONL 행 보존.

asyncio 의 StreamReader.readline() 은 한 줄이 스트림 limit(기본 64KB)을 넘으면
내부 버퍼를 비우고 ValueError 를 던진다. 즉 데이터가 사라진다. 실측으로
200,013 바이트 중 131,070 바이트가 소실됐다.

Claude 의 최종 result 이벤트와 agy 의 result 이벤트는 답변 전문을 한 줄에
담기 때문에, 긴 분석 결과에서 현실적으로 발생한다. 그래서 process.py 는
readline 대신 청크를 읽어 개행을 직접 찾는다.
"""

from __future__ import annotations

import json
import sys

from app.execution import process as proc
from app.providers.agy_stream import AgyStreamParser
from app.providers.claude_stream import ClaudeStreamParser
from app.providers.env import build_child_env


def _writer_script(tmp_path, payload_lines: list[str]):
    script = tmp_path / "writer.py"
    body = "\n".join(
        f"sys.stdout.write({line!r} + '\\n')" for line in payload_lines
    )
    script.write_text(f"import sys\n{body}\nsys.stdout.flush()\n", encoding="utf-8")
    return script


async def _collect(tmp_path, payload_lines: list[str]) -> tuple[list[str], str]:
    script = _writer_script(tmp_path, payload_lines)
    lines: list[str] = []

    async def handler(line: str) -> None:
        lines.append(line)

    result = await proc.run_streaming(
        job_id="longline-test",
        argv=[sys.executable, str(script)],
        cwd=tmp_path,
        env=build_child_env(),
        on_stdout_line=handler,
        timeout_seconds=120,
    )
    assert result.exit_code == 0, result.stderr
    return lines, result.stdout


async def test_single_200kb_line_is_not_truncated(tmp_path) -> None:
    payload = "가" * 200_000
    lines, _ = await _collect(tmp_path, [payload])
    assert len(lines) == 1
    assert len(lines[0]) == 200_000


async def test_long_line_does_not_swallow_following_lines(tmp_path) -> None:
    """예전 구현에서는 긴 줄 뒤의 줄까지 통째로 사라졌다."""
    lines, _ = await _collect(tmp_path, ["B" * 300_000, "SECOND_LINE", "THIRD_LINE"])
    assert len(lines) == 3
    assert len(lines[0]) == 300_000
    assert lines[1] == "SECOND_LINE"
    assert lines[2] == "THIRD_LINE"


async def test_raw_stdout_is_fully_captured(tmp_path) -> None:
    _, stdout = await _collect(tmp_path, ["C" * 150_000, "TAIL"])
    assert stdout.count("C") == 150_000
    assert "TAIL" in stdout


async def test_large_claude_result_event_parses(tmp_path) -> None:
    """실제 형태: 답변 전문이 담긴 단일 result 행."""
    answer = "분석 결과 본문입니다. " * 20_000  # 대략 240KB
    event = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": answer,
            "terminal_reason": "completed",
            "usage": {"input_tokens": 1000, "output_tokens": 90_000},
        },
        ensure_ascii=False,
    )
    assert len(event) > 200_000

    lines, _ = await _collect(tmp_path, [event])
    assert len(lines) == 1

    parser = ClaudeStreamParser()
    parser.feed(lines[0])
    assert parser.state.parse_errors == []
    assert parser.state.saw_result
    assert parser.state.final_text == answer


async def test_large_agy_result_event_parses(tmp_path) -> None:
    answer = "Gemini 응답 본문. " * 20_000
    event = json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": "abc",
                "status": "SUCCESS",
                "response": answer,
                "num_turns": 1,
                "usage": {"total_tokens": 1234},
            },
        },
        ensure_ascii=False,
    )
    assert len(event) > 200_000

    lines, _ = await _collect(tmp_path, [event])
    parser = AgyStreamParser()
    parser.feed(lines[0])
    assert parser.state.parse_errors == []
    assert parser.state.final_text == answer
    assert not parser.state.is_error


async def test_utf8_multibyte_split_across_chunks(tmp_path) -> None:
    """청크 경계가 한글 바이트 중간을 가르더라도 깨지지 않아야 한다."""
    payload = "한글테스트" * 40_000
    lines, _ = await _collect(tmp_path, [payload])
    assert len(lines) == 1
    assert lines[0] == payload
