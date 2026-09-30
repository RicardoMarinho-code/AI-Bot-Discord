"""Registro de uso: quem pediu o quê, quando — e o que o Discord fez em volta.

Até 20/09/2026 o journal só conhecia o player por dentro ("tocando X em 4,8s").
Não havia UMA linha dizendo que alguém rodou /play, clicou em ⏭ ou saiu da
call — reconstruir "o que as pessoas estavam fazendo quando deu errado" era
adivinhação. Aqui ficam só listeners: nenhum comando, nenhuma regra.

Privacidade: vai o ID de quem agiu (não o nome) e as opções do comando — o
nome da música pedida, que o bot já publica no canal de texto de todo jeito.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import logging
import time

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

_agora = time.monotonic  # indireção para os testes controlarem o relógio
_MAX_VALOR = 80  # link do Spotify/YouTube com tracking passa de 200 caracteres
_MAX_EM_ANDAMENTO = 200


def descreve_opcoes(opcoes) -> str:
    """`busca='never gonna give you up' modo=substituir` a partir das opções."""
    partes = []
    for opcao in opcoes or []:
        valor = opcao.get("value")
        if isinstance(valor, str):
            valor = repr(valor if len(valor) <= _MAX_VALOR else valor[: _MAX_VALOR - 1] + "…")
        partes.append(f"{opcao.get('name')}={valor}")
    return " ".join(partes)


class Registro(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._em_andamento: dict[int, float] = {}  # interaction.id -> início

    # ── comandos ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_application_command(self, ctx: discord.ApplicationContext) -> None:
        if len(self._em_andamento) >= _MAX_EM_ANDAMENTO:  # comando que deu erro não fecha
            self._em_andamento.pop(next(iter(self._em_andamento)))
        self._em_andamento[ctx.interaction.id] = _agora()
        opcoes = descreve_opcoes(getattr(ctx, "selected_options", None))
        log.info(
            "[%s] /%s%s — por %s em #%s",
            getattr(ctx.guild, "id", "dm"),
            ctx.command.qualified_name,
            f" {opcoes}" if opcoes else "",
            ctx.author.id,
            getattr(ctx.channel, "name", "?"),
        )

    @commands.Cog.listener()
    async def on_application_command_completion(self, ctx: discord.ApplicationContext) -> None:
        inicio = self._em_andamento.pop(ctx.interaction.id, None)
        if inicio is None:
            return
        gasto = _agora() - inicio
        # a janela de resposta do Discord é 3s (15min depois do defer): comando
        # lento é o "O aplicativo não respondeu" de amanhã
        log.log(
            logging.WARNING if gasto > 10 else logging.INFO,
            "[%s] /%s terminou em %.1fs",
            getattr(ctx.guild, "id", "dm"), ctx.command.qualified_name, gasto,
        )

    @commands.Cog.listener()
    async def on_application_command_error(self, ctx: discord.ApplicationContext, error) -> None:
        inicio = self._em_andamento.pop(ctx.interaction.id, None)
        if inicio is not None:  # o traceback quem loga é o handler do main.py
            log.warning(
                "[%s] /%s FALHOU depois de %.1fs",
                getattr(ctx.guild, "id", "dm"), ctx.command.qualified_name,
                _agora() - inicio,
            )

    # ── botões e menus ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        dados = interaction.data or {}
        valores = dados.get("values")
        log.info(
            "[%s] clique em %s%s — por %s",
            interaction.guild_id or "dm",
            dados.get("custom_id", "?"),
            f" {valores}" if valores else "",
            getattr(interaction.user, "id", "?"),
        )

    # ── call ────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if before.channel == after.channel:
            return  # mute/ensurdecer/câmera: não muda quem está na call
        guild = member.guild
        if member.id == self.bot.user.id:
            log.info(
                "[%s] bot na voz: %s → %s", guild.id,
                before.channel or "fora", after.channel or "fora",
            )
            return
        canal = getattr(guild.voice_client, "channel", None)
        if canal is None or canal not in (before.channel, after.channel):
            return
        ouvintes = sum(1 for m in canal.members if not m.bot)
        log.info(
            "[%s] %s %s do canal do bot — %d ouvinte(s) agora",
            guild.id, member.id, "entrou" if after.channel == canal else "saiu", ouvintes,
        )

    # ── gateway e servidores ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_connect(self) -> None:
        log.info("gateway: conectado")

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        if self.bot.is_closed():  # SIGTERM: desligamento limpo não é alarme
            log.info("gateway: desconectado (o bot está desligando)")
            return
        log.warning("gateway: conexão com o Discord caiu (o py-cord reconecta sozinho)")

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        log.info("gateway: sessão retomada")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("[%s] entrei no servidor %r (%s membros)", guild.id, guild.name, guild.member_count)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("[%s] saí (ou fui removido) do servidor %r", guild.id, guild.name)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Registro(bot))
