"""Transcrição de fala: Groq (nuvem, se houver chave) com fallback local.

Local: faster-whisper tiny/int8 — rápido mas fraco com pt+inglês misturado.
Groq: whisper-large-v3-turbo — excelente com code-switching, ~1s, tier grátis.
"""
from __future__ import annotations

import asyncio
import functools
import gc
import io
import logging
import threading
import time
import wave
from collections import deque

import numpy as np

import config
from services import http

log = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

_model = None
_model_lock = threading.Lock()
_preload_task = None  # carga do modelo local em background (nunca no comando)

# O Groq se recupera de 429 em ~1-2s; o fallback local custa 46s FRIO na VM
# de produção (medido) e o trecho é descartado por STALE_SEGMENT_S antes
# disso. Insistir na nuvem é sempre melhor que cair para o local.
_GROQ_TENTATIVAS = 3

# Caps GLOBAIS — uma API key e uma CPU para TODOS os servidores. Cada guild
# tem 3 workers de escuta (core/listen.py); sem isso, N guilds viravam 3N
# requests simultâneos no Groq (429) e um stampede de Whisper local.
_groq_sem = asyncio.Semaphore(3)
_local_sem = asyncio.Semaphore(1)  # whisper local é CPU-bound (GIL): um por vez

_agora = time.monotonic  # indireção para os testes controlarem o relógio
# Acima disto, o fallback local custa mais do que vale: no journal de 16/09
# ele levou de 83 a 131 SEGUNDOS por frase nesta VM de um núcleo, engasgando
# a música e entregando o comando quando ninguém mais esperava. A primeira
# transcrição que passar do limite desliga o local pelo resto da execução.
_LOCAL_LENTO_S = 10.0
_local_lento = False

# ── orçamento da Groq ────────────────────────────────────────────────────────
# O tier grátis do Whisper dá 20 requisições por minuto, e TODA fala da call
# vira requisição (98% sem wake word, medido em 7.069 transcrições). No journal
# o pico foi exatamente 20/min: 112 falhas por 429 e 51 comandos perdidos — o
# 429 escolhe a vítima ao acaso. Dos 80 trechos que eram comando, o mais longo
# tinha 5,6s (p90 3,9s); dos que não eram, 300 passavam de 6s. Com o minuto
# acabando, quem fica de fora é o trecho comprido, que nunca é comando.
_COMANDO_LONGO_S = 6.0  # minuto apertado (75% do teto): acima disto não vai
_COMANDO_CURTO_S = 4.0  # no teto: só o que tem cara de comando
_ESPERA_MAX_429_S = 6.0  # quanto um trecho curto espera, no total, a Groq reabrir
_envios: deque[float] = deque()  # instantes das requisições do último minuto

# contadores para o retrato de saúde (services/observabilidade): zeram na leitura
_estatisticas = {"ok": 0, "429": 0, "falhas": 0, "perdidos": 0, "poupados": 0}
_aviso_de_limite_em = float("-inf")


def leu_estatisticas() -> dict[str, int]:
    """Contadores de STT desde a última leitura (e zera)."""
    lido = dict(_estatisticas)
    for chave in _estatisticas:
        _estatisticas[chave] = 0
    return lido


def _conta_envio() -> None:
    _envios.append(_agora())


def _no_ultimo_minuto() -> int:
    limite = _agora() - 60.0
    while _envios and _envios[0] < limite:
        _envios.popleft()
    return len(_envios)


def _teto_de_duracao() -> tuple[float | None, int]:
    """(duração máxima que ainda vale enviar, requisições no último minuto)."""
    usados, rpm = _no_ultimo_minuto(), config.GROQ_STT_RPM
    if rpm <= 0:
        return None, usados
    if usados >= rpm - 1:
        return _COMANDO_CURTO_S, usados
    if usados >= rpm * 0.75:
        return _COMANDO_LONGO_S, usados
    return None, usados


