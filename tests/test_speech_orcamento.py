"""O teto de 20 requisições/minuto da Groq é de todo mundo que fala na call.

Journal de 15 a 20/09/2026: 7.069 transcrições, 98% sem wake word; o pico foi
EXATAMENTE 20 por minuto — o teto do tier grátis — e daí saíram 112 falhas por
429 e 51 comandos de voz perdidos. O 429 escolhe a vítima ao acaso, e a vítima
pode ser o "Cabrunco, pula".

Dos 80 trechos que eram comando, o mais longo tinha 5,6s (mediana 1,0s, p90
3,9s); dos 2.252 que não eram, 300 passavam de 6s. Então, quando o minuto está
acabando, quem fica de fora é o trecho comprido — que nunca é comando — e não
um qualquer.
"""
import asyncio
import logging

import numpy as np
import pytest

from services import speech


@pytest.fixture(autouse=True)
def _limpa(monkeypatch):
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "chave-de-teste")
    monkeypatch.setattr(speech.config, "GROQ_STT_RPM", 20)
    # o teto FIXO de 6s (test_speech_espera.py) cortaria as falas longas daqui
    # antes de o orçamento por minuto opinar: este arquivo testa o orçamento
    monkeypatch.setattr(speech.config, "VOICE_TETO_S", 0.0)
    monkeypatch.setattr(speech, "_local_lento", True)  # falha da Groq = "" e pronto
    speech._envios.clear()
    speech.leu_estatisticas()
    yield
    speech._envios.clear()
    speech.leu_estatisticas()


def _pcm(segundos: float) -> bytes:
    return np.zeros(int(48000 * segundos) * 2, dtype=np.int16).tobytes()


def _groq_responde(monkeypatch, texto: str = "cabrunco pula") -> list:
    enviados = []

    async def ok(audio):
        enviados.append(len(audio) / 16000)
        speech._conta_envio()
        return texto

    monkeypatch.setattr(speech, "_transcribe_groq", ok)
    return enviados


def _gasta(n: int, agora: float = 1000.0) -> None:
    speech._envios.extend([agora - 1.0] * n)


def test_com_folga_tudo_e_enviado(monkeypatch):
    enviados = _groq_responde(monkeypatch)
    monkeypatch.setattr(speech, "_agora", lambda: 1000.0)
    _gasta(5)

    assert asyncio.run(speech.transcribe(_pcm(11.0))) == "cabrunco pula"
    assert len(enviados) == 1


def test_minuto_apertado_segura_o_trecho_comprido(monkeypatch, caplog):
    enviados = _groq_responde(monkeypatch)
    monkeypatch.setattr(speech, "_agora", lambda: 1000.0)
    _gasta(15)

    with caplog.at_level(logging.INFO, logger="services.speech"):
        texto = asyncio.run(speech.transcribe(_pcm(8.0)))

    assert texto == "" and enviados == []
    assert "8.0s" in caplog.text and "15 de 20" in caplog.text
    assert speech.leu_estatisticas()["poupados"] == 1


def test_minuto_apertado_ainda_envia_o_que_tem_tamanho_de_comando(monkeypatch):
    enviados = _groq_responde(monkeypatch)
    monkeypatch.setattr(speech, "_agora", lambda: 1000.0)
    _gasta(15)

    assert asyncio.run(speech.transcribe(_pcm(3.0))) == "cabrunco pula"
    assert len(enviados) == 1


def test_no_teto_so_passa_trecho_curto(monkeypatch):
    enviados = _groq_responde(monkeypatch)
    monkeypatch.setattr(speech, "_agora", lambda: 1000.0)
    _gasta(19)

    assert asyncio.run(speech.transcribe(_pcm(5.0))) == ""
    assert asyncio.run(speech.transcribe(_pcm(2.0))) == "cabrunco pula"
    assert len(enviados) == 1


def test_envios_de_mais_de_um_minuto_atras_nao_contam(monkeypatch):
    enviados = _groq_responde(monkeypatch)
    monkeypatch.setattr(speech, "_agora", lambda: 1000.0)
    speech._envios.extend([900.0] * 25)  # 100s atrás

    assert asyncio.run(speech.transcribe(_pcm(11.0))) == "cabrunco pula"
    assert len(enviados) == 1


def test_estatisticas_contam_e_zeram_na_leitura(monkeypatch):
    _groq_responde(monkeypatch)
    monkeypatch.setattr(speech, "_agora", lambda: 1000.0)
    asyncio.run(speech.transcribe(_pcm(1.0)))
    asyncio.run(speech.transcribe(_pcm(1.0)))

    assert speech.leu_estatisticas()["ok"] == 2
    assert speech.leu_estatisticas()["ok"] == 0


def test_comando_perdido_entra_na_conta(monkeypatch):
    async def falha(audio):
        raise RuntimeError("429")

    monkeypatch.setattr(speech, "_transcribe_groq", falha)
    asyncio.run(speech.transcribe(_pcm(1.0)))

    assert speech.leu_estatisticas()["perdidos"] == 1


def test_cabecalhos_do_429_dizem_qual_limite_estourou():
    texto = speech._limites_do_429({
        "Retry-After": "2",
        "x-ratelimit-limit-requests": "2000",
        "x-ratelimit-remaining-requests": "1200",
        "x-ratelimit-reset-requests": "1m12s",
        "Content-Type": "application/json",
        "Authorization": "Bearer gsk_naoPodeAparecer",
    })

    assert "retry-after=2" in texto
    assert "x-ratelimit-remaining-requests=1200" in texto
    assert "gsk_" not in texto and "content-type" not in texto


def test_whisper_local_lento_sai_da_memoria(monkeypatch):
    """São ~150 MB de modelo numa VM de 954 MB com 257 MB em swap — e, depois
    de medido lento, ele nunca mais é usado nesta execução."""
    monkeypatch.setattr(speech, "_local_lento", False)
    speech._model = object()
    relogio = [1000.0]
    monkeypatch.setattr(speech, "_agora", lambda: relogio[0])

    def transcreve(audio):
        relogio[0] += 90.0
        return "pula"

    monkeypatch.setattr(speech, "_transcribe_sync", transcreve)

    async def falha(audio):
        raise RuntimeError("429")

    monkeypatch.setattr(speech, "_transcribe_groq", falha)
    asyncio.run(speech.transcribe(_pcm(1.0)))

    assert speech._local_lento is True
    assert speech._model is None


def test_fallback_local_desligado_por_config_nem_carrega(monkeypatch):
    """Na VM o veredito é conhecido (83–131s por frase): carregar o modelo a
    cada restart só para medir de novo custa 46s de CPU cheia com música."""
    monkeypatch.setattr(speech, "_local_lento", False)
    monkeypatch.setattr(speech.config, "VOICE_WHISPER_LOCAL", False)
    speech._model = None
    carregou = []
    monkeypatch.setattr(speech, "_ensure_preload_started", lambda: carregou.append(1))

    async def falha(audio):
        raise RuntimeError("429")

    monkeypatch.setattr(speech, "_transcribe_groq", falha)

    assert asyncio.run(speech.transcribe(_pcm(1.0))) == ""
    assert carregou == []
