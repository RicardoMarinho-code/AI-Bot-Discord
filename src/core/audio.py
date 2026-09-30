"""O que o bot toca na call: o estalo de "estou ouvindo" e a fala do Gemini."""
from __future__ import annotations

import logging
import os
import time
import wave
from collections import deque

import discord
import numpy as np
from discord.opus import Encoder

log = logging.getLogger(__name__)

FRAME_BYTES = Encoder.FRAME_SIZE  # 3840 = 20ms de PCM 48kHz stereo s16
SILENCIO = b"\x00" * FRAME_BYTES
_FRAMES_POR_S = 1000 / Encoder.FRAME_LENGTH  # 50
BYTES_POR_S = FRAME_BYTES * 50  # 192.000: 48kHz * 2 canais * 2 bytes

# ── som de "estou ouvindo" (quando alguém chama o bot) ───────────────────────
# o estalo foi cortado de um efeito de chicote (uma instância, fade no fim) e
# normalizado a -1 dBFS; 0,7 deixa folga
VOLUME_EFEITO = 0.7
EFEITO_ESCUTA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "escuta.wav"
)
_efeito_escuta: bytes | None = None


def carrega_efeito(caminho: str) -> bytes:
    """WAV 48kHz estéreo 16-bit → PCM com o volume do efeito, em frames inteiros.

    Frames inteiros porque o PCMAudio do py-cord descarta o último pedaço
    incompleto (o som terminaria com um corte seco).
    """
    with wave.open(caminho, "rb") as w:
        formato = (w.getframerate(), w.getnchannels(), w.getsampwidth())
        if formato != (48000, 2, 2):
            raise ValueError(f"{caminho}: precisa ser 48kHz estéreo 16-bit, é {formato}")
        bruto = w.readframes(w.getnframes())
    amostras = np.frombuffer(bruto[: len(bruto) - len(bruto) % 4], dtype=np.int16)
    # o silêncio do começo do arquivo (~30 ms no estalo) é atraso puro: sai
    pico = int(np.abs(amostras).max()) if len(amostras) else 0
    audivel = np.flatnonzero(np.abs(amostras) > pico * 0.01)
    if len(audivel):
        amostras = amostras[audivel[0] - audivel[0] % 2 :]  # sem partir o par L/R
    pcm = (amostras.astype(np.float32) * VOLUME_EFEITO).astype(np.int16).tobytes()
    return pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)


def efeito_escuta() -> bytes:
    """O som de escuta, carregado uma vez. b"" (sem som) se o arquivo faltar."""
    global _efeito_escuta
    if _efeito_escuta is None:
        try:
            _efeito_escuta = carrega_efeito(EFEITO_ESCUTA_PATH)
        except (OSError, ValueError, wave.Error) as exc:
            log.warning("som de escuta indisponível (%s) — seguindo sem ele", exc)
            _efeito_escuta = b""
    return _efeito_escuta


# ── a fala do bot ────────────────────────────────────────────────────────────


class FalaAoVivo:
    """Uma fala que ainda está chegando (a resposta do Gemini), tocada enquanto chega.

    O event loop escreve pedaços de qualquer tamanho; a thread de áudio tira um
    frame de 20ms por vez (deque: append/popleft são seguros entre threads). O
    Live entrega o áudio mais ou menos no ritmo da própria fala — começar no
    primeiro pedaço garantia engasgo —, então a fala só começa com um pulmão
    curto na mão. Se mesmo assim faltar, sai silêncio até o fim de verdade.

    `nao_antes` (time.monotonic): a fala não começa antes disto, mesmo com o
    áudio na mão — a pergunta vai ao Gemini assim que a frase fecha, mas quem só
    fez uma pausa não pode ser atropelado (ver core/listen.py).
    """

    def __init__(self, *, pulmao_s: float = 0.4, nao_antes: float = 0.0) -> None:
        self._frames: deque[bytes] = deque()
        self._resto = b""  # o pedaço que não fechou um frame espera o próximo
        self._pulmao = max(1, int(pulmao_s * _FRAMES_POR_S))
        self._nao_antes = nao_antes
        self._comecou = False
        self._chegou_tudo = False
        self._calada = False
        self._faltando = False
        self.frames_tocados = 0
        self.engasgos = 0  # vezes que o áudio faltou no meio da fala (para o log)

    # --- event loop ---

    def escreve(self, pcm: bytes) -> None:
        """Mais um pedaço da fala (PCM 48 kHz estéreo s16)."""
        dado = self._resto + pcm
        inteiro = len(dado) - len(dado) % FRAME_BYTES
        self._frames.extend(dado[i : i + FRAME_BYTES] for i in range(0, inteiro, FRAME_BYTES))
        self._resto = dado[inteiro:]

    def termina(self) -> None:
        """Chegou tudo: o pedaço final é completado com silêncio até fechar o frame."""
        if self._resto:
            self._frames.append(self._resto + b"\x00" * (FRAME_BYTES - len(self._resto)))
            self._resto = b""
        self._chegou_tudo = True

    def cala(self) -> bool:
        """Corta a fala no próximo frame. False se ela já tinha acabado."""
        estava = self.ativa()
        self._calada = True
        self._frames.clear()
        return estava

    def ativa(self) -> bool:
        return not self._calada and not (self._chegou_tudo and not self._frames)

    # --- thread de áudio ---

    def proximo(self) -> bytes | None:
        """O próximo frame; silêncio enquanto espera o áudio chegar; None = acabou."""
        if self._calada:
            return None
        if not self._comecou:
            if time.monotonic() < self._nao_antes:
                return SILENCIO
            if len(self._frames) < self._pulmao and not self._chegou_tudo:
                return SILENCIO
            self._comecou = True
        try:
            frame = self._frames.popleft()
        except IndexError:
            if self._chegou_tudo:
                return None
            if not self._faltando:
                self.engasgos += 1
            self._faltando = True
            return SILENCIO
        self._faltando = False
        self.frames_tocados += 1
        return frame


class FonteDaFala(discord.AudioSource):
    """A fala ao vivo como fonte de áudio do py-cord."""

    def __init__(self, fala: FalaAoVivo) -> None:
        self.fala = fala

    def read(self) -> bytes:
        return self.fala.proximo() or b""

    def is_opus(self) -> bool:
        return False
