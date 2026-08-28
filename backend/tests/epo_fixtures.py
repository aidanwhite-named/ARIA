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
        "overloaded (images=black:0, inpadoc=red:5, other=green:1000, "
        "retrieval=red:10, search=red:2)"
    ),
    "X-RegisteredQuotaPerWeek-Used": "104857600",
}
