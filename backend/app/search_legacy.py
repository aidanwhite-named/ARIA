"""Read-only version adapter. Old classification fields never enter execution."""
from __future__ import annotations
import copy

def view(manifest: dict) -> dict:
    value = copy.deepcopy(manifest)
    version = value.get("version", 0)
    if not isinstance(version, int) or version > 14:
        raise ValueError("지원하지 않는 검색 기록 버전입니다.")
    if version == 14:
        return value
    value["legacy"] = True
    value["legacy_version"] = version
    value["status"] = "incomplete" if value.get("error") else "complete"
    for item in (value.get("reported") or {}).get("candidates", []):
        item["legacy_classification"] = {
            key: item.get(key) for key in
            ("group", "provisional_group", "classification_basis", "classification_outcome")
        }
        item["evidence_level"] = "legacy"
        item["verification_issues"] = []
        # Saved reports remain untouched. A re-render does not certify old quotes.
        for row in item.get("mapping", []):
            row["quote_verified"] = False
            row["support_verified"] = False
    return value
