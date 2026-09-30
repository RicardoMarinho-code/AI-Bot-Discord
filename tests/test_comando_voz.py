"""O /voz: troca a voz do bot no servidor, sem reiniciar."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import messages as m
from cogs.voice import _PADRAO, Voice
from services import gemini


def _ctx(guild_id=42):
    return SimpleNamespace(guild=SimpleNamespace(id=guild_id), respond=AsyncMock())


def test_voz_troca_e_confirma():
    cog, ctx = Voice(MagicMock()), _ctx()
    try:
        asyncio.run(Voice.voz.callback(cog, ctx, "Charon"))
        assert gemini.voz_de(42) == "Charon"
        ctx.respond.assert_awaited_once_with(
            m.VOICE_CHANGED.format(voz="Charon", estilo=gemini.VOZES["Charon"]),
        )
    finally:
        gemini.escolhe_voz(42, None)


def test_voz_volta_ao_padrao():
    cog, ctx = Voice(MagicMock()), _ctx()
    gemini.escolhe_voz(42, "Puck")

    asyncio.run(Voice.voz.callback(cog, ctx, _PADRAO))

    assert 42 not in gemini._voz_do_servidor
    ctx.respond.assert_awaited_once_with(m.VOICE_RESET)
