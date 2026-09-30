"""429 da Groq vira ESPERA para quem tem cara de comando — e fala longa nem é enviada.

Journal de 15 a 20/09/2026: 51 comandos de voz perdidos por 429. O retry de
então esperava no máximo 2s por tentativa (3 tentativas) e tratava igual o
"Cabrunco, pula" de 1s e um desabafo de 11s. O teto da Groq é por MINUTO
(20 req/min no tier grátis) e o cabeçalho `retry-after` diz quanto falta:
  - trecho curto (≤ 4s — p90 dos comandos medidos: 3,9s): espera o que a Groq
    pedir, até 6s no total. Comando atrasado é melhor que comando perdido.
  - trecho longo: desiste no primeiro 429 — segurar um dos 3 workers por 6s em
    fala que quase nunca é comando é o que faz o comando DE VERDADE cair da fila.

E o teto de 6s virou fixo: nenhum dos 80 comandos medidos passou de 5,6s, e os
trechos acima disso eram 13% das requisições e 37% do áudio enviado.
"""
import asyncio

import numpy as np
import pytest

from services import speech


class _Resposta:
    def __init__(self, status: int, retry_after: str | None = None, texto: str = "cabrunco pula") -> None:
        self.status = status
        self.headers = {"Retry-After": retry_after} if retry_after else {}
        self._texto = texto

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return {"text": self._texto}


class _Sessao:
    def __init__(self, respostas: list) -> None:
        self.respostas = list(respostas)
        self.pedidos = 0

    def post(self, *args, **kwargs):
        self.pedidos += 1
        return self.respostas.pop(0)


@pytest.fixture
def groq(monkeypatch):
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "chave-de-teste")
    monkeypatch.setattr(speech.config, "GROQ_STT_RPM", 20)
    monkeypatch.setattr(speech.config, "VOICE_TETO_S", 6.0)
    speech._envios.clear()
    speech.leu_estatisticas()
    dormidas: list[float] = []
    relogio = [1000.0]
    monkeypatch.setattr(speech, "_agora", lambda: relogio[0])

    async def dorme(segundos: float) -> None:
        dormidas.append(segundos)
        relogio[0] += segundos

    monkeypatch.setattr(speech.asyncio, "sleep", dorme)

    def arma(respostas: list) -> _Sessao:
        sessao = _Sessao(respostas)
        monkeypatch.setattr(speech.http, "get_session", lambda: sessao)
        return sessao

    arma.dormidas = dormidas
    yield arma
    speech._envios.clear()
    speech.leu_estatisticas()


def _audio(segundos: float) -> np.ndarray:
    return np.zeros(int(16000 * segundos), dtype=np.float32)


def _pcm(segundos: float) -> bytes:
    return np.zeros(int(48000 * segundos) * 2, dtype=np.int16).tobytes()


def test_comando_curto_espera_o_retry_after_e_passa(groq):
    sessao = groq([_Resposta(429, "3"), _Resposta(200)])

    assert asyncio.run(speech._transcribe_groq(_audio(1.2))) == "cabrunco pula"
    assert sessao.pedidos == 2
    assert groq.dormidas == [3.0], "antes o teto era 2s e o segundo pedido batia no 429 de novo"


def test_fala_longa_desiste_no_primeiro_429(groq):
    sessao = groq([_Resposta(429, "3"), _Resposta(200)])

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(speech._transcribe_groq(_audio(5.5)))
    assert sessao.pedidos == 1 and groq.dormidas == []


def test_espera_tem_orcamento_total(groq):
    sessao = groq([_Resposta(429, "2")] * 10)

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(speech._transcribe_groq(_audio(1.0)))
    assert sum(groq.dormidas) <= speech._ESPERA_MAX_429_S
    assert sessao.pedidos <= 5


def test_retry_after_maior_que_o_orcamento_nem_espera(groq):
    """A Groq pedindo 40s = teto diário ou de áudio/hora: esperar 6s não resolve."""
    sessao = groq([_Resposta(429, "40"), _Resposta(200)])

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(speech._transcribe_groq(_audio(1.0)))
    assert sessao.pedidos == 1 and groq.dormidas == []


def test_erro_5xx_continua_com_as_tres_tentativas_curtas(groq):
    sessao = groq([_Resposta(503), _Resposta(502), _Resposta(200)])

    assert asyncio.run(speech._transcribe_groq(_audio(5.0))) == "cabrunco pula"
    assert sessao.pedidos == 3
    assert all(d <= 2.0 for d in groq.dormidas)


def test_429_sem_retry_after_espera_um_segundo(groq):
    groq([_Resposta(429), _Resposta(200)])

    asyncio.run(speech._transcribe_groq(_audio(1.0)))

    assert groq.dormidas == [1.0]


# ── teto fixo de duração ─────────────────────────────────────────────────────


def test_fala_acima_do_teto_nem_vai_para_a_groq(groq, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(speech, "_transcribe_groq", lambda a: pytest.fail("não devia enviar"))
    with caplog.at_level(logging.INFO, logger="services.speech"):
        assert asyncio.run(speech.transcribe(_pcm(7.5))) == ""

    assert "7.5s" in caplog.text and "teto" in caplog.text
    assert speech.leu_estatisticas()["poupados"] == 1


def test_fala_no_limite_de_um_comando_ainda_vai(groq, monkeypatch):
    async def ok(audio):
        return "cabrunco toca fulano"

    monkeypatch.setattr(speech, "_transcribe_groq", ok)

    assert asyncio.run(speech.transcribe(_pcm(5.6))) == "cabrunco toca fulano"


def test_teto_zero_desliga(groq, monkeypatch):
    async def ok(audio):
        return "texto"

    monkeypatch.setattr(speech.config, "VOICE_TETO_S", 0.0)
    monkeypatch.setattr(speech, "_transcribe_groq", ok)

    assert asyncio.run(speech.transcribe(_pcm(11.0))) == "texto"


def test_sem_groq_o_teto_nao_vale(groq, monkeypatch):
    """Whisper local não tem limite por minuto: o teto é economia de requisição."""
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "")
    monkeypatch.setattr(speech, "_transcribe_sync", lambda a: "modo local")

    assert asyncio.run(speech.transcribe(_pcm(9.0))) == "modo local"
