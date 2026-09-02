"""EPO OPS 응답 모의 데이터.

실제 OPS 응답의 형태만 따온 것이며 실제 호출로 받은 자료가 아니다. 자격증명은
어디에도 들어 있지 않다.
"""

from __future__ import annotations

SEARCH_BIBLIO = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns="http://www.epo.org/exchange">
  <ops:biblio-search total-result-count="137">
    <ops:query syntax="CQL">ti all "robot arm"</ops:query>
    <ops:range begin="1" end="2"/>
    <ops:search-result>
      <exchange-documents>
        <exchange-document system="ops.epo.org" family-id="54321"
                           country="EP" doc-number="1000000" kind="A1">
          <bibliographic-data>
            <publication-reference>
              <document-id document-id-type="docdb">
                <country>EP</country>
                <doc-number>1000000</doc-number>
                <kind>A1</kind>
                <date>20000705</date>
              </document-id>
            </publication-reference>
            <application-reference>
              <document-id document-id-type="docdb">
                <country>EP</country>
                <doc-number>99123456</doc-number>
                <kind>A</kind>
              </document-id>
            </application-reference>
            <classifications-ipcr>
              <classification-ipcr sequence="1">
                <text>B25J  9/16       20060101AFI20051220BHEP</text>
              </classification-ipcr>
            </classifications-ipcr>
            <parties>
              <applicants>
                <applicant sequence="1" data-format="docdb">
                  <applicant-name><name>ACME ROBOTICS GMBH</name></applicant-name>
                </applicant>
                <applicant sequence="1" data-format="epodoc">
                  <applicant-name><name>ACME ROBOTICS GMBH</name></applicant-name>
                </applicant>
              </applicants>
              <inventors>
                <inventor sequence="1" data-format="docdb">
                  <inventor-name><name>MUELLER, HANS</name></inventor-name>
                </inventor>
              </inventors>
            </parties>
            <invention-title lang="en">Articulated robot arm with force feedback</invention-title>
            <invention-title lang="de">Gelenkarmroboter mit Kraftrueckkopplung</invention-title>
          </bibliographic-data>
          <abstract lang="en">
            <p>A robot arm comprising a plurality of joints and a force sensor
               arranged at the end effector.</p>
          </abstract>
        </exchange-document>
        <exchange-document system="ops.epo.org" family-id="99887"
                           country="US" doc-number="9876543" kind="B2">
          <bibliographic-data>
            <publication-reference>
              <document-id document-id-type="docdb">
                <country>US</country>
                <doc-number>9876543</doc-number>
                <kind>B2</kind>
                <date>20180123</date>
              </document-id>
            </publication-reference>
            <parties>
              <applicants>
                <applicant sequence="1" data-format="docdb">
                  <applicant-name><name>GLOBEX CORP</name></applicant-name>
                </applicant>
              </applicants>
            </parties>
            <invention-title lang="en">Compliant manipulator joint</invention-title>
          </bibliographic-data>
          <abstract lang="en">
            <p>A manipulator joint that yields under external load.</p>
          </abstract>
        </exchange-document>
      </exchange-documents>
    </ops:search-result>
  </ops:biblio-search>
</ops:world-patent-data>
"""

SEARCH_EMPTY = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns="http://www.epo.org/exchange">
  <ops:biblio-search total-result-count="0">
    <ops:query syntax="CQL">ti all "nonexistent widget"</ops:query>
    <ops:range begin="1" end="0"/>
    <ops:search-result/>
  </ops:biblio-search>
</ops:world-patent-data>
"""

CLAIMS = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org"
                       xmlns:ftxt="http://www.epo.org/fulltext"
                       xmlns="http://www.epo.org/exchange">
  <ftxt:fulltext-documents>
    <ftxt:fulltext-document>
      <ftxt:publication-reference>
        <document-id document-id-type="docdb">
          <country>EP</country>
          <doc-number>1000000</doc-number>
          <kind>A1</kind>
        </document-id>
      </ftxt:publication-reference>
      <ftxt:claims lang="EN">
        <ftxt:claim num="1">
          <ftxt:claim-text>1. A robot arm comprising a base, a plurality of
          articulated joints, and a force sensor arranged at the end effector.</ftxt:claim-text>
        </ftxt:claim>
        <ftxt:claim num="2">
          <ftxt:claim-text>2. The robot arm of claim 1, wherein the force sensor
          is a six-axis load cell.</ftxt:claim-text>
        </ftxt:claim>
      </ftxt:claims>
    </ftxt:fulltext-document>
  </ftxt:fulltext-documents>
