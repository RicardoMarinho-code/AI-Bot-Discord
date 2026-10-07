"""/modo, /permitir e /bloquear: quem manda no bot é quem deu o /entrar.

No modo aberto (o padrão) todo mundo da call fala com o bot; no modo dono, só
quem o chamou e quem ele liberar. Bloqueado é ignorado em qualquer modo. Se o
dono sai da call, o controle passa para quem ficou (cogs/lifecycle.py).

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import discord
from discord.ext import commands

import messages as m
from core.assistente import MODO_ABERTO, MODO_DONO, Assistente, peek_assistente

_SEM_PING = discord.AllowedMentions.none()  # mostra o @ sem notificar ninguém


async def _sessao_do_dono(ctx: discord.ApplicationContext) -> Assistente | None:
    """A sessão, se o bot está na call e quem pediu manda; senão responde e devolve None."""
    sessao = peek_assistente(ctx.guild.id)
    if sessao is None or not sessao.conectado():
        await ctx.respond(m.NOT_IN_CALL, ephemeral=True)
        return None
    if not sessao.manda(ctx.author.id):
        await ctx.respond(
            m.OWNER_ONLY.format(dono=f"<@{sessao.dono_id}>"), ephemeral=True, allowed_mentions=_SEM_PING,
        )
        return None
    return sessao


class Sessao(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(name="modo", description="Com quem eu converso na call: todo mundo, ou só quem me chamou")
    async def modo(
        self,
        ctx: discord.ApplicationContext,
        modo: discord.Option(  # type: ignore[valid-type]
            str, "aberto: todo mundo · dono: só você e quem você liberar",
            choices=[
                discord.OptionChoice(name="aberto — todo mundo da call", value=MODO_ABERTO),
                discord.OptionChoice(name="só o dono — você e quem você liberar", value=MODO_DONO),
            ],
        ),
    ) -> None:
        sessao = await _sessao_do_dono(ctx)
        if sessao is None:
            return
        sessao.define_modo(modo)
        texto = m.MODE_OPEN if modo == MODO_ABERTO else m.MODE_OWNER.format(dono=f"<@{sessao.dono_id}>")
        # público: a call toda precisa saber com quem o bot está falando
        await ctx.respond(texto, allowed_mentions=_SEM_PING)

    @commands.slash_command(name="permitir", description="Deixo essa pessoa falar comigo (no modo só o dono)")
    async def permitir(
        self,
        ctx: discord.ApplicationContext,
        pessoa: discord.Option(discord.Member, "quem"),  # type: ignore[valid-type]
    ) -> None:
        sessao = await _sessao_do_dono(ctx)
        if sessao is None:
            return
        sessao.libera(pessoa.id)
        await ctx.respond(m.ALLOWED.format(pessoa=pessoa.mention), allowed_mentions=_SEM_PING)

    @commands.slash_command(name="bloquear", description="Passo a ignorar essa pessoa na call (em qualquer modo)")
    async def bloquear(
        self,
        ctx: discord.ApplicationContext,
        pessoa: discord.Option(discord.Member, "quem"),  # type: ignore[valid-type]
    ) -> None:
        sessao = await _sessao_do_dono(ctx)
        if sessao is None:
            return
        try:
            sessao.bloqueia(pessoa.id)
        except ValueError:
            await ctx.respond(m.CANT_BLOCK_OWNER, ephemeral=True)
            return
        await ctx.respond(m.BLOCKED.format(pessoa=pessoa.mention), allowed_mentions=_SEM_PING)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Sessao(bot))
