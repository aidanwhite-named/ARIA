"""보고서 본문의 「대응 정도」 줄에 등급 심볼을 되살린다.

문제는 프롬프트 안에 있었다. 실측(job d39dc2cc)의 Master Prompt 는 등급표를
이렇게 적어 두고,

    - `95~100%: 동일 대응 🔵`
    - `80~94%: 실질 대응 🟢`
    - `1~79%: 부분 대응 🟡`
    - `0%: 대응 없음—확인 범위 기준 ⚪`

형식은 이렇게 지정한다.

    형식: `대응 정도: [등급명] (XX%)`

형식 지정 줄에 심볼이 없다. 모델은 형식 줄을 따랐고 `대응 정도: 실질 대응 (90%)`
를 냈다. 프롬프트를 성실히 따를수록 심볼이 사라지는 셈이라, 모델에게 다시
부탁하는 방식으로는 안정화되지 않는다.

그래서 ARIA 가 붙인다. 다만 **판단은 만들지 않는다.**

  - 등급 구간과 심볼은 그 실행에 쓰인 프롬프트의 등급표에서 읽는다. 코드에
    상수로 적어 두지 않는다 — 사용자가 등급 체계를 바꾸면 코드가 그것과
    어긋난 심볼을 찍게 된다.
  - 백분율은 모델이 쓴 값을 그대로 읽고, 그 값을 바꾸지 않는다. 등급명도
    바꾸지 않는다. 붙이는 것은 심볼 한 글자뿐이다.
  - 프롬프트에 등급표가 없으면 아무것도 하지 않는다.
  - 이미 심볼이 있는 줄은 건드리지 않는다.

본문 하나만 고친다. result_text 가 화면·복사·다운로드의 공통 원본이므로 세
경로가 저절로 같아진다. 별도 요약 패널에만 붙이면 본문과 패널이 어긋난다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 등급표 한 줄. `95~100%: 동일 대응 🔵` / `0%: 대응 없음 ⚪` 두 형태를 모두 읽는다.
# 심볼은 줄 끝의 비-ASCII 기호 한 글자로 잡는다. 뒤에 백틱·공백이 붙어도 된다.
_LEGEND = re.compile(
    r"(?P<low>\d{1,3})\s*(?:~|-|–)\s*(?P<high>\d{1,3})\s*%\s*[:：]\s*"
    r"(?P<label>[^\n`]*?)\s*(?P<symbol>[^\s\w`.,()\[\]{}]+)\s*`?\s*$",
    re.MULTILINE,
)
_LEGEND_SINGLE = re.compile(
    r"(?<![\d~\-–])(?P<value>\d{1,3})\s*%\s*[:：]\s*"
    r"(?P<label>[^\n`]*?)\s*(?P<symbol>[^\s\w`.,()\[\]{}]+)\s*`?\s*$",
    re.MULTILINE,
)

# 본문의 대응 정도 줄. 등급명과 백분율을 나눠 잡는다.
_GRADE_LINE = re.compile(
    r"(?P<head>대응\s*정도\s*[:：]\s*)"
    r"(?P<label>[^\n(（`]*?)"
    r"(?P<gap>\s*)"
    r"(?P<open>[(（])\s*(?P<value>\d{1,3})\s*%"
)


@dataclass(frozen=True)
class GradeSymbol:
    low: int
    high: int
    label: str
    symbol: str

    def contains(self, value: int) -> bool:
        return self.low <= value <= self.high


def parse_legend(prompt: str) -> list[GradeSymbol]:
    """프롬프트의 등급표에서 (구간, 심볼) 을 읽는다.

    읽지 못하면 빈 목록. 그때는 아무것도 붙이지 않는다.
    """
    found: list[GradeSymbol] = []
    seen: set[tuple[int, int]] = set()
    for match in _LEGEND.finditer(prompt or ""):
        low, high = int(match.group("low")), int(match.group("high"))
        if low > high or high > 100:
            continue
        if (low, high) in seen:
            continue
        seen.add((low, high))
        found.append(
            GradeSymbol(low, high, match.group("label").strip(), match.group("symbol"))
        )
    for match in _LEGEND_SINGLE.finditer(prompt or ""):
        value = int(match.group("value"))
        if value > 100 or (value, value) in seen:
            continue
        # 구간 표기(`95~100%`)의 뒷 숫자를 단독 표기로 두 번 읽지 않는다.
        if any(entry.low != entry.high and entry.contains(value) for entry in found):
            continue
        seen.add((value, value))
        found.append(
            GradeSymbol(value, value, match.group("label").strip(), match.group("symbol"))
        )
    return found


def _has_symbol(text: str, symbols: set[str]) -> bool:
    return any(symbol and symbol in text for symbol in symbols)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().strip("`").casefold()


def _mismatch(legend: list[GradeSymbol], label: str, value: int) -> GradeSymbol | None:
    """모델이 쓴 등급명이 등급표에 있는데 그 구간이 점수와 어긋나는가.

    어긋나면 그 등급표 항목을 돌려준다. 이때는 심볼을 붙이지 않는다 — 어느
    쪽이 모델의 진짜 판정인지 코드가 알 수 없기 때문이다. 점수 쪽을 믿고
    심볼을 붙이면 「부분 대응 🟢 (40%)」처럼 등급명과 심볼이 서로 다른 말을
    하는 줄이 나오고, 등급명 쪽을 믿고 붙이면 ARIA 가 점수를 부정하는 표시를
    본문에 넣는 셈이 된다. 둘 다 판정을 만들어 내는 일이라 하지 않는다.
    """
    key = _norm(label)
    if not key:
        return None
    for entry in legend:
        if _norm(entry.label) == key:
            return None if entry.contains(value) else entry
    return None


def apply(report: str, prompt: str) -> str:
    """보고서의 대응 정도 줄에 프롬프트가 정의한 심볼을 붙인다."""
    legend = parse_legend(prompt)
    if not report or not legend:
        return report
    symbols = {entry.symbol for entry in legend}

    def replace(match: re.Match) -> str:
        whole = match.group(0)
        label = match.group("label")
        if _has_symbol(whole, symbols):
            return whole
        value = int(match.group("value"))
        if _mismatch(legend, label, value) is not None:
            return whole
        for entry in legend:
            if entry.contains(value):
                # 등급명 뒤, 백분율 앞. 등급표가 쓰는 자리와 같다.
                gap = match.group("gap") or " "
                return (
                    match.group("head")
                    + label
                    + (" " if label and not label.endswith(" ") else "")
                    + entry.symbol
                    + gap
                    + match.group("open")
                    + str(value)
                    + "%"
                )
        return whole

    return _GRADE_LINE.sub(replace, report)
