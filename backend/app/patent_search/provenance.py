"""발췌 하나의 증거 등급을 ARIA 가 직접 계산한다.

판정 단위는 '발췌'다
--------------------
후보(문헌) 단위가 아니다. 같은 특허 안에서도 어떤 발췌는 공식 청구항에서
정확히 확인되고 다른 발췌는 기계번역 초록에서만 확인될 수 있다. 후보 단위
불리언 하나로 게이팅하면 확인된 발췌 하나가 나머지 전부를 원문으로 승격시킨다.
(search_manifest._mapping_row 가 지금 그 구조다. 그 경로는 web 채널에서
original_verified 가 항상 False 라 아직 터지지 않은 잠복 결함이며, patent_db
생산자를 붙일 때 반드시 함께 고쳐야 한다.)

판정 절차 — 어댑터의 값도, 어댑터의 출처 주장도 쓰지 않는다
-----------------------------------------------------------
1. EvidenceRef 가 완전한지 본다. 없으면 검증 불가(MATCH_NONE).
2. 불변 저장소에서 원본 바이트를 다시 읽는다. 읽기 경로에서 SHA-256 을
   재계산하므로 변조·손상이면 여기서 걸린다.
3. 등록된 소스 프로필로 필드를 재추출한다. 텍스트뿐 아니라 **출처
   (source_kind, is_translation)도 프로필에서 나온다.** 어댑터가 준 값은
   어느 것도 판정에 쓰지 않는다.
4. **재추출한 값**에서 발췌를 찾는다.

exact 에는 NFKC·공백 접기를 허용하지 않는다
-------------------------------------------
NFKC 는 ㎜→mm, ℃→°C, ①→1 처럼 문자를 바꾼다. 이 기호들은 한국·일본 특허
명세서에 흔하다. 접고 나서 일치했다고 원문 인용으로 승격하면, 원문에 없는
문자열을 "이 특허는 이렇게 적혀 있다"고 진술하게 된다(실측으로 재현된 결함).

정규화해서 맞은 것은 normalized 로만 둔다. 그것도 유용한 신호지만 원문 인용은
아니다. 또한 normalized 는 span 을 남기지 않는다 — 정규화된 좌표는 원문
좌표가 아니므로, 남기면 원문 위치인 척하게 된다.

원문 등급의 두 관문
-------------------
정책(policy.raw_enabled)과 프로필(raw_capable)이 **둘 다** 참이어야 한다.
정책만 켜면 열리는 구조를 만들지 않는다. 지금은 raw_capable 프로필이 하나도
등록되어 있지 않으므로, 정책을 켜도 원문 등급은 나오지 않는다.

증거는 시점 증명이다
--------------------
아티팩트가 삭제되면 과거 판정을 재현할 수 없다. 그때 판정이 틀렸다는 뜻은
아니지만(판정 내용은 감사 기록에 남는다), 다시 확인할 수는 없다. 우리가
제공하는 것은 시점 증명이지 영구히 재확인 가능한 링크가 아니다.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from . import artifacts, parsers, policy as policy_module
from .base import ORIGINAL_SOURCE_KINDS, EvidenceRef, FieldValue, PatentRecord

# 대조 결과.
MATCH_NONE = "none"              # 재추출한 값에서 찾지 못했거나 검증 불가
MATCH_NORMALIZED = "normalized"  # NFKC·공백 정규화 후에만 일치
MATCH_EXACT = "exact"            # 재추출한 값에 문자 그대로 존재
MATCH_KINDS = (MATCH_NONE, MATCH_NORMALIZED, MATCH_EXACT)


@dataclass(frozen=True)
class ExcerptVerification:
    """발췌 하나에 대한 ARIA 의 판정. 모델 보고도, 어댑터 주장도 아니다."""

    match_kind: str = MATCH_NONE
    source_kind: str = ""
    is_translation: bool = False
    language: str = ""
    match_span: tuple[int, int] | None = None
    evidence: EvidenceRef | None = None
    profile_id: str = ""
    parser_id: str = ""
    parser_version: str = ""
    parser_sha256: str = ""
    policy_version: str = ""
    original_verified: bool = False
    reason: str = ""

    @property
    def verified(self) -> bool:
        """어떤 형태로든 보존 아티팩트에서 확인되었는가."""
        return self.match_kind in (MATCH_EXACT, MATCH_NORMALIZED)


def _normalize(value: str) -> str:
    """normalized 대조용. exact 에는 절대 쓰지 않는다."""
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def verify_excerpt(
    *,
    excerpt: str,
    field: FieldValue,
    store: artifacts.ArtifactStore,
    policy: policy_module.EvidencePolicy | None = None,
) -> ExcerptVerification:
    """발췌 하나를 보존 아티팩트에 대조한다.

    field.value 는 판정에 쓰지 않는다. evidence 가 가리키는 아티팩트에서
    값과 출처를 다시 뽑아 그것을 기준으로 삼는다.
    """
    active = policy or policy_module.current()
    evidence = field.evidence

    def fail(reason: str) -> ExcerptVerification:
        return ExcerptVerification(
            match_kind=MATCH_NONE,
            evidence=evidence,
            policy_version=active.version,
            original_verified=False,
            reason=reason,
        )

    if not excerpt:
        return fail("발췌가 비어 있습니다.")
    if evidence is None or not evidence.complete:
        # 어댑터가 값만 주고 아티팩트 참조를 주지 않았다. 그 값이 맞는지
        # 확인할 방법이 없으므로 검증하지 않는다(fail-closed).
        return fail("아티팩트 참조가 없어 재검증할 수 없습니다.")

    try:
        data = store.read(evidence.artifact_id)
    except artifacts.ArtifactCorrupted as exc:
        return fail(f"아티팩트 무결성 검사 실패: {exc}")
    except artifacts.ArtifactError as exc:
        return fail(f"아티팩트를 읽을 수 없습니다: {exc}")

    try:
        extracted = parsers.extract(data, evidence.field_path, evidence.profile_id)
    except parsers.ParserError as exc:
        return fail(f"필드를 재추출할 수 없습니다: {exc}")

    # --- 대조: 재추출한 값이 기준이다 ------------------------------------
    actual = extracted.text
    position = actual.find(excerpt)
    reason = ""
    if position >= 0:
        match_kind = MATCH_EXACT
        span: tuple[int, int] | None = (position, position + len(excerpt))
    elif _normalize(excerpt) and _normalize(excerpt) in _normalize(actual):
        # 정규화해야 맞는다 = 원문과 문자가 다르다. 원문 인용이 아니다.
        match_kind = MATCH_NORMALIZED
        span = None
        reason = (
            "정규화(NFKC·공백) 후에만 일치했습니다. 원문과 문자가 다르므로 "
            "원문 인용으로 쓸 수 없습니다."
        )
    else:
        return fail("재추출한 필드 값에서 발췌를 찾지 못했습니다.")

    # --- 원문 등급: 정책과 프로필이 둘 다 참이어야 한다 -------------------
    original = bool(
        active.raw_enabled
        and extracted.raw_capable
        and match_kind == MATCH_EXACT
        and extracted.source_kind in ORIGINAL_SOURCE_KINDS
        and not extracted.is_translation
    )

    if match_kind == MATCH_EXACT and not original and not reason:
        if not active.raw_enabled:
            reason = f"정책({active.version})이 원문 등급을 허용하지 않습니다."
        elif not extracted.raw_capable:
            reason = (
                f"프로필 {extracted.profile_id} 는 공식 원문을 증명하지 "
                f"못합니다(raw_capable=False)."
            )
        elif extracted.is_translation:
            reason = "번역 필드이므로 원문 등급을 부여하지 않습니다."
        elif extracted.source_kind not in ORIGINAL_SOURCE_KINDS:
            reason = (
                f"출처가 {extracted.source_kind} 이므로 원문 등급을 "
                f"부여하지 않습니다."
            )

    return ExcerptVerification(
        match_kind=match_kind,
        source_kind=extracted.source_kind,
        is_translation=extracted.is_translation,
        language=extracted.language,
        match_span=span,
        evidence=evidence,
        profile_id=extracted.profile_id,
        parser_id=extracted.parser_id,
        parser_version=extracted.parser_version,
        parser_sha256=extracted.parser_sha256,
        policy_version=active.version,
        original_verified=original,
        reason=reason,
    )


def verify_record_excerpt(
    *,
    excerpt: str,
    record: PatentRecord,
    field_name: str,
    store: artifacts.ArtifactStore,
    policy: policy_module.EvidencePolicy | None = None,
) -> ExcerptVerification:
    """레코드의 특정 필드에 대해 발췌를 검증한다."""
    field = (record.fields or {}).get(field_name)
    if field is None:
        active = policy or policy_module.current()
        return ExcerptVerification(
            match_kind=MATCH_NONE,
            policy_version=active.version,
            reason=f"레코드에 없는 필드입니다: {field_name}",
        )
    return verify_excerpt(
        excerpt=excerpt, field=field, store=store, policy=policy
    )


def summarize(verifications) -> dict:
    """후보 단위 요약.

    주의: 이 요약은 **보고용이지 게이팅용이 아니다.** 각 발췌의 표시는 반드시
    자기 자신의 ExcerptVerification 을 따라야 한다. any_original 을 후보의
    모든 행에 적용하면 확인된 발췌 하나가 나머지를 승격시키는, 이 모듈이
    없애려는 바로 그 결함이 된다.
    """
    items = list(verifications)
    counts = {kind: 0 for kind in MATCH_KINDS}
    for item in items:
        counts[item.match_kind] = counts.get(item.match_kind, 0) + 1
    return {
        "total": len(items),
        "counts": counts,
        "any_original": any(item.original_verified for item in items),
        "all_original": bool(items) and all(item.original_verified for item in items),
    }
