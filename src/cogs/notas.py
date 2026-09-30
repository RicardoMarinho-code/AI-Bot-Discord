"""Comando /notas: as anotações feitas por voz, no chat.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import discord
from discord.ext import commands

import messages as m
from core import notas


class Notas(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(name="notas", description="Mostro (ou apago) as suas anotações feitas por voz")
    async def notas(
        self,
        ctx: discord.ApplicationContext,
        apagar: discord.Option(bool, "apagar todas", default=False),  # type: ignore[valid-type]
    ) -> None:
        # só a pessoa vê: anotação é coisa dela
        if apagar:
            n = notas.apaga(ctx.guild.id, ctx.author.id)
            await ctx.respond(m.NOTES_DELETED.format(n=n) if n else m.NOTES_NONE, ephemeral=True)
            return
        minhas = notas.de(ctx.guild.id, ctx.author.id)
        if not minhas:
            await ctx.respond(m.NOTES_NONE, ephemeral=True)
            return
        lista = "\n".join(f"{i}. {nota}" for i, nota in enumerate(minhas, 1))
        await ctx.respond(m.NOTES_LIST.format(lista=lista)[:2000], ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Notas(bot))
