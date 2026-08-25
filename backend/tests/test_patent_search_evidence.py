"""증거 검증 코어.

검증 단위는 발췌다. 판정 근거는 어댑터가 준 값도, 어댑터의 출처 주장도 아니다.
불변 저장소에 보존된 원본 바이트를 다시 읽어, 등록된 소스 프로필로 텍스트와
출처를 함께 재추출한 값이다.
"""

from __future__ import annotations

import json

import pytest

from app.patent_search import artifacts, base, parsers, policy, provenance

# 공식 청구항에는 ㎜ 가, 기계번역 초록에는 mm 가 들어 있다. 이 대비가
# NFKC 접기의 위험을 그대로 드러낸다.
OFFICIAL_CLAIM = "반도체 기판 위에 두께 5㎜ 의 절연층을 형성하는 단계"
MT_ABSTRACT = "forming an insulating layer of 5mm thickness on a substrate"

PAYLOAD = {
    "records": [
        {
            "claims": OFFICIAL_CLAIM,
            "abstract_en": MT_ABSTRACT,
            "meta": {"count": 1},
        }
    ]
}

# 테스트 전용 프로필. 실제 Kiwee 응답을 검토해 등록하는 프로필의 자리를
# 흉내낸다. 이름을 test_ 로 시작해 실제 프로필과 섞이지 않게 한다.
PROFILE_OFFICIAL = "test_official_xml"
PROFILE_MT = "test_machine_translation"


@pytest.fixture(autouse=True)
def _profiles():
    """raw_capable 프로필을 테스트 동안만 등록하고 끝나면 되돌린다."""
    added = []
    for pid, kind, translated, raw_capable in (
        (PROFILE_OFFICIAL, base.SOURCE_OFFICIAL_XML, False, True),
        (PROFILE_MT, base.SOURCE_MACHINE_TRANSLATION, True, False),
    ):
        if pid not in parsers._PROFILES:
            parsers.register_profile(
                parsers.SourceProfile(
                    profile_id=pid,
                    parser_id=parsers.PARSER_JSON_PATH,
                    parser_version=parsers.PARSER_JSON_PATH_VERSION,
                    source_kind=kind,
                    is_translation=translated,
                    raw_capable=raw_capable,
                )
            )
            added.append(pid)
    yield
    for pid in added:
        parsers._PROFILES.pop(pid, None)


@pytest.fixture()
def store(tmp_path):
    return artifacts.ArtifactStore(tmp_path / "evidence")


