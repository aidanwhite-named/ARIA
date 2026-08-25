"""증거 아티팩트 보존 — 참조 카운트와 수거.

증거는 작업에 딸린 것이지 영구 보관물이 아니다
-----------------------------------------------
재현 가능성 때문에 증거를 남기지만, 그것이 '사용자가 이력을 지워도 특허 원문이
디스크에 남는다'는 뜻이 되면 안 된다. 삭제 의도가 지켜지지 않는 것이고,
개인정보·보안·저장공간 모두 문제가 된다.

작업이 사라지면 그 증거가 재현을 뒷받침할 대상도 사라진다. 그래서 생애주기를
작업에 묶는다.

  작업 생성/검증 시   reference(job_id, artifact_id) 로 참조 기록
  작업 삭제 시        참조가 함께 사라짐 (FK CASCADE)
  수거                아무도 참조하지 않는 아티팩트를 저장소에서 삭제

내용 주소 저장소라 여러 작업이 같은 응답을 공유할 수 있다. 그래서 작업 하나를
지웠다고 바로 지우지 않고, 참조가 0인 것만 지운다.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import EvidenceReference
from .artifacts import ArtifactIdInvalid, ArtifactStore


def reference(session: Session, job_id: str, artifact_id: str) -> None:
    """이 작업이 이 아티팩트를 쓴다고 기록한다. 이미 있으면 아무것도 안 한다.

    아직 flush 되지 않은 것까지 본다. ARIA 의 세션은 autoflush=False 라서,
    DB 만 조회하면 같은 세션에서 방금 add 한 참조를 못 보고 중복을 넣는다.
    그러면 flush 시점에 UNIQUE 제약으로 터진다 — 한 응답의 여러 필드를
    검증하면서 같은 아티팩트를 여러 번 참조하는 것은 정상 흐름이므로,
    호출자가 중복을 피하도록 요구하지 않는다.
    """
    if not job_id or not artifact_id:
        return
    pending = any(
        isinstance(obj, EvidenceReference)
        and obj.job_id == job_id
        and obj.artifact_id == artifact_id
        for obj in session.new
    )
    if pending:
        return
    exists = (
        session.query(EvidenceReference.id)
        .filter(
            EvidenceReference.job_id == job_id,
            EvidenceReference.artifact_id == artifact_id,
        )
        .first()
    )
    if exists is None:
        session.add(EvidenceReference(job_id=job_id, artifact_id=artifact_id))


def referenced_ids(session: Session) -> set[str]:
    """지금 어떤 작업이든 참조하고 있는 아티팩트 id 전부."""
    return {
        row[0]
        for row in session.query(EvidenceReference.artifact_id).distinct().all()
    }


def collect_unreferenced(session: Session, store: ArtifactStore) -> int:
    """아무도 참조하지 않는 아티팩트를 저장소에서 지운다.

    저장소를 훑어 참조 집합에 없는 것을 지운다. 참조 테이블을 기준으로 지우지
    않는 이유는, 참조 행이 CASCADE 로 이미 사라진 뒤에 호출되기 때문이다.
    """
    if not store.root.is_dir():
        return 0
    keep = referenced_ids(session)
    removed = 0
    for shard in store.root.iterdir():
        if not shard.is_dir():
            continue
        for path in shard.iterdir():
            if not path.is_file() or path.name in keep:
                continue
            try:
                store._path(path.name)
            except ArtifactIdInvalid:
                # 저장소 규칙에 맞지 않는 파일은 우리 것이 아니다. 건드리지
                # 않는다 — 만든 적 없는 파일을 지우지 않는다.
                continue
            path.unlink(missing_ok=True)
            removed += 1
        # 빈 shard 디렉터리는 정리한다. 실패해도 무시한다.
        try:
            next(shard.iterdir())
        except StopIteration:
            shard.rmdir()
        except OSError:
            pass
    return removed
