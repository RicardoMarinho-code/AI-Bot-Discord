"""Sessão aiohttp compartilhada entre os serviços (Groq STT, Groq LLM).

Criar uma ClientSession por chamada joga fora o pool de conexões e paga
handshake TLS toda vez; uma sessão módulo-level reaproveita as conexões.
Timeout é por request (cada serviço passa o seu).
"""
from __future__ import annotations

import aiohttp

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    """Sessão compartilhada (criada preguiçosamente, dentro do event loop)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close() -> None:
    if _session is not None and not _session.closed:
        await _session.close()
