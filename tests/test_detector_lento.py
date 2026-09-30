"""O detector do nome, lento, é pior que nenhum.

Journal de 27/09, e2-micro do Google (0,25 vCPU garantida), duas pessoas na
call: "ouvi o nome ... (detector em 110.01s)". Cada olhada do detector (o
Whisper tiny, ~0,25s num PC) levava 1,1s com a call calma — e, com gente
falando, as olhadas empilhavam na fila dele, a escuta esperava cada uma e as
perguntas chegavam velhas. Como com o fallback local (test_speech_cpu), a
máquina é medida: a primeira olhada lenta desliga o detector, e o nome passa a
ser procurado só no fim da frase.
"""
import asyncio
import logging

import numpy as np
import pytest

from services import speech


@pytest.fixture(autouse=True)
def _limpa(monkeypatch):
    monkeypatch.setattr(speech, "_detector_lento", False)
    monkeypatch.setattr(speech, "_detector", object())  # modelo "carregado"
    monkeypatch.setattr(speech.config, "WHISPER_DETECTOR", "tiny")


def _pcm(segundos: float = 0.8) -> bytes:
    return np.zeros(int(48000 * segundos) * 2, dtype=np.int16).tobytes()


def _detector_leva(monkeypatch, segundos: float, texto: str = "Jarvis") -> list:
    """Detector falso que 'demora' `segundos` no relógio do módulo."""
    relogio = [1000.0]
    chamadas = []
    monkeypatch.setattr(speech, "_agora", lambda: relogio[0])

    def detecta(audio):
        chamadas.append(1)
        relogio[0] += segundos
        return texto

    monkeypatch.setattr(speech, "_detecta_sync", detecta)
    return chamadas


def test_detector_rapido_continua_ligado(monkeypatch):
    _detector_leva(monkeypatch, 0.25)

    assert asyncio.run(speech.detecta(_pcm())) == "Jarvis"
    assert speech.detector_ligado()


def test_detector_lento_se_desliga_e_solta_o_modelo(monkeypatch, caplog):
    _detector_leva(monkeypatch, 1.13)  # a e2-micro com a call calma

    with caplog.at_level(logging.WARNING, logger="services.speech"):
        assert asyncio.run(speech.detecta(_pcm())) == "Jarvis"  # esta olhada ainda vale

    assert not speech.detector_ligado()
    assert speech._detector is None  # a RAM do modelo volta
    assert "desligado" in caplog.text


def test_depois_de_desligado_nem_chama_o_modelo(monkeypatch):
    chamadas = _detector_leva(monkeypatch, 5.0)
    asyncio.run(speech.detecta(_pcm()))

    assert asyncio.run(speech.detecta(_pcm())) == ""
    assert chamadas == [1]  # a segunda olhada nem recarregou o modelo


def test_carregar_o_modelo_nao_conta_como_lentidao(monkeypatch):
    """A primeira olhada antes do aquecimento do boot paga o carregamento."""
    monkeypatch.setattr(speech, "_detector", None)
    _detector_leva(monkeypatch, 3.0)

    asyncio.run(speech.detecta(_pcm()))

    assert speech.detector_ligado()


def test_segunda_olhada_com_o_whisper_maior_nao_desliga_o_detector(monkeypatch):
    relogio = [1000.0]
    monkeypatch.setattr(speech, "_agora", lambda: relogio[0])

    def maior(audio):
        relogio[0] += 3.0
        return "Jarvis"

    monkeypatch.setattr(speech, "_transcribe_sync", maior)

    asyncio.run(speech.detecta(_pcm(), forte=True))

    assert speech.detector_ligado()


@pytest.mark.parametrize("valor", ["off", "0", "nenhum", "OFF"])
def test_detector_desligado_no_env(monkeypatch, valor):
    monkeypatch.setattr(speech.config, "WHISPER_DETECTOR", valor)
    assert not speech.detector_ligado()
