"""증거 정책 — 원문 등급을 허용할지 한 곳에서 정한다.

왜 중앙 정책인가
----------------
이전에는 verify_excerpt(unlocked=True) 처럼 호출자가 잠금을 풀 수 있었다.
그건 잠금이 아니라 기본값이다. 호출부가 늘어나면 누군가는 True 를 넘기고,
그 순간 어디서 풀렸는지 추적할 수 없게 된다.

정책은 하나뿐이고, 검증 결과에 어떤 정책으로 판정했는지가 함께 남는다.
감사 기록에 정책 버전이 들어가야 "이 판정은 어떤 규칙에서 나왔나"에 답할 수
있다.

정책이 켜져도 선언은 증거가 아니다
----------------------------------
raw_enabled 는 '원문 등급을 허용하는가'만 정한다. 어떤 필드가 공식 원문인지는
정책이 아니라 등록된 소스 프로필이 정한다. 정책을 켜도 raw_capable 프로필로
뽑지 않은 값은 원문 등급을 받지 못한다. 두 관문은 독립이다 — 정책 하나를
켜면 전부 열리는 구조를 만들지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvidencePolicy:
    """증거 등급 판정 규칙. 감사 기록에 version 이 남는다."""

    version: str
    raw_enabled: bool
    note: str = ""


# 기본 정책. 원문 등급은 꺼져 있다.
#
# Kiwee 응답에 공식 XML 원문 필드가 실제로 있는지 확인되지 않았다. 도달할 수
# 없는 등급을 열어 두면 "기껏 만들었는데 아무것도 raw 를 못 받네, 조건 좀
# 풀자"는 압력이 생긴다. 안전 장치가 무너지는 전형적인 경로다.
RAW_DISABLED = EvidencePolicy(
    version="raw-disabled-1",
    raw_enabled=False,
    note="공식 XML 도달 가능성이 확인되기 전까지 원문 등급을 부여하지 않는다.",
)

# 실제 응답 검토 후 raw_capable 프로필을 등록하면서 함께 바꾼다.
RAW_ENABLED = EvidencePolicy(
    version="raw-enabled-1",
    raw_enabled=True,
    note="검토된 raw_capable 프로필에 한해 원문 등급을 허용한다.",
)

_current: EvidencePolicy = RAW_DISABLED


def current() -> EvidencePolicy:
    """지금 적용 중인 정책."""
    return _current


def set_current(policy: EvidencePolicy) -> None:
    """정책을 바꾼다. 테스트와 배포 설정에서만 쓴다."""
    global _current
    _current = policy
