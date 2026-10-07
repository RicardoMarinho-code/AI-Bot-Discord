"""/perguntar: conversar com o bot por escrito.

A resposta vem no chat — e, se o bot está numa call, também falada. É a mesma
conversa da voz: a mesma memória, as mesmas ferramentas (lembretes, notas,
contas...) e as mesmas regras de quem manda (cogs/sessao.py).

Um comando e não "mensagem que menciona o bot": ler as mensagens do chat exige
o intent de conteúdo de mensagens, que é privilegiado.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import logging

import discord
from discord.ext import commands

import messages as m
from core.assistente import peek_assistente
from core.audio import FalaAoVivo
from core.listen import aplica_extras
from services import gemini

log = logging.getLogger(__name__)

_SEM_PING = discord.AllowedMentions.none()  # a resposta vem da IA: nada de @everyone


class Chat(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(name="perguntar", description="Me pergunta por escrito: respondo aqui (e falando, se eu estiver na call)")
    async def perguntar(
        self,
        ctx: discord.ApplicationContext,
        pergunta: discord.Option(str, "o que você quer saber", max_length=500),  # type: ignore[valid-type]
    ) -> None:
        sessao = peek_assistente(ctx.guild.id)
        na_call = sessao is not None and sessao.conectado()
        if na_call and not sessao.pode_falar(ctx.author.id):
            await ctx.respond(
                m.NOT_ALLOWED.format(dono=f"<@{sessao.dono_id}>"), ephemeral=True, allowed_mentions=_SEM_PING,
            )
            return
        await ctx.defer()
        quem = getattr(ctx.author, "display_name", "") or ""
        fala: FalaAoVivo | None = None

        def ao_falar(pedaco: bytes) -> None:
            nonlocal fala
            if fala is None:
                fala = sessao.fala_ao_vivo()  # corta o que o bot estava falando
            if fala is not None:
                fala.escreve(pedaco)

        try:
            resposta = await gemini.responde(
                ctx.guild.id, b"", texto=pergunta, quem=quem, user_id=ctx.author.id,
                ao_falar=ao_falar if na_call else None,
                na_call=sessao.nomes_na_call if na_call else None,
                manda=sessao.manda(ctx.author.id) if na_call else True,
                controle=sessao.controle if na_call else None,
            )
        except Exception as exc:  # noqa: BLE001 — a pessoa vê o erro no chat
            # %s e não %r: nunca arriscar a chave num repr de erro de rede
            log.warning("💬 [%s] /perguntar falhou (%s: %s)", ctx.author.id, type(exc).__name__, exc)
            await ctx.respond(m.VOICE_ANSWER_FAILED)
            return
        finally:
            if fala is not None:
                fala.termina()
        await aplica_extras(resposta, ctx.channel, ctx.guild.id, ctx.author.id, quem)
        if not resposta.texto:
            await ctx.respond(m.VOICE_ANSWER_FAILED)
        else:
            await ctx.respond(
                m.VOICE_ANSWER.format(quem=quem or "Alguém", pergunta=pergunta[:300], resposta=resposta.texto[:1500]),
                allowed_mentions=_SEM_PING,
            )
        if na_call and resposta.quer_sair:
            await sessao.sai(f"/perguntar de {ctx.author.id}")


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Chat(bot))
