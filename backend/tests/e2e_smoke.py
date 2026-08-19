"""실제 서버를 상대로 하는 종단 확인 스크립트.

pytest 와 별개로, 서버를 띄운 상태에서 사람이 직접 돌려보는 용도다.
  python tests/e2e_smoke.py http://127.0.0.1:8799
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_fixture import build_pdf, build_scanned_like_pdf  # noqa: E402


def main(base: str) -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        mark = "PASS" if condition else "FAIL"
        print(f"[{mark}] {label}" + (f" :: {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    headers = {"X-ARIA-Client": "1"}
    with httpx.Client(base_url=base, timeout=120.0, headers=headers) as client:
        health = client.get("/api/health").json()
        check("health", health.get("status") == "ok", json.dumps(health))

        providers = client.get("/api/providers").json()["providers"]
        by_id = {p["provider"]: p for p in providers}
        check("mock provider 사용 가능", by_id["mock"]["usable"] is True)
        check(
            "claude 감지됨(미로그인)",
            by_id["claude"]["installed"] and not by_id["claude"]["usable"],
            str(by_id["claude"]),
        )

        prompt = client.post(
            "/api/prompts",
            json={
                "name": "E2E 테스트 프롬프트",
                "description": "종단 확인용",
                "body": "첨부 자료를 요약하십시오.",
                "output_mode": "markdown",
                "tags": ["e2e"],
            },
        ).json()
        check("프롬프트 생성", prompt.get("version") == 1, json.dumps(prompt)[:200])

        updated = client.put(
            f"/api/prompts/{prompt['id']}", json={"body": "본문을 바꿔서 버전을 올린다."}
        ).json()
        check("본문 수정 시 버전 증가", updated["version"] == 2, str(updated["version"]))

        versions = client.get(f"/api/prompts/{prompt['id']}/versions").json()
        check("버전 이력 2개", len(versions) == 2, str(len(versions)))

        # --- 업로드 -------------------------------------------------------
        pdf_bytes = build_pdf(
            [
                "First page about turbine blade cooling channels.",
                "Second page describing the manufacturing method.",
            ]
        )
        files = [
            ("files", ("note.txt", b"plain text attachment\nsecond line", "text/plain")),
            ("files", ("spec.pdf", pdf_bytes, "application/pdf")),
            ("files", ("scan.pdf", build_scanned_like_pdf(), "application/pdf")),
            ("files", ("evil.exe", b"MZ\x90\x00binary", "application/octet-stream")),
            ("files", ("../escape.txt", b"traversal", "text/plain")),
            ("files", ("CLAUDE.md", b"# injected config", "text/markdown")),
        ]
        upload = client.post("/api/uploads", files=files).json()
        accepted = {f["original_filename"]: f for f in upload["files"]}
        rejected = {r["filename"] for r in upload["rejected"]}

        check("txt 인라인 전달", accepted["note.txt"]["delivery_mode"] == "DELIVERED_AS_INLINE_CONTEXT")
        check("pdf 텍스트 추출", accepted["spec.pdf"]["read_ok"] is True, str(accepted.get("spec.pdf")))
        check("pdf 페이지 수 2", accepted["spec.pdf"]["page_count"] == 2, str(accepted["spec.pdf"]["page_count"]))
        check("스캔 PDF 거부", accepted["scan.pdf"]["read_ok"] is False, str(accepted.get("scan.pdf")))
        check("exe 확장자 차단", "evil.exe" in rejected, str(rejected))
        check("경로 탐색 차단", "../escape.txt" in rejected, str(rejected))
        check("CLAUDE.md 차단", "CLAUDE.md" in rejected, str(rejected))

        # --- 작업 실행 (성공 경로) ------------------------------------------
        required_map = {
            accepted["note.txt"]["attachment_id"]: True,
            accepted["spec.pdf"]["attachment_id"]: True,
            accepted["scan.pdf"]["attachment_id"]: False,
        }
        job = client.post(
            "/api/jobs",
            json={
                "prompt_id": prompt["id"],
                "provider": "mock",
                "user_input": "요약해 주세요.",
                "batch_id": upload["batch_id"],
                "required_map": required_map,
            },
        ).json()
        check("작업 생성", job["status"] in ("QUEUED", "RUNNING"), job.get("status", ""))

        final = _wait(client, job["id"])
        check("실행 성공", final["status"] == "SUCCEEDED", json.dumps(final.get("errors")))
        check(
            "선택 첨부 실패는 경고로",
            final["result_quality"] == "SUCCESS_WITH_WARNINGS",
            str(final.get("result_quality")),
        )
        check("결과 텍스트 존재", bool((final.get("result_text") or "").strip()))
        check("최종 프롬프트 해시 저장", bool(final.get("final_prompt_sha256")))

        prompt_text = client.get(f"/api/jobs/{job['id']}/final-prompt").text
        check("PDF 페이지 경계 포함", "--- PAGE 1 ---" in prompt_text)
        check("첨부 본문 인라인 포함", "turbine blade cooling" in prompt_text)
        check("런타임 컨텍스트가 시스템 프롬프트에 있음", "SYSTEM PROMPT" in prompt_text)

        # --- 실패 경로 -----------------------------------------------------
        for keyword, expected_status, expected_code in [
            ("MOCK_FAIL", "FAILED", "PROCESS_ERROR"),
            ("MOCK_EMPTY", "FAILED", "EMPTY_RESULT"),
            ("MOCK_AUTH", "FAILED", "AUTH_REQUIRED"),
            ("MOCK_RATELIMIT", "FAILED", "RATE_LIMITED"),
        ]:
            j = client.post(
                "/api/jobs",
                json={
                    "prompt_id": prompt["id"],
                    "provider": "mock",
                    "user_input": keyword,
                },
            ).json()
            done = _wait(client, j["id"])
            check(
                f"{keyword} → {expected_status}/{expected_code}",
                done["status"] == expected_status and done["error_code"] == expected_code,
                f"{done['status']}/{done['error_code']}",
            )

        # --- 필수 첨부 실패 -------------------------------------------------
        up2 = client.post(
            "/api/uploads",
            files=[("files", ("scan2.pdf", build_scanned_like_pdf(), "application/pdf"))],
        ).json()
        aid = up2["files"][0]["attachment_id"]
        j = client.post(
            "/api/jobs",
            json={
                "prompt_id": prompt["id"],
                "provider": "mock",
                "batch_id": up2["batch_id"],
                "required_map": {aid: True},
            },
        ).json()
        done = _wait(client, j["id"])
        check(
            "필수 첨부 전달 실패 → FAILED/ATTACHMENT_ERROR",
            done["status"] == "FAILED" and done["error_code"] == "ATTACHMENT_ERROR",
            f"{done['status']}/{done['error_code']}",
        )

        # --- 예산 초과 ------------------------------------------------------
        client.put("/api/settings", json={"values": {"max_inline_chars": 2000}})
        big = client.post(
            "/api/uploads",
            files=[("files", ("big.txt", ("A" * 5000).encode(), "text/plain"))],
        ).json()
        j = client.post(
            "/api/jobs",
            json={
                "prompt_id": prompt["id"],
                "provider": "mock",
                "batch_id": big["batch_id"],
            },
        ).json()
        done = _wait(client, j["id"])
        check(
            "예산 초과 → INPUT_TOO_LARGE",
            done["status"] == "FAILED" and done["error_code"] == "INPUT_TOO_LARGE",
            f"{done['status']}/{done['error_code']}",
        )
        client.put("/api/settings", json={"values": {"max_inline_chars": 300000}})

        # --- 취소 -----------------------------------------------------------
        j = client.post(
            "/api/jobs",
            json={"prompt_id": prompt["id"], "provider": "mock", "user_input": "MOCK_SLOW"},
        ).json()
        _wait_status(client, j["id"], "RUNNING")
        cancel = client.post(f"/api/jobs/{j['id']}/cancel").json()
        check("취소 요청 수락", cancel.get("cancelled") is True, str(cancel))
        done = _wait(client, j["id"])
        check("취소 상태 반영", done["status"] == "CANCELLED", done["status"])

        history = client.get("/api/history").json()
        check("이력 기록됨", len(history) >= 8, str(len(history)))

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모든 종단 확인 통과")
    return 0


def _wait(client: httpx.Client, job_id: str, timeout: float = 90.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return data
        time.sleep(0.2)
    raise TimeoutError(f"작업이 끝나지 않았습니다: {job_id}")


def _wait_status(client: httpx.Client, job_id: str, status: str, timeout: float = 30.0) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get(f"/api/jobs/{job_id}").json()["status"] == status:
            return
        time.sleep(0.1)
    raise TimeoutError(f"{status} 상태에 도달하지 못했습니다: {job_id}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"))
