"""실제 서버를 상대로 하는 종단 확인 스크립트.

기본 실행은 모델을 호출하지 않고 HTTP·프롬프트·업로드 경계만 확인한다.
두 번째 인수로 ``agy`` 를 주면 agy 실제 호출을 1회 수행한다.

    python tests/e2e_smoke.py http://127.0.0.1:8765
    python tests/e2e_smoke.py http://127.0.0.1:8765 agy
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_fixture import build_pdf  # noqa: E402


def main(base: str, live_provider: str | None = None) -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        mark = "PASS" if condition else "FAIL"
        print(f"[{mark}] {label}" + (f" :: {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    headers = {"X-ARIA-Client": "1"}
    with httpx.Client(base_url=base, timeout=300.0, headers=headers) as client:
        health = client.get("/api/health").json()
        check("health", health.get("status") == "ok", json.dumps(health))

        providers = client.get("/api/providers").json()["providers"]
        by_id = {provider["provider"]: provider for provider in providers}
        check("Mock Provider 제거", "mock" not in by_id, str(sorted(by_id)))
        check("agy Provider 감지", "agy" in by_id, str(sorted(by_id)))

        prompt = client.post(
            "/api/prompts",
            json={
                "name": "E2E 테스트 프롬프트",
                "description": "종단 확인용",
                "body": "첨부 자료를 한 문장으로 요약하십시오.",
                "output_mode": "markdown",
                "tags": ["e2e"],
            },
        ).json()
        check("프롬프트 생성", "version" not in prompt, json.dumps(prompt)[:200])

        updated = client.put(
            f"/api/prompts/{prompt['id']}", json={"body": "첨부 자료의 핵심을 요약하십시오."}
        ).json()
        check("프롬프트 본문 수정", updated["body"] == "첨부 자료의 핵심을 요약하십시오.")

        pdf_bytes = build_pdf(
            [
                "First page about turbine blade cooling channels.",
                "Second page describing the manufacturing method.",
            ]
        )
        upload = client.post(
            "/api/uploads",
            files=[("files", ("spec.pdf", pdf_bytes, "application/pdf"))],
            data={"roles": json.dumps(["CITATION"])},
        ).json()
        attachment = upload["files"][0]
        check("PDF 텍스트 추출", attachment["read_ok"] is True, str(attachment))
        check("인용발명 역할 보존", attachment["role"] == "CITATION")

        if live_provider:
            provider = by_id.get(live_provider)
            if provider is None or not provider.get("usable"):
                check(
                    f"{live_provider} 실제 실행 준비",
                    False,
                    "Settings에서 설치와 인증 상태를 확인하십시오.",
                )
            else:
                job = client.post(
                    "/api/jobs",
                    json={
                        "prompt_id": prompt["id"],
                        "provider": live_provider,
                        "claim_text": "청구항 1. 냉각 채널을 포함하는 터빈 블레이드.",
                        "batch_id": upload["batch_id"],
                    },
                ).json()
                final = _wait(client, job["id"])
                check("실제 Provider 실행 성공", final["status"] == "SUCCEEDED", str(final))
                check("결과 텍스트 존재", bool((final.get("result_text") or "").strip()))
        else:
            print("[SKIP] 실제 Provider 호출 — 두 번째 인수로 agy 를 주면 1회 호출합니다.")

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모든 종단 확인 통과")
    return 0


def _wait(client: httpx.Client, job_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return data
        time.sleep(0.2)
    raise TimeoutError(f"작업이 끝나지 않았습니다: {job_id}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    provider_id = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(main(target, provider_id))
