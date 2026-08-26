# ADR 0001 — 대용량 인용발명 PDF의 로컬 Agentic Retrieval

- 상태: 채택 (2026-08-26)
- 적용 범위: `backend/app/retrieval/*`, 구성대비 분석(`job_kind=patent_analysis`) 실행 경로

## 문제

구성대비 분석은 지금까지 인용발명 PDF의 **정규화 텍스트 전체**를 최종 프롬프트에
인라인으로 넣는다(`prompt_assembly.assemble`). Windows 명령행 길이 문제가 아니라
구조가 그렇다. 그래서 긴 한글 명세서 몇 건이면 다음 두 한도 중 하나에 먼저 걸린다.

1. `Provider.max_input_bytes` — agy 는 180,000 bytes. 한글 1자 = UTF-8 3 bytes 이므로
   대략 6만 자에서 막힌다.
2. 모델 컨텍스트 한도.

ARIA 는 문서를 조용히 자르거나 요약하지 않는다는 원칙 때문에 이 상황에서 할 수 있는
일이 `INPUT_TOO_LARGE` 로 거절하는 것뿐이었다.

## 결정

전체 본문을 넣는 대신, **ARIA 가 로컬에서 페이지·문단 단위로 색인**하고, AI 가
구조화된 검색 action 만 돌려주는 **backend-orchestrated agent loop** 를 돌린 뒤,
검증된 **근거 패키지(evidence bundle)** 만 최종 분석 프롬프트에 넣는다.

역할 분담은 코드로 강제한다.

| 하는 일 | 주체 |
|---|---|
| 청구항 구성 분해, 검색어 생성·확장 | AI |
| 추가로 읽을 페이지 결정 | AI |
| 인덱스 조회, 페이지 반환 | ARIA |
| 후보 구간의 기술적 관련성 판단 | AI |
| 페이지 수·추출 상태·검색 이력·출처 검증 | ARIA |
| 최종 구성대비 판단과 보고서 | AI |

AI 는 OS 셸이나 파일 도구를 받지 않는다. 모든 LLM 호출은 기존 Provider 추상화의
`NO_TOOLS` 정책으로 나간다.

## 검토한 라이브러리와 채택 여부

### 1. PDF 추출 — **pypdf 5.1.0 유지 (업그레이드 안 함)**

- 라이선스: BSD-3-Clause. 순수 파이썬, Windows 무관, 오프라인 동작.
- 페이지별 품질 확인에 `extract_text(extraction_mode="layout")` 를 함께 쓴다.
  이 인자는 pypdf 4.0.0 에서 들어왔고 설치본 5.1.0 의 시그니처에서 실측 확인했다
  (`Literal['plain','layout']`). 두 방식의 결과 길이가 크게 어긋나는 페이지는
  `extraction_divergence` 경고로 기록한다.
- **업그레이드하지 않는 이유**: 정규화 텍스트는 후속 분석에서 재추출하지 않고
  그대로 복제된다(README 「첨부는 참조하지 않고 복제한다」). 추출기 버전이 바뀌면
  같은 PDF의 정규화 텍스트가 달라져 "같은 자료"라는 전제가 깨지고, 이미 저장된
  실행 기록의 `final_prompt_sha256` 재현성도 흔들린다. 버전을 올릴 이유가 생기면
  `retrieval.versions.EXTRACTOR_VERSION` 을 함께 올려 인덱스를 강제 재생성해야 한다.
- **PyMuPDF / PyMuPDF4LLM: 거부.** AGPL-3.0 또는 상용 라이선스다. 저장소에 라이선스
  파일조차 없는 상태에서 AGPL 의존성을 넣으면 배포 조건이 바뀐다. 별도 승인 사안이다.

### 2. 키워드 검색 — **표준 라이브러리 `sqlite3` 의 FTS5 채택**

- 추가 의존성 0. 실행 환경에서 실측했다: Python 3.11.8 / SQLite 3.43.1,
  `ENABLE_FTS5=1`, `tokenize='unicode61'` 과 `tokenize='trigram'` 모두 생성 성공.
- 런타임에 능력을 다시 확인한다(`retrieval.index.probe_sqlite`). FTS5 가 없으면
  명확히 실패하고, trigram 만 없으면 부분문자 채널을 끄고 그 사실을 manifest 와
  보고서에 남긴다(조용한 축소 없음).