@pytest.fixture()
def artifact_id(store):
    return store.put(json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8"))


def _record(artifact_id: str) -> base.PatentRecord:
    """공식 청구항과 기계번역 초록이 한 레코드에 섞여 있다."""
    return base.PatentRecord(
        doc_number="KR10-2020-0000000",
        title="반도체 소자",
        fields={
            "claims": base.FieldValue(
                value=OFFICIAL_CLAIM,
                evidence=base.EvidenceRef(
                    artifact_id, "records/0/claims", PROFILE_OFFICIAL
                ),
            ),
            "abstract_en": base.FieldValue(
                value=MT_ABSTRACT,
                evidence=base.EvidenceRef(
                    artifact_id, "records/0/abstract_en", PROFILE_MT
                ),
            ),
        },
    )


# --- 아티팩트 저장소 ------------------------------------------------------


def test_store_roundtrip_and_content_address(store):
    data = b"hello evidence"
    aid = store.put(data)
    assert aid == artifacts.compute_id(data)
    assert store.read(aid) == data


def test_store_detects_tampering(store):
    aid = store.put(b"original bytes")
    store._path(aid).write_bytes(b"tampered bytes")
    with pytest.raises(artifacts.ArtifactCorrupted):
        store.read(aid)


def test_store_missing_artifact(store):
    with pytest.raises(artifacts.ArtifactMissing):
        store.read("0" * 64)


def test_store_rejects_non_hex_id(store):
    """임의 문자열을 경로로 쓰면 저장소 밖을 가리킬 수 있다."""
    for bad in ("", "../../../../Windows/win.ini", "ABC", "0" * 63, "g" * 64):
        with pytest.raises(artifacts.ArtifactIdInvalid):
            store.read(bad)
        assert store.exists(bad) is False


# --- 파서와 프로필 --------------------------------------------------------


def test_extract_returns_trusted_metadata(store, artifact_id):
    """텍스트뿐 아니라 출처도 프로필에서 나온다."""
    data = store.read(artifact_id)
    field = parsers.extract(data, "records/0/claims", PROFILE_OFFICIAL)
    assert field.text == OFFICIAL_CLAIM
    assert field.source_kind == base.SOURCE_OFFICIAL_XML
    assert field.is_translation is False
    assert field.raw_capable is True
    assert field.parser_sha256  # 구현 해시가 기록된다


def test_parser_sha256_is_stable(store, artifact_id):
    data = store.read(artifact_id)
    first = parsers.extract(data, "records/0/claims", PROFILE_OFFICIAL)
    second = parsers.extract(data, "records/0/claims", PROFILE_OFFICIAL)
    assert first.parser_sha256 == second.parser_sha256


def test_unregistered_profile_is_rejected(store, artifact_id):
    data = store.read(artifact_id)
    with pytest.raises(parsers.ProfileNotRegistered):
        parsers.extract(data, "records/0/claims", "made_up_profile")


def test_generic_json_profile_is_not_raw_capable():
    """일반 JSON 파서는 텍스트 존재만 증명한다. 공식 원문은 증명 못 한다."""
    profile = parsers.get_profile(parsers.PROFILE_GENERIC_JSON)
    assert profile.raw_capable is False
    assert profile.source_kind == base.SOURCE_UNKNOWN


def test_no_raw_capable_profile_registered_by_default():
    """실제 응답 검토 전에는 원문 등급이 구조적으로 도달 불가여야 한다."""
    shipped = [
        pid for pid in parsers.raw_capable_profiles() if not pid.startswith("test_")
    ]
    assert shipped == []


def test_parser_rejects_missing_path(store, artifact_id):
    data = store.read(artifact_id)
    with pytest.raises(parsers.FieldPathMissing):
        parsers.extract(data, "records/0/nope", PROFILE_OFFICIAL)


def test_parser_rejects_non_string_node(store, artifact_id):
    """dict 를 문자열로 뭉개면 원문 대조의 의미가 사라진다."""
    data = store.read(artifact_id)
    with pytest.raises(parsers.FieldPathMissing):
        parsers.extract(data, "records/0/meta", PROFILE_OFFICIAL)


def test_profile_registration_rejects_duplicate_and_unknown_parser():
    with pytest.raises(parsers.ParserError):
        parsers.register_profile(
            parsers.SourceProfile(
                profile_id=parsers.PROFILE_GENERIC_JSON,
                parser_id=parsers.PARSER_JSON_PATH,
                parser_version=parsers.PARSER_JSON_PATH_VERSION,
            )
        )
    with pytest.raises(parsers.ParserNotRegistered):
        parsers.register_profile(
            parsers.SourceProfile(
                profile_id="brand_new",
                parser_id="nonexistent",
                parser_version="1",
            )
        )


# --- 발췌 검증: 핵심 규칙 -------------------------------------------------


def test_exact_match_records_span(store, artifact_id):
    record = _record(artifact_id)
    result = provenance.verify_record_excerpt(
        excerpt="두께 5㎜ 의 절연층", record=record, field_name="claims", store=store
    )
    assert result.match_kind == provenance.MATCH_EXACT
    assert result.match_span is not None
    start, end = result.match_span
    assert OFFICIAL_CLAIM[start:end] == "두께 5㎜ 의 절연층"


def test_nfkc_difference_is_normalized_not_exact(store, artifact_id):
    """㎜ 를 mm 로 쓴 발췌는 원문에 없다. exact 가 되어선 안 된다."""
    record = _record(artifact_id)
    result = provenance.verify_record_excerpt(
        excerpt="두께 5mm 의 절연층",
        record=record,
        field_name="claims",
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert result.match_kind == provenance.MATCH_NORMALIZED
    assert result.original_verified is False
    # 정규화 좌표는 원문 좌표가 아니므로 남기지 않는다.
    assert result.match_span is None


def test_whitespace_difference_is_normalized_not_exact(store, artifact_id):
    record = _record(artifact_id)
    result = provenance.verify_record_excerpt(
        excerpt="두께  5㎜   의 절연층", record=record, field_name="claims", store=store
    )
    assert result.match_kind == provenance.MATCH_NORMALIZED
    assert result.match_span is None


def test_translation_field_never_gets_original(store, artifact_id):
    """기계번역 초록에서 정확히 일치해도 원문 등급 금지."""
    record = _record(artifact_id)
    result = provenance.verify_record_excerpt(
        excerpt="insulating layer of 5mm",
        record=record,
        field_name="abstract_en",
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert result.match_kind == provenance.MATCH_EXACT
    assert result.original_verified is False
    assert result.is_translation is True


# --- 원문 등급의 두 관문 --------------------------------------------------


def test_default_policy_blocks_raw(store, artifact_id):
    """기본 정책에서는 완벽한 exact 도 원문 등급을 못 받는다."""
    record = _record(artifact_id)
    result = provenance.verify_record_excerpt(
        excerpt="두께 5㎜ 의 절연층", record=record, field_name="claims", store=store
    )
    assert result.match_kind == provenance.MATCH_EXACT
    assert result.original_verified is False
    assert result.policy_version == policy.RAW_DISABLED.version
    assert "정책" in result.reason


def test_policy_enabled_and_profile_raw_capable_gives_original(store, artifact_id):
    record = _record(artifact_id)
    result = provenance.verify_record_excerpt(
        excerpt="두께 5㎜ 의 절연층",
        record=record,
        field_name="claims",
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert result.original_verified is True
    assert result.policy_version == policy.RAW_ENABLED.version


def test_policy_alone_does_not_open_raw(store, artifact_id):
    """정책을 켜도 raw_capable 이 아닌 프로필은 원문 등급을 못 준다.

    관문이 하나면 정책을 켜는 순간 전부 열린다. 그래서 둘로 나눴다.
    """
    field = base.FieldValue(
        value=OFFICIAL_CLAIM,
        evidence=base.EvidenceRef(
            artifact_id, "records/0/claims", parsers.PROFILE_GENERIC_JSON
        ),
    )
    result = provenance.verify_excerpt(
        excerpt="두께 5㎜ 의 절연층",
        field=field,
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert result.match_kind == provenance.MATCH_EXACT
    assert result.original_verified is False
    assert "raw_capable" in result.reason


def test_adapter_cannot_declare_source():
    """FieldValue 에는 출처를 주장할 필드가 없어야 한다.

    이게 있으면 '일반 JSON 을 official_xml 이라고 선언하면 raw 가 나오는'
    결함이 되살아난다.
    """
    names = base.FieldValue.__dataclass_fields__.keys()
    assert "source_kind" not in names
    assert "is_translation" not in names


# --- 검증 불가 경로 -------------------------------------------------------


def test_missing_evidence_ref_cannot_verify(store):
    """값만 있고 아티팩트 참조가 없으면 검증 불가."""
    field = base.FieldValue(value=OFFICIAL_CLAIM, evidence=None)
    result = provenance.verify_excerpt(
        excerpt="두께 5㎜ 의 절연층",
        field=field,
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert result.match_kind == provenance.MATCH_NONE
    assert result.original_verified is False


def test_incomplete_evidence_ref_cannot_verify(store, artifact_id):
    """프로필이 빠진 참조는 재현 가능한 재검증이 불가능하다."""
    field = base.FieldValue(
        value=OFFICIAL_CLAIM,
        evidence=base.EvidenceRef(artifact_id, "records/0/claims", ""),
    )
    result = provenance.verify_excerpt(
        excerpt="두께 5㎜ 의 절연층",
        field=field,
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert result.match_kind == provenance.MATCH_NONE


def test_tampered_artifact_fails_verification(store, artifact_id):
    record = _record(artifact_id)
    tampered = json.dumps(
        {"records": [{"claims": "조작된 내용"}]}, ensure_ascii=False
    ).encode("utf-8")
    store._path(artifact_id).write_bytes(tampered)
    result = provenance.verify_record_excerpt(
        excerpt="두께 5㎜ 의 절연층",
        record=record,
        field_name="claims",
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert result.match_kind == provenance.MATCH_NONE
    assert "무결성" in result.reason


def test_adapter_value_is_not_the_judge(store, artifact_id):
    """어댑터가 value 에 넣은 문자열이 아티팩트에 없으면 통과하지 못한다."""
    field = base.FieldValue(
        value="어댑터가 지어낸 문장",  # value 에는 있지만
        evidence=base.EvidenceRef(
            artifact_id, "records/0/claims", PROFILE_OFFICIAL
        ),  # 아티팩트에는 없다
    )
    result = provenance.verify_excerpt(
        excerpt="어댑터가 지어낸 문장",
        field=field,
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert result.match_kind == provenance.MATCH_NONE


def test_unknown_field_name(store, artifact_id):
    record = _record(artifact_id)
    result = provenance.verify_record_excerpt(
        excerpt="아무거나", record=record, field_name="description", store=store
    )
    assert result.match_kind == provenance.MATCH_NONE


# --- 발췌 사이의 독립성 ---------------------------------------------------


def test_one_verified_excerpt_does_not_promote_others(store, artifact_id):
    """한 후보 안에서 exact/normalized/none 이 섞이고, 서로를 승격시키지 않는다."""
    record = _record(artifact_id)
    good = provenance.verify_record_excerpt(
        excerpt="두께 5㎜ 의 절연층",
        record=record,
        field_name="claims",
        store=store,
        policy=policy.RAW_ENABLED,
    )
    translated = provenance.verify_record_excerpt(
        excerpt="insulating layer of 5mm",
        record=record,
        field_name="abstract_en",
        store=store,
        policy=policy.RAW_ENABLED,
    )
    absent = provenance.verify_record_excerpt(
        excerpt="존재하지 않는 구성",
        record=record,
        field_name="claims",
        store=store,
        policy=policy.RAW_ENABLED,
    )

    assert good.original_verified is True
    assert translated.original_verified is False
    assert absent.match_kind == provenance.MATCH_NONE

    summary = provenance.summarize([good, translated, absent])
    assert summary["any_original"] is True
    # any_original 이 참이어도 개별 발췌는 자기 판정을 유지해야 한다.
    assert summary["all_original"] is False
    assert translated.original_verified is False
    assert absent.original_verified is False
