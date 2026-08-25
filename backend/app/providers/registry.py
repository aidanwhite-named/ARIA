"""Provider 레지스트리와 probe 캐시."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

from .agy_cli import AgyCliProvider
from .base import ProbeResult, Provider
from .claude_cli import ClaudeCliProvider
from .codex_cli import CodexCliProvider

PROVIDER_ORDER = ["agy", "claude", "codex"]

# 도구를 끄는 수단이 없는 Provider. ARIA 는 도구 호출을 탐지해서 실패로
# 기록할 뿐 호출 자체를 막지 못한다.
#
# 이 목록은 실행을 막지 않는다. Settings 의 위험 고지와 경고 문구를 어디에
# 붙일지 정하는 데만 쓴다. 각 Provider 는 probe 에서 스스로 experimental=True
# 를 단다 — 여기는 화면 문구용 단일 출처다.
#
# Codex 는 설정으로 web_search 만 끄고 켤 수 있고 셸·파일 도구는 끄지 못한다.
# 그 하나를 끌 수 있다는 이유로 등급을 올리지 않는다 — 남는 도구가 더 위험하다.
TOOL_UNCONTROLLABLE_PROVIDERS = frozenset({"agy", "codex"})

# 캐시 수명. probe 는 Provider 하나당 CLI 를 두 번(버전·인증) 띄우므로 화면을
# 열 때마다 돌릴 수는 없다. 그렇다고 무기한 들고 있으면 사용자가 ARIA 밖에서
# 로그아웃하거나 토큰이 만료됐을 때 화면이 계속 "로그인됨" 으로 거짓말한다.
# 실측으로 그 사고가 났다: 터미널에서 agy /logout 을 마쳤는데도 `agy models` 가
# 실패하는 동안 Settings 표는 "로그인됨. 사용 가능한 모델 14개" 를 계속 보여줬다.
_CACHE_TTL_SECONDS = 60.0

_cache: dict[str, ProbeResult] = {}
_cached_at: float = 0.0
_lock = asyncio.Lock()


def build_provider(provider_id: str, overrides: dict[str, str] | None = None) -> Provider | None:
    overrides = overrides or {}
    path = overrides.get(provider_id) or None
    if provider_id == "claude":
        return ClaudeCliProvider(path)
    if provider_id == "codex":
        return CodexCliProvider(path)
    if provider_id == "agy":
        return AgyCliProvider(path)
    return None


def all_providers(overrides: dict[str, str] | None = None) -> list[Provider]:
    providers = []
    for pid in PROVIDER_ORDER:
        provider = build_provider(pid, overrides)
        if provider is not None:
            providers.append(provider)
    return providers


async def probe_all(
    overrides: dict[str, str] | None = None, force: bool = False
) -> list[ProbeResult]:
    global _cached_at
    async with _lock:
        stale = (time.monotonic() - _cached_at) >= _CACHE_TTL_SECONDS
        if force or not _cache or stale:
            results = await asyncio.gather(
                *(p.probe() for p in all_providers(overrides)), return_exceptions=True
            )
            collected: list[ProbeResult] = []
            for provider, result in zip(all_providers(overrides), results, strict=False):
                if isinstance(result, BaseException):
                    collected.append(
                        ProbeResult(
                            provider=provider.id,
                            display_name=provider.display_name,
                            install_hint=provider.install_hint,
                            notes=[f"probe 중 오류: {type(result).__name__}: {result}"],
                        )
                    )
                else:
                    collected.append(result)
            _cache.clear()
            for item in collected:
                _cache[item.provider] = item
            _cached_at = time.monotonic()
        return [_cache[pid] for pid in PROVIDER_ORDER if pid in _cache]


async def probe_one(
    provider_id: str, overrides: dict[str, str] | None = None
) -> ProbeResult | None:
    provider = build_provider(provider_id, overrides)
    if provider is None:
        return None
    result = await provider.probe()
    async with _lock:
        _cache[provider_id] = result
    return result


def cached(provider_id: str) -> ProbeResult | None:
    return _cache.get(provider_id)


def invalidate() -> None:
    global _cached_at
    _cache.clear()
    _cached_at = 0.0


def to_dict(result: ProbeResult) -> dict:
    data = asdict(result)
    data["usable"] = result.usable
    data["runnable"] = result.runnable
    return data