</ops:world-patent-data>
"""

# 엔티티 확장 폭탄. 파서에 들어가기 전에 거절되어야 한다.
BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<lolz>&lol2;</lolz>
"""

TOKEN_OK = b'{"access_token":"FAKE-TOKEN-VALUE","token_type":"BearerToken","expires_in":"1200"}'

ERROR_403 = b"""<?xml version="1.0" encoding="UTF-8"?>
<error><code>403</code><message>Quota per week exceeded</message></error>
"""

# 정상 응답에 붙는 사용량 헤더. 값은 예시다.
HEADERS_OK = {
    "Content-Type": "application/xml",
    "X-Throttling-Control": (
        "idle (images=green:200, inpadoc=green:60, other=green:1000, "
        "retrieval=green:200, search=green:30)"
    ),
    "X-IndividualQuotaPerHour-Used": "1048576",
    "X-RegisteredQuotaPerWeek-Used": "104857600",
}

HEADERS_OVERLOADED = {
    "Content-Type": "application/xml",
    "X-Throttling-Control": (
        "overloaded (images=green:50, inpadoc=green:30, other=green:1000, "
        "retrieval=green:50, search=green:5)"
    ),
    "X-RegisteredQuotaPerWeek-Used": "104857600",
}

HEADERS_RED = {
    "Content-Type": "application/xml",
    "X-Throttling-Control": (
        "overloaded (images=green:50, inpadoc=green:30, other=green:1000, "
        "retrieval=red:50, search=red:5)"
    ),
    "X-RegisteredQuotaPerWeek-Used": "104857600",
}

HEADERS_BLACK = {
    "Content-Type": "application/xml",
    "X-Throttling-Control": (
        "overloaded (images=green:50, inpadoc=green:30, other=green:1000, "
        "retrieval=green:50, search=black:0)"
    ),
    "X-RegisteredQuotaPerWeek-Used": "104857600",
}


# 검색 결과가 0건일 때 OPS 가 실제로 돌려준 응답(2026-08-30 실행에서 관측).
# 상태 코드가 404 라서 예전에는 호출 실패로 처리됐다.
SEARCH_NO_RESULTS_404 = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<fault xmlns="http://ops.epo.org">
    <code>SERVER.EntityNotFound</code>
    <message>No results found</message>
</fault>
"""

# 같은 404 라도 이건 0건이 아니다. 엔드포인트나 문헌이 없는 것이다.
SEARCH_OTHER_FAULT_404 = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<fault xmlns="http://ops.epo.org">
    <code>SERVER.ServiceNotFound</code>
    <message>Service not found</message>
</fault>
"""


# 상태 코드는 500 이지만 내용은 영구 오류다. 질의 자체가 거절된 것이라
# 기다렸다 다시 보내도 같은 답이 온다.
SEARCH_DOMAIN_ACCESS_500 = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<fault xmlns="http://ops.epo.org">
    <code>SERVER.DomainAccess</code>
    <message>The requested domain is not accessible with the given query</message>
</fault>
"""

# 진짜 일시적인 500. fault 문서가 아니므로 재시도 대상이다.
SEARCH_TRANSIENT_500 = b"""<?xml version="1.0" encoding="UTF-8"?>
<html><body>Internal Server Error</body></html>
"""


# fault 문서가 아니다. 본문 어딘가에 같은 문자열이 들어 있을 뿐이다.
# substring 검사를 쓰면 이것을 "결과 0건" 으로 오판한다.
SEARCH_ECHOES_FAULT_TEXT_404 = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org">
  <ops:meta name="note" value="SERVER.EntityNotFound"/>
  <ops:message>No results found</ops:message>
</ops:world-patent-data>
"""
