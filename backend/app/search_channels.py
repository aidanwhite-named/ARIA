"""검색 채널 실행 정책과 채널 상태 — 프롬프트 문장이 정하지 않는다.

무엇을 정하는가
---------------
어떤 채널이 도는지는 **작업 종류와 설정**이 정한다. 프롬프트에 "검색하지 마라"
라고 적혀 있어도 유사문헌 검색 작업의 채널 정책은 바뀌지 않는다. 반대로 "모든
DB를 다 뒤져라"라고 적어도 꺼진 채널이 켜지지 않는다.

    job_kind == similarity_search    검색 파이프라인을 돈다
    settings + 명시적 channels 값     그 안에서 어떤 채널을 돌지 정한다

프롬프트 본문에서 '검색하라' 같은 자연어를 찾지 않는다. 그런 판정은 사용자가
문장을 바꾸는 것만으로 감사 기록 생성을 건너뛰게 만든다.

채널
----
    web         Provider 의 웹 검색 도구
    literature  Crossref · Europe PMC 직접 검색
    epo         EPO OPS 독립 검색
    kiwee       한국특허정보원 게이트웨이. 접속·인증 미구현이라 실행하지 않는다.

채널 하나의 실패가 다른 채널의 결과를 지우지 않는다. 각 채널은 자기 상태와
사유를 남기고, 그 상태는 매니페스트에 그대로 저장된다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from . import search_manifest, search_verification

CHANNEL_WEB = "web"
CHANNEL_LITERATURE = "literature"
CHANNEL_EPO = "epo"
CHANNEL_KIWEE = "kiwee"
CHANNELS = (CHANNEL_WEB, CHANNEL_LITERATURE, CHANNEL_EPO, CHANNEL_KIWEE)

CHANNEL_LABELS = {
    CHANNEL_WEB: "웹 검색",
    CHANNEL_LITERATURE: "논문 전용 API 검색",
    CHANNEL_EPO: "EPO 독립 검색",
    CHANNEL_KIWEE: "Kiwee 특허 검색",
}

# 채널 실행 결과. 매니페스트에 이 문자열 그대로 남는다.
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUSES = (STATUS_SUCCEEDED, STATUS_PARTIAL, STATUS_FAILED, STATUS_SKIPPED)

# 왜 건너뛰었는가. SKIPPED 하나로는 "사용자가 껐다"와 "구현이 없다"를 구분할 수
# 없는데, 사용자가 할 일이 서로 다르다.
SKIP_DISABLED = "disabled"
SKIP_NOT_CONFIGURED = "not_configured"
SKIP_NOT_IMPLEMENTED = "not_implemented"
SKIP_NO_INPUT = "no_input"

# 설정 키. patent_search 의 토글과 같은 값을 가리킨다.
_ENABLE_KEYS = {
    CHANNEL_LITERATURE: "literature_integration_enabled",
    CHANNEL_EPO: "epo_integration_enabled",
    CHANNEL_KIWEE: "kiwee_integration_enabled",
}

#: 아직 접속·인증이 구현되지 않은 채널. 켜져 있어도 네트워크를 열지 않는다.
#: 흉내 낸 결과를 만들지 않고 사유와 함께 건너뛴다.
UNIMPLEMENTED = frozenset({CHANNEL_KIWEE})


@dataclass(frozen=True)
class ChannelDecision:
    """이 채널을 이번 실행에서 도는가, 돌지 않으면 왜인가."""

    channel: str
    enabled: bool
    reason: str = ""
    skip_kind: str = ""

    def as_dict(self) -> dict:
        return {
            "channel": self.channel,
            "label": CHANNEL_LABELS.get(self.channel, self.channel),
            "enabled": self.enabled,
            "reason": self.reason,
            "skip_kind": self.skip_kind,
        }


@dataclass(frozen=True)
class ChannelPolicy:
    """이번 실행의 채널 정책 전체. 감사 기록에 그대로 들어간다."""

    decisions: dict[str, ChannelDecision] = field(default_factory=dict)
    # 호출부가 명시적으로 제한한 채널 목록. None 이면 설정만으로 정했다.
    requested: tuple[str, ...] | None = None

    def runs(self, channel: str) -> bool:
        decision = self.decisions.get(channel)
        return bool(decision and decision.enabled)

    def reason(self, channel: str) -> str:
        decision = self.decisions.get(channel)
        return decision.reason if decision else "알 수 없는 채널입니다."

    def skip_kind(self, channel: str) -> str:
        decision = self.decisions.get(channel)
        return decision.skip_kind if decision else ""

    def as_dict(self) -> dict:
        return {
            "requested": list(self.requested) if self.requested is not None else None,
            "channels": [
                self.decisions[name].as_dict()
                for name in CHANNELS
                if name in self.decisions
            ],
        }


def resolve(
    values: Mapping[str, object],
    *,
    channels: Iterable[str] | None = None,
) -> ChannelPolicy:
    """설정과 명시적 채널 목록으로 이번 실행의 채널 정책을 만든다.

    ``channels`` 는 호출부가 범위를 더 좁히고 싶을 때만 준다. 넓히지는 못한다 —
    설정에서 꺼진 채널은 명시해도 켜지지 않는다. 반대 방향을 허용하면 "설정에서
    껐는데 어디선가 돌더라"가 가능해진다.

    프롬프트 본문은 이 함수에 들어오지 않는다. 그것이 이 모듈의 요점이다.
    """
    requested: tuple[str, ...] | None = None
    if channels is not None:
        requested = tuple(
            name for name in CHANNELS if name in {str(item) for item in channels}
        )

    decisions: dict[str, ChannelDecision] = {}
    for channel in CHANNELS:
        if requested is not None and channel not in requested:
            decisions[channel] = ChannelDecision(
                channel,
                False,
                reason="이 실행에서 요청되지 않은 채널입니다.",
                skip_kind=SKIP_DISABLED,
            )
            continue

        key = _ENABLE_KEYS.get(channel)
        if key is not None and not bool(values.get(key, False)):
            decisions[channel] = ChannelDecision(
                channel,
                False,
                reason=f"{CHANNEL_LABELS[channel]} 연동이 꺼져 있습니다.",
                skip_kind=SKIP_DISABLED,
            )
            continue

        if channel in UNIMPLEMENTED:
            decisions[channel] = ChannelDecision(
                channel,
                False,
                reason=(
                    f"{CHANNEL_LABELS[channel]} 은 연동이 켜져 있어도 접속·인증이 "
                    "구현되지 않아 실행하지 않습니다. 검색을 흉내 내지 않으며 "
                    "외부 요청도 보내지 않습니다."
                ),
                skip_kind=SKIP_NOT_IMPLEMENTED,
            )
            continue

        decisions[channel] = ChannelDecision(channel, True)

    return ChannelPolicy(decisions=decisions, requested=requested)


def skipped_section(policy: ChannelPolicy, channel: str) -> dict:
    """실행하지 않은 채널의 감사 기록.

    '기록이 없다'와 '실행하지 않았다'를 구분한다. 앞은 우리가 모르는 것이고
    뒤는 우리가 정한 것이다.
    """
    decision = policy.decisions.get(channel)
    return {
        "channel": channel,
        "label": CHANNEL_LABELS.get(channel, channel),
        "enabled": bool(decision and decision.enabled),
        "attempted": False,
        "status": STATUS_SKIPPED,
        "skip_kind": decision.skip_kind if decision else SKIP_DISABLED,
        "reason": decision.reason if decision else "알 수 없는 채널입니다.",
        "requests": 0,
        "candidates": [],
    }


def cell(value: str) -> str:
    """표 한 칸. 파이프와 줄바꿈이 표를 깨지 않게 한다.

    보고서 렌더러도 같은 함수를 쓴다. 채널 상태의 사유 문자열은 여기서 만들어져
    보고서로 넘어가므로, 두 곳이 각자 다듬으면 같은 값이 다르게 보인다.
    """
    text = str(value or "").replace("|", "\|")
    return " ".join(text.split()) or "-"


def _cell(value: str) -> str:
    return cell(value)


def cell(value: str) -> str:
    """표 한 칸. 파이프와 줄바꿈이 표를 깨지 않게 한다.

    보고서 렌더러도 같은 함수를 쓴다. 채널 상태의 사유 문자열은 여기서 만들어져
    보고서로 넘어가므로, 두 곳이 각자 다듬으면 같은 값이 다르게 보인다.
    """
    text = str(value or "").replace("|", "\\|")
    return " ".join(text.split()) or "-"


def _cell(value: str) -> str:
    return cell(value)


def _row(row_id: str, channel: str, label: str, status: str, detail: str) -> dict:
    return {
        "id": row_id,
        "channel": channel,
        "label": label,
        "status": status,
        "detail": str(detail or ""),
    }


def overall_status(rows: list[dict]) -> str:
    """채널 상태들을 하나로 요약한다. 미실행은 등급에 넣지 않는다."""
    graded = [
        str(row.get("status") or "")
        for row in rows
        if str(row.get("status") or "") != STATUS_SKIPPED
    ]
    if not graded or all(status == STATUS_FAILED for status in graded):
        return STATUS_FAILED
    if all(status == STATUS_SUCCEEDED for status in graded):
        return STATUS_SUCCEEDED
    return STATUS_PARTIAL


def status_rows(manifest: dict) -> list[dict]:
    """보고서 맨 위의 채널별 성공·실패.

    왜 맨 위인가
    ------------
    "EPO 검색이 됐나"를 알아내려면 예전에는 접힌 감사 블록을 펼쳐 종료 사유를
    읽어야 했다. 그런데 그것은 결과를 읽기 **전에** 알아야 하는 사실이다 —
    EPO 가 한 건도 못 찾은 실행과 EPO 검색이 실패한 실행은 후보 목록이 똑같이
    보이지만 전혀 다른 실행이다.

    검색 채널과 문헌조회 채널을 나눠 적는다. EPO 검색이 실패했는데 문헌번호
    조회가 성공한 실행을 "EPO 검색 성공"으로 읽으면 안 된다.
    """
    reported = manifest.get("reported") or {}
    observed = manifest.get("observed") or {}
    candidates = reported.get("candidates") or []
    rows: list[dict] = []

    # --- 웹 검색 ---------------------------------------------------------
    web_error = str(reported.get("web_report_error") or "")
    queries = list(observed.get("search_queries") or [])
    if not queries:
        for origin_queries in (observed.get("search_queries_by_origin") or {}).values():
            queries += list(origin_queries or [])
    web_candidates = sum(
        1
        for item in candidates
        if search_manifest.DISCOVERY_WEB in search_manifest.discovery_origins(item)
    )
    web_state = STATUS_FAILED if web_error else STATUS_SUCCEEDED
    rows.append(
        _row(
            "web_search",
            CHANNEL_WEB,
            "웹 검색",
            web_state,
            web_error or f"검색어 {len(queries)}개 · 후보 {web_candidates}건",
        )
    )

    # --- 웹페이지 확인 ----------------------------------------------------
    #
    # 검색과 열람은 다른 일이다. 검색어가 나갔다고 문헌 본문을 본 것이 아니고,
    # 403·유료 장벽으로 한 건도 못 열어도 "웹 검색 성공"으로 보이면 후보의
    # 근거 등급이 왜 낮은지 설명되지 않는다.
    attempted_pages = list(observed.get("attempted_fetch_urls") or [])
    opened_pages = list(observed.get("succeeded_fetch_urls") or [])
    if not attempted_pages:
        page_state = STATUS_SKIPPED
        page_detail = "페이지를 연 기록이 없습니다."
    elif not opened_pages:
        page_state = STATUS_FAILED
        page_detail = f"시도 {len(attempted_pages)}건 · 본문 확인 0건"
    else:
        page_state = (
            STATUS_SUCCEEDED
            if len(opened_pages) >= len(attempted_pages)
            else STATUS_PARTIAL
        )
        page_detail = (
            f"시도 {len(attempted_pages)}건 · 본문 확인 {len(opened_pages)}건"
        )
    rows.append(
        _row("web_page_fetch", CHANNEL_WEB, "웹페이지 확인", page_state, page_detail)
    )

    # --- EPO 독립 검색 ----------------------------------------------------
    epo = manifest.get("epo") or {}
    lanes = [lane for lane in (epo.get("lanes") or []) if isinstance(lane, dict)]
    epo_queries = [
        str(query.get("cql") or "")
        for lane in lanes
        for query in (lane.get("queries") or [])
        if isinstance(query, dict) and query.get("cql")
    ]
    if not epo.get("enabled"):
        epo_state = STATUS_SKIPPED
        epo_detail = str(epo.get("reason") or "EPO 연동이 꺼져 있습니다.")
    elif not lanes:
        epo_state = STATUS_SKIPPED
        epo_detail = str(epo.get("reason") or "실행된 레인이 없습니다.")
    else:
        failed = [lane for lane in lanes if search_manifest.epo_lane_failed(lane)]
        ok_lanes = [lane for lane in lanes if lane not in failed]
        epo_candidates = sum(len(lane.get("candidates") or []) for lane in lanes)
        if not ok_lanes:
            epo_state = STATUS_FAILED
        elif failed:
            epo_state = STATUS_PARTIAL
        else:
            epo_state = STATUS_SUCCEEDED
        reasons = [
            f"{lane.get('id') or '레인'}: "
            + str(
                lane.get("error")
                or lane.get("termination_detail")
                or lane.get("termination_reason")
                or "사유 미기록"
            )
            for lane in failed
        ]
        epo_detail = (
            f"검색식 {len(epo_queries)}개 · 결과 {epo_candidates}건"
            + ("" if not reasons else " · " + " / ".join(reasons))
        )
    rows.append(
        _row("epo_search", CHANNEL_EPO, "EPO 독립 검색", epo_state, epo_detail)
    )

    # --- EPO 공식 문헌조회 -------------------------------------------------
    verification = manifest.get("verification") or {}
    counts = verification.get("counts") or {}
    documents = [
        item for item in (verification.get("documents") or []) if isinstance(item, dict)
    ]
    # 무엇을 불렀는가가 아니라 무엇을 **가졌는가**로 센다. 검색 레인이 받아 둔
    # 응답을 재사용하면 그 호출의 이름은 구성요소가 아니다.
    held: set[str] = set()
    for document in documents:
        held.update(
            search_verification.constituents_present(document.get("fields") or [])
        )
    scopes = [name for name in ("biblio", "abstract", "claims") if name in held]
    targets = int(counts.get("targets") or 0)
    verified_docs = int(counts.get("verified") or 0)
    if not verification.get("attempted"):
        fetch_state = STATUS_SKIPPED
        fetch_detail = str(verification.get("reason") or "실행하지 않았습니다.")
    elif verified_docs == 0:
        fetch_state = STATUS_FAILED
        fetch_detail = f"대상 {targets}건 · 확보 0건"
    else:
        fetch_state = STATUS_SUCCEEDED if verified_docs >= targets else STATUS_PARTIAL
        fetch_detail = (
            f"대상 {targets}건 · 확보 {verified_docs}건 · 조회 범위 "
            + (", ".join(scopes) or "기록 없음")
        )
    rows.append(
        _row(
            "epo_official_fetch",
            CHANNEL_EPO,
            "EPO 공식 문헌조회",
            fetch_state,
            fetch_detail,
        )
    )

    # --- 논문 전용 API ----------------------------------------------------
    #
    # 켜져 있다는 것과 질의가 나갔다는 것, 질의가 성공했다는 것은 서로 다른
    # 사실이다. enabled 만 보고 '정상'으로 적으면 서지 DB 가 전부 오류를
    # 돌려준 실행도 성공으로 보인다.
    literature = manifest.get("literature") or {}
    paper_queries = [
        row for row in (literature.get("queries") or []) if isinstance(row, dict)
    ]
    failed_queries = [row for row in paper_queries if row.get("error")]
    if not literature.get("enabled"):
        paper_state = STATUS_SKIPPED
        paper_detail = str(literature.get("reason") or "꺼져 있습니다.")
    elif not paper_queries:
        paper_state = STATUS_SKIPPED
        paper_detail = str(literature.get("reason") or "질의가 나가지 않았습니다.")
    else:
        found = sum(int(row.get("found") or 0) for row in paper_queries)
        errors = " / ".join(
            f"{_cell(str(row.get('query') or '질의'))}: {_cell(str(row.get('error')))}"
            for row in failed_queries[:3]
        )
        if len(failed_queries) == len(paper_queries):
            paper_state = STATUS_FAILED
            paper_detail = f"질의 {len(paper_queries)}개 전부 실패 · {errors}"
        elif failed_queries:
            paper_state = STATUS_PARTIAL
            paper_detail = (
                f"질의 {len(paper_queries)}개 중 {len(failed_queries)}개 실패 · "
                f"결과 {found}건 · 후보 "
                f"{len(literature.get('candidates') or [])}건 · {errors}"
            )
        else:
            paper_state = STATUS_SUCCEEDED
            paper_detail = (
                f"질의 {len(paper_queries)}개 · 결과 {found}건 · 후보 "
                f"{len(literature.get('candidates') or [])}건"
            )
    rows.append(
        _row(
            "literature_search",
            CHANNEL_LITERATURE,
            "논문 전용 API 검색",
            paper_state,
            paper_detail,
        )
    )

    # --- 논문 공식 서지 대조 ----------------------------------------------
    #
    # 고른 수·부른 수·확보한 수는 서로 다른 사실이다. 예전에는 "대상 8건"만
    # 적혀서, 그것이 고른 8건인지 실제로 부른 8건인지 읽을 수 없었다. 2026-09-02
    # 실행에서는 고른 8 · 부른 8 · 확보 4 였고, 실패한 4자리는 아무에게도
    # 넘어가지 않았다.
    paper_verification = literature.get("verification") or {}
    if not literature.get("enabled") or not paper_verification:
        paper_fetch_state = STATUS_SKIPPED
        paper_fetch_detail = (
            "논문 후보의 공식 서지 대조를 실행하지 않았습니다."
            if literature.get("enabled")
            else str(literature.get("reason") or "꺼져 있습니다.")
        )
    else:
        # 옛 기록에는 selected/attempted 가 없다. 그때는 고른 수와 부른 수가 같아
        # target_count 하나로 둘 다 읽었으므로 그 값으로 되돌려 읽는다.
        legacy = int(paper_verification.get("target_count") or 0)
        selected = int(paper_verification.get("selected") or legacy)
        attempted = int(paper_verification.get("attempted") or legacy)
        paper_verified = int(paper_verification.get("verified") or 0)
        paper_failed = int(paper_verification.get("fetch_failed") or 0)
        backfilled = int(paper_verification.get("backfill_verified") or 0)
        parts = [
            f"선택 {selected}건",
            f"시도 {attempted}건",
            f"확보 {paper_verified}건",
        ]
        if paper_failed:
            parts.append(f"조회 실패 {paper_failed}건")
        if backfilled:
            # 앞 후보의 실패로 빈 자리를 뒤 후보가 받아 확보한 수.
            parts.append(f"대체 확보 {backfilled}건")
        if not attempted:
            paper_fetch_state = STATUS_SKIPPED
            paper_fetch_detail = "조회할 논문 후보가 없었습니다."
        elif paper_verified == 0:
            paper_fetch_state = STATUS_FAILED
            paper_fetch_detail = " · ".join(parts)
        else:
            paper_fetch_state = (
                STATUS_SUCCEEDED if paper_verified >= selected else STATUS_PARTIAL
            )
            paper_fetch_detail = " · ".join(parts)
    rows.append(
        _row(
            "literature_official_fetch",
            CHANNEL_LITERATURE,
            "논문 공식 서지 대조",
            paper_fetch_state,
            paper_fetch_detail,
        )
    )

    # --- Kiwee -----------------------------------------------------------
    #
    # 미구현 채널을 표에서 빼지 않는다. 빼면 "이 실행이 Kiwee 를 봤는지"에
    # 답할 수 없고, 나중에 구현되었을 때 옛 실행이 소급해 실행된 것처럼
    # 보인다. 기록이 없는 옛 매니페스트에서만 줄 자체를 만들지 않는다.
    kiwee = manifest.get("kiwee")
    if isinstance(kiwee, dict) and kiwee:
        rows.append(
            _row(
                "kiwee_search",
                CHANNEL_KIWEE,
                CHANNEL_LABELS[CHANNEL_KIWEE],
                str(kiwee.get("status") or STATUS_SKIPPED),
                str(kiwee.get("reason") or "실행하지 않았습니다."),
            )
        )

    return rows