def _limites_do_429(headers) -> str:
    """Os cabeçalhos de limite da Groq: dizem QUAL teto estourou (por minuto,
    por dia ou segundos de áudio por hora) e quando reabre."""
    achados = []
    for nome, valor in headers.items():
        baixo = str(nome).lower()
        if baixo == "retry-after" or baixo.startswith("x-ratelimit-"):
            achados.append(f"{baixo}={valor}")
    return " ".join(sorted(achados)) or "sem cabeçalhos de limite"


def _get_model():
    global _model
    if _model is None:
        with _model_lock:  # chamado de threads do executor: sem lock, double-load
            if _model is None:
                from faster_whisper import WhisperModel

                _model = WhisperModel(
                    config.WHISPER_MODEL, device="cpu", compute_type="int8"
                )
    return _model


def preload() -> None:
    """Carrega o modelo E faz uma inferência de aquecimento — a primeira
    transcrição real custa caro (alocação/threads); melhor pagar no boot."""
    _get_model()
    _transcribe_sync(np.zeros(8000, dtype=np.float32))


def model_ready() -> bool:
    """O Whisper local já está carregado? (carregar leva ~46s na VM)"""
    return _model is not None


def _ensure_preload_started() -> None:
    """Começa a carregar o modelo local em BACKGROUND, no máximo uma vez."""
    global _preload_task
    if _model is not None:
        return
    if _preload_task is not None and not _preload_task.done():
        return
    log.info("🎙️ carregando o Whisper local em background (~46s na VM)")
    _preload_task = asyncio.get_running_loop().create_task(
        asyncio.to_thread(preload)
    )


def pcm_to_float_mono_16k(pcm: bytes) -> np.ndarray:
    """PCM 48kHz stereo s16le (formato do Discord) → float32 mono 16kHz.

    48k→16k via média de blocos de 3 amostras (filtro-caixa + decimação):
    decimação crua ([::3]) gera aliasing que atrapalha o Whisper.
    """
    arr = np.frombuffer(pcm, dtype=np.int16)
    arr = arr[: len(arr) - (len(arr) % 2)].reshape(-1, 2).mean(axis=1)
    arr = arr[: len(arr) - (len(arr) % 3)].reshape(-1, 3).mean(axis=1)
    return (arr / 32768.0).astype(np.float32)


# Decodificação gulosa: sem temperature=0 o Whisper refaz a transcrição "com
# mais criatividade" quando duvida de si — e o tempo dispara (855 ms em vez de
# ~250 no tiny, medido em 27/09). Sem timestamps: ninguém usa, e custa.
_RAPIDO = dict(
    language="pt", beam_size=1, temperature=0.0, without_timestamps=True,
    condition_on_previous_text=False,
)


def _transcribe_sync(audio: np.ndarray) -> str:
    # sem vad_filter (nosso gate de RMS já corta silêncio) e sem
    # initial_prompt/hotwords (o Whisper alucina o prompt de volta sobre ruído:
    # com hotwords="Jarvis", "Já vi esse filme" virou "Jarvis e filme")
    segments, _info = _get_model().transcribe(audio, **_RAPIDO)
    return " ".join(seg.text.strip() for seg in segments).strip()


# ── o detector do nome: o COMEÇO da frase, enquanto a pessoa ainda fala ──────
# Medido em 27/09 (Ryzen de 12 threads; 4 vozes com o áudio comprimido como no
# Discord; nome comparado pelo som): nos primeiros 0,8s da frase, o tiny leva
# ~250 ms e achou o nome em 20 de 24 frases — o base, ~480 ms e 15 de 24. Na
# frase inteira o base é melhor (24 de 24), por isso ele continua sendo a rede
# de segurança no fim do trecho. O detector é SEMPRE local (a Groq tem teto de
# 20 requisições por minuto) e tem fila própria: nunca espera atrás do base.
_detector = None
_detector_lock = threading.Lock()
_detector_sem = asyncio.Semaphore(1)
# Lento, o detector é pior que nenhum. Medido em 27/09 na e2-micro do Google
# (0,25 vCPU garantida): 1,1s por olhada com a call calma e 110s com duas
# pessoas falando — as olhadas empilham na fila dele, a escuta espera cada uma
# e a pergunta chega velha ("transcrição atrasada"). A primeira olhada que
# passar disto o desliga pelo resto da execução (num PC ela leva ~0,25s).
_DETECTOR_LENTO_S = 1.0
_detector_lento = False
_DETECTOR_OFF = ("0", "off", "false", "no", "nao", "não", "nenhum", "desligado")


