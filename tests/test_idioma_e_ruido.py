"""O que o log de 27/09 (a primeira call com a Groq, cinco pessoas) ensinou.

- A Groq adivinhava o idioma: 20 de 74 falas saíram em outro — russo,
  islandês, lituano, espanhol, inglês inventado. O "Jarvis" dito numa fala
  "russa" se perdia. Agora o idioma vai fixo (VOICE_IDIOMA, "pt").
- As alucinações clássicas do Whisper sobre ruído ("Thank you.", "Субтитры
  создавал DimaTorzok") passavam pela janela do modo conversa e iam ao Gemini.
- "Jarvis. Parah." virou pergunta em vez de comando.
- Outro bot na call (de música, tudo indica) mandava pacotes sem parar: o
  áudio dele era guardado e analisado à toa.
- O DAVE falhava ~50 vezes por segundo e o total não dizia de quem era o áudio
  virando silêncio — agora o diagnóstico conta por pessoa.
"""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from core import intents
from core import pycord_voice_patch as patch
from core.intents import is_stt_noise, parse_command, registra_nome
from core.listen import CommandSink
from services import gemini, speech


@pytest.fixture(autouse=True)
def jarvis(monkeypatch):
    monkeypatch.setattr(intents, "_NOMES", set())
    registra_nome("Jarvis")


# ── o idioma da Groq ─────────────────────────────────────────────────────────


class _Form:
    def __init__(self) -> None:
        self.campos: dict[str, object] = {}

    def add_field(self, nome, valor, **_kw) -> None:
        self.campos[nome] = valor


class _Resposta:
    status = 200
    headers: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def raise_for_status(self) -> None:
        pass

    async def json(self):
        return {"text": "Jarvis, que horas são?"}


@pytest.fixture
def groq(monkeypatch):
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "chave-de-teste")
    enviados: list[_Form] = []

    class _Sessao:
        def post(self, *_args, data=None, **_kw):
            enviados.append(data)
            return _Resposta()

    monkeypatch.setattr(speech.http, "get_session", lambda: _Sessao())
    monkeypatch.setattr("aiohttp.FormData", _Form)
    speech._envios.clear()
    yield enviados
    speech._envios.clear()


def test_groq_recebe_o_idioma_da_call(groq, monkeypatch):
    monkeypatch.setattr(speech.config, "VOICE_IDIOMA", "pt")

    texto = asyncio.run(speech._transcribe_groq(np.zeros(16000, dtype=np.float32)))

    assert texto == "Jarvis, que horas são?"
    assert groq[0].campos["language"] == "pt"


def test_idioma_vazio_deixa_a_groq_adivinhar(groq, monkeypatch):
    monkeypatch.setattr(speech.config, "VOICE_IDIOMA", "")

    asyncio.run(speech._transcribe_groq(np.zeros(16000, dtype=np.float32)))

    assert "language" not in groq[0].campos


# ── alucinações sobre ruído ──────────────────────────────────────────────────


@pytest.mark.parametrize("texto", [
    "Thank you.", "Thanks for watching!", "Субтитры создавал DimaTorzok", "Хайры.",
    "Реклама. Та бам, патси.", "Mm-hmm.", "Uhum.", "Tá bom.", "E...", "Obrigado por assistir.",
])
def test_alucinacoes_classicas_sao_ruido(texto):
    assert is_stt_noise(texto)


@pytest.mark.parametrize("texto", ["E a de Portugal?", "tchau pessoal", "Por quê?", "Sim, e amanhã?"])
def test_continuacao_de_verdade_nao_e_ruido(texto):
    assert not is_stt_noise(texto)


def test_para_escrito_com_h_ainda_e_comando():
    assert parse_command("Jarvis. Parah.") == ("cala", "")


# ── outros bots na call ──────────────────────────────────────────────────────


def _pcm() -> SimpleNamespace:
    return SimpleNamespace(pcm=b"\x01\x00" * 960)


def test_audio_de_outro_bot_nem_entra_no_sink():
    sink = CommandSink()
    sink.write(_pcm(), SimpleNamespace(id=5, bot=True))
    sink.write(_pcm(), SimpleNamespace(id=6, bot=False))
    sink.write(_pcm(), SimpleNamespace(id=7))  # discord.Object: sem .bot, é gente

    assert sorted(user_id for user_id, *_ in sink.abertos()) == [6, 7]


# ── o DAVE por pessoa ────────────────────────────────────────────────────────


class _Dave:
    ready = True

    def __init__(self, falha_para: set[int]) -> None:
        self.falha_para = falha_para

    def decrypt(self, user_id, _media, payload):
        if user_id in self.falha_para:
            raise RuntimeError("não é da chave deste grupo")
        return b"opus:" + payload


def _descriptografa(dave, ssrc: int, mapa: dict[int, int]):
    estado = SimpleNamespace(dave_session=dave, ssrc_user_map=mapa)
    decryptor = SimpleNamespace(
        client=SimpleNamespace(_connection=estado), _decryptor_rtp=lambda pacote: pacote.payload,
    )
    pacote = SimpleNamespace(ssrc=ssrc, payload=b"x", decrypted_data=None)
    return patch._fixed_decrypt_rtp(decryptor, pacote)


def test_diagnostico_conta_o_dave_de_cada_pessoa(monkeypatch):
    monkeypatch.setattr(patch, "DIAG_POR_USUARIO", {})
    dave = _Dave(falha_para={8})
    mapa = {1: 7, 2: 8}

    assert _descriptografa(dave, 1, mapa) == b"opus:x"
    assert _descriptografa(dave, 2, mapa) == patch._reader.OPUS_SILENCE  # vira silêncio
    _descriptografa(dave, 2, mapa)

    assert patch.DIAG_POR_USUARIO == {7: [1, 0], 8: [0, 2]}


# ── o que o Gemini promete ───────────────────────────────────────────────────


def test_instrucao_nao_deixa_o_gemini_prometer_o_que_nao_faz():
    """27/09: "posso colocar a música pra tocar", "mandado o recado pro Olavo" — e
    um "puedo" no meio da resposta."""
    texto = gemini._instrucao()
    assert "não toca músicas" in texto and "não manda mensagens" in texto
    assert "nunca espanhol" in texto
