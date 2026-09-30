"""Fallback do Groq nunca pode carregar o Whisper local dentro do comando.

Medido na VM de produção (2 vCPU): carregar o faster-whisper leva **46s** e
transcrever 3s com o modelo quente leva 4,9s. Como `STALE_SEGMENT_S` é 12s, o
resultado de uma transcrição fria é descartado — foram 51s de CPU saturada
(engasgando a música junto) para jogar fora. E o `_local_sem` tem 1 permissão:
enquanto isso, TODA fala seguinte fica na fila. Em produção o Groq devolveu
429 vinte e uma vezes.
"""
import asyncio

import numpy as np
import pytest

from services import speech


@pytest.fixture(autouse=True)
def _limpa(monkeypatch):
    speech._model = None
    speech._preload_task = None
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "chave-de-teste")
    yield
    speech._model = None
    speech._preload_task = None


def _pcm(segundos: float = 1.0) -> bytes:
    # PCM 48kHz estéreo s16le, como o Discord entrega
    return np.zeros(int(48000 * segundos) * 2, dtype=np.int16).tobytes()


def _groq_falha(monkeypatch, erro=RuntimeError("429 Too Many Requests")):
    async def falha(audio):
        raise erro

    monkeypatch.setattr(speech, "_transcribe_groq", falha)


def test_groq_ok_nao_toca_no_local(monkeypatch):
    chamou_local = []

    async def ok(audio):
        return "toca calcinha preta"

    monkeypatch.setattr(speech, "_transcribe_groq", ok)
    monkeypatch.setattr(speech, "_transcribe_sync", lambda a: chamou_local.append(1) or "")

    assert asyncio.run(speech.transcribe(_pcm())) == "toca calcinha preta"
    assert chamou_local == []


def test_groq_falha_com_modelo_frio_desiste_na_hora(monkeypatch):
    """51s dentro do comando é pior que perder o comando: o resultado seria descartado."""
    _groq_falha(monkeypatch)
    monkeypatch.setattr(
        speech, "_transcribe_sync", lambda a: pytest.fail("não pode carregar frio")
    )
    iniciados = []
    monkeypatch.setattr(speech, "_ensure_preload_started", lambda: iniciados.append(1))

    assert asyncio.run(speech.transcribe(_pcm())) == ""
    assert iniciados == [1]  # mas deixa o modelo carregando para a próxima


def test_groq_falha_com_modelo_quente_usa_o_local(monkeypatch):
    _groq_falha(monkeypatch)
    speech._model = object()  # já carregado
    monkeypatch.setattr(speech, "_transcribe_sync", lambda a: "pula essa")

    assert asyncio.run(speech.transcribe(_pcm())) == "pula essa"


def test_preload_em_background_roda_uma_vez_so():
    chamadas = []

    async def run():
        speech.preload = lambda: chamadas.append(1)  # noqa: E731
        speech._ensure_preload_started()
        speech._ensure_preload_started()
        speech._ensure_preload_started()
        if speech._preload_task is not None:
            await speech._preload_task

    original = speech.preload
    try:
        asyncio.run(run())
    finally:
        speech.preload = original
    assert chamadas == [1]


def test_sem_chave_groq_usa_o_local_direto(monkeypatch):
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "")
    monkeypatch.setattr(speech, "_transcribe_sync", lambda a: "modo local")

    assert asyncio.run(speech.transcribe(_pcm())) == "modo local"


def test_ruido_curto_nem_chega_no_groq(monkeypatch):
    monkeypatch.setattr(
        speech, "_transcribe_groq", lambda a: pytest.fail("não devia chamar")
    )
    assert asyncio.run(speech.transcribe(_pcm(0.05))) == ""


def test_groq_tenta_mais_de_duas_vezes_antes_de_desistir():
    """Groq se recupera de 429 em ~1-2s; desistir cedo joga o comando no local de 51s."""
    assert speech._GROQ_TENTATIVAS >= 3
