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
import unicodedata

import discord

import config
import messages as m
from core.audio import BYTES_POR_S, FalaAoVivo, FonteDaFala

log = logging.getLogger(__name__)

EMPTY_LEAVE_S = 120  # segundos sozinho na call até sair (cogs/lifecycle.py)

# quem fala com o bot: no modo aberto, todo mundo da call; no modo dono, só quem
# o chamou (o /entrar) e quem ele liberar
MODO_ABERTO = "aberto"
MODO_DONO = "dono"


def _sem_acento(texto: str) -> str:
    """"Júlia" → "julia": o Gemini escreve o nome do jeito que ouviu."""
    return "".join(
        c for c in unicodedata.normalize("NFD", texto.casefold()) if unicodedata.category(c) != "Mn"
    )


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
        # quem manda: quem deu o /entrar; vale enquanto o bot está na call
        self.dono_id: int | None = None
        self.modo = MODO_ABERTO
        self.convidados: set[int] = set()  # no modo dono, também falam com o bot
        self.bloqueados: set[int] = set()  # ignorados em qualquer modo ("ignora o Pedro")

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

    def ids_na_call(self) -> list[int]:
        """Quem está na call com o bot (sem ele), pelos voice_states (ver nomes_na_call)."""
        if not self.voice or not self.voice.channel:
            return []
        eu = self.guild.me.id if self.guild.me is not None else None
        return [uid for uid in self.voice.channel.voice_states if uid != eu]

    # --- quem manda ---

    def _dono_presente(self) -> bool:
        return self.dono_id is not None and self.dono_id in self.ids_na_call()

    def pode_falar(self, user_id: int) -> bool:
        """O bot escuta essa pessoa? Bloqueado nunca; no modo dono, só ele e os
        convidados — a não ser que o dono tenha saído da call (aí volta a ser aberto)."""
        if user_id == self.dono_id:
            return True
        if user_id in self.bloqueados:
            return False
        if self.modo == MODO_ABERTO or not self._dono_presente():
            return True
        return user_id in self.convidados

    def manda(self, user_id: int) -> bool:
        """Pode tirar o bot da call, mudar o modo e liberar/bloquear pessoas: o
        dono — ou qualquer um, se não há dono na call (ninguém fica sem controle)."""
        return user_id == self.dono_id or not self._dono_presente()

    def assume(self, user_id: int) -> None:
        """Quem deu o /entrar vira o dono, se não há outro dono na call."""
        if not self._dono_presente():
            self.dono_id = user_id
            self.convidados.discard(user_id)
            self.bloqueados.discard(user_id)

    def define_modo(self, modo: str) -> None:
        if modo not in (MODO_ABERTO, MODO_DONO):
            raise ValueError(f"modo desconhecido: {modo}")
        self.modo = modo

    def libera(self, user_id: int) -> None:
        self.bloqueados.discard(user_id)
        self.convidados.add(user_id)

    def bloqueia(self, user_id: int) -> None:
        if user_id == self.dono_id:
            raise ValueError("o dono não pode ser bloqueado")
        self.convidados.discard(user_id)
        self.bloqueados.add(user_id)

    def passa_o_controle(self) -> int | None:
        """O dono saiu da call: quem estiver lá (e não for bot) assume. None = ninguém."""
        bots = {m.id for m in self.pessoas_e_bots() if m.bot}
        for uid in self.ids_na_call():
            if uid != self.dono_id and uid not in bots and uid not in self.bloqueados:
                self.dono_id = uid
                self.convidados.discard(uid)
                return uid
        self.dono_id = None
        return None

    def pessoas_e_bots(self) -> list[discord.Member]:
        if not self.voice or not self.voice.channel:
            return []
        return list(self.voice.channel.members)

    async def controle(self, acao: str, valor: str) -> dict:
        """O que o Gemini pede por voz, já conferido que quem pediu manda:
        acao "modo" (aberto/dono), "liberar" ou "bloquear" (pelo nome de quem está na call)."""
        if acao == "modo":
            try:
                self.define_modo(valor.strip().lower())
            except ValueError as erro:
                return {"erro": str(erro)}
            return {"resultado": f"modo {self.modo}"}
        if acao not in ("liberar", "bloquear"):
            return {"erro": f"ação desconhecida: {acao}"}
        procurado = _sem_acento(valor.strip())
        if not procurado:
            return {"erro": "de quem?"}
        nomes = {uid: await self.nome_de(uid) for uid in self.ids_na_call()}
        achados = [uid for uid, nome in nomes.items() if nome and procurado in _sem_acento(nome)]
        exatos = [uid for uid in achados if _sem_acento(nomes[uid]) == procurado]
        achados = exatos or achados
        if not achados:
            return {"erro": f"ninguém na call se chama {valor}", "na_call": [n for n in nomes.values() if n]}
        if len(achados) > 1:
            return {"erro": f"mais de uma pessoa com {valor}: qual?", "opcoes": [nomes[uid] for uid in achados]}
        uid = achados[0]
        try:
            (self.libera if acao == "liberar" else self.bloqueia)(uid)
        except ValueError as erro:
            return {"erro": str(erro)}
        return {"resultado": f"{nomes[uid]} {'liberado' if acao == 'liberar' else 'bloqueado'}"}

    async def nomes_na_call(self) -> list[str]:
        """Os apelidos de quem está na call com o bot (sem ele).

        Pelos voice_states do canal e não por channel.members: sem o intent de
        membros, quem já estava na call quando o bot entrou fica fora do cache.
        """
        nomes = [await self.nome_de(uid) for uid in self.ids_na_call()]
        return [nome for nome in nomes if nome]

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
        # a próxima sessão começa do zero: quem der o /entrar manda
        self.dono_id, self.modo = None, MODO_ABERTO
        self.convidados.clear()
        self.bloqueados.clear()
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
