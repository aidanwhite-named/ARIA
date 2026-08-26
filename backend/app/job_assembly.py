"""작업 하나의 최종 프롬프트 조립.

runner 와 preflight 가 **같은 함수**를 부른다.

두 곳이 각자 조립하면 준비 화면이 안내한 크기와 실제로 나가는 크기가 어긋나고,
그 어긋남은 실행이 실패한 뒤에야 드러난다. 2026-08-25 실행이 그랬다 — 화면은
「허용 800,000자」라고 안내했는데 agy 는 210,743 바이트에서 막았다. 문자수와
바이트가 다른 축인 데다, 화면이 세던 것은 조립 전 원본이었고 실제로 나간 것은
런타임 컨텍스트·경계 표시·명세서 절이 모두 붙은 최종 본문이었다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import search_manifest, search_prompt
from .config import (
    AGY_SEARCH_RUNTIME_CONTEXT,
    CODEX_SEARCH_RUNTIME_CONTEXT,
    SEARCH_RUNTIME_CONTEXT,
)
from .enums import AttachmentRole, JobKind
from .ingestion.service import IngestedFile, read_normalized
from .prompt_assembly import AssembledPrompt, assemble, assemble_search
from .prompt_assembly import included_attachments as prompt_assembly_included

# 검색 실행의 런타임 규칙은 Provider 가 실제로 가진 도구에 맞춰야 한다.
# 도구 이름이 다를 뿐 아니라, Codex 는 페이지를 여는 도구 자체가 없고 agy 는
# 가져온 페이지를 파일로만 돌려준다. 정책 이름으로 고르므로 Provider 가 늘어도
# 이 표만 채우면 된다.
SEARCH_CONTEXT_BY_POLICY = {
    "agy_web_search": AGY_SEARCH_RUNTIME_CONTEXT,
    "codex_web_search": CODEX_SEARCH_RUNTIME_CONTEXT,
}

# 분석 경로에는 레인이 없다. 하나뿐인 조립본을 담는 이름.
LANE_SINGLE = "single"

# 체크된 자료가 하나도 없을 때 세 경로가 함께 쓰는 문구. 화면 안내(preflight),
# 작업 생성 거절(API), 실행 실패(runner)가 같은 말을 해야 한다.
NO_INCLUDED_MATERIAL = (
    "분석에 포함할 인용발명 문헌이 하나도 없습니다. 「분석에 포함」을 체크한 "
    "PDF 가 최소 1건 있어야 구성대비 분석을 실행할 수 있습니다."
)


# 이 실행의 분석 자료를 고르는 단 하나의 계약. 정의는 prompt_assembly 에 있고
# (조립 마지막 층에서도 같은 함수를 쓴다) 여기서는 이름만 다시 내보낸다.
# preflight 와 runner 는 job_assembly 를 통해 부른다.
included_attachments = prompt_assembly_included


class SpecUnreadable(Exception):
    """출원발명 문서를 넣었는데 본문을 읽지 못했다.

    그냥 지나치면 사용자는 명세서를 반영한 검색을 받았다고 믿게 된다.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        super().__init__(filename)


@dataclass
class AssemblyResult:
    """조립 결과. 레인이 하나든 둘이든 같은 모양으로 돌려준다."""

    lanes: dict[str, AssembledPrompt]
    spec_document: dict | None = None
    search_prompt_sha: str = ""
    claim_boundary_neutralized: bool = False
    spec_boundary_neutralized: bool = False
    focus_boundary_neutralized: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def representative(self) -> AssembledPrompt:
        """저장 메타데이터와 공통 코드가 쓰는 대표 조립본.

        명세서가 있으면 전체 입력을 기록하는 보조 조립본을 쓴다.
        """
        return (
            self.lanes.get(search_manifest.ORIGIN_SPEC_ASSISTED)
            or self.lanes.get(search_manifest.ORIGIN_CLAIM_ONLY)
            or self.lanes[LANE_SINGLE]
        )

    def lane_bytes(self) -> dict[str, int]:
        """레인마다 Provider 에게 실제로 나갈 UTF-8 바이트 수.

        Provider 의 바이트 한도는 레인마다 따로 걸린다. 두 레인을 합쳐서 재면
        각각은 통과하는 입력이 초과로 잡힌다.
        """
        return {
            name: len(lane.system_prompt.encode("utf-8"))
            + len(lane.user_message.encode("utf-8"))
            for name, lane in self.lanes.items()
        }


def search_spec(attachments: list[IngestedFile]) -> IngestedFile | None:
    """검색 실행에 넣은 출원발명 문서. 없으면 None.

    검색 작업의 첨부는 이것 하나뿐이다. 여러 건이 들어오는 경우는 작업 생성
    단계에서 이미 거절된다.
    """
    for item in attachments:
        if item.role == AttachmentRole.APPLICATION:
            return item
    return None


