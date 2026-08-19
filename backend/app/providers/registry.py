"""Provider 레지스트리와 probe 캐시."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from .agy_cli import AgyCliProvider
from .base import ProbeResult, Provider
from .claude_cli import ClaudeCliProvider
from .pending import CodexCliProvider

PROVIDER_ORDER = ["agy", "claude", "codex"]

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


def to_dict(result: ProbeResult) -> dict:
    data = asdict(result)
    data["usable"] = result.usable
    data["runnable"] = result.runnable
    return data
