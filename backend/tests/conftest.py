"""테스트 공통 설정.

app 을 import 하기 전에 데이터 디렉터리를 임시 경로로 돌려서, 테스트가
사용자의 실제 %LOCALAPPDATA%\\ARIA 를 건드리지 않게 한다.
"""

from __future__ import annotations

import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="aria-test-")
os.environ["ARIA_DATA_DIR"] = _TEST_DATA_DIR

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import PATHS  # noqa: E402
from app.db import init_engine  # noqa: E402


@pytest.fixture(scope="session")
def data_dir() -> str:
    return _TEST_DATA_DIR


@pytest.fixture()
def work_dir(tmp_path):
    target = tmp_path / "run"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture(scope="module")
def client():
    from app.main import app

    PATHS.ensure()
    init_engine()
    with TestClient(app) as test_client:
        # CSRF 가드가 변경 요청에 요구하는 헤더.
        test_client.headers.update({"X-ARIA-Client": "1"})
        yield test_client


def wait_for_job(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return data
        time.sleep(0.15)
    raise AssertionError(f"작업이 끝나지 않았습니다: {job_id}")
