"""EPO 사용량이 실제로 남는가 — 저장, 누적, 동시 실행.

외부 리뷰(2026-08-28)가 잡아낸 결함의 회귀 테스트다. QuotaLedger 를 만들 때
저장 경로가 묶여 있지 않아서, 사용량이 백엔드 객체 메모리에만 있다가
사라지고 있었다. 프로세스를 다시 띄우면 로컬 카운터가 0 으로 돌아갔고 화면도
갱신된 값을 볼 수 없었다.

여기서 지키는 것은 네 가지다.

  1. 응답 하나마다 즉시 DB 에 남는다(실행이 중간에 끊겨도 사용량은 남는다).
  2. 저장은 **덮어쓰기가 아니라 누적**이다.
  3. 동시 실행에서도 한쪽이 사라지지 않는다.
  4. 사용자는 이 값을 PUT 으로 고칠 수 없다.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app import settings_service
from app.db import session_scope
from app.models import AppSetting
from app.patent_search import epo_backend, epo_client, epo_cql, epo_quota

from . import epo_fixtures as fx
from .test_epo_search import TEST_KEY, TEST_SECRET, FakeTransport, ok, token_response

WEEK = epo_quota.week_key()


@pytest.fixture()
def clean_quota(client):
    """이 파일의 테스트들이 서로의 사용량을 물려받지 않게 한다."""

    def reset() -> None:
        # 전역 원장은 프로세스 안에서 계속 살아 있으므로 테스트마다 버린다.
        settings_service.reset_epo_ledger()
        # merge_epo_quota 를 거치지 않고 직접 쓴다. 저장 실패를 흉내내려고
        # 그 함수를 monkeypatch 한 테스트의 정리까지 같이 터지면 안 된다.
        with session_scope() as session:
            row = session.get(AppSetting, settings_service.EPO_QUOTA_KEY)
            blank = {"week": WEEK, "local_bytes": 0, "requests": 0}
            if row is None:
                session.add(AppSetting(key=settings_service.EPO_QUOTA_KEY, value=blank))
            else:
                row.value = blank

    reset()
    yield
    reset()


def _stored() -> dict:
    with session_scope() as session:
        values = settings_service.get_all(session)
    return dict(values.get("epo_quota_state") or {})


def _install_credentials() -> None:
    with session_scope() as session:
        settings_service.update(
            session,
            {
                epo_backend.SETTING_ENABLED: True,
                epo_backend.SETTING_CONSUMER_KEY: TEST_KEY,
                epo_backend.SETTING_CONSUMER_SECRET: TEST_SECRET,
            },
        )


def _run_one_search(transport) -> None:
    """저장 경로가 묶인 정식 통로로 검색 한 번."""
    with session_scope() as session:
        backend = settings_service.epo_backend_for(session)
    backend._client = epo_client.OpsClient(
        key=TEST_KEY,
        secret=TEST_SECRET,
        ledger=backend.ledger,
        transport=transport,
        sleep=lambda _s: None,
    )
    backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))


ONE_RUN_BYTES = len(fx.TOKEN_OK) + len(fx.SEARCH_BIBLIO)


# --------------------------------------------------------------- 실제 저장


def test_search_usage_is_persisted(client, clean_quota) -> None:
    """검색 한 번의 사용량이 DB 에 남는다. 예전에는 메모리에만 있다 사라졌다."""
    _install_credentials()
    _run_one_search(FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO)))

    stored = _stored()
    assert stored["local_bytes"] == ONE_RUN_BYTES
    assert stored["requests"] == 2
    assert stored["ops_weekly_bytes"] == 104857600
    assert stored["throttle"]["system_state"] == "idle"


def test_next_run_starts_from_the_stored_usage(client, clean_quota) -> None:
    """다음 실행(새 객체)이 앞 실행의 사용량 위에서 시작한다."""
    _install_credentials()
    for _ in range(2):
        _run_one_search(FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO)))

    stored = _stored()
    assert stored["local_bytes"] == ONE_RUN_BYTES * 2
    assert stored["requests"] == 4


def test_usage_survives_a_failed_run(client, clean_quota) -> None:
    """실행이 실패해도 나간 바이트는 나간 것이다. 같이 롤백되면 한도가 느슨해진다."""
    _install_credentials()
    # 500 은 재시도 대상이라 최초 1회 + 재시도 2회를 준비한다.
    transport = FakeTransport(
        token_response(),
        ok(b"<error/>", status=500),
        ok(b"<error/>", status=500),
        ok(b"<error/>", status=500),
    )
    with session_scope() as session:
        backend = settings_service.epo_backend_for(session)
    backend._client = epo_client.OpsClient(
        key=TEST_KEY,
        secret=TEST_SECRET,
        ledger=backend.ledger,
        transport=transport,
        sleep=lambda _s: None,
    )
    with pytest.raises(epo_client.OpsError):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert _stored()["local_bytes"] > 0


def test_loaded_state_is_not_counted_twice(client, clean_quota) -> None:
    """불러온 값을 증분으로 다시 올리면 실행할 때마다 사용량이 두 배가 된다."""
    settings_service.merge_epo_quota(
        {"week": WEEK, "local_bytes": 5000, "requests": 3}
    )
    _install_credentials()
    with session_scope() as session:
        backend = settings_service.epo_backend_for(session)
    # 아무 호출도 하지 않았으니 저장할 증분이 없어야 한다.
    assert backend.ledger.peek_delta()["local_bytes"] == 0
    settings_service.persist_epo_quota(backend.ledger)
    assert _stored()["local_bytes"] == 5000


# --------------------------------------------------------------- 누적 규칙


def test_merge_accumulates_and_never_overwrites(client, clean_quota) -> None:
    settings_service.merge_epo_quota({"week": WEEK, "local_bytes": 100, "requests": 1})
    settings_service.merge_epo_quota({"week": WEEK, "local_bytes": 250, "requests": 2})
    stored = _stored()
    assert stored["local_bytes"] == 350
    assert stored["requests"] == 3


def test_ops_header_is_not_summed(client, clean_quota) -> None:
    """OPS 가 준 값은 이미 누적치다. 더하면 두 배가 되고 한도가 일찍 걸린다."""
    settings_service.merge_epo_quota({"week": WEEK, "ops_weekly_bytes": 1000})
    settings_service.merge_epo_quota({"week": WEEK, "ops_weekly_bytes": 1500})
    assert _stored()["ops_weekly_bytes"] == 1500


def test_ops_header_never_shrinks(client, clean_quota) -> None:
    """작은 값으로 덮이면 한도가 느슨해진다. 큰 쪽을 남긴다."""
    settings_service.merge_epo_quota({"week": WEEK, "ops_weekly_bytes": 9000})
    settings_service.merge_epo_quota({"week": WEEK, "ops_weekly_bytes": 10})
    assert _stored()["ops_weekly_bytes"] == 9000


def test_new_week_resets_the_local_counter(client, clean_quota) -> None:
    settings_service.merge_epo_quota(
        {"week": "1999-W01", "local_bytes": 10**9, "requests": 99}
    )
    settings_service.merge_epo_quota({"week": WEEK, "local_bytes": 7, "requests": 1})
    stored = _stored()
    assert stored["week"] == WEEK
    assert stored["local_bytes"] == 7
    assert stored["requests"] == 1


# ------------------------------------------------------------- 동시 실행


def test_concurrent_runs_do_not_lose_usage(client, clean_quota) -> None:
    """읽고-쓰기가 겹치면 한쪽 사용량이 사라진다. 사라지면 한도가 무력해진다.

    처음 구현은 잠금 안에서 flush 만 하고 커밋은 호출자에게 맡겼는데, 잠금을
    놓은 뒤 커밋 전에 다른 스레드가 읽어 옛 값 위에 썼다. 실측으로 재현했다 —
    8스레드 × 25회에서 정확히 절반이 사라졌다.
    """
    threads = 8
    per_thread = 25
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(per_thread):
                settings_service.merge_epo_quota(
                    {"week": WEEK, "local_bytes": 10, "requests": 1}
                )
        except Exception as exc:  # pragma: no cover - 실패 시 원인 보고용
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()

    assert not errors, errors
    stored = _stored()
    assert stored["local_bytes"] == threads * per_thread * 10
    assert stored["requests"] == threads * per_thread


# ------------------------------------------------------- 사용자가 못 고친다


def test_quota_state_is_not_user_editable(client, clean_quota) -> None:
    """PUT 으로 되돌릴 수 있으면 주간 한도는 언제든 무력화된다."""
    assert "epo_quota_state" not in settings_service.EDITABLE_KEYS
    response = client.put(
        "/api/settings",
        json={"values": {"epo_quota_state": {"local_bytes": 0}}},
    )
    assert response.status_code == 400


def test_api_reports_persisted_usage(client, clean_quota) -> None:
    settings_service.merge_epo_quota(
        {
            "week": WEEK,
            "local_bytes": 12345,
            "requests": 2,
            "ops_weekly_bytes": 67890,
        }
    )
    quota = client.get("/api/settings").json()["epo_quota"]
    assert quota["local_bytes"] == 12345
    assert quota["ops_weekly_bytes"] == 67890
    assert quota["effective_weekly_bytes"] == 67890
    assert quota["weekly_limit_bytes"] == epo_quota.WEEKLY_QUOTA_BYTES


# =====================================================================
# 2차 리뷰: 하드 상한이 **전역**으로 지켜지는가.
# =====================================================================


def test_backends_share_one_global_ledger(client, clean_quota) -> None:
    """쿼터가 하나이므로 원장도 하나여야 한다."""
    _install_credentials()
    with session_scope() as session:
        a = settings_service.epo_backend_for(session)
    with session_scope() as session:
        b = settings_service.epo_backend_for(session)
    assert a.ledger is b.ledger


def test_stale_backend_cannot_spend_exhausted_quota(client, clean_quota) -> None:
    """전역 잔량이 100바이트인데 두 백엔드가 모두 통과하던 결함의 회귀.

    각 백엔드가 자기가 처음 읽은 스냅샷으로만 판정하면, 동시에 도는 두 실행이
    같은 잔량을 두 번 쓴다. 바이트가 사라지지 않는 것과 한도가 지켜지는 것은
    다른 문제다.
    """
    _install_credentials()
    # 사용량이 낮을 때 두 실행이 시작됐다.
    with session_scope() as session:
        a = settings_service.epo_backend_for(session)
    with session_scope() as session:
        b = settings_service.epo_backend_for(session)

    # A 쪽 실행이 한도 직전까지 태웠고 그것이 저장됐다.
    settings_service.merge_epo_quota(
        {"week": WEEK, "local_bytes": epo_quota.WEEKLY_QUOTA_BYTES - 100}
    )
    with session_scope() as session:
        settings_service.epo_ledger(session)   # 저장된 값과 맞춘다

    for backend in (a, b):
        with pytest.raises(epo_quota.QuotaExceeded):
            backend.ledger.reserve(epo_client.MAX_RESPONSE_BYTES)


def test_only_one_of_two_racing_requests_gets_the_last_slot(
    client, clean_quota
) -> None:
    """마지막 한 자리를 두고 두 스레드가 겹쳐도 하나만 통과해야 한다."""
    _install_credentials()
    with session_scope() as session:
        ledger = settings_service.epo_ledger(session)
    # 예약이 정확히 하나만 들어갈 자리를 남긴다.
    ledger.sync_from_stored(
        epo_quota.QuotaState(
            week=WEEK,
            local_bytes=epo_quota.WEEKLY_QUOTA_BYTES
            - epo_client.MAX_RESPONSE_BYTES
            - 1,
        )
    )

    granted: list[int] = []
    refused: list[int] = []
    barrier = threading.Barrier(2)

    def worker(index: int) -> None:
        barrier.wait()
        try:
            ledger.reserve(epo_client.MAX_RESPONSE_BYTES)
            granted.append(index)
        except epo_quota.QuotaExceeded:
            refused.append(index)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 1, (granted, refused)
    assert len(refused) == 1


# =====================================================================
# 2차 리뷰: 저장이 실패해도 증분이 살아 있는가.
# =====================================================================


def test_failed_save_keeps_the_increment_for_retry(
    client, clean_quota, monkeypatch
) -> None:
    """눈금을 저장 전에 옮기면 실패한 증분이 영영 사라진다."""
    _install_credentials()
    with session_scope() as session:
        ledger = settings_service.epo_ledger(session)

    def explode(_delta):
        raise RuntimeError("disk full")

    monkeypatch.setattr(settings_service, "merge_epo_quota", explode)
    ledger.record(body_bytes=123, headers={})
    assert ledger.pending_bytes == 123
    assert "disk full" in settings_service.epo_persist_error()

    monkeypatch.undo()
    ledger.record(body_bytes=1, headers={})
    assert ledger.pending_bytes == 0
    assert settings_service.epo_persist_error() == ""
    # 실패했던 123 바이트가 함께 올라갔다.
    assert _stored()["local_bytes"] == 124


def test_save_failure_does_not_mask_the_network_error(
    client, clean_quota, monkeypatch
) -> None:
    """응답 직후 저장이 터지면 사용자는 DB 오류를 진짜 원인으로 오해한다."""
    _install_credentials()
    with session_scope() as session:
        backend = settings_service.epo_backend_for(session)

    def explode(_delta):
        raise RuntimeError("disk full")

    monkeypatch.setattr(settings_service, "merge_epo_quota", explode)

    def refuse(request, timeout):
        raise OSError("connection reset")

    backend._client = epo_client.OpsClient(
        key=TEST_KEY,
        secret=TEST_SECRET,
        ledger=backend.ledger,
        transport=refuse,
        sleep=lambda _s: None,
    )
    with pytest.raises(epo_client.OpsUnavailable, match="접속하지 못했습니다"):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))


def test_pending_bytes_still_enforce_the_limit(client, clean_quota, monkeypatch) -> None:
    """저장이 안 되는 동안에도 한도는 지켜져야 한다."""
    _install_credentials()
    with session_scope() as session:
        ledger = settings_service.epo_ledger(session)

    monkeypatch.setattr(
        settings_service,
        "merge_epo_quota",
        lambda _delta: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    ledger.record(body_bytes=epo_quota.WEEKLY_QUOTA_BYTES, headers={})
    assert ledger.pending_bytes == epo_quota.WEEKLY_QUOTA_BYTES
    with pytest.raises(epo_quota.QuotaExceeded):
        ledger.check()


def test_persist_failure_is_surfaced_to_the_user(
    client, clean_quota, monkeypatch
) -> None:
    _install_credentials()
    with session_scope() as session:
        ledger = settings_service.epo_ledger(session)
    monkeypatch.setattr(
        settings_service,
        "merge_epo_quota",
        lambda _delta: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    ledger.record(body_bytes=10, headers={})

    payload = client.get("/api/settings").json()
    assert "disk full" in payload["epo_quota"]["persist_error"]
    assert any("사용량을 저장하지 못했습니다" in note for note in payload["warnings"])


# =====================================================================
# 3차 리뷰: 잠금 순서 역전(AB-BA 교착).
#
#   이쪽:   원장 잠금 → (저장 콜백) → 저장소 잠금
#   저쪽:   저장소 잠금 → (동기화) → 원장 잠금
#
# 두 방향이 동시에 일어나면 서로를 기다리며 멈춘다. 아래 테스트들은 그
# 순서가 다시 생기면 실패한다.
# =====================================================================


def test_persist_callback_runs_outside_the_ledger_lock() -> None:
    """저장 콜백이 원장 잠금을 쥔 채 불리면 그 안에서 저장소 잠금을 기다린다."""
    ledger = epo_quota.QuotaLedger(state=epo_quota.QuotaState(week=WEEK))
    acquired: list[bool] = []

    def on_change(_state) -> None:
        # 다른 스레드에서 원장 잠금을 잡아 본다. 콜백이 잠금 안에서 불렸다면
        # 여기서 잡히지 않는다(RLock 은 소유 스레드만 재진입 가능).
        def probe() -> None:
            got = ledger._lock.acquire(timeout=1.0)
            acquired.append(got)
            if got:
                ledger._lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(3)

    ledger.on_change = on_change
    ledger.record(body_bytes=1, headers={})
    assert acquired == [True], "저장 콜백이 원장 잠금 안에서 불렸습니다."


def test_settle_also_releases_the_lock_before_the_callback() -> None:
    """settle 은 record 와 다른 경로다. 둘 다 지켜야 한다."""
    ledger = epo_quota.QuotaLedger(state=epo_quota.QuotaState(week=WEEK))
    acquired: list[bool] = []

    def on_change(_state) -> None:
        def probe() -> None:
            got = ledger._lock.acquire(timeout=1.0)
            acquired.append(got)
            if got:
                ledger._lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(3)

    ledger.on_change = on_change
    reservation = ledger.reserve(1000)
    ledger.settle(reservation, body_bytes=10, headers={})
    assert acquired == [True], "settle 이 원장 잠금 안에서 콜백을 불렀습니다."


def test_sync_runs_outside_the_store_lock(client, clean_quota) -> None:
    """동기화가 저장소 잠금 안에서 돌면 그 안에서 원장 잠금을 기다린다."""
    _install_credentials()
    with session_scope() as session:
        ledger = settings_service.epo_ledger(session)

    free: list[bool] = []
    original = ledger.sync_from_stored

    def spy(stored):
        # 저장소 잠금은 재진입 불가(Lock)다. 여기서 잡히면 밖에 있다는 뜻이다.
        got = settings_service._EPO_QUOTA_LOCK.acquire(blocking=False)
        free.append(got)
        if got:
            settings_service._EPO_QUOTA_LOCK.release()
        return original(stored)

    ledger.sync_from_stored = spy
    try:
        with session_scope() as session:
            settings_service.epo_ledger(session)
    finally:
        del ledger.sync_from_stored
    assert free == [True], "동기화가 저장소 잠금 안에서 실행됐습니다."


# 교착을 **별도 프로세스**에서 확인한다.
#
# 같은 프로세스에서 돌리면, 교착이 났을 때 전역 잠금을 쥔 스레드가 영영 끝나지
# 않아 그 뒤의 정리(fixture teardown)까지 함께 멈춘다. 실측으로 확인했다 —
# 옛 순서로 되돌려 돌리니 테스트 실행 전체가 5분을 넘겨도 끝나지 않았다.
# 서브프로세스로 빼면 타임아웃으로 깔끔하게 실패한다.
_DEADLOCK_PROBE = """
import os, sys, tempfile, threading
os.environ["ARIA_DATA_DIR"] = tempfile.mkdtemp(prefix="aria-deadlock-")
sys.path.insert(0, {backend!r})
from app.db import init_engine, session_scope
init_engine()
from app import settings_service

