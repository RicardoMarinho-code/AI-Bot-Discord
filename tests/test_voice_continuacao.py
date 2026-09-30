"""A escuta: acha o nome, monta a pergunta (mesmo com pausa no meio) e manda.

O corte por silêncio (VOICE_GAP_S, 0,8s) parte a pergunta de quem pensa no
meio: "Jarvis, qual é… a capital da França?" viravam dois trechos, e o
segundo (sem o nome) era jogado fora. Agora a continuação entra junto.
"""
import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import messages as m
from core import intents, listen
from core.listen import BYTES_PER_SECOND, VoiceListener

UM_SEGUNDO = b"\x01\x00" * (BYTES_PER_SECOND // 2)


@pytest.fixture(autouse=True)
def jarvis(monkeypatch):
    monkeypatch.setattr(intents, "_NOMES", set())
    intents.registra_nome("Jarvis")
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    monkeypatch.setattr(listen, "_JUNTA_S", 0.02)


def _listener():
    assistente = MagicMock()
    assistente.guild.id = 1
    assistente.cala.return_value = False
    assistente.sai = AsyncMock()
    assistente.espera_efeito = AsyncMock()
    canal = MagicMock()
    canal.send = AsyncMock()
    escuta = VoiceListener(assistente, canal)
    escuta.active = True
    escuta.despachadas = []
    escuta._conversa_nova = lambda user_id, texto, pcm, **_kw: escuta.despachadas.append((user_id, texto, pcm))
    return escuta


def _transcreve(monkeypatch, *falas):
    """speech.transcribe devolve as falas em ordem e anota o tamanho do que recebeu."""
    fila = iter(falas)
    recebidos = []

    async def transcreve(pcm):
        recebidos.append(len(pcm))
        return next(fila)

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    return recebidos


async def _roda(escuta, segmentos, *, depois_s: float = 0.2):
    """Põe os trechos na fila como o ticker poria e roda o worker."""
    for user_id, pcm in segmentos:
        escuta._pendentes[user_id] = escuta._pendentes.get(user_id, 0) + 1
        escuta._segments.put_nowait((user_id, pcm, time.monotonic(), None))
    tarefa = asyncio.create_task(escuta._worker())
    fim = time.monotonic() + 2.0
    while not escuta._segments.empty() and time.monotonic() < fim:
        await asyncio.sleep(0.01)
    await asyncio.sleep(depois_s)  # a pergunta é despachada depois de _JUNTA_S
    escuta.active = False
    tarefa.cancel()
    try:
        await tarefa
    except asyncio.CancelledError:
        pass


# ── a pergunta ───────────────────────────────────────────────────────────────


def test_pergunta_com_o_nome_vai_ao_gemini_com_o_audio(monkeypatch):
    _transcreve(monkeypatch, "Jarvis, qual a capital da França?")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)]))

    assert escuta.despachadas == [(7, "Jarvis, qual a capital da França?", UM_SEGUNDO)]
    escuta.assistente.tocar_efeito.assert_called_once()  # o estalo de "estou ouvindo"


def test_continuacao_depois_da_pausa_entra_na_mesma_pergunta(monkeypatch):
    recebidos = _transcreve(monkeypatch, "Jarvis, qual é")
    escuta = _listener()
    continuacao = b"\x02\x00" * (BYTES_PER_SECOND // 2)

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO), (7, continuacao)]))

    [(user_id, _texto, pcm)] = escuta.despachadas
    assert user_id == 7
    assert pcm == UM_SEGUNDO + listen._PAUSA_ENTRE_TRECHOS + continuacao
    assert len(recebidos) == 1  # a continuação nem passa pelo Whisper


def test_fala_de_outra_pessoa_nao_entra_na_pergunta(monkeypatch):
    _transcreve(monkeypatch, "Jarvis, qual é", "e aí, tudo bem?")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO), (8, UM_SEGUNDO)]))

    assert [(u, pcm) for u, _, pcm in escuta.despachadas] == [(7, UM_SEGUNDO)]