- **Elasticsearch / Qdrant / Chroma: 거부.** 별도 서버 프로세스, 오프라인 설치 부담,
  실행별 격리 폴더/삭제 정책과 맞지 않는다. 문서 규모(수백 페이지 × 수 건)에서
  필요성이 증명되지 않는다.

### 3. 의미 검색 — **어댑터만 구현, 기본 비활성 (`requirements.txt` 에 넣지 않음)**

- `sentence-transformers` 는 `torch` 를 끌고 온다. 휠 크기가 수 GB 이고 모델은
  최초 실행에 네트워크 다운로드가 필요하다. 두 조건 모두 "오프라인에서도 동작"과
  "설치 크기 확인" 요구를 정면으로 어긴다. 사용자 승인 없이 기본 의존성에 넣지 않는다.
- 대신 `backend/requirements-semantic.txt` 로 **opt-in** 분리하고, 설정
  `retrieval_semantic_enabled`(기본 `false`)로만 켠다. 켜져 있어도 import 나 모델
  로딩이 실패하면 키워드 채널만으로 계속 진행하고, 비활성 사유를
  `retrieval_manifest.semantic` 과 근거 패키지, 보고서에 남긴다.
- 모델 이름과 revision 을 코드에 고정한다(`retrieval.semantic.MODEL_NAME/REVISION`).
  한국어·다국어 특허 문언 때문에 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
  를 기본값으로 두고, 작은 평가 fixture(`tests/test_retrieval_semantic.py`)가 모델
  없이도 통과하도록 어댑터 경계에서 검증한다.
- **별도 Vector DB: 거부.** 문서 하나가 수천 청크 규모이므로 순수 파이썬 코사인
  정렬로 충분하다. 필요성이 증명되지 않은 인프라를 넣지 않는다.

### 4. RAG 프레임워크 — **LlamaIndex / Haystack 모두 거부**

- ARIA 에는 이미 Provider 추상화, Job Runner, Event Bus, History, Prompt Store 가
  있다. 두 프레임워크의 가치는 대부분 그 층에 있고, 도입하면 기존 층을 대체하게
  된다. 그건 요구사항이 명시적으로 금지한다.
- 두 프레임워크의 retriever/node 모델에는 ARIA 가 필요로 하는 **PDF 페이지 번호 ·
  특허 문단번호 · 추출 상태 · 검색 채널 출처**가 1급 필드로 없다. metadata dict 에
  실어도 감사 기록의 강제성이 사라진다.
- `llama-index-core` 만 골라도 의존성 트리가 크고(aiohttp, tiktoken, nltk 계열),
  기본 흐름이 OpenAI 임베딩/LLM 클라이언트를 전제한다. API Key 를 쓰지 않는다는
  ARIA 원칙과 충돌한다.
- 실제로 없던 것은 (a) 페이지 색인 (b) 채널 융합 두 가지뿐이고, 표준 라이브러리로
  약 1,000 줄이다. 프레임워크를 새로 발명한 것이 아니라 **SQLite FTS5 + RRF** 라는
  검증된 구성을 쓴다.

### 5. 결과 병합 — **Reciprocal Rank Fusion (RRF)**

- 채널마다 점수 척도가 다르다(BM25 는 음수, trigram 은 일치 개수). 정규화보다 순위
  기반 융합이 척도에 둔감하고 검증 가능하다. `score = Σ 1/(k + rank)`, `k=60`.
- 전역 top-k 를 쓰지 않는다. **청구항 구성 × 인용문헌** 단위로 최소 후보 수를
  보장해서 한 문헌이 결과를 독점하지 못하게 한다.

### 6. OCR — **이번 단계에서 제외**

- OCR 엔진, Tesseract, OCRmyPDF, 이미지 OCR, 외부 OCR API 를 추가하지 않는다.
  텍스트가 없는 페이지는 `empty_or_low_text` / `visual_review_required` 로 기록하고,
  그 페이지가 있으면 "문헌에 없음" 판정을 코드로 차단한다. OCR 버튼이나 OCR 을 한
  것처럼 보이는 UI 도 만들지 않는다.

## 결과

- 새 런타임 의존성 **0개**. `requirements.txt` 는 그대로다.
- 180,000 bytes 를 넘는 인용문헌도 retrieval 모드에서는 수만 자 규모의 근거 패키지로
  조립되어 agy 에도 전달된다.
