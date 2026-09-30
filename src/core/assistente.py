"""O assistente num servidor: a conexão de voz, a fala do bot e a escuta.

Um por servidor (guild). O py-cord toca UMA fonte por vez na call: o estalo de
"estou ouvindo" e a fala do Gemini se revezam — o que chega depois corta o que
estava tocando. É o comportamento certo: chamar o bot pelo nome no meio de uma
resposta interrompe a resposta, como numa conversa.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

import discord

import config
import messages as m
from core.audio import BYTES_POR_S, FalaAoVivo, FonteDaFala

log = logging.getLogger(__name__)

EMPTY_LEAVE_S = 120  # segundos sozinho na call até sair (cogs/lifecycle.py)


class Assistente:
    """Um por servidor (guild)."""

    def __init__(self, bot: discord.Bot, guild: discord.Guild) -> None:
        self.bot = bot
        self.guild = guild
        self.text_channel: discord.abc.Messageable | None = None
        self.escuta = None  # core.listen.VoiceListener enquanto está na call
        self._fala: FalaAoVivo | None = None  # a resposta do Gemini tocando
        self._vez = 0  # qual som é o atual: o fim de um antigo não mexe no novo
        self._tocando = False
        self._efeito_ate = 0.0  # monotonic em que o estalo atual termina
        self._empty_task: asyncio.Task | None = None  # timer do auto-leave
        self._nomes: dict[int, str] = {}  # user_id → apelido, para o Gemini saber com quem fala

    # --- estado ---

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    def conectado(self) -> bool:
        return bool(self.voice and self.voice.is_connected())

    def pessoas(self) -> list[discord.Member]:
        """Quem está na call com o bot (sem contar bots)."""
        if not self.voice or not self.voice.channel:
            return []
        return [membro for membro in self.voice.channel.members if not membro.bot]

    async def nome_de(self, user_id: int) -> str:
        """Como chamar quem falou (o apelido no servidor), ou "" se não souber.

        O bot não pede o intent de membros (privilegiado): quem já estava na
        call quando ele entrou não está no cache. Buscar UM membro pela API não
        precisa do intent — e o nome fica guardado para a próxima pergunta.
        """
        if user_id <= 0:  # áudio ainda sem pessoa mapeada (SSRC)
            return ""
        if user_id not in self._nomes:
            membro = self.guild.get_member(user_id)
            if membro is None:
                try:
                    membro = await self.guild.fetch_member(user_id)
                except discord.HTTPException:
                    return ""
            self._nomes[user_id] = membro.display_name
        return self._nomes[user_id]

    # --- conexão ---

    async def conecta(self, channel: discord.VoiceChannel) -> None:
        if self.voice and self.voice.is_connected():
            if self.voice.channel != channel:
                log.info("[%s] mudando de canal de voz: %s → %s", self.guild.id, self.voice.channel, channel)
                await self.voice.move_to(channel)
            return
        if self.voice is not None:  # voice client zumbi (ex.: pós-desconexão) trava o connect
            log.warning("[%s] voice client zumbi — derrubando antes de reconectar", self.guild.id)
            await self.voice.disconnect(force=True)
        inicio = time.monotonic()
        try:
            await channel.connect()
        except Exception as exc:
            # o handshake de voz é a parte que o bot não controla (DAVE, região)
            log.warning(
                "[%s] não consegui entrar em %s depois de %.1fs: %r",
                self.guild.id, channel, time.monotonic() - inicio, exc,
            )
            raise
        log.info(
            "[%s] entrei em %s em %.1fs (%d pessoa(s) no canal)",
            self.guild.id, channel, time.monotonic() - inicio, len(self.pessoas()),
        )

    async def sai(self, motivo: str = "") -> None:
        """Para de escutar, cala e sai da call. `motivo` só vai para o log."""
        log.info("[%s] saindo da call (%s)", self.guild.id, motivo or "sem motivo informado")
        self.cancel_empty_leave()
        if self.escuta is not None:
            self.escuta.stop()
            self.escuta = None
        self.cala()
        if self.voice is not None and self.voice.is_connected():
            await self.voice.disconnect()

    # --- o que o bot toca ---

    def _toca(self, fonte: discord.AudioSource) -> None:
        voice = self.voice
        if voice.is_playing():
            voice.stop_playing()  # o som novo entra no lugar (ver core/pycord_voice_patch.py)
        self._vez += 1
        vez = self._vez
        self._tocando = True
        voice.play(fonte, after=lambda _erro: self._fim_do_som(vez))

    def _fim_do_som(self, vez: int) -> None:
        """Callback do py-cord (thread de áudio) quando um som acaba."""
        if vez == self._vez:
            self._tocando = False

    def tocar_efeito(self, pcm: bytes) -> None:
        """O estalo de "estou ouvindo". Interrompe a fala, se o bot estava falando."""
        if not config.VOICE_SOM_ESCUTA or not pcm or not self.conectado():
            return
        self.cala()
        self._toca(discord.PCMAudio(io.BytesIO(pcm)))
        self._efeito_ate = time.monotonic() + len(pcm) / BYTES_POR_S

    async def espera_efeito(self) -> None:
        """Dá tempo de o estalo terminar antes de algo que cortaria o som (sair)."""
        resta = self._efeito_ate - time.monotonic()
        if resta > 0:
            await asyncio.sleep(resta)

    def fala_ao_vivo(self, *, nao_antes: float = 0.0) -> FalaAoVivo | None:
        """Começa a tocar uma fala que vai chegar aos pedaços (resposta do Gemini).
        Uma fala nova corta a anterior. `nao_antes`: ver FalaAoVivo. None = fora da call."""
        if not self.conectado():
            return None
        self.cala()
        fala = FalaAoVivo(nao_antes=nao_antes)
        self._toca(FonteDaFala(fala))
        self._fala = fala
        return fala

    def cala(self) -> bool:
        """Corta a fala do bot. True = ele estava falando."""
        fala, self._fala = self._fala, None
        return fala is not None and fala.cala()

    # --- auto-leave ---

    def schedule_empty_leave(self) -> None:
        """Agenda a saída por canal vazio (idempotente enquanto pendente)."""
        if self._empty_task is not None and not self._empty_task.done():
            return
        log.info("[%s] fiquei sozinho na call — saio em %ds se ninguém voltar", self.guild.id, EMPTY_LEAVE_S)
        self._empty_task = asyncio.create_task(self._sai_se_continuar_vazio())

    def cancel_empty_leave(self) -> None:
        if self._empty_task is not None:
            if not self._empty_task.done() and self._empty_task is not asyncio.current_task():
                log.info("[%s] alguém voltou para a call — fico", self.guild.id)
                self._empty_task.cancel()
            self._empty_task = None

    async def _sai_se_continuar_vazio(self) -> None:
        await asyncio.sleep(EMPTY_LEAVE_S)
        if not self.conectado() or self.pessoas():
            self._empty_task = None
            return
        canal = self.text_channel
        await self.sai("canal vazio")
        if canal is not None:
            try:
                await canal.send(m.AUTO_LEFT_EMPTY)
            except discord.HTTPException:
                pass


_sessoes: dict[int, Assistente] = {}


def get_assistente(bot: discord.Bot, guild: discord.Guild) -> Assistente:
    sessao = _sessoes.get(guild.id)
    if sessao is None:
        sessao = Assistente(bot, guild)
        _sessoes[guild.id] = sessao
    return sessao


def peek_assistente(guild_id: int) -> Assistente | None:
    """O assistente do servidor, SEM criar um novo (eventos de lifecycle)."""
    return _sessoes.get(guild_id)


def pop_assistente(guild_id: int) -> Assistente | None:
    return _sessoes.pop(guild_id, None)


def todos() -> list[Assistente]:
    return list(_sessoes.values())


async def entra_na_call(ctx: discord.ApplicationContext) -> Assistente | None:
    """Confere que o autor está num canal de voz e leva o bot até lá.

    Responde com o erro apropriado e devolve None quando não dá.
    """
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.respond(m.JOIN_VOICE_FIRST)
        return None
    sessao = get_assistente(ctx.bot, ctx.guild)
    await sessao.conecta(ctx.author.voice.channel)
    return sessao
