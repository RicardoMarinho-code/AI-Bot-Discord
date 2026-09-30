"""O Whisper local não pode sequestrar a CPU e cortar a música.

Journal de produção (16/09), durante uma call com quatro pessoas falando: o
Groq devolveu 429 em rajada, o bot caiu para o Whisper local (já carregado)
e as transcrições levaram **83, 85, 87, 88, 99, 124 e 131 segundos**. Nesta
VM há um núcleo só: enquanto isso a música engasga, e o comando ainda é
executado mais de um minuto depois de a pessoa falar — quando ninguém mais
espera por ele.

O fallback local só vale a pena se for rápido. Em vez de fixar um "esta
máquina é pequena", medimos: a primeira transcrição local que passar do
limite desliga o fallback pelo resto da execução. Numa máquina rápida nada
muda; numa lenta, perde-se o comando (que já estava perdido) e a música
fica intacta. Sem Groq configurado o local continua sendo usado, lento ou
não — é o único motor que existe.
"""
import asyncio
import logging

import numpy as np
import pytest

from services import speech


@pytest.fixture(autouse=True)
def _limpa(monkeypatch):
    speech._model = object()  # modelo "carregado"
    speech._preload_task = None
    monkeypatch.setattr(speech, "_local_lento", False)
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "chave-de-teste")
    yield
    speech._model = None
    speech._preload_task = None


def _pcm(segundos: float = 1.0) -> bytes:
    return np.zeros(int(48000 * segundos) * 2, dtype=np.int16).tobytes()


def _groq_falha(monkeypatch):
    async def falha(audio):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(speech, "_transcribe_groq", falha)


def _local_leva(monkeypatch, segundos: float, texto: str = "pula") -> list:
    """Whisper local falso que 'demora' `segundos` no relógio do módulo."""
    relogio = [1000.0]
    chamadas = []
    monkeypatch.setattr(speech, "_agora", lambda: relogio[0])

    def transcreve(audio):
        chamadas.append(1)
        relogio[0] += segundos
        return texto

    monkeypatch.setattr(speech, "_transcribe_sync", transcreve)
    return chamadas


def test_local_rapido_continua_sendo_usado(monkeypatch):
    _groq_falha(monkeypatch)
    chamadas = _local_leva(monkeypatch, segundos=2.0)

    for _ in range(3):
        assert asyncio.run(speech.transcribe(_pcm())) == "pula"

    assert len(chamadas) == 3
    assert speech._local_lento is False


def test_local_lento_desliga_o_fallback(monkeypatch, caplog):
    _groq_falha(monkeypatch)
    chamadas = _local_leva(monkeypatch, segundos=90.0)

    with caplog.at_level(logging.WARNING, logger="services.speech"):
        primeira = asyncio.run(speech.transcribe(_pcm()))
        segunda = asyncio.run(speech.transcribe(_pcm()))
        terceira = asyncio.run(speech.transcribe(_pcm()))

    assert primeira == "pula"  # a primeira ainda vale
    assert segunda == "" and terceira == ""  # as seguintes nem tentam
    assert len(chamadas) == 1, "o modelo local não pode ser chamado de novo"
    assert speech._local_lento is True
    assert caplog.text.count("desligando o fallback local") == 1


def test_groq_voltando_funciona_mesmo_com_o_local_desligado(monkeypatch):
    monkeypatch.setattr(speech, "_local_lento", True)

    async def groq_ok(audio):
        return "toca musica"

    monkeypatch.setattr(speech, "_transcribe_groq", groq_ok)

    assert asyncio.run(speech.transcribe(_pcm())) == "toca musica"


def test_sem_groq_o_local_e_usado_mesmo_lento(monkeypatch):
    """Sem nuvem, um comando lento é melhor que bot surdo."""
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "")
    chamadas = _local_leva(monkeypatch, segundos=90.0)

    for _ in range(2):
        assert asyncio.run(speech.transcribe(_pcm())) == "pula"

    assert len(chamadas) == 2


def test_modelo_frio_continua_nao_carregando_dentro_do_comando(monkeypatch):
    """Regressão: carregar custa ~46s e o trecho já estaria velho."""
    speech._model = None
    _groq_falha(monkeypatch)
    carregou = []
    monkeypatch.setattr(speech, "_ensure_preload_started", lambda: carregou.append(1))
    monkeypatch.setattr(
        speech, "_transcribe_sync", lambda audio: pytest.fail("não podia transcrever")
    )

    assert asyncio.run(speech.transcribe(_pcm())) == ""
    assert carregou == [1]