- 실행별로 `extraction_report.json`, `retrieval_manifest.json`, `retrieval_trace.jsonl`,
  `evidence_bundle.json`, 인덱스 재현 정보가 남는다.

## 안전장치 (2026-08-26 외부 리뷰 후 보강)

1차 구현에는 "골격은 맞지만 잘못된 대응 판정을 막지 못한다"는 결함이 있었다.
아래 여섯 가지를 코드로 강제하고 각각에 회귀 테스트를 붙였다.

| 결함 | 지금 |
|---|---|
| 출원발명·기타 첨부가 인용발명 검색 대상에 섞임 | `enums.is_local_search_target` 하나가 색인 여부와 본문 전달 여부를 함께 정한다. 출원발명은 색인하지 않고 본문은 전부 인라인. |
| 검색 corpus 의 별칭이 최종 프롬프트와 어긋남 | 정렬(`ordered_attachments`)과 별칭 부여(`assign_aliases`)를 `citation_mapping` 한 모듈에 두고, 양쪽이 같은 함수를 부른다. |
| 인용문헌 일부 색인 실패해도 분석 성공 | 하나라도 실패하면 `RETRIEVAL_UNAVAILABLE` 로 중단. |
| 「미발견」이 모든 문헌 검색 여부를 확인하지 않음 | `ComponentState.searched` 에 문헌별 검색 기록(0건 검색 포함). 문헌마다 확장 검색을 확인한다. |
| AI 가 보지 않은 청크를 근거로 승인받음 | `RetrievalRun.exposed_chunks` 에 실제로 반환한 청크만 담고, 그 밖의 지목은 거절. |
| 빈 구성·불완전한 finalize 승인 | 구성 0개면 `RETRIEVAL_FAILED`. finalize 는 선언된 구성을 정확히 한 번씩 덮어야 한다. |
| 근거 예산이 상한이 아님 | 더하기 **전에** 검사한다. `evidence_chars <= max_evidence_chars` 가 불변조건. |

### 2차 보강 — 컨텍스트 크기 보장

1차 보강의 "더하기 전에 검사"만으로는 부족했다. 근거 구간은 막았지만 서지
발췌·구성 메타데이터·문헌별 검색 기록은 예산과 무관하게 늘어나서, 구성 20개 ×
긴 문구 조합에서 예산 5,000자에 렌더링 31,395자가 나왔다(실측).

| 결함 | 지금 |
|---|---|
| 렌더링 전체가 예산을 넘음 | `evidence.fit()` 이 **완성된 문자열을 직접 재고** 서지 발췌 → 메타데이터 축약 → 근거 구간 순으로 줄인다. 원문은 자르지 않고, 못 맞추면 `RETRIEVAL_FAILED` + 필요 문자 수 안내. |
| preflight 자리표가 렌더링 구조를 반영 못 함 | 예산의 뜻이 "렌더링 전체"가 되면서 자리표는 그냥 `max_evidence_chars` 개의 한글 문자다. `fit()` 이 문자·바이트 양쪽을 강제하므로 참된 상한. |
| action 하나가 라운드 예산을 통째로 넘김 | 남은 예산을 `_search`/`_read` 에 넘겨 **반환되는 청크 단위**로 자른다. 반환하지 않은 청크는 `exposed_chunks` 에 넣지 않는다. |
| 예산 때문에 뺀 후보가 「없음」으로 읽힘 | 뺀 개수를 AI 에게 알리고 `budget_exhausted` 로 올려 not_found 를 막는다. |
| 안내 문구가 실제 동작과 다름 | 검색 대상 수와 전체 인라인 자료 수를 나눠서 안내한다. 근거 패키지의 문헌 목록에 자료 구분(인용발명/기타/출원발명)을 함께 적는다. |

## 남는 한계

- 텍스트 레이어가 없는 페이지의 내용은 이번 단계에서 확인할 수 없다. 해당 구성은
  `visual_review_required` 로 표시되고 사람이 원본 PDF 를 봐야 한다.
- 검색어를 만드는 것은 AI 이므로, AI 가 떠올리지 못한 표현으로만 기재된 구성은
  검토 범위에 들어오지 않는다. 그래서 결과 문구를 "문헌에 없음"이 아니라
  "설정된 검색어와 추출 텍스트의 검토 범위에서는 대응 구성을 확인하지 못함"으로
  제한한다.
