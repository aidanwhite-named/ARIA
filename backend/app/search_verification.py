"""Mechanical evidence checks only. Never choose, rank, or reclassify candidates."""
from __future__ import annotations

import copy
import re
from . import search_manifest as manifest
from .config import PATHS
from .patent_search.artifacts import ArtifactStore
from .patent_search.base import EvidenceRef, FieldValue
from .patent_search.provenance import verify_excerpt

ISSUE_LABELS = {
    "identifier_unverified": "식별 미확인",
    "identifier_invalid": "식별자 형식 오류",
    "identifier_mismatch": "응답 또는 URL의 문헌 식별자 불일치",
    "source_not_read": "페이지 본문 열람 미확인",
    "quote_unverified": "직접 인용 검증 불가",
    "support_unverified": "근거 문장 대조 미확인",
    "duplicate_group_conflict": "동일 문헌에 대한 LLM 그룹 충돌",
    "publication_date_conflict": "공개일 출처 충돌",
    "source_conflict": "출처 간 필드 내용 차이",
}
LEVEL_LABELS = {
    "search_snippet_only": "검색 스니펫·모델 판단 / 원문 미검증",
    "source_page_reviewed": "페이지 열람 확인 / 원문 인용 미검증",
    "official_bibliographic": "공식 서지 확보",
    "official_abstract": "공식 초록 확보",
    "official_claims": "공식 청구항 확보",
    "official_full_text": "공식 전문 확보",
}
SCOPES = ("bibliographic", "abstract", "claims", "description", "family")

def _key(candidate: dict) -> str:
    return manifest.identity_key(candidate.get("doc_number", ""), candidate.get("doi", ""))

def _record_key(record: dict) -> str:
    number = str(record.get("document_number") or "")
    return manifest.identity_key(doi=number) if number.lower().startswith("10.") else manifest.identity_key(number)

def _ref_key(ref: dict) -> tuple:
    return tuple(str(ref.get(key) or "") for key in ("artifact_id", "field_path", "profile_id"))

def _scope(field: str) -> str:
    prefix = field.split(":")[0]
    if prefix in ("abstract", "claims", "description", "family", "full_text"):
        return prefix
    return "bibliographic"

def _matching_sources(candidate: dict, journal: list[dict], store) -> list[dict]:
    found = []
    for call in journal:
        if call.get("state") != "completed" or call.get("ok") is not True:
            continue
        result = call.get("result") or {}
        for record in result.get("records", []):
            if _record_key(record) != _key(candidate):
                continue
            refs = record.get("evidence_refs") or {}
            verified_fields = {}
            for name, text in (record.get("fields") or {}).items():
                ref = refs.get(name)
                if not isinstance(text, str) or not text or not isinstance(ref, dict):
                    continue
                evidence = EvidenceRef(*_ref_key(ref))
                check = verify_excerpt(excerpt=text, field=FieldValue(text, evidence), store=store)
                if check.verified:
                    verified_fields[name] = {"text": text, "evidence_ref": ref}
            if verified_fields:
                found.append({"tool": call.get("tool"), "call_id": call.get("id"),
                              "url": record.get("url", ""), "document_number": record.get("document_number"),
                              "fields": verified_fields})
    return found

