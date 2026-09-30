"""Ciclo de vida: sair com a call vazia, ser tirado à força, sair do servidor."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.assistente as assistente_mod
from cogs.lifecycle import Lifecycle


@pytest.fixture
def sessao(monkeypatch):
    """Um assistente registrado para o servidor 1, com a voz conectada."""
    s = MagicMock()
    s.conectado.return_value = True
    s.sai = AsyncMock()
    monkeypatch.setattr(assistente_mod, "_sessoes", {1: s})
    return s


def _cog() -> Lifecycle:
    bot = MagicMock()
    bot.user.id = 999
    return Lifecycle(bot)


def _membro(user_id: int):
    return SimpleNamespace(id=user_id, guild=SimpleNamespace(id=1))


def _estado(canal):
    return SimpleNamespace(channel=canal)


def test_bot_tirado_da_call_limpa_tudo(sessao):
    asyncio.run(_cog().on_voice_state_update(_membro(999), _estado("call"), _estado(None)))
    sessao.sai.assert_awaited_once()


def test_call_vazia_agenda_a_saida(sessao):
    sessao.pessoas.return_value = []
    asyncio.run(_cog().on_voice_state_update(_membro(7), _estado("call"), _estado(None)))
    sessao.schedule_empty_leave.assert_called_once()


def test_alguem_na_call_cancela_a_saida(sessao):
    sessao.pessoas.return_value = ["Ana"]
    asyncio.run(_cog().on_voice_state_update(_membro(7), _estado(None), _estado("call")))
    sessao.cancel_empty_leave.assert_called_once()


def test_servidor_sem_assistente_e_ignorado(monkeypatch):
    monkeypatch.setattr(assistente_mod, "_sessoes", {})
    asyncio.run(_cog().on_voice_state_update(_membro(7), _estado("call"), _estado(None)))


def test_sair_do_servidor_desliga_a_escuta_e_esquece_o_assistente(sessao):
    escuta = sessao.escuta

    asyncio.run(_cog().on_guild_remove(SimpleNamespace(id=1)))

    escuta.stop.assert_called_once()
    sessao.cala.assert_called_once()
    assert assistente_mod.peek_assistente(1) is None