def detector_ligado() -> bool:
    """O detector do nome está rodando? (WHISPER_DETECTOR=off desliga; lento, desliga sozinho)"""
    return not _detector_lento and config.WHISPER_DETECTOR.lower() not in _DETECTOR_OFF


def _get_detector():
    global _detector
    if _detector is None:
        with _detector_lock:
            if _detector is None:
                from faster_whisper import WhisperModel

                _detector = WhisperModel(config.WHISPER_DETECTOR, device="cpu", compute_type="int8")
    return _detector


def _detecta_sync(audio: np.ndarray) -> str:
    segments, _info = _get_detector().transcribe(audio, **_RAPIDO)
    return " ".join(seg.text.strip() for seg in segments).strip()


def preload_detector() -> None:
    """Carrega o detector e aquece (a primeira inferência paga alocação)."""
    _detecta_sync(np.zeros(8000, dtype=np.float32))


async def detecta(pcm: bytes, *, forte: bool = False) -> str:
    """Transcrição do começo de uma frase (PCM do Discord) — só para achar o nome.

    `forte`: com o Whisper maior (já carregado) em vez do tiny. É para a segunda
    olhada em diante, com mais áudio: onde o tiny erra sempre do mesmo jeito
    ("Já do que você", "Já viscou"), o base com 1,2s de contexto acerta.
    """
    audio = await asyncio.to_thread(pcm_to_float_mono_16k, pcm)
    if len(audio) < 1600:  # < 0.1s: ruído
        return ""
    semaforo, funcao = (_local_sem, _transcribe_sync) if forte else (_detector_sem, _detecta_sync)
    async with semaforo:
        if not forte and not detector_ligado():
            return ""  # desligado enquanto esta olhada esperava a fila: nem recarrega o modelo
        carregado = _detector is not None  # carregar o modelo não é lentidão
        comeco = _agora()
        texto = await asyncio.get_running_loop().run_in_executor(None, funcao, audio)
        gasto = _agora() - comeco
    if not forte and carregado and gasto > _DETECTOR_LENTO_S:
        _desliga_detector(gasto, len(audio) / 16000)
    return texto


def _desliga_detector(gasto: float, falado: float) -> None:
    global _detector, _detector_lento
    if _detector_lento:
        return
    _detector_lento = True
    with _detector_lock:
        _detector = None  # a RAM do modelo volta (numa VM de 1 GB, faz falta)
    gc.collect()
    log.warning(
        "🎙️ o detector do nome levou %.1fs para %.1fs de fala nesta máquina (num PC,"
        " ~0,25s) — desligado: lento assim ele só atrasa as perguntas. O nome passa"
        " a ser procurado no fim da frase%s.",
        gasto, falado, ", pela Groq" if config.GROQ_API_KEY else "",
    )


def _to_wav_bytes(audio: np.ndarray) -> bytes:
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(pcm16.tobytes())
    return buffer.getvalue()


def _retry_delay(status: int, retry_after: str | None) -> float | None:
    """Atraso antes de retentar o Groq, ou None se o status não merece retry.

    Cap de 2s: segmentos além de STALE_SEGMENT_S são descartados no worker —
    esperar mais só produziria transcrição de fala velha.
    """
    if status not in (429, 500, 502, 503):
        return None
    try:
        return min(float(retry_after or 1.0), 2.0)
    except ValueError:
        return 1.0