def verify(reported: dict, observed: dict, journal: list[dict], *, store=None) -> dict:
    result = copy.deepcopy(reported)
    store = store or ArtifactStore(PATHS.evidence_dir.resolve())
    read_urls = {manifest.normalize_url(url) for url in observed.get("succeeded_fetch_urls", [])}
    candidates, seen = [], {}
    for candidate in result["candidates"]:
        candidate["verification_issues"] = []
        key = _key(candidate)
        if key not in ("patent:", "doi:") and key in seen:
            first = seen[key]
            if first["group"] != candidate["group"]:
                first["verification_issues"].append("duplicate_group_conflict")
            continue
        seen[key] = candidate
        candidates.append(candidate)
    result["candidates"] = candidates
    for candidate in candidates:
        issues = candidate["verification_issues"]
        number, doi = candidate.get("doc_number", ""), candidate.get("doi", "")
        if doi:
            if not re.fullmatch(r"10\.\d{4,9}/\S+", manifest.identity_key(doi=doi)[4:], re.I):
                issues.append("identifier_invalid")
        elif number and not re.fullmatch(r"[A-Z]{2}\d+[A-Z]?\d?", manifest.identity_key(number)[7:]):
            issues.append("identifier_invalid")
        sources = _matching_sources(candidate, journal, store)
        candidate["evidence_sources"] = sources
        scope = {name: "not_requested" for name in SCOPES}
        delivered = {}
        values_by_field = {}
        for source in sources:
            for name, field in source["fields"].items():
                kind = _scope(name)
                if kind in scope:
                    scope[kind] = "verified"
                delivered[_ref_key(field["evidence_ref"])] = field
                values_by_field.setdefault(name, set()).add(field["text"])
        # Fetch attempts are independent from the returned content. Failed or
        # missing constituents must not become document-wide 'verified'.
        for call in journal:
            args = call.get("arguments") or {}
            requested = args.get("publication_number") or args.get("doi") or ""
            if not requested:
                continue
            request_key = manifest.identity_key(doi=requested) if args.get("doi") else manifest.identity_key(requested)
            if request_key != _key(candidate):
                continue
            requested_scope = args.get("constituent", "abstract" if args.get("doi") else "claims")
            requested_scope = "bibliographic" if requested_scope == "biblio" else requested_scope
            if requested_scope in scope and scope[requested_scope] != "verified":
                scope[requested_scope] = "unavailable"
            response = call.get("result") or {}
            if response.get("identifier_matched") is False:
                issues.append("identifier_mismatch")
        level = "search_snippet_only"
        url = manifest.normalize_url(candidate.get("url"))
        # Reading a different publication is not evidence for this candidate.
        explicit = re.search(r"/patent/([A-Z]{2}[\d/.-]+[A-Z]\d?)", str(candidate.get("url") or ""), re.I)
        url_mismatch = bool(explicit and number and
                            manifest.identity_key(explicit.group(1)) != manifest.identity_key(number))
        if url_mismatch:
            issues.append("identifier_mismatch")
        if url and url in read_urls and not url_mismatch:
            level = "source_page_reviewed"
        elif url:
            issues.append("source_not_read")
        if sources:
            level = "official_bibliographic"
            if scope["abstract"] == "verified":
                level = "official_abstract"
            if scope["claims"] == "verified":
                level = "official_claims"
            if "full_text" in values_by_field:
                level = "official_full_text"
        else:
            issues.append("identifier_unverified")
        candidate["verification_scope"] = scope
        candidate["evidence_level"] = level
        candidate["reported_publication_date"] = candidate.get("publication_date", "")
        dates = values_by_field.get("publication_date", set())
        candidate["publication_date"] = next(iter(dates)) if len(dates) == 1 else ""
        if len(dates) > 1:
            issues.append("publication_date_conflict")
        if any(len(values) > 1 for field, values in values_by_field.items() if field != "publication_date"):
            issues.append("source_conflict")
        # Candidate-level excerpts without a field reference cannot be verified.
        if candidate.get("verbatim_excerpt"):
            issues.append("quote_unverified")
        candidate["verbatim_excerpt"] = ""
        candidate["source_location"] = ""
        for row in candidate["mapping"]:
            ref = row.get("evidence_ref")
            field = delivered.get(_ref_key(ref)) if isinstance(ref, dict) else None
            support = row.get("support_text") or ""
            row["support_verified"] = False
            row["quote_verified"] = False
            if field and support and support in field["text"]:
                check = verify_excerpt(
                    excerpt=support, field=FieldValue(field["text"], EvidenceRef(*_ref_key(ref))), store=store
                )
                row["support_verified"] = check.verified
            if support and not row["support_verified"]:
                issues.append("support_unverified")
            excerpt = row.get("verbatim_excerpt") or ""
            if field and excerpt and excerpt in field["text"]:
                check = verify_excerpt(
                    excerpt=excerpt, field=FieldValue(field["text"], EvidenceRef(*_ref_key(ref))), store=store
                )
                row["quote_verified"] = check.original_verified
            if not row["quote_verified"]:
                if excerpt:
                    issues.append("quote_unverified")
                row["verbatim_excerpt"] = ""
                row["translation"] = ""
                row["source_location"] = ""
            # Technical degree/counterpart/similar/different are model judgments.
            # They are intentionally never changed by these checks.
        candidate["verification_issues"] = list(dict.fromkeys(issues))
    return result
