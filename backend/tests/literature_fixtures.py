"""Crossref·Europe PMC 응답 고정 자료.

epo_fixtures 와 달리 **실제 호출로 받은 응답**이다(2026-09-01 채취). 형태만
흉내 낸 자료를 쓰지 않는 이유는, 이 파서가 다루어야 하는 것이 바로 실제 응답의
지저분한 부분이기 때문이다 — Crossref 초록은 JATS 조각으로 오고, 저자는
given/family 로 쪼개져 있고, Europe PMC 는 제목 끝에 마침표를 붙인다. 손으로
만든 자료로는 그 셋 다 놓친다.

크기를 줄이려고 참고문헌 목록(reference)과 전문 링크 목록(fullTextUrlList) 등
파서가 읽지 않는 배열은 지웠다. 파서가 읽는 경로는 하나도 건드리지 않았으며,
배열 인덱스가 바뀌지 않도록 항목을 지우는 대신 키만 지웠다.

자격증명은 어디에도 들어 있지 않다. 두 API 모두 인증이 없다.
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent / "fixtures" / "literature"

# 목표 문헌. 2026-09-01 조사에서 웹 채널이 식별하지 못한 논문이다.
TARGET_DOI = "10.3390/s25103219"
TARGET_TITLE = (
    "The Design of a Computer Vision Sensor Based on a Low-Power Edge "
    "Detection Circuit"
)


def _read(name: str) -> bytes:
    return (_DIR / name).read_bytes()


#: ``/works/10.3390/s25103219`` 응답. 초록이 JATS 조각으로 들어 있다.
CROSSREF_WORK = _read("crossref_work.json")

#: ``/works?query.bibliographic=computer vision sensor low-power edge detection
#: circuit`` 응답. 목표 문헌이 1위로 온다.
CROSSREF_SEARCH = _read("crossref_search.json")

#: Europe PMC 개념 검색 응답. 같은 문헌이 1위로 온다 — Crossref 가 제목으로
#: 찾는 것과 달리 이쪽은 초록 문장으로 찾는다.
EUROPEPMC_SEARCH = _read("epmc_search.json")

#: ``DOI:"10.3390/s25103219"`` 로 지정한 Europe PMC 응답. 초록이 평문이다.
EUROPEPMC_DETAIL = _read("epmc_detail.json")

#: 결과 0건인 Crossref 검색 응답.
CROSSREF_EMPTY = (
    b'{"status":"ok","message-type":"work-list","message":'
    b'{"total-results":0,"items":[]}}'
)

#: 결과 0건인 Europe PMC 응답.
EUROPEPMC_EMPTY = b'{"version":"6.9","hitCount":0,"resultList":{"result":[]}}'
