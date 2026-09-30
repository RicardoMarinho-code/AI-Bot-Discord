"""O nome ouvido ENQUANTO a pessoa fala — o estalo antes de a frase acabar.

Medido em 27/09 com a fala chegando em tempo real e o Whisper de verdade: o
estalo tocava 3,8s depois do "Jarvis" (a escuta esperava a frase acabar, mais
0,8s de silêncio, mais a transcrição). Com o começo da frase indo ao detector
enquanto ela ainda chega: 0,5s.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from core import intents, listen
from core.listen import BYTES_PER_SECOND, CommandSink, VoiceListener, _Buffer, _Cedo

UM_SEGUNDO = b"\x01\x00" * (BYTES_PER_SECOND // 2)


@pytest.fixture(autouse=True)
def jarvis(monkeypatch):
    monkeypatch.setattr(intents, "_NOMES", set())
    intents.registra_nome("Jarvis")
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    monkeypatch.setattr(listen, "_JUNTA_S", 0.01)


def _escuta():
    assistente = MagicMock()
    assistente.guild.id = 1
    assistente.cala.return_value = False
    assistente.sai = AsyncMock()
    assistente.espera_efeito = AsyncMock()
    canal = MagicMock()
    canal.send = AsyncMock()
    escuta = VoiceListener(assistente, canal)
    escuta.active = True
    escuta._sink = CommandSink()
    escuta.despachadas = []
    escuta._conversa_nova = lambda user_id, texto, pcm, **_kw: escuta.despachadas.append((user_id, texto, pcm))
    return escuta


def _fala(escuta, user_id: int, segundos: float, *, parado_s: float = 0.0) -> _Buffer:
    """Abre (ou aumenta) o trecho de uma pessoa no sink, como a thread de áudio faria."""
    pcm = b"\x01\x00" * int(BYTES_PER_SECOND * segundos / 2)
    escuta._sink.write(SimpleNamespace(pcm=pcm), SimpleNamespace(id=user_id))
    buffer = escuta._sink._buffers[user_id]
    buffer.last = time.monotonic() - parado_s
    return buffer


def _detector(monkeypatch, *textos):
    fila = iter(textos)
    chamadas = []

    async def detecta(pcm, **kw):
        chamadas.append(len(pcm) / BYTES_PER_SECOND)
        return next(fila)

    monkeypatch.setattr(listen.speech, "detecta", detecta)
    return chamadas


async def _espera_checagens(escuta):
    for _ in range(50):
        await asyncio.sleep(0)
    if escuta._checagens:
        await asyncio.wait(set(escuta._checagens))


# ── quando olhar ─────────────────────────────────────────────────────────────


def test_com_pouca_fala_ainda_nao_olha(monkeypatch):
    chamadas = _detector(monkeypatch)
    escuta = _escuta()

    async def roda():
        _fala(escuta, 7, 0.5)  # falando, sem pausa, antes do primeiro ponto
        escuta._agenda_checagens()
        await _espera_checagens(escuta)

    asyncio.run(roda())
    assert chamadas == []


def test_olha_quando_ja_chegou_audio_para_o_nome(monkeypatch):
    chamadas = _detector(monkeypatch, "Jarvis, qual")
    escuta = _escuta()

    async def roda():
        _fala(escuta, 7, listen._CEDO_PRIMEIRO_S)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)

    asyncio.run(roda())
    assert chamadas == [pytest.approx(listen._CEDO_PRIMEIRO_S)]


def test_pausa_depois_do_nome_olha_na_hora(monkeypatch):
    """ "Jarvis," [vírgula] — ou só "Jarvis" e a pessoa espera o estalo."""
    chamadas = _detector(monkeypatch, "Jarvis")
    escuta = _escuta()

    async def roda():
        _fala(escuta, 7, 0.5, parado_s=listen._CEDO_PAUSA_S)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)

    asyncio.run(roda())
    assert len(chamadas) == 1
    escuta.assistente.tocar_efeito.assert_called_once()


def test_continuacao_de_pergunta_nem_passa_pelo_detector(monkeypatch):
    chamadas = _detector(monkeypatch)
    escuta = _escuta()

    async def roda():
        escuta._comeca_pergunta(7, "Jarvis, qual é", UM_SEGUNDO)
        _fala(escuta, 7, 1.5)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        escuta._montando[7].tarefa.cancel()

    asyncio.run(roda())
    assert chamadas == []


def test_olha_de_novo_se_o_nome_ainda_nao_veio_e_desiste_depois(monkeypatch):
    chamadas = _detector(monkeypatch, "Ei", "Ei, ahn")
    escuta = _escuta()

    async def roda():
        buffer = _fala(escuta, 7, listen._CEDO_PRIMEIRO_S)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        _fala(escuta, 7, listen._CEDO_PASSO_S)  # chegou mais fala
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        buffer.cedo.checado_s = listen._CEDO_ATE_S  # já olhou o bastante
        escuta._agenda_checagens()
        return buffer

    buffer = asyncio.run(roda())
    assert len(chamadas) == 2
    assert buffer.cedo.achou is False
    escuta.assistente.tocar_efeito.assert_not_called()


# ── o que fazer com o que o detector ouviu ───────────────────────────────────


def test_nome_no_comeco_toca_o_estalo_com_a_pessoa_ainda_falando(monkeypatch):
    _detector(monkeypatch, "Jávez, que horas")  # como o tiny escreve "Jarvis"
    escuta = _escuta()

    async def roda():
        buffer = _fala(escuta, 7, 1.0)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        return buffer

    buffer = asyncio.run(roda())
    assert buffer.cedo.achou is True
    escuta.assistente.tocar_efeito.assert_called_once()
    assert 7 in escuta._sink._buffers  # a frase nem acabou


def test_nome_no_meio_da_conversa_nao_toca(monkeypatch):
    _detector(monkeypatch, "não, do jarvis")
    escuta = _escuta()

    async def roda():
        buffer = _fala(escuta, 7, 1.0)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        return buffer

    buffer = asyncio.run(roda())
    assert buffer.cedo.achou is False
    escuta.assistente.tocar_efeito.assert_not_called()


def test_palavras_demais_sem_o_nome_param_as_checagens(monkeypatch):
    _detector(monkeypatch, "bora jogar hoje à noite")
    escuta = _escuta()

    async def roda():
        buffer = _fala(escuta, 7, 1.0)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        return buffer

    assert asyncio.run(roda()).cedo.achou is False


def test_so_ruido_nem_chama_o_detector(monkeypatch):
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (0, b""))
    chamadas = _detector(monkeypatch)
    escuta = _escuta()

    async def roda():
        buffer = _fala(escuta, 7, 1.0)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        return buffer

    buffer = asyncio.run(roda())
    assert chamadas == [] and buffer.cedo.checado_s == pytest.approx(1.0)


def test_detector_quebrado_nao_derruba_nada(monkeypatch):
    async def quebra(pcm, **kw):
        raise RuntimeError("modelo não carregou")

    monkeypatch.setattr(listen.speech, "detecta", quebra)
    escuta = _escuta()

    async def roda():
        buffer = _fala(escuta, 7, 1.0)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        return buffer

    buffer = asyncio.run(roda())
    assert buffer.cedo.tarefa is None and buffer.cedo.achou is None  # o fim do trecho decide


# ── o fim do trecho, com o nome já ouvido ────────────────────────────────────


def _cedo(achou, texto="Jarvis, qual") -> _Cedo:
    cedo = _Cedo()
    cedo.achou, cedo.texto, cedo.checado_s = achou, texto, 1.0
    return cedo


def _transcricao(monkeypatch, *textos):
    fila = iter(textos)
    chamadas = []

    async def transcreve(pcm):
        chamadas.append(len(pcm))
        return next(fila)

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    return chamadas


async def _processa_e_despacha(escuta, *args):
    await escuta._processa(*args)
    await asyncio.sleep(0.1)  # o despacho sai depois de _JUNTA_S


def test_frase_longa_com_o_nome_ouvido_vai_direto_sem_transcrever_de_novo(monkeypatch):
    chamadas = _transcricao(monkeypatch)
    escuta = _escuta()
    tres_segundos = UM_SEGUNDO * 3

    asyncio.run(_processa_e_despacha(escuta, 7, tres_segundos, time.monotonic(), _cedo(True)))

    assert chamadas == []  # nenhuma transcrição no fim: o Whisper maior é pulado
    assert [(u, pcm) for u, _, pcm in escuta.despachadas] == [(7, tres_segundos)]
    escuta.assistente.tocar_efeito.assert_not_called()  # o estalo já tinha tocado


def test_frase_longa_que_comeca_com_palavra_de_comando_e_pergunta(monkeypatch):
    """O detector viu "Jarvis, para" — mas a frase seguiu: "…para que serve o bicarbonato?"."""
    _transcricao(monkeypatch)
    escuta = _escuta()

    asyncio.run(_processa_e_despacha(escuta, 7, UM_SEGUNDO * 3, time.monotonic(), _cedo(True, "Jarvis, para")))

    assert len(escuta.despachadas) == 1
    escuta.assistente.cala.assert_not_called()


def test_frase_curta_com_o_nome_ouvido_confere_o_comando(monkeypatch):
    _transcricao(monkeypatch, "Jarvis, para.")
    escuta = _escuta()

    asyncio.run(_processa_e_despacha(escuta, 7, UM_SEGUNDO, time.monotonic(), _cedo(True, "Jarvis")))

    escuta.assistente.cala.assert_called()  # parou de falar
    assert escuta.despachadas == []
    escuta.assistente.tocar_efeito.assert_not_called()  # sem segundo estalo


def test_transcricao_final_perdeu_o_nome_vale_o_detector(monkeypatch):
    _transcricao(monkeypatch, "Jardim, que horas são?")  # o base errou onde o tiny acertou
    escuta = _escuta()

    asyncio.run(_processa_e_despacha(escuta, 7, UM_SEGUNDO, time.monotonic(), _cedo(True, "Jávez, que horas")))

    assert len(escuta.despachadas) == 1


def test_sem_o_nome_cedo_o_fim_do_trecho_ainda_pode_achar(monkeypatch):
    """O detector é rápido, não infalível: o Whisper maior no fim é a rede de segurança."""
    _transcricao(monkeypatch, "Jarvis, que horas são?")
    escuta = _escuta()

    asyncio.run(_processa_e_despacha(escuta, 7, UM_SEGUNDO * 3, time.monotonic(), _cedo(False, "Já do que você")))

    assert len(escuta.despachadas) == 1
    escuta.assistente.tocar_efeito.assert_called_once()  # o estalo, mais tarde


def test_trecho_que_acabou_com_o_detector_rodando_espera_ele(monkeypatch):
    _transcricao(monkeypatch)
    escuta = _escuta()

    async def roda():
        cedo = _Cedo()

        async def detector_lento():
            await asyncio.sleep(0.05)
            cedo.achou, cedo.texto = True, "Jarvis, conta"

        cedo.tarefa = asyncio.create_task(detector_lento())
        await _processa_e_despacha(escuta, 7, UM_SEGUNDO * 3, time.monotonic(), cedo)

    asyncio.run(roda())
    assert len(escuta.despachadas) == 1


# ── o sink ───────────────────────────────────────────────────────────────────


def test_sink_mostra_quem_esta_falando_e_entrega_o_comeco():
    sink = CommandSink()
    sink.write(SimpleNamespace(pcm=UM_SEGUNDO), SimpleNamespace(id=7))

    [(user_id, buffer, falado, parado)] = sink.abertos()
    assert user_id == 7 and falado == pytest.approx(1.0) and parado < 0.5
    assert sink.comeco(buffer, 100) == UM_SEGUNDO[:100]


def test_colheita_leva_o_que_ja_se_sabe_do_trecho(monkeypatch):
    monkeypatch.setattr(config, "VOICE_GAP_S", 0.0)
    sink = CommandSink()
    sink.write(SimpleNamespace(pcm=UM_SEGUNDO), SimpleNamespace(id=7))
    sink._buffers[7].cedo.achou = True

    [(user_id, pcm, _born, cedo)] = sink.harvest()

    assert user_id == 7 and pcm == UM_SEGUNDO and cedo.achou is True


def test_desligar_cancela_as_checagens(monkeypatch):
    async def detector_eterno(pcm, **kw):
        await asyncio.sleep(10)

    monkeypatch.setattr(listen.speech, "detecta", detector_eterno)
    escuta = _escuta()

    async def roda():
        _fala(escuta, 7, 1.0)
        escuta._agenda_checagens()
        await asyncio.sleep(0.01)
        [tarefa] = escuta._checagens
        escuta.stop()
        await asyncio.sleep(0)
        return tarefa

    assert asyncio.run(roda()).cancelled()


# ── o detector (services/speech) ─────────────────────────────────────────────


def test_detector_usa_o_whisper_rapido_e_ignora_ruido_curto(monkeypatch):
    from services import speech

    recebidos = []
    monkeypatch.setattr(speech, "_detecta_sync", lambda audio: recebidos.append(len(audio)) or "Jarvis")

    assert asyncio.run(speech.detecta(UM_SEGUNDO)) == "Jarvis"
    assert recebidos == [16000]  # 1s a 16 kHz mono
    assert asyncio.run(speech.detecta(b"\x00" * 1000)) == ""  # < 0,1s: nem tenta


# ── ajustes medidos no corpus de 4 vozes ─────────────────────────────────────


def test_primeira_olhada_e_do_tiny_e_as_seguintes_do_whisper_maior(monkeypatch):
    """Onde o tiny erra sempre do mesmo jeito ("Já do que você"), o base com
    mais contexto acerta — a segunda olhada em diante usa ele."""
    fortes = []

    async def detecta(pcm, *, forte=False):
        fortes.append(forte)
        return "Já do"  # curto: o nome ainda pode vir

    monkeypatch.setattr(listen.speech, "detecta", detecta)
    monkeypatch.setattr(listen.speech, "model_ready", lambda: True)
    escuta = _escuta()

    async def roda():
        _fala(escuta, 7, listen._CEDO_PRIMEIRO_S)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        _fala(escuta, 7, listen._CEDO_PASSO_S)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)

    asyncio.run(roda())
    assert fortes == [False, True]


def test_whisper_maior_frio_nao_e_carregado_pela_checagem(monkeypatch):
    fortes = []

    async def detecta(pcm, *, forte=False):
        fortes.append(forte)
        return "Ei"

    monkeypatch.setattr(listen.speech, "detecta", detecta)
    monkeypatch.setattr(listen.speech, "model_ready", lambda: False)  # ex.: com Groq, o base não é pré-carregado
    escuta = _escuta()

    async def roda():
        _fala(escuta, 7, listen._CEDO_PRIMEIRO_S)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)
        _fala(escuta, 7, listen._CEDO_PASSO_S)
        escuta._agenda_checagens()
        await _espera_checagens(escuta)

    asyncio.run(roda())
    assert fortes == [False, False]


def test_jarvis_dito_rapido_nao_some_como_ruido(monkeypatch):
    """Um "Jarvis." sozinho tem ~0,35s de fala depois de aparar as pontas: com o
    corte antigo (0,4s) ele era descartado e o bot nunca ouvia."""
    rapido = UM_SEGUNDO[: int(BYTES_PER_SECOND * 0.3) // 4 * 4]
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, rapido))
    _transcricao(monkeypatch, "Jarvis.")
    escuta = _escuta()

    asyncio.run(_processa_e_despacha(escuta, 7, UM_SEGUNDO[: len(UM_SEGUNDO) // 2], time.monotonic(), None))

    escuta.assistente.tocar_efeito.assert_called_once()
    assert escuta._chamados  # abriu a janela para a pergunta
