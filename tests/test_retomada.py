"""A pergunta vai ao Gemini assim que a frase fecha — e volta, se a pessoa não
tinha terminado.

Journal de 27/09 (e2-micro, duas pessoas na call): o bot levava ~5s para
começar a responder — 0,8s de silêncio para fechar o trecho, mais 0,7s de
espera por uma continuação, e só então o Gemini começava a pensar (~2s). E
quem fazia uma pausa mais longa no meio da pergunta perdia o resto: a
continuação chegava com a pergunta já enviada, era descartada ("sem o nome")
e o Gemini, com meia pergunta, pedia para repetir.

Agora a pergunta vai na hora e o Gemini pensa enquanto a escuta espera; é a
FALA do bot que não começa antes de _JUNTA_S. Se quem perguntou volta a falar
antes de o bot engrenar na resposta, o pedido é cancelado e a pergunta volta a
ser montada com o resto.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from core import intents, listen
from core.audio import FRAME_BYTES, SILENCIO, FalaAoVivo
from core.listen import BYTES_PER_SECOND, CommandSink, VoiceListener, _Despachada
from services import gemini

UM_SEGUNDO = b"\x01\x00" * (BYTES_PER_SECOND // 2)
CONTINUACAO = b"\x02\x00" * (BYTES_PER_SECOND // 2)


@pytest.fixture(autouse=True)
def jarvis(monkeypatch):
    monkeypatch.setattr(intents, "_NOMES", set())
    intents.registra_nome("Jarvis")
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))


def _escuta(*, conversa_de_verdade: bool = False):
    assistente = MagicMock()
    assistente.guild.id = 1
    assistente.cala.return_value = False
    assistente.nome_de = AsyncMock(return_value="Ricardo")
    assistente.fala_ao_vivo.return_value = None
    canal = MagicMock()
    canal.send = AsyncMock()
    escuta = VoiceListener(assistente, canal)
    escuta.active = True
    escuta._sink = CommandSink()
    escuta.despachadas = []
    if not conversa_de_verdade:
        def anota(user_id, texto, pcm, *, fim=None):
            escuta._interrompe_conversa()  # como a de verdade
            escuta.despachadas.append((user_id, pcm, fim))

        escuta._conversa_nova = anota
    return escuta


def _fala(escuta, user_id: int, segundos: float):
    """Abre (ou aumenta) o trecho de uma pessoa no sink, como a thread de áudio faria."""
    pcm = b"\x02\x00" * int(BYTES_PER_SECOND * segundos / 2)
    escuta._sink.write(SimpleNamespace(pcm=pcm), SimpleNamespace(id=user_id))
    return escuta._sink._buffers[user_id]


async def _pergunta(escuta, user_id=7, pcm=UM_SEGUNDO):
    """Uma pergunta montada e despachada (o trecho fechou agora)."""
    escuta._comeca_pergunta(user_id, "Jarvis, qual é", pcm, fim=time.monotonic())
    await asyncio.sleep(0.02)


# ── a pergunta vai na hora ───────────────────────────────────────────────────


def test_pergunta_vai_ao_gemini_sem_esperar_a_junta(monkeypatch):
    monkeypatch.setattr(listen, "_JUNTA_S", 5.0)  # antes: 5s parado antes de mandar
    escuta = _escuta()

    async def roda():
        fim = time.monotonic()
        escuta._comeca_pergunta(7, "Jarvis, que horas são?", UM_SEGUNDO, fim=fim)
        await asyncio.sleep(0.05)
        return fim

    fim = asyncio.run(roda())
    assert escuta.despachadas == [(7, UM_SEGUNDO, fim)]  # já foi, com o fim do trecho


def test_ainda_falando_a_pergunta_espera(monkeypatch):
    escuta = _escuta()

    async def roda():
        escuta._comeca_pergunta(7, "Jarvis, qual é", UM_SEGUNDO)
        _fala(escuta, 7, 0.5)  # a continuação já está chegando
        await asyncio.sleep(0.1)
        assert escuta.despachadas == []
        escuta._sink._buffers.pop(7)  # o trecho fechou (e foi para a fila)
        escuta._montando[7].tarefa.cancel()

    asyncio.run(roda())


def test_o_bot_nao_fala_antes_da_junta(monkeypatch):
    """A resposta chega cedo, mas a fala espera: quem só respirou não é atropelado."""
    async def responde(guild_id, pcm, *, quem="", ao_falar=None, na_call=None):
        ao_falar(b"\x00" * FRAME_BYTES)
        return gemini.Resposta(texto="São três horas.", segundos_de_fala=1.0)

    monkeypatch.setattr(gemini, "responde", responde)
    escuta = _escuta(conversa_de_verdade=True)

    async def roda():
        fim = time.monotonic()
        escuta._conversa_nova(7, "Jarvis, que horas são?", UM_SEGUNDO, fim=fim)
        await escuta._conversa
        return fim

    fim = asyncio.run(roda())
    escuta.assistente.fala_ao_vivo.assert_called_once_with(nao_antes=pytest.approx(fim + listen._JUNTA_S))


def test_fala_ao_vivo_espera_o_nao_antes():
    fala = FalaAoVivo(pulmao_s=0.02, nao_antes=time.monotonic() + 0.05)
    frame = b"\x01" * FRAME_BYTES
    fala.escreve(frame * 3)

    assert fala.proximo() == SILENCIO  # o áudio está na mão, mas ainda não é a hora
    time.sleep(0.06)
    assert fala.proximo() == frame


# ── quem perguntou voltou a falar ────────────────────────────────────────────


def test_voltou_a_falar_logo_depois_retoma_e_junta_o_resto(monkeypatch):
    transcritos = []

    async def transcreve(pcm):
        transcritos.append(len(pcm))
        return ""

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    escuta = _escuta()

    async def roda():
        await _pergunta(escuta)
        assert len(escuta.despachadas) == 1
        _fala(escuta, 7, listen._RETOMA_MIN_S)
        escuta._confere_retomada()
        assert 7 in escuta._montando  # voltou a ser montada
        escuta.assistente.cala.assert_called()  # e o bot calou
        await asyncio.sleep(0.1)
        assert len(escuta.despachadas) == 1  # ainda falando: segura
        # o trecho novo fecha: o ticker o colhe e o worker o analisa
        escuta._sink._buffers.pop(7)
        escuta._pendentes[7] = 1
        await escuta._processa(7, CONTINUACAO, time.monotonic(), None)
        escuta._solta_pendente(7)
        await asyncio.sleep(0.1)

    asyncio.run(roda())
    [_primeira, (user_id, pcm, _fim)] = escuta.despachadas
    assert user_id == 7
    assert pcm == UM_SEGUNDO + listen._PAUSA_ENTRE_TRECHOS + CONTINUACAO
    assert transcritos == []  # a continuação nem passa pelo Whisper


def test_retomar_cancela_o_pedido_que_ainda_pensa(monkeypatch):
    async def pensa_para_sempre(guild_id, pcm, *, quem="", ao_falar=None, na_call=None):
        await asyncio.sleep(10)

    monkeypatch.setattr(gemini, "responde", pensa_para_sempre)
    escuta = _escuta(conversa_de_verdade=True)

    async def roda():
        await _pergunta(escuta)
        conversa = escuta._conversa
        _fala(escuta, 7, listen._RETOMA_MIN_S)
        escuta._confere_retomada()
        await asyncio.sleep(0)
        assert conversa.cancelled()
        escuta._montando[7].tarefa.cancel()

    asyncio.run(roda())


def test_pouca_fala_ainda_nao_retoma():
    escuta = _escuta()

    async def roda():
        await _pergunta(escuta)
        _fala(escuta, 7, listen._RETOMA_MIN_S / 2)
        escuta._confere_retomada()

    asyncio.run(roda())
    assert escuta._montando == {} and escuta._despachada is not None


def test_ruido_nao_retoma_e_e_olhado_de_novo_com_mais_fala(monkeypatch):
    olhados = []

    def energia(pcm, thr):
        olhados.append(len(pcm))
        return (0, b"")  # só ruído

    monkeypatch.setattr(listen, "analyze_speech", energia)
    escuta = _escuta()

    async def roda():
        await _pergunta(escuta)
        _fala(escuta, 7, listen._RETOMA_MIN_S)
        escuta._confere_retomada()
        escuta._confere_retomada()  # nada novo chegou: não olha de novo
        _fala(escuta, 7, listen._RETOMA_PASSO_S)
        escuta._confere_retomada()

    asyncio.run(roda())
    assert escuta._montando == {}
    assert len(olhados) == 2
    # a segunda olhada é só o que chegou (e um pouco antes), não o trecho todo
    assert olhados[1] < int((listen._RETOMA_MIN_S + listen._RETOMA_PASSO_S) * BYTES_PER_SECOND)


def test_falando_sem_parar_a_pergunta_vai_no_limite_e_nao_fica_voltando(monkeypatch):
    """Passou de _ESPERA_CONTINUACAO_MAX_S falando: a pergunta vai com a pessoa
    ainda falando — esse mesmo trecho não é "voltou a falar" (senão, a cada 8s
    de falatório, um pedido ao Gemini era aberto e cancelado)."""
    monkeypatch.setattr(listen, "_ESPERA_CONTINUACAO_MAX_S", 0.05)
    escuta = _escuta()

    async def roda():
        escuta._comeca_pergunta(7, "Jarvis, deixa eu te contar", UM_SEGUNDO)
        _fala(escuta, 7, 1.0)  # e segue falando
        await asyncio.sleep(0.15)
        assert len(escuta.despachadas) == 1  # foi no limite
        _fala(escuta, 7, 1.0)
        escuta._confere_retomada()
        assert 7 not in escuta._montando
        # parou e voltou: esse trecho novo, sim, é continuação
        escuta._sink._buffers.pop(7)
        _fala(escuta, 7, listen._RETOMA_MIN_S)
        escuta._confere_retomada()
        assert 7 in escuta._montando
        escuta._montando[7].tarefa.cancel()

    asyncio.run(roda())


def test_fala_de_outra_pessoa_nao_retoma():
    escuta = _escuta()

    async def roda():
        await _pergunta(escuta, user_id=7)
        _fala(escuta, 8, 1.0)
        escuta._confere_retomada()

    asyncio.run(roda())
    assert escuta._montando == {}


def test_com_o_bot_falando_ha_tempo_e_reacao_nao_continuacao():
    escuta = _escuta()

    async def roda():
        await _pergunta(escuta)
        escuta._despachada.falando_desde = time.monotonic() - listen._RETOMA_FALANDO_S - 0.1
        _fala(escuta, 7, 1.0)
        escuta._confere_retomada()

    asyncio.run(roda())
    assert escuta._montando == {} and escuta._despachada is None  # a janela fechou


def test_com_o_bot_comecando_a_falar_ainda_retoma_e_marca():
    escuta = _escuta()

    async def roda():
        await _pergunta(escuta)
        escuta._despachada.falando_desde = time.monotonic() - 0.5
        _fala(escuta, 7, listen._RETOMA_MIN_S)
        escuta._confere_retomada()
        montagem = escuta._montando[7]
        montagem.tarefa.cancel()
        return montagem

    assert asyncio.run(roda()).retomou_falando is True


def test_so_corta_a_resposta_uma_vez_por_pergunta():
    """Sem fone, o microfone de quem perguntou pega a voz do bot: cada resposta
    cortada viraria outra resposta cortada."""
    escuta = _escuta()

    async def roda():
        escuta._despachada = _Despachada(
            7, "Jarvis, qual é", UM_SEGUNDO, time.monotonic(), retomou_falando=True,
            falando_desde=time.monotonic() - 0.3,
        )
        _fala(escuta, 7, 1.0)
        escuta._confere_retomada()

    asyncio.run(roda())
    assert escuta._montando == {} and escuta._despachada is None


def test_bot_ainda_calado_retoma_mesmo_depois_de_ja_ter_cortado():
    """O corte-único é para quando o bot JÁ fala (eco); calado, eco não há."""
    escuta = _escuta()

    async def roda():
        escuta._despachada = _Despachada(
            7, "Jarvis, qual é", UM_SEGUNDO, time.monotonic(), retomou_falando=True,
        )
        _fala(escuta, 7, listen._RETOMA_MIN_S)
        escuta._confere_retomada()
        escuta._montando[7].tarefa.cancel()

    asyncio.run(roda())


def test_janela_expira_se_o_gemini_nao_responde():
    escuta = _escuta()

    async def roda():
        await _pergunta(escuta)
        escuta._despachada.em = time.monotonic() - listen._RETOMA_MAX_S - 1
        _fala(escuta, 7, 1.0)
        escuta._confere_retomada()

    asyncio.run(roda())
    assert escuta._montando == {} and escuta._despachada is None


def test_resposta_terminada_fecha_a_janela(monkeypatch):
    """Depois da resposta, a fala nova é conversa (modo conversa, com memória)."""
    async def responde(guild_id, pcm, *, quem="", ao_falar=None, na_call=None):
        return gemini.Resposta(texto="São três horas.", segundos_de_fala=1.0)

    monkeypatch.setattr(gemini, "responde", responde)
    escuta = _escuta(conversa_de_verdade=True)

    async def roda():
        await _pergunta(escuta)
        await escuta._conversa

    asyncio.run(roda())
    assert escuta._despachada is None


def test_primeiro_audio_marca_quando_o_bot_comeca_a_falar(monkeypatch):
    liberar = asyncio.Event()

    async def responde(guild_id, pcm, *, quem="", ao_falar=None, na_call=None):
        ao_falar(b"\x00" * FRAME_BYTES)
        await liberar.wait()
        return gemini.Resposta(texto="ok", segundos_de_fala=1.0)

    monkeypatch.setattr(gemini, "responde", responde)
    escuta = _escuta(conversa_de_verdade=True)

    async def roda():
        await _pergunta(escuta)
        falando_desde = escuta._despachada.falando_desde
        liberar.set()
        await escuta._conversa
        return falando_desde

    falando_desde = asyncio.run(roda())
    # nunca antes de _JUNTA_S depois do fim do trecho (a fala espera)
    assert falando_desde is not None and falando_desde >= time.monotonic() - 1.0


def test_desligar_esquece_a_pergunta_despachada():
    escuta = _escuta()

    async def roda():
        await _pergunta(escuta)
        escuta.stop()

    asyncio.run(roda())
    assert escuta._despachada is None


# ── o detector do nome desligado ─────────────────────────────────────────────


def test_detector_desligado_nao_olha_o_comeco_das_frases(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_DETECTOR", "off")
    chamadas = []

    async def detecta(pcm, **kw):
        chamadas.append(1)
        return "Jarvis"

    monkeypatch.setattr(listen.speech, "detecta", detecta)
    escuta = _escuta()

    async def roda():
        _fala(escuta, 7, 1.5)
        escuta._agenda_checagens()
        await asyncio.sleep(0.02)

    asyncio.run(roda())
    assert chamadas == []
