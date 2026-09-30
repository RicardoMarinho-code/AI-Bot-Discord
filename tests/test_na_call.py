"""Quem está na call, para o Gemini: pelos voice_states, sem o bot."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.assistente import Assistente


def _assistente(monkeypatch, voice_states=None):
    guild = MagicMock()
    guild.me = SimpleNamespace(id=100)
    voz = SimpleNamespace(channel=SimpleNamespace(voice_states=voice_states)) if voice_states is not None else None
    monkeypatch.setattr(Assistente, "voice", property(lambda self: voz))
    assistente = Assistente(MagicMock(), guild)
    nomes = {7: "Ricardo", 8: "Pedro"}
    assistente.nome_de = AsyncMock(side_effect=lambda uid: nomes.get(uid, ""))
    return assistente


def test_nomes_na_call_sem_o_bot_e_sem_quem_nao_tem_nome(monkeypatch):
    assistente = _assistente(monkeypatch, {7: None, 100: None, 8: None, 9: None})

    assert asyncio.run(assistente.nomes_na_call()) == ["Ricardo", "Pedro"]


def test_fora_da_call_ninguem(monkeypatch):
    assert asyncio.run(_assistente(monkeypatch).nomes_na_call()) == []
