"""Kiwee(한국특허정보원 게이트웨이) 특허 검색 백엔드.

현재는 골격만 있다. 실제 접속(gateway.kiwee.or.kr 의 Solr/게이트웨이 API)은
다음 두 가지가 확정된 뒤에만 구현한다.

  1) 공급자(한국특허정보원)의 외부 호출 허용과 공식 API 계약.
     클라이언트 바이너리에 URL·자격증명이 보인다는 것과 '공식적으로 호출해도
     되는 API'는 다른 문제다. 게이트웨이는 로그인 세션키와 클라이언트 버전을
     검사하고, Solr 경로는 별도 자격증명 방식을 쓴다.
  2) NK 검색과의 동등성이 검증된 어댑터. NK 는 q 하나만 보내지 않는다.
     국가·문헌종류별 shard 선택, surround 근접연산, 동의어·분류코드 확장,
     패밀리 정리를 거친다. 이를 재현하지 않으면 결과 수·순위가 달라진다.

그 전까지 search() 는 절대 네트워크를 열지 않고 PatentSearchNotConfigured 를
던진다. 이 골격의 목적은 '연동 지점을 모듈로 고정'하는 것뿐이다 — 실제 검색
동작을 흉내내지 않는다.
"""

from __future__ import annotations

from .base import (
    BackendStatus,
    PatentSearchBackend,
    PatentSearchNotConfigured,
    PatentSearchQuery,
    PatentSearchResponse,
)

_NOT_CONFIGURED_DETAIL = (
    "연동이 켜져 있으나 접속·인증이 아직 구현되지 않아 실제 검색은 수행되지 "
    "않습니다. 공급자 승인과 API 계약 확정 후 활성화됩니다."
)


class KiweePatentSearchBackend(PatentSearchBackend):
    id = "kiwee"
    display_name = "Kiwee 특허 검색"

    def status(self) -> BackendStatus:
        return BackendStatus(
            backend_id=self.id,
            display_name=self.display_name,
            enabled=True,
            configured=False,
            detail=_NOT_CONFIGURED_DETAIL,
        )

    def search(self, query: PatentSearchQuery) -> PatentSearchResponse:
        raise PatentSearchNotConfigured(_NOT_CONFIGURED_DETAIL)
