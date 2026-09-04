# 유사문헌 검색 단순화 — 변경·검증 기록

작성일: 2026-09-04. 현재 작업 트리 기준이며, 아직 커밋하거나 실행 중인 서버를 재시작하지 않았다.

## 결론과 완료 범위

검색 판단 경로를 하나로 바꿨다. 독립 EPO·논문 검색, 청구항/명세서 이중 실행,
강제 후보 합집합, 비용·슬롯 기반 후보 선택, 공식 응답을 이용한 두 번째 AI 분류를 제거했다.
기존 엔진을 선택하는 모드나 기능 플래그는 없다.

그러나 **모든 Provider에서 실서비스 검증까지 완료한 상태는 아니다.** Claude 인증,
agy의 실행별 MCP 연결, 기존 Kiwee 미구현 부분은 아래 제약을 확인해야 한다.
또 원안의 Master Prompt와 Search Prompt 동시 조립 여부는 사용자 확인을 기다리고 있다.

현재 실행은 다음과 같다.

사용자 입력 + 선택한 검색 전략 + ARIA 실행 계약 → 선택한 Provider 1회 실행
→ LLM이 도구·질의·확장·후보·순서·A/B/C/null을 결정
→ ARIA가 JSON/식별자/보존 근거/공개일을 검사 → 감사 기록 및 보고서 저장.

명세서는 동일 실행의 참고자료이며 별도 검색 레인이 아니다. 분석·문헌 로컬 검색의
별도 기능은 이번 유사문헌 검색 단순화의 삭제 대상이 아니다.

## 책임별 변경

### 유지한 사실 검증과 안전장치

- A/B/C/null과 기술적 대응 판단은 그대로 보존한다. 증거 등급으로 그룹을 바꾸지 않는다.
- 정확한 공개번호 또는 DOI 중복만 정리하고 먼저 나온 후보를 유지한다.
  국가·공개종류가 다른 문헌을 패밀리라는 이유로 합치지 않는다. 그룹 충돌은 코드로 기록한다.
- 문헌번호 형식과 URL의 명백한 문헌번호 불일치를 검사한다. 다른 문헌의 페이지를
  읽었다고 해당 후보가 확인된 것으로 표시하지 않는다. 조회 실패는 문헌 부재가 아니다.
- 도구 응답의 식별자, 아티팩트 해시, 등록된 파서의 필드 경로를 대조한다.
  모델이 직접 쓴 `original_verified`, 출처, 증거 등급은 신뢰하지 않는다.
- 초록·청구항·명세서·서지·패밀리의 확보 범위를 각각 표시한다. 보존된 API 응답과
  일치하는 문장도 무조건 원문 직접 인용으로 승격하지 않는다.
- 실제 페이지 열람과 URL 조회 시도를 구분한다. Codex의 네이티브 URL 조회 이벤트만으로는
  본문 열람 성공을 인정하지 않는다. agy는 현재 대화에서 반환된 페이지 파일의 실제 읽기를 확인한다.
- 입력 경계 중화, 입력 크기, 전체 시간, 도구 정책, 결과 형식 검사, 취소·실패 감사 기록을 유지한다.
- EPO CQL의 구조·필드 검증, 오류 코드, HTTP 예산, 데이터 쿼터 원장, 응답 아티팩트와
  provenance 검증·보존 기간 관리는 기존 하위 계층을 이용한다.
- `NO_TOOLS` 평가가 도구 이름 목록뿐 아니라 실제 호출 기록도 검사하도록 보강했다.

### LLM 호출 도구로 전환

`search_mcp_server.py`는 실행별 stdio 서버다. ARIA가 선택한 작업 폴더와 설정만 전달한다.
호스트의 전역 MCP 설정을 추가·변경하지 않는다.

- `epo_search`: 구조화 CQL 입력을 검증하고 실제 CQL 및 응답 아티팩트 참조를 반환한다.
- `epo_fetch`: LLM이 고른 문헌번호와 constituent를 조회한다.
- `literature_search` / `literature_fetch`: LLM이 작성한 질의 또는 정확한 DOI를 사용한다.
- `search_capabilities`: 비활성·인증 미설정·미구현 여부를 알린다. 외부 검색은 하지 않는다.
- Kiwee는 기존 백엔드가 접속 미구현 상태여서 사용 가능 도구로 광고하지 않는다.

검색 질의를 다른 용도로 자동 변환하지 않는다. 실제 인자·질의·CQL·응답·실패를
작업별 `search_tool_calls.jsonl`에 기록하고 매 호출 flush/fsync한다.
MCP 한도를 넘는 N+1번째 호출은 요청을 보내기 전에 거절하고, 재시작 시 원장에서 사용량을 복원한다.
EPO 요청은 프로세스 간 잠금 안에서 영속 쿼터를 동기화한다.

### 삭제한 정책·파일

- `execution/runner.py`: 독립 EPO·논문 실행 함수, 이중 검색 실행과 병합, 후처리 공식 재분류.
- `search_verification.py`: 검증 후보 선발, 출처별 예약석, 재사용/예상 비용 순위,
  승격·잠정 상태, coverage 기준의 등급 판정.