def test_so_o_comeco_do_trecho_vai_para_o_whisper(monkeypatch):
    recebidos = _transcreve(monkeypatch, "Jarvis, me explica a teoria da relatividade")
    escuta = _listener()
    dez_segundos = UM_SEGUNDO * 10

    asyncio.run(_roda(escuta, [(7, dez_segundos)]))

    assert recebidos == [int(listen._CABECA_S * BYTES_PER_SECOND)]
    assert escuta.despachadas[0][2] == dez_segundos  # o Gemini ouve TUDO


def test_espera_a_pessoa_terminar_de_falar(monkeypatch):
    """Voltou a falar depois do corte: o trecho novo ainda está aberto no sink."""
    _transcreve(monkeypatch, "Jarvis, qual é")
    escuta = _listener()
    escuta._sink = MagicMock()
    aberto = [True]
    escuta._sink.trecho_aberto.side_effect = lambda user_id: aberto[0]

    async def roda():
        tarefa = asyncio.create_task(_roda(escuta, [(7, UM_SEGUNDO)], depois_s=0.3))
        await asyncio.sleep(0.15)
        assert escuta.despachadas == []  # ainda falando: segura
        aberto[0] = False
        await tarefa

    asyncio.run(roda())

    assert len(escuta.despachadas) == 1


def test_microfone_com_ruido_constante_nao_segura_para_sempre(monkeypatch):
    monkeypatch.setattr(listen, "_ESPERA_CONTINUACAO_MAX_S", 0.05)
    _transcreve(monkeypatch, "Jarvis, qual é")
    escuta = _listener()
    escuta._sink = MagicMock()
    escuta._sink.trecho_aberto.return_value = True

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)], depois_s=0.3))

    assert len(escuta.despachadas) == 1


def test_pergunta_gigante_sai_sem_esperar_mais(monkeypatch):
    monkeypatch.setattr(listen, "_PERGUNTA_MAX_S", 1.5)
    monkeypatch.setattr(listen, "_JUNTA_S", 5.0)  # sem o teto, esperaria
    _transcreve(monkeypatch, "Jarvis, deixa eu te contar")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO), (7, UM_SEGUNDO)]))

    assert len(escuta.despachadas) == 1


# ── só o nome, e depois a pergunta ───────────────────────────────────────────


def test_chamar_e_depois_perguntar(monkeypatch):
    _transcreve(monkeypatch, "Jarvis.", "qual a capital da França?")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO), (7, UM_SEGUNDO)]))

    assert [(u, t) for u, t, _ in escuta.despachadas] == [(7, "qual a capital da França?")]


def test_janela_de_chamar_expira(monkeypatch):
    monkeypatch.setattr(listen, "_CHAMADO_S", -1.0)
    _transcreve(monkeypatch, "Jarvis.", "qual a capital da França?")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO), (7, UM_SEGUNDO)]))

    assert escuta.despachadas == []


@pytest.mark.parametrize("ruido", ["Tchau.", "Obrigado.", "E aí"])
def test_ruido_transcrito_nao_vira_pergunta_nem_gasta_a_janela(monkeypatch, ruido):
    """17/09, bot de música: "Cabrunco." e, 4s depois, ruído transcrito como
    "Tchau." — o bot saiu da call. O Whisper inventa essas frases sobre ruído."""
    _transcreve(monkeypatch, "Jarvis.", ruido, "que horas são?")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)] * 3))

    assert [t for _, t, _ in escuta.despachadas] == ["que horas são?"]


def test_so_o_nome_interrompe_o_bot(monkeypatch):
    _transcreve(monkeypatch, "Jarvis")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)]))

    escuta.assistente.cala.assert_called()
    escuta.text_channel.send.assert_not_awaited()  # sem mensagem: o estalo já responde


# ── comandos ─────────────────────────────────────────────────────────────────


def test_para_cala_o_bot(monkeypatch):
    _transcreve(monkeypatch, "Jarvis, para")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)]))

    escuta.assistente.cala.assert_called()
    assert escuta.despachadas == []


def test_sai_da_call_despede_antes_de_sair(monkeypatch):
    _transcreve(monkeypatch, "Jarvis, sai da call")
    escuta = _listener()
    ordem = []
    escuta.text_channel.send = AsyncMock(side_effect=lambda msg: ordem.append(msg))
    escuta.assistente.sai = AsyncMock(side_effect=lambda motivo: ordem.append("saiu"))

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)]))

    assert ordem == [m.VOICE_BYE, "saiu"]


