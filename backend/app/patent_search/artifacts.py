"""증거 아티팩트 저장소 (불변, 내용 주소).

검증기가 "이 발췌는 원문에 있다"고 말하려면 원본 바이트가 남아 있어야 한다.
그 바이트를 보관하는 곳이다.

내용 주소를 쓰는 이유
---------------------
artifact_id 를 바이트의 SHA-256 으로 정한다. 그러면 해시를 따로 들고 다니며
비교할 필요가 없고, 저장된 파일이 id 로 다시 해시되지 않으면 그 자체가 변조·
손상의 증거가 된다. '해시 필드가 있다'는 것과 '해시가 맞다'는 것은 다르다 —
후자만 검증이다.

PATHS.artifacts_dir 를 쓰지 않는 이유
-------------------------------------
그 디렉터리는 이력 삭제 시 통째로 비워진다(api/history.py). 증거를 거기 두면
사용자가 이력을 지우는 순간 과거 검증이 조용히 무효가 된다. 증거는 생애주기가
다르므로 별도 디렉터리(evidence)를 쓴다.

불변성
------
같은 내용은 같은 id 를 가지므로 덮어쓰기가 의미상 무해하다. 그래도 이미 있는
파일은 다시 쓰지 않는다 — 쓰지 않으면 깨뜨릴 수도 없다.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .base import PatentSearchError

# 아티팩트 id 는 SHA-256 hex 다. 다른 문자열을 경로로 쓰면 저장소 밖을
# 가리킬 수 있다(../.. 등). 해시 검사가 뒤에서 걸러 주긴 하지만, 애초에
# 임의 경로를 열지 않는 편이 낫다.
_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactError(PatentSearchError):
    """아티팩트를 읽을 수 없거나 무결성 검사에 실패했다."""


class ArtifactMissing(ArtifactError):
    """그 id 의 아티팩트가 저장소에 없다."""


class ArtifactCorrupted(ArtifactError):
    """저장된 바이트가 id(SHA-256)와 일치하지 않는다. 변조 또는 손상."""


class ArtifactIdInvalid(ArtifactError):
    """아티팩트 id 형식이 SHA-256 hex 가 아니다."""


def compute_id(data: bytes) -> str:
    """아티팩트 id = 내용의 SHA-256 (hex)."""
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    """파일시스템 기반 불변 저장소."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, artifact_id: str) -> Path:
        if not _ID_PATTERN.match(artifact_id or ""):
            raise ArtifactIdInvalid(
                f"아티팩트 id 형식이 올바르지 않습니다: {artifact_id!r}"
            )
        # 한 디렉터리에 파일이 수만 개 쌓이지 않도록 앞 2자로 나눈다.
        return self.root / artifact_id[:2] / artifact_id

    def put(self, data: bytes) -> str:
        """바이트를 보존하고 id 를 돌려준다. 이미 있으면 그대로 둔다."""
        artifact_id = compute_id(data)
        path = self._path(artifact_id)
        if path.exists():
            return artifact_id
        path.parent.mkdir(parents=True, exist_ok=True)
        # 임시 파일에 쓰고 원자적으로 옮긴다. 중간에 끊겨도 반쪽 파일이
        # 정상 id 로 남지 않는다.
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return artifact_id

    def read(self, artifact_id: str) -> bytes:
        """바이트를 돌려주기 전에 무결성을 검사한다.

        읽기 경로에서 항상 검사한다. '저장할 때 맞았으니 괜찮다'는 가정은
        디스크 손상·수동 편집·복원 실수를 놓친다.
        """
        path = self._path(artifact_id)
        if not path.is_file():
            raise ArtifactMissing(f"아티팩트를 찾을 수 없습니다: {artifact_id}")
        data = path.read_bytes()
        actual = compute_id(data)
        if actual != artifact_id:
            raise ArtifactCorrupted(
                f"아티팩트 내용이 id 와 일치하지 않습니다: {artifact_id} "
                f"(실제 {actual})"
            )
        return data

    def exists(self, artifact_id: str) -> bool:
        try:
            return self._path(artifact_id).is_file()
        except ArtifactIdInvalid:
            return False
