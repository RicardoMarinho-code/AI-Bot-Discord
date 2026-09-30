"""Os lembretes sobrevivem a um reinício do bot (data/lembretes.json)."""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import discord

import messages as m
from core import lembretes


def _lembrete(daqui_s=600.0, texto="tirar a pizza", channel_id=5):
    return lembretes.Lembrete(
        guild_id=1, user_id=7, channel_id=channel_id, texto=texto, vence_em=time.time() + daqui_s,
    )


def _no_arquivo():
    with open(lembretes._ARQUIVO, encoding="utf-8") as f:
        return json.load(f)


def _canal():
    canal = MagicMock()
    canal.send = AsyncMock()
    return canal


def test_agendado_vai_para_o_arquivo_e_sai_quando_avisa():
    canal = _canal()

    async def roda():
        lembretes.agenda(canal, _lembrete(daqui_s=600))
        lembretes.agenda(canal, _lembrete(daqui_s=0.01, texto="já"))
        assert [item["texto"] for item in _no_arquivo()] == ["tirar a pizza", "já"]
        await asyncio.sleep(0.05)
        assert [item["texto"] for item in _no_arquivo()] == ["tirar a pizza"]
        lembretes.cancela(1, 7)

    asyncio.run(roda())

    canal.send.assert_awaited_once_with(m.REMINDER.format(mencao="<@7>", texto="já"))
    assert _no_arquivo() == []


def test_bot_desligando_nao_apaga_o_arquivo():
    """O asyncio.run cancela as tarefas no fim, como o bot desligando."""

    async def roda():
        lembretes.agenda(_canal(), _lembrete())

    asyncio.run(roda())

    assert [item["texto"] for item in _no_arquivo()] == ["tirar a pizza"]
    lembretes.pendentes.clear()


def test_no_boot_os_salvos_voltam_e_o_vencido_avisa_na_hora():
    canal = _canal()
    lembretes.pendentes.clear()
    with open(lembretes._ARQUIVO, "w", encoding="utf-8") as f:
        json.dump([vars(_lembrete(daqui_s=-30, texto="venceu desligado"))], f)
    bot = MagicMock()
    bot.get_channel.return_value = canal

    async def roda():
        assert await lembretes.restaura(bot) == 1
        await asyncio.sleep(0.02)

    asyncio.run(roda())

    bot.get_channel.assert_called_once_with(5)
    canal.send.assert_awaited_once_with(m.REMINDER.format(mencao="<@7>", texto="venceu desligado"))
    assert _no_arquivo() == []


def test_no_boot_o_de_um_canal_que_sumiu_e_descartado():
    with open(lembretes._ARQUIVO, "w", encoding="utf-8") as f:
        json.dump([vars(_lembrete(channel_id=404))], f)
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "sumiu"))

    assert asyncio.run(lembretes.restaura(bot)) == 0
    assert _no_arquivo() == []


def test_arquivo_estragado_nao_derruba_o_boot():
    with open(lembretes._ARQUIVO, "w", encoding="utf-8") as f:
        f.write("{isso não é json")

    assert asyncio.run(lembretes.restaura(MagicMock())) == 0


def test_sem_arquivo_nada_volta():
    assert asyncio.run(lembretes.restaura(MagicMock())) == 0
