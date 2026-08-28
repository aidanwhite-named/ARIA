"""EPO OPS 사용량 — 관측, 보존, 차단.

OPS 는 요청 수가 아니라 **데이터량**으로 과금된다. 그래서 "몇 번 불렀나"를
세는 것으로는 한도에 얼마나 다가갔는지 알 수 없고, 403 을 받고 나서야 알게
된다. 그 시점에는 그 주가 이미 끝나 있다.

두 개의 숫자를 따로 본다
------------------------
    OPS 가 알려주는 값   응답 헤더의 주간·시간당 사용량. **권위 있는 값**이다.
    ARIA 가 세는 값      우리가 받은 응답 바이트의 누적. 헤더가 없을 때의
                         대비책이고, 헤더와 어긋나면 그 사실 자체가 신호다.

둘을 하나로 합치지 않는다. 합치면 "OPS 는 3GB 라는데 우리는 1GB 로 셌다"는
불일치가 사라지고, 어느 쪽이 틀렸는지 물을 수 없게 된다. 판정에는 둘 중 **큰
값**을 쓴다 — 적게 세고 넘기는 것보다 많이 세고 일찍 멈추는 편이 낫다.

무엇을 막고 무엇을 관측만 하는가
--------------------------------
    주간 4GB      계약값이다. 하드 차단한다.
    시간당        OPS 가 헤더로 사용량을 주지만 계약 상한값이 우리 쪽에
                  확정되어 있지 않다. 그래서 기본은 **관측·표시만** 하고,
                  사용자가 상한을 넣으면 그때부터 차단한다. 모르는 숫자를
                  기본값으로 박아 두면 "왜 멈췄지"에 답할 수 없다.
    스로틀링 상태  OPS 자신이 지금 부하로 판단한 값이다. 추측이 아니라 관측
                  이므로 항상 켜 두고, 위험 상태면 즉시 EPO 채널만 멈춘다.

이 모듈은 네트워크를 모른다. 헤더 문자열과 바이트 수만 받는다.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

# 주간 계약량. 사용자 확정값(2026-08-28).
#
# **십진 GB 다.** EPO 는 "4GB" 라고만 적고 이진 단위라고 명시하지 않는다.
# 4 GiB(4*1024^3)로 잡으면 계약값보다 7.4% 큰 값을 한도로 쓰게 되고, 그
# 차이만큼은 "아직 남았다"고 표시하면서 실제로는 넘긴 상태가 된다. 한도를
# 모를 때는 작은 쪽으로 잡는다 — 틀렸을 때 일찍 멈추는 쪽이 낫다.
WEEKLY_QUOTA_BYTES = 4 * 1000 * 1000 * 1000

# 이 비율을 넘으면 화면에 경고를 띄운다. 차단은 100% 에서만 한다.
WARN_RATIO = 0.8

# OPS 응답 헤더 이름. 대소문자를 가리지 않고 찾는다.
HEADER_THROTTLE = "x-throttling-control"
HEADER_HOURLY_USED = "x-individualquotaperhour-used"
HEADER_WEEKLY_USED = "x-registeredquotaperweek-used"

# 스로틀링 상태 중 EPO 채널을 멈춰야 하는 값.
DANGEROUS_SYSTEM_STATES = frozenset({"overloaded"})
DANGEROUS_SERVICE_COLORS = frozenset({"red", "black"})
# 이 서비스들이 위험 상태면 멈춘다. 우리가 실제로 쓰는 것만 본다 — images 가
# 빨간 것을 이유로 검색을 멈추면 멈출 이유가 없을 때 멈추게 된다.
WATCHED_SERVICES = ("search", "retrieval")

_COLORS = ("green", "yellow", "red", "black")
_PAIR = re.compile(r"([A-Za-z_]+)\s*=\s*([^,()]+)")
_DIGITS = re.compile(r"\d+")


class QuotaError(Exception):
    """사용량 때문에 EPO 채널을 계속할 수 없다."""


class QuotaExceeded(QuotaError):
    """주간(또는 설정된 시간당) 한도를 넘었다."""


class Throttled(QuotaError):
    """OPS 가 지금 위험한 부하 상태라고 보고했다."""


@dataclass(frozen=True)
class Reservation:
    """날아가 있는 요청 하나가 잡아 둔 최대 응답량.

    "검사"와 "차지"가 따로 있으면 그 사이에 다른 요청이 끼어든다. 둘을 한
    잠금 안에서 함께 하고, 응답이 오면 실제 바이트로 정산한다. 정산은 반드시
    일어나야 하므로 호출부는 finally 에서 부른다 — 예약이 남으면 쓰지도 않은
    양이 한도를 차지한 채 굳는다.
    """

    amount: int


@dataclass(frozen=True)
class ThrottleReading:
    """``X-Throttling-Control`` 한 줄에서 읽어낸 것.

    raw 를 함께 보관한다. 이 헤더의 내부 형식은 OPS 쪽 사정으로 바뀔 수 있고,
    그때 우리 파서가 못 읽더라도 원문은 남아 있어야 무슨 일이 있었는지 나중에
    확인할 수 있다.
    """

    raw: str = ""
    system_state: str = ""
    services: dict[str, str] = field(default_factory=dict)

    @property
    def dangerous(self) -> bool:
        if self.system_state.lower() in DANGEROUS_SYSTEM_STATES:
            return True
        for name in WATCHED_SERVICES:
            if self.services.get(name, "").lower() in DANGEROUS_SERVICE_COLORS:
                return True
        return False

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "system_state": self.system_state,
            "services": dict(self.services),
            "dangerous": self.dangerous,
        }


def parse_throttle(value: str) -> ThrottleReading:
    """``idle (search=green:30, retrieval=green:200)`` 같은 줄을 읽는다.

    괄호 안 값의 내부 형식은 가정하지 않는다. 색깔 단어가 있으면 그것만 뽑고,
    없으면 값을 그대로 둔다. 형식을 강하게 가정하면 OPS 가 조금만 바꿔도
    파서가 조용히 빈 값을 돌려주고, 그 순간 위험 상태를 못 보게 된다.
    """
    raw = str(value or "").strip()
    if not raw:
        return ThrottleReading()
    head = raw.split("(", 1)[0].strip()
    services: dict[str, str] = {}
    inside = raw[raw.find("(") + 1 : raw.rfind(")")] if "(" in raw and ")" in raw else ""
    for name, rest in _PAIR.findall(inside):
        text = rest.strip()
        color = next((c for c in _COLORS if c in text.lower()), "")
        services[name.strip().lower()] = color or text
    return ThrottleReading(raw=raw, system_state=head.lower(), services=services)


def _header(headers, name: str) -> str:
    """대소문자를 가리지 않고 헤더를 찾는다."""
    if not headers:
        return ""
    for key, value in dict(headers).items():
        if str(key).lower() == name:
            return str(value or "")
    return ""


def _first_int(text: str) -> int | None:
    """헤더 값에서 첫 정수. ``12345`` 도 ``12345 bytes`` 도 읽는다."""
    match = _DIGITS.search(str(text or ""))
    return int(match.group(0)) if match else None


def _max_or_none(left, right):
    values = [int(v) for v in (left, right) if v is not None]
    return max(values) if values else None


def week_key(now: datetime | None = None) -> str:
    """ARIA 로컬 카운터의 주간 창. UTC ISO 주(월요일 시작).

    OPS 의 주간 창과 정확히 같다고 주장하지 않는다. 로컬 카운터는 헤더가 없을
    때의 대비책이므로, 창이 조금 어긋나면 더 보수적으로 셀 뿐이다.
    """
    moment = now or datetime.now(timezone.utc)
    year, week, _ = moment.astimezone(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(frozen=True)
class QuotaState:
    """저장되는 사용량 상태. AppSetting 한 칸에 JSON 으로 들어간다."""

    week: str = ""
    local_bytes: int = 0            # ARIA 가 센 이번 주 누적 응답 바이트
    requests: int = 0               # 이번 주 OPS 요청 수 (참고용)
    ops_weekly_bytes: int | None = None   # OPS 헤더가 알려준 주간 사용량
    ops_hourly_bytes: int | None = None   # OPS 헤더가 알려준 시간당 사용량
    throttle: ThrottleReading = field(default_factory=ThrottleReading)
    observed_at: str = ""

    @property
    def effective_weekly_bytes(self) -> int:
        """판정에 쓰는 주간 사용량. 두 값 중 큰 쪽."""
        return max(self.local_bytes, self.ops_weekly_bytes or 0)

    def to_dict(self) -> dict:
        return {
            "week": self.week,
            "local_bytes": self.local_bytes,
            "requests": self.requests,
            "ops_weekly_bytes": self.ops_weekly_bytes,
            "ops_hourly_bytes": self.ops_hourly_bytes,
            "throttle": self.throttle.to_dict(),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data) -> "QuotaState":
        if not isinstance(data, dict):
            return cls()
        throttle = data.get("throttle")
        reading = ThrottleReading()
        if isinstance(throttle, dict):
            services = throttle.get("services")
            reading = ThrottleReading(
                raw=str(throttle.get("raw") or ""),
                system_state=str(throttle.get("system_state") or ""),
                services=(
                    {str(k): str(v) for k, v in services.items()}
                    if isinstance(services, dict)
                    else {}
                ),
            )

        def _int_or_none(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            week=str(data.get("week") or ""),
            local_bytes=max(0, _int_or_none(data.get("local_bytes")) or 0),
            requests=max(0, _int_or_none(data.get("requests")) or 0),
            ops_weekly_bytes=_int_or_none(data.get("ops_weekly_bytes")),
            ops_hourly_bytes=_int_or_none(data.get("ops_hourly_bytes")),
            throttle=reading,
            observed_at=str(data.get("observed_at") or ""),
        )


@dataclass
class QuotaLedger:
    """사용량을 세고, 한도를 넘으면 막는다.

    상태를 어디에 저장할지는 모른다. ``state`` 를 들고 있다가 바뀌면
    ``on_change`` 로 알려 줄 뿐이다 — 이 모듈이 DB 를 알면 테스트에서 세션이
    필요해지고, 그러면 사용량 계산 테스트가 DB 테스트가 된다.

    저장은 **덮어쓰기가 아니라 누적**으로 한다. drain() 이 "지난번 비운 뒤로
    늘어난 만큼"을 돌려주고, 저장하는 쪽이 그 증분을 기존 값에 더한다.
    현재 상태를 통째로 써 넣으면, 두 실행이 같은 값을 읽고 각자 쓸 때 한쪽의
    사용량이 사라진다. 사용량이 사라지는 방향의 결함은 한도를 무력화한다.
    """

    state: QuotaState = field(default_factory=QuotaState)
    weekly_limit: int = WEEKLY_QUOTA_BYTES
    hourly_limit: int = 0            # 0 = 관측만, 차단하지 않음
    on_change: object = None         # Callable[[QuotaState], None] | None

    # 마지막으로 **저장에 성공한** 시점의 눈금. 증분 계산의 기준이다.
    _acked_bytes: int = field(default=0, init=False, repr=False)
    _acked_requests: int = field(default=0, init=False, repr=False)
    # 지금 날아가 있는 요청들이 잡아 둔 양의 합.
    _reserved: int = field(default=0, init=False, repr=False)
    # check 와 reserve 를 한 동작으로 묶는다. 둘이 떨어져 있으면 그 사이에
    # 다른 스레드가 같은 잔량을 보고 통과한다.
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        # 주가 바뀌었으면 로컬 카운터를 새로 시작한다. OPS 가 준 값은 그대로
        # 두지 않고 비운다 — 지난 주의 사용량을 이번 주 값으로 보이게 하면
        # 안 된다.
        current = week_key()
        if self.state.week != current:
            self.state = QuotaState(week=current)
            self._changed()
        # 불러온 상태는 이미 저장되어 있는 값이다. 이것을 증분으로 다시
        # 올리면 실행할 때마다 사용량이 두 배가 된다.
        self._acked_bytes = self.state.local_bytes
        self._acked_requests = self.state.requests

    def _changed(self) -> None:
        if callable(self.on_change):
            self.on_change(self.state)

    # --- 판정 -----------------------------------------------------------
    def check(self, reserve: int = 0) -> None:
        """다음 요청을 보내도 되는가. 안 되면 예외를 던진다.

        요청 **전에** 부른다. 보내고 나서 세면 한도를 넘긴 뒤에 알게 된다.

        reserve 는 이번 요청이 최대로 받을 수 있는 바이트다. 이것을 더해서
        보지 않으면 "한도 1바이트 전"에서도 응답 상한만큼 더 받아 한도를
        넘긴다. 하드 상한이라고 부르려면 넘길 수 있는 경로가 없어야 한다.
        """
        if self.state.throttle.dangerous:
            raise Throttled(
                "EPO OPS 가 과부하 상태를 보고했습니다"
                f"({self.state.throttle.raw or self.state.throttle.system_state}). "
                "EPO 채널을 중단합니다."
            )
        headroom = max(0, int(reserve or 0)) + self._reserved
        used = self.state.effective_weekly_bytes
        if self.weekly_limit and used + headroom >= self.weekly_limit:
            raise QuotaExceeded(
                f"이번 주 EPO OPS 사용량이 한도에 도달했습니다"
                f"({used:,} + 이번 요청 최대 {headroom:,} / "
                f"{self.weekly_limit:,} bytes). 다음 주까지 EPO 채널을 사용할 "
                "수 없습니다."
            )
        hourly = self.state.ops_hourly_bytes or 0
        if self.hourly_limit and hourly + headroom >= self.hourly_limit:
            raise QuotaExceeded(
                f"시간당 EPO OPS 사용량이 설정한 한도에 도달했습니다"
                f"({hourly:,} + 이번 요청 최대 {headroom:,} / "
                f"{self.hourly_limit:,} bytes)."
            )

    @property
    def remaining_weekly(self) -> int:
        if not self.weekly_limit:
            return 0
        return max(0, self.weekly_limit - self.state.effective_weekly_bytes)

    @property
    def warn(self) -> bool:
        if not self.weekly_limit:
            return False
        return self.state.effective_weekly_bytes >= self.weekly_limit * WARN_RATIO

    # --- 관측 -----------------------------------------------------------
    def record(self, *, body_bytes: int, headers=None) -> QuotaState:
        """응답 하나를 반영한다. 요청 **후에** 부른다.

        저장 콜백(on_change)은 **잠금을 놓은 뒤** 부른다. 잠금을 쥔 채 부르면
        잠금 순서가 역전된다.

            이쪽:   원장 잠금 → (저장 콜백) → 저장소 잠금
            저쪽:   저장소 잠금 → (동기화) → 원장 잠금

        두 방향이 동시에 일어나면 서로를 기다리며 멈춘다(AB-BA 교착). 콜백을
        밖으로 빼면 원장 잠금을 쥔 채 다른 잠금을 기다리는 경로가 사라진다.

        콜백이 보는 것은 방금 확정된 상태이고, 그 사이에 다른 응답이 들어와도
        peek/ack 이 증분 단위로 처리하므로 중복도 누락도 없다.
        """
        with self._lock:
            state = self._record_locked(body_bytes=body_bytes, headers=headers)
        self._changed()
        return state

    def _record_locked(self, *, body_bytes: int, headers=None) -> QuotaState:
        throttle = self.state.throttle
        weekly = self.state.ops_weekly_bytes
        hourly = self.state.ops_hourly_bytes

        raw_throttle = _header(headers, HEADER_THROTTLE)
        if raw_throttle:
            throttle = parse_throttle(raw_throttle)
        observed_weekly = _first_int(_header(headers, HEADER_WEEKLY_USED))
        if observed_weekly is not None:
            weekly = observed_weekly
        observed_hourly = _first_int(_header(headers, HEADER_HOURLY_USED))
        if observed_hourly is not None:
            hourly = observed_hourly

        self.state = QuotaState(
            week=self.state.week or week_key(),
            local_bytes=self.state.local_bytes + max(0, int(body_bytes or 0)),
            requests=self.state.requests + 1,
            ops_weekly_bytes=weekly,
            ops_hourly_bytes=hourly,
            throttle=throttle,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
        # _changed() 는 여기서 부르지 않는다. 잠금 밖에서 부르는 것이 계약이다.
        return self.state

    # --- 예약 ------------------------------------------------------------
    def reserve(self, amount: int) -> Reservation:
        """검사와 차지를 **한 동작으로** 한다. 못 하면 예외.

        예전에는 논리 검색 하나당 check() 를 한 번만 했다. 그런데 토큰이 없으면
        토큰 응답과 검색 응답 두 개가 오고, 401 재인증이 끼면 더 온다. 예약
        하나로 여러 응답을 받으면 한도를 넘긴다 — 실측으로 36바이트 초과를
        재현했다. 그래서 예약 단위를 **실제 HTTP 전송 하나**로 내렸다.
        """
        headroom = max(0, int(amount or 0))
        with self._lock:
            self.check(reserve=headroom)
            self._reserved += headroom
            return Reservation(amount=headroom)

    def settle(
        self, reservation: Reservation, *, body_bytes: int, headers=None
    ) -> QuotaState:
        """예약을 풀고 실제로 받은 양으로 정산한다. 반드시 불려야 한다.

        상태 갱신은 잠금 안에서, 저장 콜백은 잠금 **밖에서** 한다. 이유는
        아래 record 의 주석에 있다.
        """
        with self._lock:
            self._reserved = max(0, self._reserved - max(0, reservation.amount))
            state = self._record_locked(body_bytes=body_bytes, headers=headers)
        self._changed()
        return state

    @property
    def reserved_bytes(self) -> int:
        return self._reserved

    # --- 저장 (2단계: peek → 저장 성공 → ack) -----------------------------
    def peek_delta(self) -> dict:
        """저장할 증분을 **눈금을 옮기지 않고** 본다.

        옮기고 나서 저장하면, 저장이 실패했을 때 그 증분을 다시 낼 수 없다.
        실측으로 재현했다 — 123바이트를 기록한 뒤 저장을 실패시키자 다음
        호출이 0을 돌려주었고 그만큼이 영영 사라졌다.
        """
        with self._lock:
            return {
                "week": self.state.week,
                "local_bytes": max(0, self.state.local_bytes - self._acked_bytes),
                "requests": max(0, self.state.requests - self._acked_requests),
                "ops_weekly_bytes": self.state.ops_weekly_bytes,
                "ops_hourly_bytes": self.state.ops_hourly_bytes,
                "throttle": self.state.throttle.to_dict(),
                "observed_at": self.state.observed_at,
            }

    def ack(self, delta: dict) -> None:
        """저장에 **성공한 뒤** 눈금을 옮긴다."""
        if not isinstance(delta, dict):
            return
        with self._lock:
            self._acked_bytes += max(0, int(delta.get("local_bytes") or 0))
            self._acked_requests += max(0, int(delta.get("requests") or 0))

    @property
    def pending_bytes(self) -> int:
        """아직 저장되지 않은 증분. 저장이 실패하면 여기 남아 있다."""
        with self._lock:
            return max(0, self.state.local_bytes - self._acked_bytes)

    def sync_from_stored(self, stored: QuotaState) -> None:
        """저장된 값과 맞춘다. 아직 저장 못 한 증분은 잃지 않는다.

        전역 원장은 오래 살아 있으므로 그 사이에 주가 바뀌거나 다른 경로가
        저장소를 고쳤을 수 있다. 판정 기준은 **지금 주**다.

          - 우리 쪽이 지난 주면 새 주로 넘어간다(누적 초기화).
          - 저장소가 지난 주면 반영하지 않는다. 지난 주 누적을 이번 주 값으로
            되살리면 한도가 잘못 걸린다.
          - 같은 주면 큰 쪽을 남긴다. 우리가 쓴 것은 이미 저장소에 들어 있고,
            아직 못 올린 증분(pending)은 그대로 지킨다.

        이 메서드는 자기 잠금만 쓴다. 저장소 잠금을 쥔 채 부르면 순서가
        역전되므로 호출부는 잠금 밖에서 부른다.
        """
        current = week_key()
        with self._lock:
            pending = max(0, self.state.local_bytes - self._acked_bytes)
            pending_requests = max(0, self.state.requests - self._acked_requests)

            if self.state.week != current:
                # 우리 쪽이 낡았다. 이전 주 누적은 이월하지 않는다.
                self.state = QuotaState(week=current)
                self._acked_bytes = 0
                self._acked_requests = 0
                pending = 0
                pending_requests = 0

            if stored.week != current:
                # 저장소가 이번 주 값이 아니다. 반영하지 않는다.
                return

            local = max(self.state.local_bytes, stored.local_bytes)
            requests = max(self.state.requests, stored.requests)
            self.state = QuotaState(
                week=current,
                local_bytes=local,
                requests=requests,
                ops_weekly_bytes=_max_or_none(
                    self.state.ops_weekly_bytes, stored.ops_weekly_bytes
                ),
                ops_hourly_bytes=(
                    stored.ops_hourly_bytes
                    if stored.ops_hourly_bytes is not None
                    else self.state.ops_hourly_bytes
                ),
                throttle=(
                    self.state.throttle if self.state.throttle.raw else stored.throttle
                ),
                observed_at=self.state.observed_at or stored.observed_at,
            )
            self._acked_bytes = max(0, local - pending)
            self._acked_requests = max(0, requests - pending_requests)

    def snapshot(self) -> dict:
        """화면·manifest 에 실을 값. 두 숫자를 따로 남긴다."""
        return {
            "week": self.state.week,
            "weekly_limit_bytes": self.weekly_limit,
            "hourly_limit_bytes": self.hourly_limit,
            "local_bytes": self.state.local_bytes,
            "ops_weekly_bytes": self.state.ops_weekly_bytes,
            "ops_hourly_bytes": self.state.ops_hourly_bytes,
            "effective_weekly_bytes": self.state.effective_weekly_bytes,
            "remaining_weekly_bytes": self.remaining_weekly,
            "requests": self.state.requests,
            "reserved_bytes": self._reserved,
            "pending_bytes": self.pending_bytes,
            "warn": self.warn,
            "throttle": self.state.throttle.to_dict(),
            "observed_at": self.state.observed_at,
        }