def _espera_do_429(retry_after: str | None, paciente: bool, ja_esperou: float) -> float | None:
    """Quanto esperar depois de um 429, ou None para desistir.

    O teto da Groq é por MINUTO e o `retry-after` diz quanto falta. Trecho com
    cara de comando (curto) espera o que ela pedir, até _ESPERA_MAX_429_S no
    total: comando atrasado é melhor que comando perdido (foram 51 em cinco
    dias). Trecho longo desiste já — segurar um dos 3 workers da escuta por 6s
    em fala que quase nunca é comando derruba da fila o comando de verdade. E
    um `retry-after` maior que o orçamento é teto diário/de áudio: esperar 6s
    não resolve.
    """
    if not paciente:
        return None
    try:
        pedido = max(0.1, float(retry_after or 1.0))
    except ValueError:
        pedido = 1.0
    if ja_esperou + pedido > _ESPERA_MAX_429_S:
        return None
    return pedido


async def _transcribe_groq(audio: np.ndarray) -> str:
    import aiohttp  # dependência do py-cord, já instalada

    wav = await asyncio.to_thread(_to_wav_bytes, audio)
    paciente = len(audio) / 16000 <= _COMANDO_CURTO_S
    ja_esperou = 0.0  # em 429, somado
    tentativas_5xx = 0
    delay: float | None = None
    while True:
        if delay is not None:
            await asyncio.sleep(delay)  # fora do semáforo: os outros seguem
        # FormData não é reutilizável entre requests: monta a cada tentativa
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", "whisper-large-v3-turbo")
        if config.VOICE_STT_PROMPT:
            # bias de vocabulário: inclina o Whisper a transcrever o nome do bot
            # certo (o aviso de alucinação lá embaixo é sobre o initial_prompt do
            # whisper LOCAL — o large-v3 da Groq lida bem)
            form.add_field("prompt", config.VOICE_STT_PROMPT)
        if config.VOICE_IDIOMA:
            # adivinhando o idioma, 20 de 74 falas de uma call brasileira saíram
            # em outro (27/09): russo, islandês, lituano, inglês inventado…
            # Com o idioma fixo, palavras em inglês no meio continuam saindo
            form.add_field("language", config.VOICE_IDIOMA)
        async with _groq_sem:
            _conta_envio()  # cada tentativa gasta do minuto, inclusive o retry
            async with http.get_session().post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                retry_after = resp.headers.get("Retry-After")
                if resp.status == 429:
                    _avisa_limite(resp.headers)
                    delay = _espera_do_429(retry_after, paciente, ja_esperou)
                    ja_esperou += delay or 0.0
                else:
                    # 5xx: soluço da Groq, some em 1–2s — as 3 tentativas curtas
                    delay = _retry_delay(resp.status, retry_after)
                    tentativas_5xx += 1
                    if tentativas_5xx >= _GROQ_TENTATIVAS:
                        delay = None
                if delay is None:
                    resp.raise_for_status()
                    data = await resp.json()
                    return (data.get("text") or "").strip()


def _avisa_limite(headers) -> None:
    """429 da Groq: conta sempre, explica no log no máximo uma vez por minuto."""
    global _aviso_de_limite_em
    _estatisticas["429"] += 1
    if _agora() - _aviso_de_limite_em < 60.0:
        return
    _aviso_de_limite_em = _agora()
    log.warning(
        "🎙️ Groq devolveu 429 com %d requisições no último minuto — %s",
        _no_ultimo_minuto(), _limites_do_429(headers),
    )