with session_scope() as s:
    settings_service.update(s, {{"epo_integration_enabled": True,
                                "epo_consumer_key": "K",
                                "epo_consumer_secret": "S"}})
with session_scope() as s:
    ledger = settings_service.epo_ledger(s)

stop = threading.Event()
errors = []

def recorder():
    try:
        for _ in range({rounds}):
            # 원장 잠금 -> 저장 콜백 -> 저장소 잠금
            ledger.record(body_bytes=1, headers={{}})
    except Exception as exc:
        errors.append(repr(exc))
    finally:
        stop.set()

def syncer():
    try:
        while not stop.is_set():
            # 저장소 잠금 -> 동기화 -> 원장 잠금
            with session_scope() as s:
                settings_service.epo_ledger(s)
    except Exception as exc:
        errors.append(repr(exc))

threads = [threading.Thread(target=recorder), threading.Thread(target=syncer)]
for t in threads:
    t.start()
for t in threads:
    t.join()
assert not errors, errors
print("OK", ledger.state.local_bytes)
"""


def test_recording_and_syncing_concurrently_do_not_deadlock() -> None:
    """두 방향을 강제로 교차시킨다. 잠금 순서가 역전돼 있으면 여기서 멈춘다.

        이쪽:   원장 잠금 → (저장 콜백) → 저장소 잠금
        저쪽:   저장소 잠금 → (동기화) → 원장 잠금
    """
    rounds = 200
    script = _DEADLOCK_PROBE.format(
        backend=str(Path(__file__).resolve().parents[1]), rounds=rounds
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "60초 안에 끝나지 않았습니다. 잠금 순서 역전으로 교착된 것입니다."
        ) from None
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.startswith("OK"), done.stdout
    # 교착을 피하려고 아무것도 안 한 것이 아니다.
    assert int(done.stdout.split()[1]) >= rounds


# =====================================================================
# 4차 리뷰: 저장 콜백을 원장 잠금 밖으로 뺀 뒤 생긴 **중복 집계** 경쟁.
#
#   A: peek -> 증분 1
#   B: peek -> 증분 2   (A 가 아직 ack 하지 않아 A 것까지 다시 보인다)
#   둘 다 저장 -> DB 3, 실제 2
#
# 덜 세는 게 아니라 더 세는 결함이라 조용하다. 한도를 일찍 소진시켜 멀쩡한
# 검색을 차단하고, pending 이 0 이라 화면에도 드러나지 않는다.
# =====================================================================


def test_persist_holds_its_own_lock_across_merge(client, clean_quota, monkeypatch):
    """peek → merge → ack 이 한 잠금 안에 있어야 겹치지 않는다."""
    _install_credentials()
    with session_scope() as session:
        ledger = settings_service.epo_ledger(session)

    observed: list[bool] = []
    real_merge = settings_service.merge_epo_quota

    def merge_spy(delta):
        # 비재진입 Lock 이라, 잡히지 않으면 누군가(=우리) 쥐고 있다는 뜻이다.
        free = settings_service._EPO_PERSIST_LOCK.acquire(blocking=False)
        observed.append(not free)
        if free:
            settings_service._EPO_PERSIST_LOCK.release()
        return real_merge(delta)

    monkeypatch.setattr(settings_service, "merge_epo_quota", merge_spy)
    ledger.record(body_bytes=7, headers={})
    assert observed == [True], "merge 가 저장 잠금 밖에서 실행됐습니다."


def test_overlapping_persists_do_not_double_count(client, clean_quota, monkeypatch):
    """두 콜백을 강제로 교차시킨다. 옛 구현에서는 2바이트가 3으로 저장됐다."""
    _install_credentials()
    with session_scope() as session:
        ledger = settings_service.epo_ledger(session)

    real_merge = settings_service.merge_epo_quota
    original_peek = ledger.peek_delta
    b_peeked = threading.Event()

    def peek_spy():
        delta = original_peek()
        if threading.current_thread().name == "B":
            b_peeked.set()
        return delta

    def merge_spy(delta):
        if threading.current_thread().name == "A":
            # B 가 겹쳐 들어올 틈을 준다. 직렬화돼 있으면 B 는 아직 peek 하지
            # 못하고, 이 대기는 시간 초과로 끝난다 — 그게 정상 경로다.
            b_peeked.wait(2.0)
        return real_merge(delta)

    ledger.peek_delta = peek_spy
    monkeypatch.setattr(settings_service, "merge_epo_quota", merge_spy)
    try:
        threads = [
            threading.Thread(
                target=lambda: ledger.record(body_bytes=1, headers={}),
                name=name,
                daemon=True,
            )
            for name in ("A", "B")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not [t.name for t in threads if t.is_alive()]
    finally:
        del ledger.peek_delta

    assert ledger.state.local_bytes == 2, "실제 사용량"
    assert _stored()["local_bytes"] == 2, "영구 저장량 (옛 구현에서는 3)"
    assert ledger.pending_bytes == 0, "미저장 증분"


def test_many_concurrent_persists_match_actual_usage(client, clean_quota) -> None:
    """겹침을 인위적으로 만들지 않아도 합계가 어긋나지 않아야 한다."""
    _install_credentials()
    with session_scope() as session:
        ledger = settings_service.epo_ledger(session)

    threads_count = 8
    per_thread = 20
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(per_thread):
                ledger.record(body_bytes=1, headers={})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(threads_count)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(30)

    assert not errors, errors
    expected = threads_count * per_thread
    assert ledger.state.local_bytes == expected
    assert _stored()["local_bytes"] == expected
    assert ledger.pending_bytes == 0