- `search_manifest.py`: 채널 강제 병합, 분류 교체 상태기계, 내부 슬롯·재사용 계획.
- `search_plan.py`, `literature_query.py`: 자동 검색 계획, 특허→논문 질의 변환,
  논문 개념 점수와 후보 순위. 질의 생성은 LLM의 책임으로 이동했다.
- `patent_search/epo_agent.py`, `epo_actions.py`, `epo_prompts.py`: 별도 EPO 에이전트 실행기.
- 프론트엔드의 이전 분류 상태 타입·렌더러와 세부 후보/검증 슬롯 설정.

제거한 설정 키:
`epo_max_results_per_query`, `epo_shortlist_limit`, `epo_verification_targets`,
`epo_max_search_calls`, `epo_channel_timeout_seconds`, `literature_max_queries`,
`literature_shortlist_limit`, `literature_verification_targets`, `official_coverage_ratio`.
마지막 coverage 키는 시작 시점의 미커밋 변경에 있던 항목이다.
DB의 기존 설정 행과 과거 결과는 삭제하지 않으며, 폐기한 키는 활성 설정에서 읽거나 수정하지 않는다.

기본 검색 UI는 Provider·모델·검색 전략·선택적 기준일·깊이를 사용한다.
깊이는 빠르게 15회/300초, 기본 40회/900초, 심층 80회/1800초이며,
사용자의 전체 상한과 비교해 더 작은 값을 적용한다. 기본 전역 상한이 40회/900초이면
심층을 골라도 그 이상 늘어나지 않는다. 프리셋에 출처별 후보 슬롯은 없다.

Git HEAD 대비 `backend/app/**/*.py`의 비어 있지 않은 줄 수는 32,959 → 23,455줄로
9,504줄 줄었다. 새 MCP 코드도 포함한 수치다. 이는 시작 당시 Claude의 미커밋 변경과
이번 변경이 함께 포함된 비교이며, 전부 이번 작업 단독 감소량이라고 해석하면 안 된다.
편집 가능한 설정은 HEAD의 50개에서 42개로 줄었다.

## 기준일·프롬프트·이전 기록

- `search_cutoff_date`는 선택 필드다. 비어 있으면 오늘 날짜를 넣지 않는다.
- 공개일만 사용한다. 모델이 쓴 날짜만으로 후보를 제외하지 않는다. 보존 근거에서 확인한
  공개일이 기준일 뒤일 때만 최종 목록에서 제외하고 감사 기록·보고서에는 사유와 함께 남긴다.
  공개일 불명·경계에 걸친 부분 날짜는 남긴다.
- 기준일은 프롬프트와 MCP 실행 설정에 전달한다. EPO 도구는 LLM이 선택한 `pd` 날짜 범위
  CQL을 지원한다. 다만 **ARIA가 모든 DB 요청에 날짜 조건을 자동 삽입하지는 않는다.**
  날짜 미상 자료가 DB에서 조용히 제외되는 것을 피하기 위한 것으로, 원안의 자동 조건 전달과는
  차이가 있다. 범위 제한 질의와 제한 없는 확장 질의의 선택은 LLM에 맡긴다.
- 검색은 현재 검색 전략 프롬프트와 ARIA 실행 계약을 조립한다. 분석용 Master Prompt는
  출력 계약이 다르므로 확인 없이 추가하지 않았다. 원안의 두 프롬프트 동시 적용은 미완료다.
- 새 manifest는 v14다. `group`, `evidence_level`, `verification_scope`,
  `verification_issues`를 분리하고 실제 호출, 모델 원출력, 정규화 결과를 별도로 보존한다.
- v13 이하 기록은 `search_legacy.view` 읽기 전용 어댑터로 처리한다. 저장된 원래 보고서는
  건드리지 않는다. 프론트엔드에서는 구버전임을 표시하고 원기록을 열람한다.
  구버전 실행 엔진을 유지하거나 옛 근거를 새 검증 성공으로 승격하지 않는다.

## Provider별 실제 확인 범위

| Provider | 새 ARIA 도구 연결 | 실제 CLI 확인 |
|---|---|---|
| Codex | 실행별 MCP, 도구별 명시 허용 목록 | 설치된 0.149.0에서 capabilities 호출·원장 기록 성공 |
| Claude | strict MCP 설정과 허용 목록 구현 | 로그인되지 않아 실제 호출 검증 미완료 (`loggedIn: false`) |
| agy | 네이티브 웹 검색 유지, ARIA MCP는 `unsupported_transport` | 설치된 1.1.25에서 실행별 MCP 설정 경로 미확인 |

Codex의 실제 확인은 외부 특허/논문 API를 호출하지 않는 연결 점검이다.
EPO·논문의 실서버 검색 품질과 계정별 권한까지 검증했다는 뜻은 아니다.
agy의 전역 MCP 등록은 사용자 설정에 영향을 주므로 이번 작업에서 수행하지 않았다.