def assemble_job(
    *,
    job_kind: JobKind,
    master_prompt: str,
    attachments: list[IngestedFile],
    runtime_context: str,
    runtime_context_enabled: bool,
    # None(또는 0) = ARIA 자체 글자 수 한도 없음. 그래도 Provider 전송 한도와
    # 모델 컨텍스트 한도는 남는다 — 그 검사는 조립 뒤에 바이트로 이뤄진다.
    max_chars: int | None,
    claim_text: str = "",
    focus_text: str = "",
    followup_instruction: str = "",
    prior_claim_text: str = "",
    prior_report: str = "",
    prior_citation_mapping: dict | None = None,
    tool_policy_name: str = "",
) -> AssemblyResult:
    """이 작업이 Provider 에게 실제로 보낼 본문을 만든다.

    검색이면 청구항 단독 / 명세서 보조 두 레인을, 분석이면 하나를 돌려준다.
    InputTooLarge 와 SearchPromptError 는 그대로 올린다 — 호출부가 실행 실패로
    기록할지(runner) 화면에 안내할지(preflight) 정한다.

    호출부가 이미 걸렀더라도 여기서 한 번 더 included_attachments 를 통과시킨다.
    "제외한 자료는 프롬프트에 한 글자도 들어가지 않는다"는 불변조건을 조립
    바로 앞에서 지키기 위해서다.
    """
    attachments = included_attachments(attachments)
    if job_kind is not JobKind.SIMILARITY_SEARCH:
        return AssemblyResult(
            lanes={
                LANE_SINGLE: assemble(
                    master_prompt=master_prompt,
                    attachments=attachments,
                    runtime_context=runtime_context,
                    runtime_context_enabled=runtime_context_enabled,
                    max_chars=max_chars,
                    claim_text=claim_text,
                    followup_instruction=followup_instruction,
                    prior_claim_text=prior_claim_text,
                    prior_report=prior_report,
                    prior_citation_mapping=prior_citation_mapping,
                )
            }
        )

    # 검색 프롬프트는 실행 시점에 파일에서 다시 읽지 않는다. 작업 생성 시
    # 스냅샷한 본문으로 돈다 — 큐에서 기다리는 동안 파일이 바뀌어도 이 실행의
    # 계약은 흔들리지 않아야 한다. 해시는 그 스냅샷에 대해 계산한다.
    spec = search_spec(attachments)
    spec_text = read_normalized(spec) if spec is not None else ""
    if spec is not None and not spec_text.strip():
        raise SpecUnreadable(spec.original_filename)

    # 가장 중요한 불변조건: 기본 검색 프롬프트에는 명세서 본문이 단 한 글자도
    # 들어가지 않는다. 같은 호출 안에서 "먼저 청구항만 보라"고 부탁하는 대신
    # 컨텍스트 자체를 격리한다.
    claim_rendered = search_prompt.render(master_prompt, claim_text, "", focus_text)
    search_context = SEARCH_CONTEXT_BY_POLICY.get(
        tool_policy_name, SEARCH_RUNTIME_CONTEXT
    )
    lanes: dict[str, AssembledPrompt] = {
        search_manifest.ORIGIN_CLAIM_ONLY: assemble_search(
            search_prompt_body=claim_rendered.body,
            runtime_context=search_context,
            max_chars=max_chars,
            # 파일 신원조차 모델 컨텍스트에는 들어가지 않지만, 단독 실행의
            # 조립 기록도 입력 파일과 분리해 둔다.
            attachments=[],
        )
    }

    spec_document: dict | None = None
    spec_boundary_neutralized = False
    if spec is not None:
        spec_document = {
            "attachment_id": spec.attachment_id,
            "filename": spec.original_filename,
            "sha256": spec.sha256,
            "page_count": spec.page_count,
            "char_count": len(spec_text),
        }
        assisted_rendered = search_prompt.render(
            master_prompt, claim_text, spec_text, focus_text
        )
        spec_boundary_neutralized = assisted_rendered.spec_boundary_neutralized
        lanes[search_manifest.ORIGIN_SPEC_ASSISTED] = assemble_search(
            search_prompt_body=assisted_rendered.body,
            runtime_context=search_context,
            max_chars=max_chars,
            attachments=attachments,
        )

    return AssemblyResult(
        lanes=lanes,
        spec_document=spec_document,
        search_prompt_sha=search_prompt.sha256(master_prompt),
        claim_boundary_neutralized=claim_rendered.claim_boundary_neutralized,
        spec_boundary_neutralized=spec_boundary_neutralized,
        focus_boundary_neutralized=claim_rendered.focus_boundary_neutralized,
    )
