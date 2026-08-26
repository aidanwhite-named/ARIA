"""인덱스와 추출기의 버전 식별자.

이 두 값이 인덱스 재사용 여부를 정한다. PDF sha256 이 같아도 여기가 바뀌면
저장된 인덱스를 버리고 다시 만든다. 추출 결과가 달라진 인덱스를 "같은 자료"로
계속 쓰면, 보고서의 페이지·문단 출처가 실제 원문과 어긋나도 아무도 모른다.

값을 올려야 하는 때:
  INDEX_VERSION     스키마, 청킹 규칙, 저장 필드가 바뀌었을 때
  EXTRACTOR_VERSION 추출 방식이나 pypdf 버전이 바뀌었을 때
"""

from __future__ import annotations

import sqlite3
import sys

import pypdf

# 인덱스 스키마와 청킹 규칙의 버전.
INDEX_VERSION = 1

# 추출기 신원. pypdf 버전을 그대로 싣는다 — requirements 를 올리면 자동으로
# 달라지므로, 의존성만 바꾸고 인덱스를 그대로 쓰는 실수가 생기지 않는다.
EXTRACTOR_VERSION = f"pypdf-{pypdf.__version__}+aria-1"


def library_versions() -> dict[str, str]:
    """실행 기록에 남길 라이브러리·런타임 버전.

    "이 근거 패키지를 어떤 도구가 만들었는가"를 나중에 재현하려면 인덱스 버전
    만으로는 부족하다. SQLite 빌드가 다르면 토크나이저 동작이 달라질 수 있다.
    """
    return {
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "pypdf": pypdf.__version__,
        "index_version": str(INDEX_VERSION),
        "extractor_version": EXTRACTOR_VERSION,
    }