Codex의 `-c` 경로는 따옴표를 포함해 분리되므로, ARIA가 소유한 서버/환경 키는
따옴표 없는 경로를 쓰고 값만 인코딩한다. 읽기 전용으로 선언된 ARIA 도구에만
`default_tools_approval_mode="writes"`를 적용하며 전역 승인 우회는 사용하지 않는다.
근거: [설정 경로 처리](https://raw.githubusercontent.com/openai/codex/main/codex-rs/config/src/overrides.rs),
[MCP 설정 타입](https://raw.githubusercontent.com/openai/codex/main/codex-rs/config/src/mcp_types.rs).

중요한 제한: Codex/agy의 네이티브 도구는 ARIA가 모든 호출을 사전에 차단하는 구조가 아니다.
기존 샌드박스와 호출 관측·정책 위반 실패 처리를 유지한다. 네이티브 호출의 전체 한도는
이벤트 관측 후 중단이고, MCP 서버 내부 한도는 외부 요청 전 거절이다.
두 경계를 같은 의미의 완전한 사전 하드캡이라고 주장하지 않는다.

## 테스트 변경과 결과

삭제한 테스트는 폐기된 독립 레인·후보 병합·승격 정책에 대한 테스트다:
`test_epo_agent`, `test_epo_channel_runner`, `test_epo_channel_comparison`,
`test_epo_discovery`, `test_literature_channel`, `test_literature_query`, `test_search_plan`,
그리고 이전 manifest/report/verification/gates/unverified-title/channel-status 전용 파일.
프론트엔드의 구 분류 상태기계 테스트도 제거했다.

대신 `test_single_agent_search.py`와 새 렌더링 테스트에 단일 실행, 순서·그룹 보존,
가짜 인용 차단, 응답 전달 여부와 아티팩트 변조, DOI 구별, N+1 거절/재시작,
CQL 오류·날짜 범위, 실제 질의 보존, 미설정 사유, legacy 불변성, 프로토콜/Provider 설정을 넣었다.
이전 문제 실행의 18개 후보 스냅샷도 순서·그룹·기술적 대응 보존을 검증한다.

`test_search_allowlist`는 유지·수정했고, `test_epo_tool_isolation`은 이름 목록이 비어 있어도
실제 도구 호출을 거절하는 Provider별 테스트로 교체했다.
하위 EPO OPS/CQL/쿼터/아티팩트/provenance/논문 안전 테스트는 유지했다.
API의 취소·실패·사전 점검·입력 경계 테스트 역시 유지했다.

- 백엔드 전체 기본 테스트: 1,004개 통과, 실제 CLI opt-in 테스트 16개 제외 (409.54초).
  이후 마지막 정리·보완이 영향을 주는 테스트 182개를 다시 실행해 통과했다.
  마지막 보완 후 전체를 다시 실행한 것은 아니며, 두 숫자를 합산한 테스트 수로 해석하지 않는다.
- 마지막 날짜/CQL/논문/단일 에이전트 관련 묶음: 171개 통과.
- 마지막 실행 정책/단일 에이전트/검색 API/프롬프트/날짜 묶음: 182개 통과.
  capabilities 호출만으로 검색 성공을 인정하지 않는 검사와 검색 깊이의 API→manifest 전달도 포함한다.
- 프론트엔드: 7개 파일, 77개 테스트 통과. TypeScript 검사와 Vite 빌드 통과.
- 실제 Codex MCP 연결 테스트: 통과. 실제 Claude MCP 연결 테스트: 인증 미설정으로 실패.
- `git diff --check`: 통과. pytest-asyncio의 기본 fixture scope 관련 기존 경고는 남아 있다.

삭제 전 코드·테스트 18개는 임시 백업에 복사했다:
`C:\Users\ADMINI~1\AppData\Local\Temp\aria-retired-search-b2f65aeb0a40432689dfc897dd74baff`.
추적 파일은 Git에서도 복원 가능하다. 실행 이력·사용자 문헌·자격증명은 삭제하지 않았다.

## 후속 확인이 필요한 항목

1. Claude 로그인 후 실제 MCP 연결 및 한 건의 검색을 확인한다.
2. agy에도 EPO/논문을 반드시 제공할지 결정한다. 필요하다면 지원되는 실행별 연결 방식이나
   별도 승인된 전역 등록 방식을 추가 조사해야 한다. Kiwee 실제 접속 계약도 별도 필요하다.
3. 검색에 분석용 Master Prompt까지 결합할지, 검색 전략 하나를 쓰는 현재 분리를 유지할지 확인한다.
4. 자동 DB 날짜 필터가 반드시 필요한 경우, 날짜 미상 문헌 보존 요구와 함께 동작을 확정한다.
5. 서버 재시작 후 사용자 계정·설정으로 실제 검색을 확인한다. 현재 완료 판정은 코드/자동 테스트와
   Codex 연결 점검 범위까지이며, 세 Provider 전체 운영 검증 완료가 아니다.
