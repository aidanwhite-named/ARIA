from __future__ import annotations

import json

import pytest

from app import analysis_manifest


def _block(items: list[dict]) -> str:
    return (
        "보고서 본문\n"
        "[ARIA_COMPONENT_ANALYSIS_V1]\n"
        + json.dumps({"items": items}, ensure_ascii=False)
        + "\n[/ARIA_COMPONENT_ANALYSIS_V1]\n"
    )


def _item(**overrides) -> dict:
    row = {
        "claim": "청구항 1",
        "symbol": "(A)",
        "feature": "센서 신호를 결합하는 구성",
        "similarity": 92,
        "status": "matched",
        "difference": "",
    }
    row.update(overrides)
    return row


def test_marks_below_threshold_and_not_found_as_searchable() -> None:
    parsed = analysis_manifest.parse(
        _block(
            [
                _item(),
                _item(
                    symbol="(B)",
                    similarity=79,
                    status="below_threshold",
                    difference="제어 관계가 다름",
                ),
                _item(symbol="(C)", similarity=None, status="not_found"),
                _item(symbol="(D)", similarity=None, status="unreadable"),
            ]
        )
    )

    assert [row["id"] for row in parsed["items"]] == ["C001", "C002", "C003", "C004"]
    assert [row["search_eligible"] for row in parsed["items"]] == [
        False,
        True,
        True,
        False,
    ]
    assert parsed["threshold"] == 80


def test_rejects_status_and_score_mismatch() -> None:
    with pytest.raises(analysis_manifest.ComponentAnalysisError, match="맞지 않습니다"):
        analysis_manifest.parse(
            _block([_item(similarity=82, status="below_threshold")])
        )


def test_protocol_block_is_removed_from_user_report() -> None:
    report = _block([_item()])
    visible = analysis_manifest.strip_block(report)
    assert "보고서 본문" in visible
    assert "ARIA_COMPONENT_ANALYSIS_V1" not in visible


def test_requires_exactly_one_block() -> None:
    with pytest.raises(analysis_manifest.ComponentAnalysisError, match="찾지 못했습니다"):
        analysis_manifest.parse("보고서만 있음")
