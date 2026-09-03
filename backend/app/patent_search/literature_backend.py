"""비특허문헌 백엔드 — Crossref·Europe PMC 를 ARIA 가 직접 조회한다.

EPO 백엔드와 같은 자리를 차지하되 다루는 문헌 종류가 다르다. EPO OPS 는 특허를
주고 논문을 모른다. 청구항과 겨루는 선행문헌의 상당수가 논문이므로, 특허만
받는 공식 채널 하나로는 검증 단계가 논문 후보를 통째로 건너뛴다. 실제로
2026-09-01 실행에서 논문 후보 ``10.3390/s20133649`` 는 DOI 라는 이유로
search_verification.targets 에서 조용히 빠졌고, 웹에서도 mdpi.com 이 403 을
돌려줘 끝까지 "미확인 검색 단서"로 남았다.

두 가지 일을 한다
-----------------
발견(search)   모델이 실제로 사용한 검색어로 ARIA 가 직접 서지 DB 에 묻는다.
               웹 검색 각주가 익명이라 후보가 될 수 없었던 문헌을, 제목과 DOI 가
               붙은 상태로 데려온다.
확보(fetch)    후보의 DOI 로 등록 서지와 초록을 받아 원본을 보존한다. 발행사
               사이트(mdpi.com 등)를 열지 않으므로 403 에 걸리지 않는다.

자격증명이 없다
---------------
EPO 와 달리 키가 필요 없다. 그래서 ``configured`` 는 항상 참이고, 사용자가 켜기만
하면 동작한다. 대신 예의를 지키는 쪽으로 기본값을 잡는다 — 질의 수 상한과
네트워크 시간 예산을 두고, Crossref 에는 연락처를 함께 보낸다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from . import artifacts, literature_client, literature_parser
from .base import (
    BackendStatus,
    EvidenceRef,
    FieldValue,
    PatentRecord,
    PatentSearchBackend,
    PatentSearchError,
    PatentSearchQuery,
    PatentSearchResponse,
)

SETTING_ENABLED = "literature_integration_enabled"
SETTING_MAILTO = "literature_contact_email"
SETTING_MAX_QUERIES = "literature_max_queries"
SETTING_MAX_RESULTS = "literature_max_results_per_query"
SETTING_HTTP_BUDGET = "literature_http_budget_seconds"

BACKEND_ID = "literature"

# 논문에는 청구항이 없다. EPO 의 구성요소 목록을 그대로 쓰면 매 후보마다
# claims 조회가 한 번씩 실패하고, 그 실패가 기록에서 "청구항을 못 받았다"로
# 읽힌다. 받을 수 없는 것을 받으려 시도하지 않는다.
CONSTITUENTS = ("abstract", "biblio")

_READY_DETAIL = "Crossref·Europe PMC 조회 준비됨 (자격증명 불필요)"
_DISABLED_DETAIL = "비특허문헌 연동이 꺼져 있습니다."


class LiteratureBackend(PatentSearchBackend):
    """Crossref + Europe PMC. 두 곳을 함께 쓰고 DOI 로 합친다."""

    id = BACKEND_ID
    display_name = "Crossref · Europe PMC"

    def __init__(self, client=None, store=None) -> None:
        self._client = client
        self._store = store
        self._mailto = ""
        self._max_results = 10
        self._http_budget = literature_client.DEFAULT_HTTP_BUDGET_SECONDS
        self._search_calls = 0
        self._detail_fetches = 0
        literature_parser.register()

    # --- 설정 -----------------------------------------------------------
    def configure(self, values: Mapping[str, Any]) -> None:
        self._mailto = str(values.get(SETTING_MAILTO) or "").strip()
        self._max_results = _positive_int(values.get(SETTING_MAX_RESULTS), 10)
        self._http_budget = float(
            _positive_int(
                values.get(SETTING_HTTP_BUDGET),
                int(literature_client.DEFAULT_HTTP_BUDGET_SECONDS),
            )
        )
        if self._client is not None:
            self._client.mailto = self._mailto
            self._client.http_budget_seconds = self._http_budget

    def status(self) -> BackendStatus:
        return BackendStatus(
            backend_id=self.id,
            display_name=self.display_name,
            enabled=True,
            # 키가 없으므로 설정만으로 준비가 끝난다.
            configured=True,
            detail=_READY_DETAIL,
        )

    def usage(self) -> dict:
        client = self._client
        base = (
            client.usage()
            if client is not None
            else {
                "calls_by_kind": {},
                "http_seconds": 0.0,
                "http_budget_seconds": self._http_budget,
            }
        )
        base["search_calls"] = self._search_calls
        base["detail_fetches"] = self._detail_fetches
        return base

    # --- 내부 자원 -------------------------------------------------------
    def _require_client(self) -> literature_client.LiteratureClient:
        if self._client is None:
            self._client = literature_client.LiteratureClient(
                mailto=self._mailto,
                http_budget_seconds=self._http_budget,
            )
        return self._client

    def _require_store(self) -> artifacts.ArtifactStore:
        if self._store is None:
            # 늦게 만든다. import 시점에 디렉터리를 건드리지 않기 위해서다.
            from ..config import PATHS

            PATHS.evidence_dir.mkdir(parents=True, exist_ok=True)
            self._store = artifacts.ArtifactStore(PATHS.evidence_dir.resolve())
        return self._store

    @property
    def artifact_store(self) -> artifacts.ArtifactStore:
        """응답 원본을 보존하는 저장소. 검증 단계가 여기서 다시 읽는다.

        EPO 백엔드와 **같은 디렉터리**를 쓴다. 아티팩트 id 가 내용 해시라
        충돌하지 않고, 후보 검증이 두 채널의 근거를 한 저장소에서 읽을 수 있다.
        """
        return self._require_store()

    # --- 발견 -----------------------------------------------------------
    def search(self, query: PatentSearchQuery) -> PatentSearchResponse:
        """두 서지 DB 에 같은 질의를 보내고 결과를 DOI 로 합친다.

        한쪽만 쓰지 않는 이유는 색인 방식이 다르기 때문이다(literature_client
        모듈 주석의 실측표를 보라). 두 응답 모두 아티팩트로 보존하고, 각 필드는
        자기가 나온 응답을 가리킨다 — 합쳤다고 근거가 섞이지는 않는다.
        """
        client = self._require_client()
        store = self._require_store()
        rows = max(1, min(int(query.max_results or 0) or self._max_results,
                          literature_client.MAX_ROWS_PER_QUERY))

        records: list[PatentRecord] = []
        # 중복은 **한 DB 안에서만** 없앤다. 두 DB 가 같은 문헌을 돌려준 사실은
        # 지우지 않는다 — "서로 다른 색인이 같은 문헌을 데려왔다"는 교차 확인
        # 신호이고, 호출부가 후보 순위를 매길 때 쓰는 몇 안 되는 관측이다.
        # 여기서 접어 버리면 그 신호가 언제나 1 이 되어 아무 일도 하지 않는다.
        seen: set[tuple[str, str]] = set()
        notes: list[str] = []
        failed: list[str] = []
        artifact_ids: list[str] = []
        status = 0
        request_url = ""

        for source, call_fn, read_fn in (
            (
                literature_client.SOURCE_CROSSREF,
                client.search_crossref,
                literature_parser.read_crossref_items,
            ),
            (
                literature_client.SOURCE_EUROPEPMC,
                client.search_europepmc,
                literature_parser.read_europepmc_results,
            ),
        ):
            try:
                call = call_fn(query.text, rows=rows)
            except literature_client.LiteratureError as exc:
                # 한쪽이 죽어도 다른 쪽 결과를 버리지 않는다. 조용히 넘기지도
                # 않는다 — 무엇이 실패했는지 notes 와 failed_sources 에 남는다.
                notes.append(f"{source} 검색 실패: {exc}")
                failed.append(source)
                continue
            self._search_calls += 1
            # 원본을 먼저 보존하고, 보존된 바이트에서 후보를 만든다.
            artifact_id = store.put(call.body)
            artifact_ids.append(artifact_id)
            status = status or call.status
            request_url = request_url or call.url
            if call.no_results:
                continue
            try:
                works = read_fn(call.body)
            except literature_parser.LiteratureParseError as exc:
                notes.append(
                    f"{source} 응답을 읽지 못했습니다: {exc} "
                    f"(원본은 아티팩트 {artifact_id[:12]}… 에 보존되어 있습니다)"
                )
                continue
            for work in works:
                key = (source, work.doi)
                if key in seen:
                    continue
                seen.add(key)
                records.append(_record_for(work, artifact_id))

        return PatentSearchResponse(
            records=tuple(records),
            # 같은 DOI 가 두 번 들어 있을 수 있다(두 DB 가 모두 찾은 문헌).
            # 문헌 수가 아니라 **레코드 수**다.
            total_found=len(records),
            raw_artifact_id=artifact_ids[0] if artifact_ids else "",
            fetched_at=datetime.now(timezone.utc).isoformat(),
            http_status=status,
            request_url=request_url,
            notes=tuple(notes),
            failed_sources=tuple(failed),
        )

    # --- 확보 -----------------------------------------------------------
    def fetch_document(
        self, doi: str, constituent: str = "abstract", *, agent_budget: bool = True
    ) -> PatentSearchResponse:
        """후보 하나의 등록 서지를 받는다. 발행사 사이트를 열지 않는다.

        ``abstract`` 는 Europe PMC 를 먼저 본다. 초록이 평문으로 오고 한 번에
        제목·저자·저널까지 함께 오기 때문이다. 그 색인에 없는 문헌(생의학
        범위 밖)은 Crossref 로 넘어간다. ``biblio`` 는 Crossref 만 본다.

        ``agent_budget`` 인자는 EPO 백엔드와 시그니처를 맞추기 위한 것이다. 이
        백엔드에는 LLM 루프가 없어 상한이 없고, 호출 횟수는 호출부가 센다.
        """
        client = self._require_client()
        store = self._require_store()
        key = literature_client.normalize_doi(doi)

        if constituent == "biblio":
            plan = ((literature_client.SOURCE_CROSSREF, client.fetch_crossref,
                     literature_parser.read_crossref_work),)
        else:
            plan = (
                (
                    literature_client.SOURCE_EUROPEPMC,
                    client.fetch_europepmc,
                    _first_europepmc,
                ),
                (
                    literature_client.SOURCE_CROSSREF,
                    client.fetch_crossref,
                    literature_parser.read_crossref_work,
                ),
            )

        notes: list[str] = []
        last: PatentSearchResponse | None = None
        for source, call_fn, read_fn in plan:
            call = call_fn(key)
            self._detail_fetches += 1
            artifact_id = store.put(call.body)
            response = PatentSearchResponse(
                records=(),
                total_found=0,
                raw_artifact_id=artifact_id,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                http_status=call.status,
                request_url=call.url,
                notes=tuple(notes),
            )
            last = response
            if call.no_results:
                notes.append(f"{source} 에 이 DOI 의 레코드가 없습니다.")
                continue
            try:
                work = read_fn(call.body)
            except literature_parser.LiteratureParseError as exc:
                notes.append(f"{source} 응답을 읽지 못했습니다: {exc}")
                continue
            if work is None or work.doi != key:
                # 다른 문헌이 온 응답을 이 후보의 근거로 쓰지 않는다. 2026-09-01
                # 실행에서 모델이 엉뚱한 PMC 문서(신경섬유종증 논문)를 열고도
                # 열람 성공으로 세어졌다. 같은 사고를 백엔드에서 차단한다.
                notes.append(
                    f"{source} 응답의 DOI 가 요청한 값과 다릅니다: "
                    f"{(work.doi if work else '없음')!r} != {key!r}"
                )
                continue
            if not work.text_fields():
                notes.append(f"{source} 응답에 쓸 수 있는 필드가 없습니다.")
                continue
            return replace(
                response,
                records=(_record_for(work, artifact_id),),
                total_found=1,
                notes=tuple(notes),
            )

        if last is None:  # pragma: no cover - plan 은 항상 비어 있지 않다
            raise PatentSearchError("조회 계획이 비어 있습니다.")
        return replace(last, notes=tuple(notes))


def _first_europepmc(body: bytes):
    works = literature_parser.read_europepmc_results(body)
    return works[0] if works else None


def _record_for(work, artifact_id: str) -> PatentRecord:
    """서지 레코드 하나를 Provider 중립 레코드로. 모든 필드가 아티팩트를 가리킨다."""
    profile_id = literature_parser.PROFILE_BY_SOURCE[work.source]
    fields = {
        name: FieldValue(
            value=text,
            evidence=EvidenceRef(
                artifact_id=artifact_id,
                field_path=work.paths[name],
                profile_id=profile_id,
            ),
        )
        for name, text in work.text_fields().items()
    }
    return PatentRecord(
        doc_number=work.doi,
        title=work.title,
        fields=fields,
        source_url=work.url,
    )


def _positive_int(value, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback
