"""Escuta da call: capta a fala, acha o nome do bot e manda a pergunta ao Gemini.

Fluxo: py-cord entrega pacotes PCM por usuário na thread de áudio →
CommandSink acumula por usuário → um ticker no event loop fecha o trecho
quando o usuário para de falar (o Discord PARA de mandar pacotes no
silêncio, então o corte é por ausência de pacotes, não por frames mudos)
→ o COMEÇO do trecho vai para o Whisper, que só precisa achar o nome →
com o nome, o trecho inteiro vai como ÁUDIO para o Gemini (junto com a
continuação, se a pessoa fez uma pausa no meio da pergunta) → a resposta
toca na call enquanto chega.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from discord.sinks import Sink

import config
import messages as m
from core import audio, lembretes
from core.intents import chamou_de_frente, first_word, is_stt_noise, parse_command
from services import gemini, speech

if TYPE_CHECKING:
    from core.assistente import Assistente

log = logging.getLogger(__name__)


BYTES_PER_SECOND = 192_000  # 48kHz * 2 canais * 2 bytes
MIN_SPEECH_S = 0.4
# depois de aparar o silêncio das pontas: um "Jarvis" dito rápido tem ~0,35s
# de fala (medido em 27/09) — com o corte em 0,4s ele sumia como "ruído"
_FALA_APARADA_MIN_S = 0.25
STALE_SEGMENT_S = 12.0  # trecho mais velho que isso é descartado (anti-lag)
# pergunta é mais comprida que comando de música (o teto era 12s no bot de base)
MAX_SEGMENT_S = 20.0
_RMS_WIN_S = 0.02  # análise de energia em janelas de 20ms
_MIN_SPEECH_WINDOWS = 3  # fala de verdade tem ≥3 janelas acima do limiar
# colheita dos trechos terminados e agenda das checagens do nome: a varredura
# é barata (só compara timestamps), e cada volta pode adiantar o estalo
_TICK_S = 0.05
# ── o nome ouvido ENQUANTO a pessoa fala ─────────────────────────────────────
# Esperar a frase acabar (+0,8s de silêncio + a transcrição) punha o estalo
# 3,8s depois do "Jarvis" (medido em 27/09, frases de ~3s). Agora o começo da
# frase vai ao detector (tiny, ~250 ms) assim que chega áudio para conter o
# nome — que termina em ~0,5–0,8s — e o estalo sai com a pessoa ainda falando.
_CEDO_PRIMEIRO_S = 0.8  # a primeira olhada
_CEDO_PASSO_S = 0.4  # sem o nome ainda (ex.: "Ei… Jarvis"): olha de novo com mais isto
_CEDO_ATE_S = 2.2  # o nome vem nas 3 primeiras palavras: passou disto, não vem
_CEDO_PAUSA_S = 0.25  # parou de falar ("Jarvis," + vírgula, ou só "Jarvis"): olha já
_CEDO_MIN_S = 0.3  # menos fala que isto nem vale olhar
# o nome ouvido cedo e a frase longa: é pergunta, sem transcrever de novo —
# comando ("para", "sai da call") é curto
_CURTO_S = 2.0
# o nome é procurado nas 3 primeiras palavras (core/intents): o detector viu
# mais que isso e nada do nome, então não vem
_PALAVRAS_ATE_O_NOME = 3
# descarte de trecho curto (respiro, clique) não vai para o journal; a
# partir daqui era fala de verdade e a pessoa merece saber por que sumiu
_LOG_DISCARD_MIN_S = 1.0
# só o COMEÇO do trecho vai para o Whisper: o nome está nas primeiras palavras,
# e o resto quem entende é o Gemini, ouvindo o áudio. Pergunta longa não custa
# mais CPU (Whisper local) nem estoura o teto da Groq (VOICE_TETO_S).
_CABECA_S = 4.0
# "Jarvis" [pausa] "qual a capital da França?": quem disse só o nome tem esta
# janela para completar — o trecho seguinte é a pergunta.
_CHAMADO_S = 6.0
# O corte por silêncio (VOICE_GAP_S) parte a pergunta de quem pensa no meio
# ("Jarvis, qual é… a capital da França?"). A pergunta vai ao Gemini assim que
# o trecho fecha — ele já vai pensando —, mas o bot só começa a falar isto
# depois do fim do trecho: quem só respirou não é atropelado. Antes a escuta
# ESPERAVA isto para mandar, e o tempo do Gemini vinha depois (medido em
# 27/09: 0,8 + 0,7 + ~2s do Gemini). Se a pessoa voltar a falar, a pergunta
# volta a ser montada com o resto (ver _confere_retomada).
_JUNTA_S = 0.7
_ESPERA_CONTINUACAO_MAX_S = 8.0  # microfone com ruído constante não segura a pergunta
_PERGUNTA_MAX_S = 60.0  # a pergunta montada não cresce para sempre
_PAUSA_ENTRE_TRECHOS = b"\x00" * (BYTES_PER_SECOND * 3 // 10)  # 0,3s: o Gemini ouve a pausa
# A pergunta já foi e quem perguntou voltou a falar: não tinha terminado. Até o
# bot estar falando há _RETOMA_FALANDO_S, é continuação — o pedido é cancelado
# e a pergunta volta a ser montada com o resto. Visto em 27/09: a continuação
# era descartada ("sem o nome") e o Gemini, com meia pergunta, pedia para repetir.
_RETOMA_MIN_S = 0.3  # fala nova com ao menos isto (e voz de verdade) já conta
_RETOMA_PASSO_S = 0.2  # o que era só ruído é olhado de novo com mais isto de fala
_RETOMA_FALANDO_S = 1.5  # o bot falando há mais que isto: é reação, não continuação
_RETOMA_MAX_S = 8.0  # o Gemini mudo não segura a janela para sempre
# o Gemini recusou a pergunta em menos que isto e sem ter falado nada: vale uma
# segunda tentativa (sem a memória). Depois disso, quem perguntou já desistiu.
_SEGUNDA_CHANCE_ATE_S = 5.0
# mandado embora pelo Gemini: espera a despedida tocar, mas não para sempre
_DESPEDIDA_MAX_S = 10.0


_DEBUG_DIR = os.path.join("data", "voice-debug")


def _debug(msg: str) -> None:
    """Diagnóstico opt-in (VOICE_DEBUG=1): print bloqueia o event loop."""
    if config.VOICE_DEBUG:
        print(msg)


def _save_debug_wav(user_id: int, pcm: bytes) -> str:
    """Guarda o trecho como .wav (mantém os 10 últimos) para diagnóstico."""
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        existing = sorted(f for f in os.listdir(_DEBUG_DIR) if f.endswith(".wav"))
        for old in existing[:-9]:
            os.remove(os.path.join(_DEBUG_DIR, old))
        path = os.path.join(_DEBUG_DIR, f"seg-{int(time.time() * 1000)}-{user_id}.wav")
        with wave.open(path, "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(48000)
            out.writeframes(pcm)
        return path
    except Exception:  # noqa: BLE001 — diagnóstico nunca derruba a escuta
        return ""


def analyze_speech(pcm: bytes, threshold: float) -> tuple[int, bytes]:
    """(janelas de 20ms com energia ≥ limiar, PCM aparado nas bordas mudas).

    RMS por janela em vez de RMS global: uma fala curta dentro de um trecho
    comprido não é diluída pela média. O trim corta o silêncio das pontas —
    payload menor para o STT. Pura (numpy) para testes.
    """
    arr = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16).astype(
        np.float32
    )
    win = int(BYTES_PER_SECOND / 2 * _RMS_WIN_S)  # amostras int16 por janela
    usable = len(arr) - (len(arr) % win)
    if usable <= 0:
        return 0, b""
    windows = arr[:usable].reshape(-1, win)
    rms = np.sqrt(np.mean(windows * windows, axis=1))
    hot = np.flatnonzero(rms >= threshold)
    if len(hot) == 0:
        return 0, b""
    first = max(int(hot[0]) - 1, 0)  # 1 janela de margem de cada lado
    last = min(int(hot[-1]) + 2, len(windows))
    end = last * win * 2 if last < len(windows) else len(pcm)
    return len(hot), pcm[first * win * 2 : end]


def peak_rms(pcm: bytes) -> float:
    """Maior RMS de janela de 20ms do trecho — para o log de descarte dizer
    quão longe do limiar a fala ficou (calibração de VOICE_MIN_RMS por mic)."""
    arr = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16).astype(
        np.float32
    )
    win = int(BYTES_PER_SECOND / 2 * _RMS_WIN_S)
    usable = len(arr) - (len(arr) % win)
    if usable <= 0:
        return 0.0
    windows = arr[:usable].reshape(-1, win)
    return float(np.sqrt(np.mean(windows * windows, axis=1)).max())


def cabeca(pcm: bytes) -> bytes:
    """O começo do trecho que vai para o Whisper, sem partir uma amostra."""
    limite = int(_CABECA_S * BYTES_PER_SECOND)
    return pcm if len(pcm) <= limite else pcm[: limite - limite % 4]


class _Cedo:
    """O que a escuta já sabe de um trecho ANTES de ele terminar (ver
    VoiceListener._agenda_checagens): o nome foi dito ou não."""

    __slots__ = ("checado_s", "achou", "texto", "tarefa")

    def __init__(self) -> None:
        self.checado_s = 0.0  # até onde o começo já passou pelo detector
        self.achou: bool | None = None  # None = ainda não sabe
        self.texto = ""  # a última transcrição do começo
        self.tarefa: asyncio.Task | None = None  # checagem em andamento


class _Buffer:
    __slots__ = ("data", "last", "cedo")

    def __init__(self) -> None:
        self.data = bytearray()
        self.last = time.monotonic()
        self.cedo = _Cedo()


class CommandSink(Sink):
    """Acumula PCM por usuário; a colheita acontece no event loop.

    Os atributos ``__sink_listeners__``/``walk_children``/``is_opus`` são
    shims para a API nova de recepção do py-cord 2.8, que a base antiga não
    tem (ver core/pycord_voice_patch.py).
    """

    __sink_listeners__: list[tuple[str, str]] = []

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._buffers: dict[int, _Buffer] = {}
        self.diag_writes = 0
        self.diag_bytes = 0

    def walk_children(self):
        return iter(())

    def is_opus(self) -> bool:
        return False  # queremos PCM já decodificado

    def write(self, data, user) -> None:  # thread de áudio do py-cord
        # data: discord.voice.packets.VoiceData; user: Member/Object/None
        pcm = getattr(data, "pcm", None) or b""
        if not pcm or getattr(user, "bot", False) is True:
            # outro bot na call (27/09: tudo indica que um de música, mandando
            # pacotes sem parar) não fala com o Jarvis: guardar e analisar o
            # áudio dele era CPU jogada fora — e música transcrita gasta a Groq
            return
        user_id = getattr(user, "id", 0) or 0
        if user_id == 0:
            # SSRC ainda sem Member mapeado: bucket próprio por fonte (id
            # negativo não colide com IDs reais) — antes tudo caía no bucket
            # 0 e vozes de pessoas diferentes iam misturadas para o STT
            ssrc = getattr(getattr(data, "packet", None), "ssrc", 0)
            user_id = -ssrc if ssrc else 0
        with self._lock:
            self.diag_writes += 1
            self.diag_bytes += len(pcm)
            buffer = self._buffers.setdefault(user_id, _Buffer())
            buffer.data += pcm
            buffer.last = time.monotonic()

    def trecho_aberto(self, user_id: int) -> bool:
        """A pessoa está no meio de um trecho que ainda não foi colhido?"""
        with self._lock:
            return user_id in self._buffers

    def abertos(self) -> list[tuple[int, _Buffer, float, float]]:
        """(user_id, buffer, segundos de fala, segundos desde o último pacote)
        de cada trecho ainda aberto — quem está falando AGORA."""
        agora = time.monotonic()
        with self._lock:
            return [
                (user_id, buffer, len(buffer.data) / BYTES_PER_SECOND, agora - buffer.last)
                for user_id, buffer in self._buffers.items()
            ]

    def comeco(self, buffer: _Buffer, max_bytes: int) -> bytes:
        """Cópia do começo de um trecho aberto (a thread de áudio segue escrevendo)."""
        with self._lock:
            return bytes(buffer.data[:max_bytes])

    def desde(self, buffer: _Buffer, inicio: int) -> bytes:
        """Cópia de um trecho aberto a partir de `inicio` bytes (o que chegou de novo)."""
        with self._lock:
            return bytes(buffer.data[inicio:])

    def trecho_de(self, user_id: int) -> _Buffer | None:
        """O trecho aberto de uma pessoa, se ela está falando agora."""
        with self._lock:
            return self._buffers.get(user_id)

    def harvest(self) -> list[tuple[int, bytes, float, _Cedo]]:
        """Trechos de fala terminados (usuário calou ou estourou o máximo)."""
        now = time.monotonic()
        ended: list[tuple[int, bytes, _Cedo]] = []
        # dentro do lock SÓ o corte dos buffers: a thread de áudio do
        # py-cord espera por ele a cada pacote (e segura o RLock do router
        # enquanto isso) — numpy/print aqui dentro atrasava todo mundo
        with self._lock:
            for user_id, buffer in list(self._buffers.items()):
                duration = len(buffer.data) / BYTES_PER_SECOND
                if (now - buffer.last) >= config.VOICE_GAP_S or duration >= MAX_SEGMENT_S:
                    del self._buffers[user_id]
                    ended.append((user_id, bytes(buffer.data), buffer.cedo))
        # aqui só o filtro barato de duração; o gate de energia (numpy sobre
        # MB) roda no worker, fora do event loop
        done: list[tuple[int, bytes, float, _Cedo]] = []
        for user_id, pcm, cedo in ended:
            duration = len(pcm) / BYTES_PER_SECOND
            if duration >= MIN_SPEECH_S:
                done.append((user_id, pcm, now, cedo))
            else:
                _debug(f"🎙️ segmento user={user_id} dur={duration:.2f}s -> curto demais")
        return done

    def cleanup(self) -> None:  # base escreveria arquivos; não gravamos nada
        with self._lock:
            self._buffers.clear()


@dataclass
class _Montagem:
    """Uma pergunta sendo montada: o trecho com o nome + as continuações."""

    pcm: bytearray
    texto: str  # o começo transcrito (só para o chat, se o Gemini não transcrever)
    tarefa: asyncio.Task | None = None  # o despacho agendado
    inicio: float = field(default_factory=time.monotonic)
    fim: float = field(default_factory=time.monotonic)  # quando o último trecho fechou
    retomou_falando: bool = False  # já cortou o bot uma vez para esperar o resto


@dataclass
class _Despachada:
    """A pergunta que acabou de ir ao Gemini — quem perguntou ainda pode completá-la."""

    user_id: int
    texto: str
    pcm: bytes
    fim: float  # quando o último trecho dela fechou
    # Cortar a resposta para esperar o resto só uma vez com o bot já falando: sem
    # fone, o microfone de quem perguntou pega a voz do bot — e cada resposta
    # cortada viraria outra resposta cortada.
    retomou_falando: bool = False
    falando_desde: float | None = None  # quando o bot começa a falar (pode ser no futuro)
    # o trecho que já estava aberto quando a pergunta foi (a pessoa falou sem
    # parar além de _ESPERA_CONTINUACAO_MAX_S): não é "voltou a falar" — sem
    # isto, cada 8s de falatório viravam um pedido cancelado
    ja_aberto: _Buffer | None = None
    buffer: _Buffer | None = None  # o trecho aberto já olhado (só ruído até agora)
    olhado_s: float = 0.0  # quanto dele já foi olhado
    em: float = field(default_factory=time.monotonic)


class VoiceListener:
    """Um por servidor; vive enquanto o bot está na call."""

    def __init__(self, assistente: "Assistente", text_channel) -> None:
        self.assistente = assistente
        self.text_channel = text_channel
        self.active = False
        self._sink: CommandSink | None = None
        # maxsize limita o PCM bruto retido (~8 × 3,8MB); cheia = drop-oldest
        self._segments: asyncio.Queue[tuple[int, bytes, float, _Cedo | None]] = asyncio.Queue(maxsize=8)
        self._tasks: list[asyncio.Task] = []
        self._sobrecarga = 0  # trechos descartados por fila cheia desde o último aviso
        self._sobrecarga_avisada = float("-inf")
        self._chamados: dict[int, float] = {}  # quem disse só o nome → até quando completa
        self._montando: dict[int, _Montagem] = {}
        self._pendentes: dict[int, int] = {}  # trechos de cada pessoa na fila ou em análise
        self._conversa: asyncio.Task | None = None  # o pedido ao Gemini em andamento
        self._despachada: _Despachada | None = None  # a pergunta dele, enquanto dá para completar
        self._checagens: set[asyncio.Task] = set()  # detector do nome rodando em trechos abertos

    async def start(self) -> None:
        self._sink = CommandSink()
        voice = self.assistente.voice
        self._sink.init(voice)  # o start_recording do py-cord 2.8 não faz mais isso
        voice.start_recording(self._sink, self._on_finished)
        self.active = True
        log.info(
            "🎙️ [%s] escuta ligada em %s (motor: %s; detector do nome: %s)",
            self.assistente.guild.id, getattr(voice, "channel", "?"),
            "Groq" if config.GROQ_API_KEY else f"Whisper local {config.WHISPER_MODEL}",
            config.WHISPER_DETECTOR if speech.detector_ligado() else "desligado",
        )
        # um worker só: os trechos de uma pessoa são analisados na ordem em que
        # ela falou — é o que deixa a continuação de uma pergunta entrar junto
        self._tasks = [asyncio.create_task(self._ticker()), asyncio.create_task(self._worker())]

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        log.info("🎙️ [%s] escuta desligada", self.assistente.guild.id)
        voice = self.assistente.voice
        try:
            # is_recording() é MÉTODO no py-cord 2.8 (não existe atributo .recording)
            if voice is not None and voice.is_recording():
                voice.stop_recording()
        except Exception:  # noqa: BLE001 — desligar nunca pode explodir
            pass
        for task in [*self._tasks, *self._checagens]:
            task.cancel()
        self._tasks = []
        self._checagens.clear()
        for montagem in self._montando.values():
            if montagem.tarefa is not None:
                montagem.tarefa.cancel()
        self._montando.clear()
        self._chamados.clear()
        self._pendentes.clear()
        self._despachada = None
        if self._conversa is not None:
            self._conversa.cancel()
            self._conversa = None
        gemini.esquece(self.assistente.guild.id)  # a próxima escuta começa outra conversa
        while not self._segments.empty():  # PCM pendente (MB) não fica preso
            self._segments.get_nowait()

    def _on_finished(self, error: Exception | None = None, *args) -> None:
        """Callback síncrono do AudioReader quando a gravação termina.

        Se a escuta ainda está ligada, a gravação morreu por baixo de nós
        (ex.: erro no PacketRouter) — agenda um reinício.
        """
        if error is not None:
            log.warning("🎙️ [%s] a gravação terminou com erro: %r", self.assistente.guild.id, error)
        if self.active:
            asyncio.run_coroutine_threadsafe(self._restart_recording(), self.assistente.bot.loop)

    async def _restart_recording(self) -> None:
        await asyncio.sleep(0.5)
        if not self.active:
            return
        voice = self.assistente.voice
        if voice is None or not voice.is_connected() or voice.is_recording():
            return
        log.warning("🎙️ gravação caiu — reiniciando a escuta")
        self._sink = CommandSink()
        self._sink.init(voice)
        try:
            voice.start_recording(self._sink, self._on_finished)
        except Exception as exc:  # noqa: BLE001 — tenta de novo no próximo ciclo
            log.warning("🎙️ falha ao reiniciar escuta: %r", exc)

    # --- colheita ---

    async def _ticker(self) -> None:
        from core.pycord_voice_patch import DIAG, DIAG_POR_USUARIO

        ticks = 0
        while self.active:
            await asyncio.sleep(_TICK_S)
            ticks += 1
            for item in self._sink.harvest():
                if self._segments.full():  # sobrecarga: sacrifica o mais velho
                    velho = self._segments.get_nowait()
                    self._solta_pendente(velho[0])
                    self._sobrecarga += 1
                    if time.monotonic() - self._sobrecarga_avisada > 30.0:
                        log.warning(
                            "🎙️ [%s] fila de transcrição cheia: %d trecho(s) descartado(s)"
                            " sem transcrever — o STT não acompanha a conversa",
                            self.assistente.guild.id, self._sobrecarga,
                        )
                        self._sobrecarga_avisada = time.monotonic()
                        self._sobrecarga = 0
                self._pendentes[item[0]] = self._pendentes.get(item[0], 0) + 1
                self._segments.put_nowait(item)
            self._confere_retomada()  # antes do detector: continuação não passa por ele
            self._agenda_checagens()
            if config.VOICE_DEBUG and ticks % int(5 / _TICK_S) == 0:  # diagnóstico a cada 5s
                # de quem é o áudio que o DAVE não abriu (vira silêncio): só
                # quem teve falha, como descriptografados/falhas
                falhas = {
                    user_id: f"{ok}/{falhou}"
                    for user_id, (ok, falhou) in dict(DIAG_POR_USUARIO).items() if falhou
                }
                log.info(
                    "🎙️ diag: sink_writes=%d sink_kb=%d %s dave_falhas_por_pessoa=%s",
                    self._sink.diag_writes, self._sink.diag_bytes // 1024, DIAG, falhas,
                )

    def _solta_pendente(self, user_id: int) -> None:
        restantes = self._pendentes.get(user_id, 0) - 1
        if restantes > 0:
            self._pendentes[user_id] = restantes
        else:
            self._pendentes.pop(user_id, None)

    # --- o nome, enquanto a pessoa ainda fala ---

    def _agenda_checagens(self) -> None:
        """Manda o começo de quem está falando AGORA ao detector — o nome dito
        dispara o estalo antes de a frase acabar."""
        if self._sink is None or not speech.detector_ligado():
            return
        for user_id, buffer, falado_s, parado_s in self._sink.abertos():
            cedo = buffer.cedo
            if cedo.achou is not None or cedo.tarefa is not None or user_id in self._montando:
                continue  # já sabe, já está olhando, ou é continuação de pergunta
            if cedo.checado_s >= _CEDO_ATE_S:
                cedo.achou = False
                continue
            alvo = _CEDO_PRIMEIRO_S if not cedo.checado_s else cedo.checado_s + _CEDO_PASSO_S
            pausou = parado_s >= _CEDO_PAUSA_S and falado_s >= max(_CEDO_MIN_S, cedo.checado_s + 0.1)
            if falado_s + 0.01 >= alvo or pausou:  # 0,01: 0,8 + 0,4 dá 1,2000000000000002
                tarefa = asyncio.create_task(self._checa_cedo(user_id, buffer))
                cedo.tarefa = tarefa
                self._checagens.add(tarefa)
                tarefa.add_done_callback(self._checagens.discard)

    async def _checa_cedo(self, user_id: int, buffer: _Buffer) -> None:
        cedo = buffer.cedo
        try:
            if self._sink is None:
                return
            pcm = self._sink.comeco(buffer, int(_CEDO_ATE_S * BYTES_PER_SECOND))
            falado = len(pcm) / BYTES_PER_SECOND
            hot, aparado = await asyncio.to_thread(analyze_speech, pcm, config.VOICE_MIN_RMS)
            if hot < _MIN_SPEECH_WINDOWS:
                cedo.checado_s = falado  # só ruído até aqui
                return
            inicio = time.monotonic()
            texto = await speech.detecta(aparado, forte=cedo.checado_s > 0 and speech.model_ready())
            cedo.checado_s, cedo.texto = falado, texto
            comando = parse_command(texto) if texto else None
            if comando is not None and chamou_de_frente(texto):
                cedo.achou = True
                log.info(
                    "🎙️ [%s] ouvi o nome com %.1fs de fala (detector em %.2fs): estalo já",
                    user_id, falado, time.monotonic() - inicio,
                )
                self._toca_estalo()
            elif comando is not None or len(texto.split()) > _PALAVRAS_ATE_O_NOME:
                # o nome no meio da frase (conversa), ou palavras demais sem ele
                cedo.achou = False
        except Exception:  # noqa: BLE001 — a checagem é atalho; o fim do trecho decide
            log.warning("🎙️ a checagem antecipada do nome falhou", exc_info=True)
        finally:
            cedo.tarefa = None

    def _toca_estalo(self) -> None:
        try:
            self.assistente.tocar_efeito(audio.efeito_escuta())  # "estou ouvindo" (e cala o bot)
        except Exception:  # noqa: BLE001 — o som nunca impede o pedido
            log.warning("🎙️ não consegui tocar o som de escuta", exc_info=True)

    # --- o trecho terminado ---

    async def _worker(self) -> None:
        while self.active:
            user_id, pcm, born, cedo = await self._segments.get()
            try:
                await self._processa(user_id, pcm, born, cedo)
            except Exception:  # noqa: BLE001 — worker não pode morrer
                log.exception("🎙️ erro ao processar um trecho de fala")
            finally:
                self._solta_pendente(user_id)

    async def _processa(self, user_id: int, pcm: bytes, born: float, cedo: _Cedo | None = None) -> None:
        duracao = len(pcm) / BYTES_PER_SECOND
        if time.monotonic() - born > STALE_SEGMENT_S:
            log.info("🎙️ [%s] trecho de %.1fs descartado: transcrição atrasada", user_id, duracao)
            return
        bruto = pcm
        hot, pcm = await asyncio.to_thread(analyze_speech, pcm, config.VOICE_MIN_RMS)
        if hot < _MIN_SPEECH_WINDOWS or len(pcm) / BYTES_PER_SECOND < _FALA_APARADA_MIN_S:
            if duracao >= _LOG_DISCARD_MIN_S:
                # o pico diz se foi fala baixa (perto do limiar) ou silêncio
                pico = await asyncio.to_thread(peak_rms, bruto)
                perto = hot >= 1 or pico >= 0.8 * config.VOICE_MIN_RMS
                log.log(
                    logging.INFO if perto else logging.DEBUG,
                    "🎙️ [%s] trecho de %.1fs descartado como ruído"
                    " (%d janelas com voz, pico RMS %.0f, limiar VOICE_MIN_RMS=%.0f)",
                    user_id, duracao, hot, pico, config.VOICE_MIN_RMS,
                )
            else:
                _debug(f"🎙️ trecho de user={user_id} descartado (ruído, {hot} janelas)")
            return
        if config.VOICE_DEBUG:  # WAV de MBs: só opt-in, e fora do loop
            await asyncio.to_thread(_save_debug_wav, user_id, pcm)
        if user_id in self._montando:
            # a pessoa continuou a pergunta depois de uma pausa: entra junto,
            # sem nem transcrever — quem entende é o Gemini
            self._junta(user_id, pcm, born)
            return

        if cedo is not None and cedo.tarefa is not None:
            # a frase acabou com o detector ainda olhando o começo: espera (é rápido)
            await asyncio.wait({cedo.tarefa})
        # o nome ouvido ANTES de a frase acabar: o estalo já tocou
        ja_tocou = cedo is not None and cedo.achou is True
        inicio = time.monotonic()
        if ja_tocou and len(pcm) / BYTES_PER_SECOND > _CURTO_S:
            # frase longa: é pergunta (comando é curto) — nada de transcrever de
            # novo, o áudio inteiro vai direto ao Gemini
            text, comando = cedo.texto, ("pergunta", "")
        else:
            # o fim do trecho decide com o Whisper maior (ou a Groq): é a rede
            # de segurança do detector, e quem lê os comandos curtos inteiros
            try:
                text = await speech.transcribe(cabeca(pcm))
            except Exception as exc:  # noqa: BLE001 — trecho ruim não derruba a escuta
                log.warning("🎙️ erro na transcrição: %r", exc)
                if not ja_tocou:
                    return
                text = ""
            comando = parse_command(text) if text else None
            if comando is None and ja_tocou:
                # o detector ouviu o nome e a transcrição final não: vale o detector
                text = cedo.texto
                comando = parse_command(text) or ("pergunta", "")
        elapsed = time.monotonic() - inicio
        if config.VOICE_DEBUG:
            log.info("🎙️ [%s] transcrito em %.1fs: %r → %s", user_id, elapsed, text, comando)
        else:
            # sem o conteúdo: fala da call não vira log permanente (privacidade)
            log.info(
                "🎙️ [%s] transcrito em %.1fs (%d chars, %.1fs de fala, %s%s)",
                user_id, elapsed, len(text or ""), len(pcm) / BYTES_PER_SECOND,
                comando[0] if comando else "sem o nome", ", nome ouvido cedo" if ja_tocou else "",
            )

        prazo = self._chamados.pop(user_id, 0.0)
        if comando is None:
            if prazo > time.monotonic():
                if text and not is_stt_noise(text):
                    # disse só o nome antes: este trecho é a pergunta
                    self._comeca_pergunta(user_id, text, pcm, born)
                else:
                    # o Whisper inventa "Tchau." sobre ruído: não gasta a janela
                    self._chamados[user_id] = prazo
            elif text:
                # só a primeira palavra (a tentativa de nome), nunca o conteúdo
                log.info("🎙️ [%s] sem o nome (começa com %r)", user_id, first_word(text))
            return

        acao, _frase = comando
        if acao == "pergunta" and not ja_tocou and not chamou_de_frente(text):
            # nome no MEIO da frase ("não, do jarvis lá"): era conversa entre as
            # pessoas, não pedido — ninguém quer o bot se metendo
            log.info("🎙️ [%s] nome no meio da frase: era conversa", user_id)
            return
        if not ja_tocou:
            self._toca_estalo()
        if acao == "chamou":
            self._interrompe_conversa()
            self._chamados[user_id] = time.monotonic() + _CHAMADO_S
        elif acao == "cala":
            self._interrompe_conversa()
        elif acao == "sai":
            await self._sai(user_id)
        else:
            self._comeca_pergunta(user_id, text, pcm, born)

    # --- a pergunta sendo montada ---

    def _comeca_pergunta(self, user_id: int, texto: str, pcm: bytes, fim: float | None = None) -> None:
        """`fim`: quando o trecho fechou (o bot não fala antes de _JUNTA_S depois disso)."""
        self._montando[user_id] = _Montagem(
            pcm=bytearray(pcm), texto=texto, fim=time.monotonic() if fim is None else fim,
        )
        self._agenda_despacho(user_id)

    def _junta(self, user_id: int, pcm: bytes, fim: float | None = None) -> None:
        montagem = self._montando[user_id]
        montagem.pcm += _PAUSA_ENTRE_TRECHOS + pcm
        montagem.fim = time.monotonic() if fim is None else fim
        log.info("🎙️ [%s] a pergunta continuou depois de uma pausa: juntei", user_id)
        if len(montagem.pcm) / BYTES_PER_SECOND >= _PERGUNTA_MAX_S:
            self._despacha(user_id)
        else:
            self._agenda_despacho(user_id)

    def _agenda_despacho(self, user_id: int) -> None:
        montagem = self._montando[user_id]
        if montagem.tarefa is not None:
            montagem.tarefa.cancel()
        montagem.tarefa = asyncio.create_task(self._despacha_quando_terminar(user_id))

    def _continua_falando(self, user_id: int) -> bool:
        """Tem mais fala dessa pessoa a caminho? (trecho aberto ou na fila)"""
        aberto = self._sink is not None and self._sink.trecho_aberto(user_id)
        return aberto or self._pendentes.get(user_id, 0) > 0

    async def _despacha_quando_terminar(self, user_id: int) -> None:
        # sem esperar _JUNTA_S aqui: a pergunta vai já e o Gemini pensa enquanto
        # isso — é a fala dele que espera (FalaAoVivo.nao_antes), e quem volta a
        # falar retoma a pergunta (_confere_retomada)
        limite = time.monotonic() + _ESPERA_CONTINUACAO_MAX_S
        while self._continua_falando(user_id) and time.monotonic() < limite:
            await asyncio.sleep(0.05)  # a continuação chega pelo worker (_junta)
        self._despacha(user_id)

    def _despacha(self, user_id: int) -> None:
        montagem = self._montando.pop(user_id, None)
        if montagem is None:
            return
        if montagem.tarefa is not None and montagem.tarefa is not asyncio.current_task():
            montagem.tarefa.cancel()
        pcm = bytes(montagem.pcm)
        self._conversa_nova(user_id, montagem.texto, pcm, fim=montagem.fim)
        # depois de _conversa_nova: ela esquece a pergunta anterior
        self._despachada = _Despachada(
            user_id, montagem.texto, pcm, montagem.fim, retomou_falando=montagem.retomou_falando,
            ja_aberto=self._sink.trecho_de(user_id) if self._sink is not None else None,
        )

    # --- quem perguntou voltou a falar ---

    def _confere_retomada(self) -> None:
        """A pergunta já foi ao Gemini e quem perguntou voltou a falar: ele não
        tinha terminado. Cancela o pedido e volta a montar a pergunta — o resto
        dela entra junto quando o trecho novo fechar (_junta)."""
        despachada = self._despachada
        if despachada is None or self._sink is None:
            return
        agora = time.monotonic()
        desde = despachada.falando_desde
        falando = desde is not None and agora >= desde
        if agora - despachada.em > _RETOMA_MAX_S or (
            desde is not None and falando
            and (despachada.retomou_falando or agora - desde > _RETOMA_FALANDO_S)
        ):
            self._despachada = None  # a resposta já vai longe: fala nova é reação
            return
        if despachada.user_id in self._montando:
            return  # já está montando outra pergunta dele
        for user_id, buffer, falado_s, _parado_s in self._sink.abertos():
            if user_id != despachada.user_id or buffer is despachada.ja_aberto:
                continue
            if buffer is not despachada.buffer:
                despachada.buffer, despachada.olhado_s = buffer, 0.0
            if falado_s < max(_RETOMA_MIN_S, despachada.olhado_s + _RETOMA_PASSO_S):
                return
            # só o que chegou desde a última olhada (e um pouco antes, para não
            # partir uma sílaba): numpy no event loop, então pouco de cada vez
            inicio = int(max(0.0, despachada.olhado_s - _RETOMA_PASSO_S) * BYTES_PER_SECOND)
            despachada.olhado_s = falado_s
            hot, _aparado = analyze_speech(self._sink.desde(buffer, inicio - inicio % 4), config.VOICE_MIN_RMS)
            if hot >= _MIN_SPEECH_WINDOWS:
                self._retoma(despachada, falando)
            return

    def _retoma(self, despachada: _Despachada, falando: bool) -> None:
        self._interrompe_conversa()  # cala o bot (se já falava) e cancela o pedido
        self._montando[despachada.user_id] = _Montagem(
            pcm=bytearray(despachada.pcm), texto=despachada.texto, fim=despachada.fim,
            retomou_falando=despachada.retomou_falando or falando,
        )
        self._agenda_despacho(despachada.user_id)
        log.info(
            "🎙️ [%s] voltou a falar logo depois da pergunta%s: esperando o resto",
            despachada.user_id, " (cortei a resposta)" if falando else "",
        )

    # --- a conversa com o Gemini ---

    def _conversa_nova(self, user_id: int, texto: str, pcm: bytes, *, fim: float | None = None) -> None:
        """Pede ao Gemini em SEGUNDO PLANO: a resposta chega no ritmo da fala (10s
        ou mais) e, com o worker preso nela, um "Jarvis, para" no meio da resposta
        só seria ouvido depois do fim. Um pedido novo interrompe o anterior.

        `fim`: quando a fala da pergunta acabou de chegar (o trecho fechou) — o
        bot não começa a falar antes de _JUNTA_S depois disso."""
        self._interrompe_conversa()
        self._conversa = asyncio.create_task(self._conversa_protegida(user_id, texto, pcm, fim))

    def _interrompe_conversa(self) -> bool:
        """Cala o bot e cancela o pedido em andamento. True = ele falava ou pensava."""
        self._despachada = None  # sem pedido, não há o que retomar
        tarefa, self._conversa = self._conversa, None
        pensando = (
            tarefa is not None and not tarefa.done() and tarefa is not asyncio.current_task()
        )
        if pensando:
            tarefa.cancel()
        return self.assistente.cala() or pensando

    async def _conversa_protegida(self, user_id: int, texto: str, pcm: bytes, fim: float | None = None) -> None:
        try:
            await self._pergunta_ao_gemini(user_id, texto, pcm, fim)
        except Exception:  # noqa: BLE001 — tarefa solta não pode morrer calada
            log.exception("🗣️ [%s] erro na conversa com o Gemini", user_id)

    async def _pergunta_ao_gemini(self, user_id: int, texto: str, pcm: bytes, fim: float | None = None) -> None:
        """A pergunta (o ÁUDIO, não a transcrição) vai ao Gemini; a resposta toca
        enquanto chega. O áudio e não o texto: o Whisper local erra muito, e o
        Gemini entende a fala melhor do que qualquer transcrição."""
        inicio = time.monotonic()
        nao_antes = fim + _JUNTA_S if fim is not None else 0.0  # quem só respirou não é atropelado
        # a pergunta desta conversa, enquanto ainda dá para completá-la (_despacha
        # a anota logo depois de criar esta tarefa, antes de ela rodar)
        minha = self._despachada if self._despachada and self._despachada.user_id == user_id else None
        quem = await self.assistente.nome_de(user_id)
        fala: audio.FalaAoVivo | None = None
        sem_voz = False  # o bot saiu da call no meio: a resposta fica só no chat
        comecou_s: float | None = None
        falou_em: float | None = None  # quando a fala começa de fato (o áudio chegou e passou de nao_antes)

        def ao_falar(pedaco: bytes) -> None:
            nonlocal fala, sem_voz, comecou_s, falou_em
            if fala is None and not sem_voz:
                agora = time.monotonic()
                comecou_s, falou_em = agora - inicio, max(agora, nao_antes)
                fala = self.assistente.fala_ao_vivo(nao_antes=nao_antes)
                sem_voz = fala is None
                if minha is not None:
                    minha.falando_desde = falou_em  # a janela de retomada fecha a partir daqui
            if fala is not None:
                fala.escreve(pedaco)

        try:
            try:
                resposta = await gemini.responde(
                    self.assistente.guild.id, pcm, quem=quem, ao_falar=ao_falar,
                    na_call=self.assistente.nomes_na_call, user_id=user_id,
                )
            except Exception as exc:  # noqa: BLE001 — a segunda chance decide abaixo
                if comecou_s is not None or time.monotonic() - inicio > _SEGUNDA_CHANCE_ATE_S:
                    raise
                # recusou de cara, antes de falar (27/09: "1007 Precondition check
                # failed" logo depois de duas trocas sem sentido na memória): de
                # novo, uma vez, com a conversa zerada
                log.warning(
                    "🗣️ [%s] o Gemini recusou (%s: %s) — tentando de novo sem a memória da conversa",
                    user_id, type(exc).__name__, exc,
                )
                gemini.esquece(self.assistente.guild.id)
                resposta = await gemini.responde(
                    self.assistente.guild.id, pcm, quem=quem, ao_falar=ao_falar,
                    na_call=self.assistente.nomes_na_call, user_id=user_id,
                )
        except Exception as exc:  # noqa: BLE001 — pergunta perdida não derruba a escuta
            # %s e não %r: nunca arriscar a chave num repr de erro de rede
            log.warning("🗣️ [%s] o Gemini falhou (%s: %s)", user_id, type(exc).__name__, exc)
            if comecou_s is None:  # já falando: o que chegou fica, sem aviso de erro
                await self.text_channel.send(m.VOICE_ANSWER_FAILED)
            return
        finally:
            if fala is not None:
                fala.termina()
            if minha is not None and self._despachada is minha:
                # respondida (ou perdida): o que a pessoa disser agora é conversa
                # nova — o modo conversa, com a memória da troca, cuida dela
                self._despachada = None
        # sem o conteúdo no journal (privacidade): a conversa vai só para o chat
        # "depois do fim da pergunta": o que quem perguntou sente — o trecho
        # fecha VOICE_GAP_S depois do último som dele
        espera = (
            f", ~{falou_em - (fim - config.VOICE_GAP_S):.1f}s depois do fim da pergunta"
            if falou_em is not None and fim is not None else ""
        )
        log.info(
            "🗣️ [%s] Gemini respondeu: começou a falar em %s%s, %.1fs de fala, %.1fs no total%s",
            user_id,
            f"{comecou_s:.1f}s" if comecou_s is not None else "—",
            espera,
            resposta.segundos_de_fala,
            time.monotonic() - inicio,
            " (sem voz: fora da call)" if sem_voz else "",
        )
        if config.VOICE_DEBUG:  # o conteúdo só no diagnóstico: é a conversa das pessoas
            log.info(
                "🗣️ [%s] o Gemini ouviu %r e respondeu %r", user_id, resposta.pergunta, resposta.texto,
            )
        for segundos, lembrete in resposta.lembretes:
            self._agenda_lembrete(user_id, segundos, lembrete)
        if resposta.quer_sair:
            # mandaram o bot embora de um jeito que a lista curta não pega
            # ("ninguém te chamou, vaza"): o Gemini entendeu e se despediu
            log.info("🎙️ [%s] o Gemini entendeu que era para sair da call", user_id)
            if resposta.texto and config.RESPOSTAS_NO_CHAT:
                await self.text_channel.send(m.VOICE_ANSWER.format(
                    quem=quem or "Alguém",
                    pergunta=(resposta.pergunta or texto)[:300],
                    resposta=resposta.texto[:1500],
                ))
            if fala is not None:
                limite = time.monotonic() + _DESPEDIDA_MAX_S
                while fala.ativa() and time.monotonic() < limite:
                    await asyncio.sleep(0.1)  # a despedida toca inteira antes de sair
            if self._conversa is asyncio.current_task():
                self._conversa = None  # sair desliga a escuta: não cancela esta tarefa no meio
            await self._sai(user_id)
            return
        if resposta.segundos_de_fala and config.VOICE_CONVERSA_S > 0 and not sem_voz:
            # modo conversa: quem perguntou continua o papo SEM repetir o nome —
            # a próxima fala dela nesta janela é pergunta (a fala chega no ritmo
            # dela, então a resposta está acabando de tocar agora)
            self._chamados[user_id] = time.monotonic() + config.VOICE_CONVERSA_S
        if not resposta.texto and not resposta.segundos_de_fala:
            await self.text_channel.send(m.VOICE_ANSWER_FAILED)
        elif resposta.texto and config.RESPOSTAS_NO_CHAT:
            await self.text_channel.send(m.VOICE_ANSWER.format(
                quem=quem or "Alguém",
                pergunta=(resposta.pergunta or texto)[:300],
                resposta=resposta.texto[:1500],
            ))

    def _agenda_lembrete(self, user_id: int, segundos: float, texto: str) -> None:
        """"Jarvis, me avisa em 10 minutos": daqui a `segundos`, marca a pessoa no chat."""
        lembretes.agenda(self.text_channel, lembretes.Lembrete(
            guild_id=self.assistente.guild.id,
            user_id=user_id,
            channel_id=int(getattr(self.text_channel, "id", 0) or 0),
            texto=texto or m.REMINDER_SEM_TEXTO,
            vence_em=time.time() + segundos,
        ))
        log.info("⏰ [%s] lembrete agendado para daqui a %ds", user_id, segundos)

    # --- sair ---

    async def _sai(self, user_id: int) -> None:
        await self.assistente.espera_efeito()
        # a mensagem ANTES: sair desliga esta escuta, e este worker junto
        try:
            await self.text_channel.send(m.VOICE_BYE)
        except Exception:  # noqa: BLE001 — a despedida não impede a saída
            log.warning("🎙️ não consegui me despedir no chat", exc_info=True)
        await self.assistente.sai(f"pedido por voz de {user_id}")
