"""O assistente na call: o estalo, a fala, interromper, sair e com quem fala."""
import asyncio
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import numpy as np
import pytest
from apoio import FakeChannel, FakeGuild, FakeMember, FakeVoice, faz_assistente

import config
import core.assistente as assistente_mod
import messages as m
from core import audio
from core.assistente import entra_na_call
from core.audio import FRAME_BYTES, FalaAoVivo, FonteDaFala


@pytest.fixture(autouse=True)
def _som_ligado(monkeypatch):
    monkeypatch.setattr(config, "VOICE_SOM_ESCUTA", True)


def _frame(valor: int) -> bytes:
    return np.full(FRAME_BYTES // 2, valor, dtype=np.int16).tobytes()


# ── o arquivo do estalo ──────────────────────────────────────────────────────


def test_estalo_do_repositorio_carrega_em_frames_inteiros():
    pcm = audio.carrega_efeito(audio.EFEITO_ESCUTA_PATH)
    assert pcm and len(pcm) % FRAME_BYTES == 0


def test_wav_no_formato_errado_e_recusado(tmp_path):
    caminho = tmp_path / "mono.wav"
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 100)

    with pytest.raises(ValueError):
        audio.carrega_efeito(str(caminho))


def test_sem_arquivo_segue_sem_som(monkeypatch):
    monkeypatch.setattr(audio, "EFEITO_ESCUTA_PATH", "nao-existe.wav")
    monkeypatch.setattr(audio, "_efeito_escuta", None)
    assert audio.efeito_escuta() == b""


# ── estalo e fala ────────────────────────────────────────────────────────────


def test_estalo_toca_e_segura_o_tempo_dele():
    sessao = faz_assistente()
    sessao.tocar_efeito(_frame(1) * 50)  # 1s

    voz = sessao.voice
    assert isinstance(voz.tocados[0], discord.PCMAudio)
    assert sessao._efeito_ate > 0


def test_estalo_desligado_nao_toca(monkeypatch):
    monkeypatch.setattr(config, "VOICE_SOM_ESCUTA", False)
    sessao = faz_assistente()
    sessao.tocar_efeito(_frame(1))
    assert sessao.voice.tocados == []


def test_fora_da_call_nada_toca():
    sessao = faz_assistente(voice=FakeVoice(connected=False))
    sessao.tocar_efeito(_frame(1))
    assert sessao.fala_ao_vivo() is None
    assert sessao.voice.tocados == []


def test_fala_toca_como_fonte_ao_vivo():
    sessao = faz_assistente()
    fala = sessao.fala_ao_vivo()

    fonte = sessao.voice.tocados[0]
    assert isinstance(fonte, FonteDaFala) and fonte.fala is fala


def test_chamar_de_novo_corta_a_resposta():
    """O estalo de uma chamada nova interrompe o bot no meio da resposta."""
    sessao = faz_assistente()
    fala = sessao.fala_ao_vivo()
    fala.escreve(_frame(1) * 20)

    sessao.tocar_efeito(_frame(2))

    assert not fala.ativa()
    assert sessao.voice.paradas == 1  # o som anterior foi cortado para o novo entrar


def test_fala_nova_corta_a_anterior_e_cala_diz_se_falava():
    sessao = faz_assistente()
    primeira = sessao.fala_ao_vivo()
    primeira.escreve(_frame(1) * 10)
    segunda = sessao.fala_ao_vivo()
    segunda.escreve(_frame(1) * 10)

    assert not primeira.ativa()
    assert sessao.cala() is True
    assert sessao.cala() is False


def test_fim_de_um_som_velho_nao_mexe_no_novo():
    sessao = faz_assistente()
    sessao.tocar_efeito(_frame(1))
    fim_do_estalo = sessao.voice.after
    sessao.fala_ao_vivo()

    fim_do_estalo(None)  # o py-cord avisa o fim do estalo DEPOIS de a fala começar

    assert sessao._tocando is True


def test_espera_o_estalo_acabar(monkeypatch):
    sessao = faz_assistente()
    agora = [100.0]
    monkeypatch.setattr(assistente_mod.time, "monotonic", lambda: agora[0])
    sessao._efeito_ate = 100.05
    esperas = []

    async def dorme(segundos):
        esperas.append(segundos)

    monkeypatch.setattr(assistente_mod.asyncio, "sleep", dorme)
    asyncio.run(sessao.espera_efeito())

    assert esperas == [pytest.approx(0.05)]


# ── sair ─────────────────────────────────────────────────────────────────────


def test_sair_para_a_escuta_cala_e_desconecta():
    sessao = faz_assistente()
    escuta = MagicMock()
    sessao.escuta = escuta
    fala = sessao.fala_ao_vivo()
    fala.escreve(_frame(1) * 10)

    asyncio.run(sessao.sai("teste"))

    escuta.stop.assert_called_once()
    assert sessao.escuta is None
    assert not fala.ativa()
    assert sessao.voice.desconectou


def _canal_de_voz(*membros):
    return SimpleNamespace(members=list(membros))


