"""Lembretes pedidos por voz ("Jarvis, me avisa em 10 minutos").

Na hora, o bot marca quem pediu no chat. Os pendentes ficam num arquivo
(data/lembretes.json) e voltam quando o bot reinicia: um lembrete de 2h não
pode sumir porque a VM reiniciou no meio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass

import discord

import messages as m

log = logging.getLogger(__name__)

_ARQUIVO = os.path.join("data", "lembretes.json")


@dataclass
class Lembrete:
    guild_id: int
    user_id: int
    channel_id: int  # onde avisar — é o que sobrevive a um reinício
    texto: str
    vence_em: float  # time.time(): o /lembretes mostra como "daqui a X" do Discord


# os pendentes: referência forte (o asyncio só guarda uma fraca) e fora da
# escuta, para o aviso chegar mesmo se o bot sair da call antes
pendentes: dict[asyncio.Task, Lembrete] = {}


def de(guild_id: int, user_id: int) -> list[Lembrete]:
    """Os lembretes pendentes de alguém num servidor, do mais próximo ao mais longe."""
    return sorted(
        (lem for lem in pendentes.values() if lem.guild_id == guild_id and lem.user_id == user_id),
        key=lambda lem: lem.vence_em,
    )


def cancela(guild_id: int, user_id: int) -> int:
    """Cancela os lembretes pendentes de alguém; devolve quantos eram."""
    tarefas = [t for t, lem in pendentes.items() if lem.guild_id == guild_id and lem.user_id == user_id]
    for tarefa in tarefas:
        tarefa.cancel()
        pendentes.pop(tarefa, None)  # já, sem esperar o callback: o /lembretes logo depois não o vê
    if tarefas:
        _salva()
    return len(tarefas)


def agenda(canal: discord.abc.Messageable, lembrete: Lembrete) -> None:
    """Na hora do lembrete (ou já, se venceu com o bot desligado), avisa no canal."""

    async def avisa() -> None:
        await asyncio.sleep(max(0.0, lembrete.vence_em - time.time()))
        try:
            await canal.send(m.REMINDER.format(mencao=f"<@{lembrete.user_id}>", texto=lembrete.texto))
        except Exception:  # um aviso perdido não derruba nada
            log.warning("⏰ [%s] não consegui mandar o lembrete", lembrete.user_id, exc_info=True)

    def terminou(tarefa: asyncio.Task) -> None:
        pendentes.pop(tarefa, None)
        # cancelada é o bot desligando (quem cancela pelo /lembretes já tirou
        # da lista): o arquivo fica como está, para o lembrete voltar no boot
        if not tarefa.cancelled():
            _salva()

    tarefa = asyncio.create_task(avisa())
    pendentes[tarefa] = lembrete
    tarefa.add_done_callback(terminou)
    _salva()


def _salva() -> None:
    try:
        os.makedirs(os.path.dirname(_ARQUIVO), exist_ok=True)
        temporario = _ARQUIVO + ".tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump([asdict(lem) for lem in pendentes.values()], f, ensure_ascii=False)
        os.replace(temporario, _ARQUIVO)  # nunca um arquivo pela metade
    except OSError:
        log.warning("⏰ não consegui salvar os lembretes", exc_info=True)


def _le() -> list[Lembrete]:
    with open(_ARQUIVO, encoding="utf-8") as f:
        return [Lembrete(**item) for item in json.load(f)]


async def restaura(bot: discord.Bot) -> int:
    """No boot: reagenda os lembretes salvos; devolve quantos voltaram."""
    try:
        salvos = await asyncio.to_thread(_le)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, TypeError):
        log.warning("⏰ o arquivo de lembretes está estragado — ignorando", exc_info=True)
        return 0
    voltaram = 0
    for lembrete in salvos:
        canal = bot.get_channel(lembrete.channel_id)
        if canal is None:
            try:
                canal = await bot.fetch_channel(lembrete.channel_id)
            except discord.HTTPException:
                log.info("⏰ o canal %s de um lembrete sumiu — descartado", lembrete.channel_id)
                continue
        agenda(canal, lembrete)
        voltaram += 1
    _salva()  # os descartados saem do arquivo
    if voltaram:
        log.info("⏰ %d lembrete(s) de antes do reinício voltaram", voltaram)
    return voltaram
