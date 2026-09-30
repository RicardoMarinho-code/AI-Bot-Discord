"""Comando /status: como o bot está neste servidor, num relance.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import time

import discord
from discord.ext import commands

import config
import messages as m
from core import lembretes
from core.assistente import peek_assistente
from services import gemini

_NO_AR_DESDE = time.time()  # carregado no boot: "no ar há 3h"


def ha_quanto(segundos: float) -> str:
    """3725 → "1h02min"; 90 → "1min"; 20 → "menos de 1min"."""
    minutos = int(segundos // 60)
    if minutos < 1:
        return "menos de 1min"
    dias, minutos = divmod(minutos, 24 * 60)
    horas, minutos = divmod(minutos, 60)
    if dias:
        return f"{dias}d{horas:02d}h"
    if horas:
        return f"{horas}h{minutos:02d}min"
    return f"{minutos}min"


def descreve(bot: discord.Bot, guild_id: int) -> str:
    sessao = peek_assistente(guild_id)
    if sessao is not None and sessao.conectado():
        call = m.STATUS_IN_CALL.format(canal=sessao.voice.channel.mention)
    else:
        call = m.STATUS_OUT_OF_CALL
    voz = gemini.voz_de(guild_id)
    pendentes = sum(1 for lem in lembretes.pendentes.values() if lem.guild_id == guild_id)
    return m.STATUS.format(
        call=call,
        voz=f"{voz} ({gemini.VOZES[voz]})" if voz in gemini.VOZES else (voz or "a padrão do Google"),
        trocas=len(gemini._memoria(guild_id)) // 2,  # pergunta + resposta
        lembretes=pendentes,
        ping=round(bot.latency * 1000) if bot.latency == bot.latency else "?",  # NaN antes de conectar
        no_ar=ha_quanto(time.time() - _NO_AR_DESDE),
        modelo=config.GEMINI_LIVE_MODEL,
    )


class Status(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(name="status", description="Como eu estou neste servidor: call, voz, memória, lembretes")
    async def status(self, ctx: discord.ApplicationContext) -> None:
        await ctx.respond(descreve(self.bot, ctx.guild.id), ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Status(bot))