def test_sai_sozinho_quando_a_call_esvazia(monkeypatch):
    monkeypatch.setattr(assistente_mod, "EMPTY_LEAVE_S", 0.01)
    canal_texto = FakeChannel()
    sessao = faz_assistente(voice=FakeVoice(channel=_canal_de_voz()), channel=canal_texto)

    async def roda():
        sessao.schedule_empty_leave()
        await asyncio.sleep(0.1)

    asyncio.run(roda())

    assert sessao.voice.desconectou
    assert canal_texto.sent == [m.AUTO_LEFT_EMPTY]


def test_nao_sai_se_alguem_voltou(monkeypatch):
    monkeypatch.setattr(assistente_mod, "EMPTY_LEAVE_S", 0.01)
    canal = _canal_de_voz()
    sessao = faz_assistente(voice=FakeVoice(channel=canal))

    async def roda():
        sessao.schedule_empty_leave()
        canal.members.append(FakeMember(7, "Ana"))  # voltou antes do prazo
        await asyncio.sleep(0.1)

    asyncio.run(roda())

    assert not sessao.voice.desconectou


def test_cancelar_a_saida_segura_o_bot(monkeypatch):
    monkeypatch.setattr(assistente_mod, "EMPTY_LEAVE_S", 0.05)
    sessao = faz_assistente(voice=FakeVoice(channel=_canal_de_voz()))

    async def roda():
        sessao.schedule_empty_leave()
        await asyncio.sleep(0)
        sessao.cancel_empty_leave()
        await asyncio.sleep(0.1)

    asyncio.run(roda())

    assert not sessao.voice.desconectou


def test_pessoas_nao_conta_bots():
    canal = _canal_de_voz(FakeMember(1, "Ana"), FakeMember(2, "Outro bot", bot=True))
    sessao = faz_assistente(voice=FakeVoice(channel=canal))
    assert [p.display_name for p in sessao.pessoas()] == ["Ana"]


# ── com quem o Gemini está falando ───────────────────────────────────────────


def test_nome_vem_do_cache():
    guild = FakeGuild(FakeVoice(), membros=[FakeMember(7, "Ricardo")])
    sessao = faz_assistente(guild=guild)
    assert asyncio.run(sessao.nome_de(7)) == "Ricardo"
    assert guild.buscados == []


def test_nome_fora_do_cache_busca_uma_vez_na_api():
    """Sem o intent de membros, quem já estava na call não está no cache."""
    guild = FakeGuild(FakeVoice(), remotos=[FakeMember(7, "Ricardo")])
    sessao = faz_assistente(guild=guild)

    assert asyncio.run(sessao.nome_de(7)) == "Ricardo"
    assert asyncio.run(sessao.nome_de(7)) == "Ricardo"
    assert guild.buscados == [7]


def test_nome_desconhecido_fica_vazio():
    sessao = faz_assistente(guild=FakeGuild(FakeVoice()))
    assert asyncio.run(sessao.nome_de(7)) == ""
    assert asyncio.run(sessao.nome_de(-1234)) == ""  # áudio sem pessoa mapeada


# ── entrar na call ───────────────────────────────────────────────────────────


def test_entrar_sem_estar_numa_call_avisa():
    ctx = MagicMock()
    ctx.author.voice = None
    ctx.respond = AsyncMock()

    assert asyncio.run(entra_na_call(ctx)) is None
    ctx.respond.assert_awaited_once_with(m.JOIN_VOICE_FIRST)


def test_ja_conectado_em_outro_canal_so_muda_de_canal():
    voz = FakeVoice(channel="canal-velho")
    voz.move_to = AsyncMock()
    sessao = faz_assistente(voice=voz)

    asyncio.run(sessao.conecta("canal-novo"))

    voz.move_to.assert_awaited_once_with("canal-novo")


def test_voz_zumbi_e_derrubada_antes_de_conectar():
    voz = FakeVoice(connected=False)
    sessao = faz_assistente(voice=voz)
    canal = MagicMock()
    canal.connect = AsyncMock()

    asyncio.run(sessao.conecta(canal))

    assert voz.desconectou
    canal.connect.assert_awaited_once()


def test_fala_ao_vivo_e_um_objeto_de_verdade():
    sessao = faz_assistente()
    assert isinstance(sessao.fala_ao_vivo(), FalaAoVivo)


def test_silencio_do_comeco_do_estalo_e_cortado(tmp_path):
    """O arquivo do estalo começava com ~30 ms de nada: atraso puro."""
    caminho = tmp_path / "com_silencio.wav"
    silencio = np.zeros(48000 // 10 * 2, dtype=np.int16)  # 100 ms
    som = np.full(4800 * 2, 10000, dtype=np.int16)
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(np.concatenate([silencio, som]).tobytes())

    pcm = np.frombuffer(audio.carrega_efeito(str(caminho)), dtype=np.int16)

    assert pcm[0] != 0  # começa já no som
    assert len(pcm) * 2 % FRAME_BYTES == 0
