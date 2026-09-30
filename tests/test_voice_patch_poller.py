"""Alguém sair da call não pode matar o poller do websocket de voz.

`VoiceClient._remove_ssrc` do py-cord 2.8.1 faz `self._reader.speaking_timer…`
sem checar se existe reader — e só existe enquanto o /ouvir está gravando. Com
a escuta desligada, o primeiro `client_disconnect` (alguém saiu da call)
levanta AttributeError DENTRO de `_poll_ws`, que só trata queda de conexão: a
task morre ("Task exception was never retrieved" no journal de 12/09 e 20/09)
e ninguém mais processa os eventos do gateway de voz daquela conexão.
"""
import asyncio
import logging

import pytest

from core import pycord_voice_patch as patch


class _Missing:
    """Como o MISSING do py-cord: falsy e sem atributo nenhum."""

    def __bool__(self) -> bool:
        return False


class FakeTimer:
    def __init__(self) -> None:
        self.soltos: list[int] = []

    def drop_ssrc(self, ssrc: int) -> None:
        self.soltos.append(ssrc)


class FakeReader:
    def __init__(self) -> None:
        self.speaking_timer = FakeTimer()


class FakeVoiceClient:
    _remove_ssrc = patch._fixed_remove_ssrc

    def __init__(self, reader) -> None:
        self._reader = reader
        self._id_to_ssrc = {10: 777}
        self._ssrc_to_id = {777: 10}


def test_saida_de_alguem_sem_escuta_ligada_nao_explode():
    client = FakeVoiceClient(_Missing())
    client._remove_ssrc(user_id=10)  # não pode levantar

    assert client._id_to_ssrc == {} and client._ssrc_to_id == {}


def test_com_escuta_ligada_o_timer_continua_sendo_avisado():
    reader = FakeReader()
    client = FakeVoiceClient(reader)
    client._remove_ssrc(user_id=10)

    assert reader.speaking_timer.soltos == [777]
    assert client._ssrc_to_id == {}


def test_usuario_desconhecido_nao_faz_nada():
    client = FakeVoiceClient(_Missing())
    client._remove_ssrc(user_id=99)

    assert client._id_to_ssrc == {10: 777}


def test_erro_no_hook_vira_log_e_nao_mata_o_poller(caplog):
    """Rede de segurança para o PRÓXIMO bug do upstream no mesmo lugar."""
    async def hook_bugado(self, ws, msg):
        raise AttributeError("outro atributo que o upstream esqueceu")

    protegido = patch._protege_hook(hook_bugado)
    with caplog.at_level(logging.ERROR, logger="core.pycord_voice_patch"):
        asyncio.run(protegido(object(), None, {"op": 13}))  # não pode levantar

    assert "op 13" in caplog.text


def test_cancelamento_atravessa_a_protecao():
    async def hook(self, ws, msg):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(patch._protege_hook(hook)(object(), None, {"op": 1}))


def test_apply_instala_os_dois():
    voice_client = pytest.importorskip("discord.voice.client")
    patch.apply()
    assert voice_client.VoiceClient._remove_ssrc is patch._fixed_remove_ssrc
    assert getattr(voice_client.VoiceClient._recv_hook, "_cabrunco_patched", False)
    patch.apply()  # idempotente: não embrulha o hook duas vezes
    assert not getattr(voice_client.VoiceClient._recv_hook.__wrapped__, "_cabrunco_patched", False)
