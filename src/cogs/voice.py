"""/entrar, /sair e /voz: o bot entra na call de quem chamou, escuta e responde.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import logging

import discord
from discord.ext import commands

import messages as m
from core.assistente import entra_na_call, peek_assistente
from core.listen import VoiceListener
from services import gemini

log = logging.getLogger(__name__)

_PADRAO = "padrao"
_ESCOLHAS = [
    discord.OptionChoice(name=f"{nome} — {estilo}", value=nome) for nome, estilo in gemini.VOZES.items()
] + [discord.OptionChoice(name="A de sempre (a do .env)", value=_PADRAO)]


class Voice(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(
        name="entrar",
        description=f'Entro na sua call e fico ouvindo: me chame com "{m.NOME}, ..."',
    )
    async def entrar(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        try:
            sessao = await entra_na_call(ctx)
        except Exception as exc:  # noqa: BLE001 — melhor que "pensando…" eterno
            log.warning("[%s] /entrar falhou ao conectar: %r", ctx.guild.id, exc)
            await ctx.respond(m.JOIN_FAILED.format(error=exc))
            return
        if sessao is None:
            return
        if not sessao.conectado():
            await ctx.respond(m.CONN_DROPPED)
            return
        sessao.text_channel = ctx.channel
        if sessao.escuta is not None and sessao.escuta.active:
            # já estava escutando (talvez em outro canal): só muda onde responde
            sessao.escuta.text_channel = ctx.channel
            await ctx.respond(m.JOINED)
            return
        escuta = VoiceListener(sessao, ctx.channel)
        try:
            await escuta.start()
        except Exception as exc:  # noqa: BLE001 — a pessoa vê o erro no chat
            log.warning("[%s] não consegui ligar a escuta: %r", ctx.guild.id, exc, exc_info=exc)
            escuta.stop()
            await ctx.respond(m.JOIN_FAILED.format(error=exc))
            return
        sessao.escuta = escuta
        await ctx.respond(m.JOINED)

    @commands.slash_command(name="sair", description="Saio da call")
    async def sair(self, ctx: discord.ApplicationContext) -> None:
        sessao = peek_assistente(ctx.guild.id)
        if sessao is None or not sessao.conectado():
            await ctx.respond(m.NOT_IN_CALL, ephemeral=True)
            return
        await ctx.defer()
        await sessao.sai(f"/sair de {ctx.author.id}")
        await ctx.respond(m.LEFT)


    @commands.slash_command(name="voz", description="Troco a voz com que eu falo neste servidor")
    async def voz(
        self,
        ctx: discord.ApplicationContext,
        nome: discord.Option(str, "a voz nova", choices=_ESCOLHAS),  # type: ignore[valid-type]
    ) -> None:
        # vale já na próxima pergunta: cada pergunta abre uma sessão Live nova
        if nome == _PADRAO:
            gemini.escolhe_voz(ctx.guild.id, None)
            await ctx.respond(m.VOICE_RESET)
            return
        gemini.escolhe_voz(ctx.guild.id, nome)
        await ctx.respond(m.VOICE_CHANGED.format(voz=nome, estilo=gemini.VOZES[nome]))


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Voice(bot))