async def _transcribe_local(audio: np.ndarray) -> str:
    """Whisper local, cronometrado: se for lento demais aqui, é a última vez."""
    global _local_lento
    loop = asyncio.get_running_loop()
    async with _local_sem:  # N guilds caindo para o local viram fila, não stampede
        comeco = _agora()
        texto = await loop.run_in_executor(
            None, functools.partial(_transcribe_sync, audio)
        )
        gasto = _agora() - comeco
    if config.GROQ_API_KEY and not _local_lento and gasto > _LOCAL_LENTO_S:
        _local_lento = True
        _solta_o_modelo()
        log.warning(
            "🎙️ o Whisper local levou %.0fs para %.1fs de fala nesta máquina —"
            " desligando o fallback local (ele satura a CPU e corta a música)."
            " Os comandos de voz passam a depender só do Groq.",
            gasto, len(audio) / 16000,
        )
    return texto


def _solta_o_modelo() -> None:
    """Devolve a RAM do Whisper local: medido lento, não é mais usado nesta
    execução — e são ~150 MB numa VM de 954 MB que já vive com swap."""
    global _model
    with _model_lock:
        _model = None
    gc.collect()


async def transcribe(pcm: bytes) -> str:
    """Transcreve um trecho de fala (bytes PCM do Discord) para texto."""
    # numpy sobre megabytes: fora do event loop
    audio = await asyncio.to_thread(pcm_to_float_mono_16k, pcm)
    if len(audio) < 1600:  # < 0.1s: ruído
        return ""
    if config.GROQ_API_KEY:
        duracao = len(audio) / 16000
        if 0 < config.VOICE_TETO_S < duracao:
            # fixo, não só com o minuto apertado: nenhum dos 80 comandos
            # medidos passou de 5,6s, e o que passava de 6s eram 13% das
            # requisições e 37% do áudio da call mandado a terceiros
            _estatisticas["poupados"] += 1
            log.info(
                "🎙️ trecho de %.1fs não foi para a Groq: acima do teto de %.0fs"
                " (nenhum comando medido passou de 5,6s)",
                duracao, config.VOICE_TETO_S,
            )
            return ""
        teto, usados = _teto_de_duracao()
        if teto is not None and duracao > teto:
            _estatisticas["poupados"] += 1
            log.info(
                "🎙️ trecho de %.1fs não foi para a Groq: %d de %d requisições do"
                " minuto já gastas — guardando o resto para o que tem tamanho de"
                " comando (até %.0fs)",
                duracao, usados, config.GROQ_STT_RPM, teto,
            )
            return ""
        try:
            texto = await _transcribe_groq(audio)
            _estatisticas["ok"] += 1
            return texto
        except Exception as exc:  # noqa: BLE001 — nuvem fora: cai para o local
            _estatisticas["falhas"] += 1
            if not config.VOICE_WHISPER_LOCAL:
                _estatisticas["perdidos"] += 1
                log.warning(
                    "🎙️ Groq falhou (%s) e o fallback local está desligado"
                    " (VOICE_WHISPER_LOCAL=0) — comando perdido",
                    type(exc).__name__,
                )
                return ""
            if _local_lento:
                _estatisticas["perdidos"] += 1
                # já medimos: aqui o local custa mais que o comando vale
                log.warning(
                    "🎙️ Groq falhou (%s) e o Whisper local é lento demais nesta"
                    " máquina — comando perdido, mas a música segue inteira",
                    type(exc).__name__,
                )
                return ""
            if not model_ready():
                # carregar o modelo AQUI custaria ~46s na VM, saturando as 2
                # vCPUs (engasga a música) e segurando _local_sem — e o
                # trecho seria descartado por STALE_SEGMENT_S no fim. Melhor
                # perder ESTE comando e deixar o modelo pronto para o próximo.
                _ensure_preload_started()
                _estatisticas["perdidos"] += 1
                log.warning(
                    "🎙️ Groq falhou (%s) e o Whisper local ainda está frio —"
                    " comando perdido; carregando o local para os próximos",
                    exc,
                )
                return ""
            # %s e não %r: o repr da ClientResponseError inclui os headers,
            # ou seja, grava a chave da Groq em texto puro no journal
            log.warning(
                "🎙️ Groq falhou (%s: %s) — usando Whisper local",
                type(exc).__name__, exc,
            )
    return await _transcribe_local(audio)
