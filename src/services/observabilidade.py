"""O logging do bot: segredos mascarados, erros de thread dentro do log e o
ruído conhecido das bibliotecas resumido.

Veio do bot de música que serviu de base, onde a leitura do journal inteiro
(20/09/2026) mostrou:

  - a chave da Groq em texto puro 25 vezes (o repr do ClientResponseError do
    aiohttp traz os headers da request). A garantia não pode depender de
    ninguém nunca mais escrever um `%r` no lugar errado: o formatador mascara.
  - tracebacks de THREAD ("Exception in thread voice-receiver…") caindo crus
    no stderr, sem horário e sem nível.
  - `CryptoError: Decryption failed` do receptor de voz: centenas de ERROR com
    ~15 linhas de traceback cada. É rotina da transição de epoch do DAVE.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
import warnings

import config

log = logging.getLogger(__name__)

FORMATO = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# ── segredos ─────────────────────────────────────────────────────────────────

_PADROES_DE_SEGREDO = (
    (re.compile(r"gsk_[A-Za-z0-9]{8,}"), "gsk_***"),  # chave da Groq
    # chaves do Google (Gemini): o formato antigo e o do AI Studio de 2026
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "AIza***"),
    (re.compile(r"\bAQ\.[0-9A-Za-z_-]{20,}"), "AQ.***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    # token de bot do Discord: id.base64.hmac
    (re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"), "***token-discord***"),
    # "Identifying ourselves: {... 'token': 'ef3d431e…', 'session_id': …}" — o
    # py-cord loga em INFO o token e a sessão de cada conexão de voz
    (re.compile(r"""(['"](?:token|session_id)['"]\s*:\s*['"])[^'"]{6,}(['"])"""), r"\1***\2"),
)


def _segredos_do_env() -> list[str]:
    valores = (config.DISCORD_TOKEN, config.GROQ_API_KEY, config.GEMINI_API_KEY)
    return [v for v in valores if len(v) >= 8]


def mascara(texto: str) -> str:
    """Tira do texto qualquer credencial conhecida — pelo valor e pelo formato."""
    for valor in _segredos_do_env():
        if valor in texto:
            texto = texto.replace(valor, "***")
    for padrao, troca in _PADROES_DE_SEGREDO:
        texto = padrao.sub(troca, texto)
    return texto


class Redator(logging.Formatter):
    """Formatter que mascara segredos na linha PRONTA (mensagem + traceback)."""

    def format(self, record: logging.LogRecord) -> str:
        return mascara(super().format(record))


# ── ruído conhecido ──────────────────────────────────────────────────────────


class FiltroDeRotina(logging.Filter):
    """Cala mensagens de biblioteca que são rotina mas saem como WARNING.

    `Socket reader … is waiting to be set as running` sai 2x por conexão de
    voz — é só o leitor de voz esperando a vez.
    """

    def __init__(self, marcas: tuple[str, ...]) -> None:
        super().__init__()
        self._marcas = marcas

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            mensagem = record.getMessage()
        except Exception:  # noqa: BLE001 — filtro de log nunca pode levantar
            return True
        return not any(marca in mensagem for marca in self._marcas)


class FiltroDeRajada(logging.Filter):
    """Deixa passar UM registro por janela e conta os outros.

    Para ruído que é rotina mas não pode sumir de vez: o primeiro da janela
    sai (sem traceback), os seguintes viram "(+N iguais)" no próximo que sair.
    """

    def __init__(self, marcas: tuple[str, ...], janela_s: float = 60.0) -> None:
        super().__init__()
        self._marcas = marcas
        self._janela_s = janela_s
        self._ultimo = float("-inf")
        self._calados = 0
        self._relogio = time.monotonic

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            mensagem = record.getMessage()
        except Exception:  # noqa: BLE001 — filtro de log nunca pode levantar
            return True
        if not any(marca in mensagem for marca in self._marcas):
            return True
        agora = self._relogio()
        if agora - self._ultimo < self._janela_s:
            self._calados += 1
            return False
        record.msg = mensagem + (
            f" (+{self._calados} iguais caladas desde o último aviso)" if self._calados else ""
        )
        record.args = ()
        record.exc_info = None  # ~15 linhas de traceback por pacote de voz
        record.exc_text = None
        self._ultimo = agora
        self._calados = 0
        return True


# ── erros que não passavam pelo logging ──────────────────────────────────────


def _excecao_nao_tratada(tipo, valor, tb) -> None:
    if issubclass(tipo, KeyboardInterrupt):
        sys.__excepthook__(tipo, valor, tb)
        return
    log.critical("exceção não tratada — o processo vai cair", exc_info=(tipo, valor, tb))


def _excecao_em_thread(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    log.error(
        "exceção não tratada na thread %s",
        args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def configura_logging() -> None:
    """Logging do bot: formato único, segredos mascarados, nada fora dele."""
    raiz = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in raiz.handlers):
        raiz.addHandler(logging.StreamHandler())
    raiz.setLevel(logging.WARNING)
    for handler in raiz.handlers:
        handler.setFormatter(Redator(FORMATO))
    # módulos do bot em INFO (eventos operacionais); libs continuam em WARNING
    for nome in ("main", "core", "services", "cogs"):
        logging.getLogger(nome).setLevel(logging.INFO)
    # INFO nos módulos de voz: mostra close codes e o porquê de desconexões
    logging.getLogger("discord.voice").setLevel(logging.INFO)
    logging.getLogger("discord.voice.state").addFilter(
        FiltroDeRotina(("is waiting to be set as running",))
    )
    # o bot não pede o intent de membros: quem já estava na call quando ele entra
    # não está no cache e o py-cord avisa disso em WARNING a cada conexão
    logging.getLogger("discord.voice.client").addFilter(
        FiltroDeRotina(("Skipping member referencing ID",))
    )
    # spam de SenderReport a cada segundo durante a gravação
    leitor = logging.getLogger("discord.voice.receive.reader")
    leitor.setLevel(logging.WARNING)
    leitor.addFilter(FiltroDeRajada(("Decryption failed", "CryptoError")))

    sys.excepthook = _excecao_nao_tratada
    threading.excepthook = _excecao_em_thread
    # warnings.warn também caía cru no stderr; o do DAVE é justamente o que
    # core/pycord_voice_patch.py conserta — puro ruído
    logging.captureWarnings(True)
    warnings.filterwarnings("ignore", message="Voice reception is currently broken")
