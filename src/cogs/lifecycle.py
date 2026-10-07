"""Ciclo de vida da voz: sair com a call vazia, kick forçado, saída de servidor.

Sem isso o bot ficava na call sozinho para sempre (banda + transcrição à toa),
uma desconexão forçada por moderador deixava a escuta órfã, e o assistente de
um servidor removido vivia na memória eternamente.
"""
import logging

import discord
from discord.ext import commands

import messages as m
from core.assistente import peek_assistente, pop_assistente

log = logging.getLogger(__name__)


class Lifecycle(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        sessao = peek_assistente(member.guild.id)
        if sessao is None:
            return
        # o PRÓPRIO bot desconectado à força (kick da call): limpa tudo em vez de
        # deixar a escuta rodando contra voz morta. Não reconecta sozinho — se um
        # moderador tirou, foi por algum motivo.
        if member.id == self.bot.user.id and after.channel is None:
            await sessao.sai("o bot foi desconectado da call (kick ou queda de voz)")
            return
        if not sessao.conectado():
            return
        if not sessao.pessoas():
            sessao.schedule_empty_leave()
        else:
            sessao.cancel_empty_leave()
        await self._passa_o_controle_se_o_dono_saiu(sessao, member, before, after)

    async def _passa_o_controle_se_o_dono_saiu(self, sessao, member, before, after) -> None:
        """O dono saiu da call do bot: quem ficou assume, e o chat fica sabendo."""
        canal_do_bot = sessao.voice.channel if sessao.voice else None
        if member.id != sessao.dono_id or before.channel is None or before.channel != canal_do_bot:
            return
        if after.channel == before.channel:
            return  # mutou, desmutou, ligou a câmera: continua na call
        novo = sessao.passa_o_controle()
        log.info("[%s] o dono saiu da call — o controle passou para %s", member.guild.id, novo)
        if novo is None or sessao.text_channel is None:
            return
        try:
            await sessao.text_channel.send(
                m.OWNER_TRANSFERRED.format(novo=f"<@{novo}>"),
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(novo)]),
            )
        except discord.HTTPException:
            log.warning("[%s] não consegui avisar quem passou a mandar", member.guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Diz a que veio ao entrar num servidor novo.

        Sem isto o bot entrava e ficava mudo: ninguém descobria o `/entrar`.
        Escolhe o canal de sistema do servidor e, se não der para escrever nele,
        o primeiro que der.
        """
        canais = [guild.system_channel, *guild.text_channels]
        for canal in canais:
            if canal is None:
                continue
            try:
                permissoes = canal.permissions_for(guild.me)
                if not permissoes.send_messages:
                    continue
                await canal.send(m.GUILD_WELCOME)
            except Exception:  # noqa: BLE001 — entrar no servidor não pode falhar por isso
                log.warning("[%s] não consegui dar boas-vindas em #%s", guild.id, canal.name)
                continue
            return
        log.info("[%s] entrei mas não achei canal onde escrever", guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        sessao = pop_assistente(guild.id)
        if sessao is None:
            return
        # sem chamadas à API do Discord: o bot já está fora do servidor
        sessao.cancel_empty_leave()
        sessao.cala()
        if sessao.escuta is not None:
            sessao.escuta.stop()
            sessao.escuta = None


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Lifecycle(bot))
