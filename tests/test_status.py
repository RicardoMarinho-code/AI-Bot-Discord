"""O /status: call, voz, memória, lembretes e ping, só para quem pediu."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from cogs import status
from core import lembretes
from services import gemini


@pytest.mark.parametrize(("segundos", "texto"), [
    (20, "menos de 1min"), (90, "1min"), (3725, "1h02min"), (2 * 86400 + 3 * 3600, "2d03h"),
])
def test_ha_quanto(segundos, texto):
    assert status.ha_quanto(segundos) == texto


def test_status_fora_da_call(monkeypatch):
    monkeypatch.setattr(status, "peek_assistente", lambda gid: None)
    monkeypatch.setattr(config, "GEMINI_VOZ", "")
    gemini.esquece(50)

    texto = status.descreve(SimpleNamespace(latency=0.0421), 50)

    assert status.m.STATUS_OUT_OF_CALL in texto
    assert "a padrão do Google" in texto and "42 ms" in texto and "0 troca(s)" in texto


def test_status_na_call_com_voz_memoria_e_lembretes(monkeypatch):
    sessao = MagicMock()
    sessao.conectado.return_value = True
    sessao.voice.channel.mention = "<#99>"
    monkeypatch.setattr(status, "peek_assistente", lambda gid: sessao)
    monkeypatch.setattr(gemini, "_voz_do_servidor", {51: "Charon"})
    gemini.esquece(51)
    gemini._lembra(51, "capital da França?", "Paris.")
    tarefa = MagicMock()
    monkeypatch.setitem(lembretes.pendentes, tarefa, lembretes.Lembrete(51, 7, 5, "pizza", time.time() + 60))

    texto = status.descreve(SimpleNamespace(latency=float("nan")), 51)

    assert "Na call em <#99>" in texto and "Charon (masculina, grave e séria)" in texto
    assert "1 troca(s)" in texto and "aqui: 1" in texto and "Ping: ? ms" in texto
    gemini.esquece(51)


def test_comando_responde_so_para_quem_pediu(monkeypatch):
    monkeypatch.setattr(status, "peek_assistente", lambda gid: None)
    ctx = SimpleNamespace(guild=SimpleNamespace(id=52), respond=AsyncMock())
    cog = status.Status(SimpleNamespace(latency=0.01))

    asyncio.run(status.Status.status.callback(cog, ctx))

    assert ctx.respond.await_args.kwargs == {"ephemeral": True}
