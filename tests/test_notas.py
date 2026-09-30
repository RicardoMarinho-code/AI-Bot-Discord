"""Anotações por voz ("Jarvis, anota: ...") e o /notas."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import messages as m
from cogs.notas import Notas
from core import notas
from services import gemini


def test_anota_le_e_apaga_so_as_de_quem_pediu():
    assert notas.anota(1, 7, "  comprar   pão ") == 1
    assert notas.anota(1, 7, "ração do gato") == 2
    notas.anota(1, 8, "do outro")
    notas.anota(2, 7, "outro servidor")

    assert notas.de(1, 7) == ["comprar pão", "ração do gato"]
    assert notas.apaga(1, 7) == 2
    assert notas.de(1, 7) == [] and notas.de(1, 8) == ["do outro"] and notas.de(2, 7) == ["outro servidor"]


def test_notas_sobrevivem_a_um_reinicio(monkeypatch):
    notas.anota(1, 7, "comprar pão")
    monkeypatch.setattr(notas, "_notas", None)  # o bot reiniciou

    assert notas.de(1, 7) == ["comprar pão"]


def test_nota_vazia_ou_bloco_cheio_nao_entram(monkeypatch):
    with pytest.raises(ValueError):
        notas.anota(1, 7, "   ")
    monkeypatch.setattr(notas, "MAX_NOTAS", 2)
    notas.anota(1, 7, "a")
    notas.anota(1, 7, "b")
    with pytest.raises(ValueError):
        notas.anota(1, 7, "c")
    assert notas.de(1, 7) == ["a", "b"]


def test_nota_longa_e_cortada():
    notas.anota(1, 7, "x" * 1000)
    assert len(notas.de(1, 7)[0]) == notas.MAX_CARACTERES


def test_arquivo_estragado_comeca_vazio():
    with open(notas._ARQUIVO, "w", encoding="utf-8") as f:
        f.write("{nada")
    assert notas.de(1, 7) == []


def test_ferramentas_de_notas_do_gemini():
    assert gemini.bloco_de_notas(gemini._ANOTAR, {"texto": "comprar pão"}, 1, 7) == {"resultado": "ok, anotado", "total": 1}
    assert gemini.bloco_de_notas(gemini._MINHAS_NOTAS, None, 1, 7) == {"notas": ["comprar pão"]}
    assert gemini.bloco_de_notas(gemini._APAGAR_NOTAS, None, 1, 7) == {"apagadas": 1}
    assert "erro" in gemini.bloco_de_notas(gemini._ANOTAR, {"texto": ""}, 1, 7)
    assert "erro" in gemini.bloco_de_notas(gemini._ANOTAR, {"texto": "x"}, 1, 0)  # sem saber quem fala


def _ctx(user_id=7):
    return SimpleNamespace(
        guild=SimpleNamespace(id=1), author=SimpleNamespace(id=user_id), respond=AsyncMock(),
    )


def test_comando_notas_lista_numerado_so_para_quem_pediu():
    notas.anota(1, 7, "comprar pão")
    notas.anota(1, 7, "ração do gato")
    ctx = _ctx()

    asyncio.run(Notas.notas.callback(Notas(None), ctx, False))

    ctx.respond.assert_awaited_once_with(
        m.NOTES_LIST.format(lista="1. comprar pão\n2. ração do gato"), ephemeral=True,
    )


def test_comando_notas_apagar_e_vazio():
    notas.anota(1, 7, "comprar pão")
    ctx = _ctx()
    asyncio.run(Notas.notas.callback(Notas(None), ctx, True))
    ctx.respond.assert_awaited_once_with(m.NOTES_DELETED.format(n=1), ephemeral=True)

    ctx = _ctx()
    asyncio.run(Notas.notas.callback(Notas(None), ctx, False))
    ctx.respond.assert_awaited_once_with(m.NOTES_NONE, ephemeral=True)
