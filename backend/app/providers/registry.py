"""Provider 레지스트리와 probe 캐시."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from .agy_cli import AgyCliProvider
from .base import ProbeResult, Provider
from .claude_cli import ClaudeCliProvider
from .pending import CodexCliProvider

PROVIDER_ORDER = ["agy", "claude", "codex"]

# 기술적으로 동작하지만 ARIA 의 안전 원칙(도구 없는 실행)을 충족하지
# 못하는 Provider. 사용자가 Settings 에서 명시적으로 켜야 실행된다.
#
# agy 에는 도구를 끄는 플래그가 없다. ARIA 는 도구 호출을 탐지해서
# 실패로 기록할 뿐 호출 자체를 막지 못하므로, 기본값은 '꺼짐' 이다.
EXPERIMENTAL_PROVIDERS = frozenset({"agy"})

_cache: dict[str, ProbeResult] = {}
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
    async with _lock:
        if force or not _cache:
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
    _cache.clear()


def apply_optin(
    results: list[ProbeResult], enabled: list[str] | None
) -> list[ProbeResult]:
    """실험적 Provider 의 opt-in 여부를 probe 결과에 반영한다."""
    allowed = set(enabled or [])
    for item in results:
        if item.provider in EXPERIMENTAL_PROVIDERS:
            item.experimental = True
            item.opted_in = item.provider in allowed
    return results


def is_allowed(provider_id: str, enabled: list[str] | None) -> bool:
    """이 Provider 로 실행해도 되는가.

    실험적 Provider 는 사용자가 Settings 에서 켜지 않으면 거부한다.
    """
    if provider_id not in EXPERIMENTAL_PROVIDERS:
        return True
    return provider_id in set(enabled or [])


def to_dict(result: ProbeResult) -> dict:
    data = asdict(result)
    data["usable"] = result.usable
    data["runnable"] = result.runnable
    return data