# ── o que NÃO é com o bot ────────────────────────────────────────────────────


def test_nome_no_meio_da_conversa_e_ignorado(monkeypatch):
    _transcreve(monkeypatch, "não, do jarvis lá do filme")
    escuta = _listener()

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)]))

    assert escuta.despachadas == []
    escuta.assistente.tocar_efeito.assert_not_called()  # nem o estalo


def test_fala_sem_o_nome_e_ignorada_e_o_log_nao_guarda_o_conteudo(monkeypatch, caplog):
    _transcreve(monkeypatch, "Bora jogar hoje à noite?")
    escuta = _listener()

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)]))

    assert escuta.despachadas == []
    assert "'bora'" in caplog.text  # só a primeira palavra (a tentativa de nome)
    assert "jogar" not in caplog.text


def test_trecho_atrasado_e_descartado(monkeypatch):
    _transcreve(monkeypatch)
    escuta = _listener()

    async def roda():
        escuta._pendentes[7] = 1
        escuta._segments.put_nowait((7, UM_SEGUNDO, time.monotonic() - listen.STALE_SEGMENT_S - 1, None))
        tarefa = asyncio.create_task(escuta._worker())
        await asyncio.sleep(0.05)
        escuta.active = False
        tarefa.cancel()

    asyncio.run(roda())

    assert escuta.despachadas == []
    assert escuta._pendentes == {}  # contado e descontado


def test_ruido_longo_descartado_vai_para_o_log(monkeypatch, caplog):
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (1, b""))
    monkeypatch.setattr(listen, "peak_rms", lambda pcm: config.VOICE_MIN_RMS)
    escuta = _listener()

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda(escuta, [(7, UM_SEGUNDO * 2)]))

    assert "descartado como ruído" in caplog.text


# ── ciclo de vida da escuta ──────────────────────────────────────────────────


def test_desligar_cancela_a_pergunta_montando_e_a_conversa(monkeypatch):
    escuta = _listener()
    esquecidos = []
    monkeypatch.setattr(listen.gemini, "esquece", esquecidos.append)

    async def roda():
        escuta._comeca_pergunta(7, "Jarvis, qual é", UM_SEGUNDO)
        escuta._conversa = asyncio.create_task(asyncio.sleep(10))
        tarefa_da_montagem = escuta._montando[7].tarefa
        conversa = escuta._conversa
        escuta.stop()
        await asyncio.sleep(0)
        assert tarefa_da_montagem.cancelled() and conversa.cancelled()

    asyncio.run(roda())

    assert escuta._montando == {} and escuta._conversa is None
    assert esquecidos == [1]  # a próxima escuta começa outra conversa


def test_ticker_conta_os_trechos_de_cada_pessoa(monkeypatch):
    escuta = _listener()
    escuta._sink = MagicMock()
    colheitas = iter([[(7, UM_SEGUNDO, 0.0, None), (7, UM_SEGUNDO, 0.0, None)]])
    escuta._sink.harvest.side_effect = lambda: next(colheitas, [])
    monkeypatch.setattr(listen, "_TICK_S", 0.001)

    async def roda():
        tarefa = asyncio.create_task(escuta._ticker())
        await asyncio.sleep(0.02)
        escuta.active = False
        await tarefa

    asyncio.run(roda())

    assert escuta._pendentes == {7: 2}
    assert escuta._continua_falando(7)


def test_cabeca_nao_parte_amostra():
    pcm = b"\x00" * (int(listen._CABECA_S * BYTES_PER_SECOND) + 3)
    assert len(listen.cabeca(pcm)) % 4 == 0
    assert listen.cabeca(b"\x00" * 8) == b"\x00" * 8


def test_na_janela_da_conversa_a_continuacao_vira_pergunta(monkeypatch):
    """Modo conversa: depois da resposta, "e a de Portugal?" sem o nome."""
    _transcreve(monkeypatch, "E a de Portugal?")
    escuta = _listener()
    escuta._chamados[7] = time.monotonic() + 8

    asyncio.run(_roda(escuta, [(7, UM_SEGUNDO)]))

    assert [t for _, t, _ in escuta.despachadas] == ["E a de Portugal?"]
