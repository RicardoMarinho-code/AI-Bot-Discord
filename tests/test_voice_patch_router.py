"""Teardown do PacketRouter não pode explodir por gravação já parada.

O `finally` do `PacketRouter.run()` do py-cord 2.8.1 chama
`client.stop_recording()` SEM checar se ainda está gravando. Como o bot para
a gravação primeiro (core/listen.py), a thread do router morria com
`RecordingException: You are not recording` — 36 tracebacks no journal de
produção, um por vez que a escuta foi desligada.
"""
import pytest

from core import pycord_voice_patch as patch


class FakeClient:
    def __init__(self, gravando: bool) -> None:
        self.gravando = gravando
        self.paradas = 0

    def is_recording(self) -> bool:
        return self.gravando

    def stop_recording(self) -> None:
        if not self.gravando:
            raise RuntimeError("You are not recording")
        self.gravando = False
        self.paradas += 1


class FakeReader:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.error = None


class FakeWaiter:
    def __init__(self) -> None:
        self.limpezas = 0

    def clear(self) -> None:
        self.limpezas += 1


class FakeRouter:
    run = patch._fixed_router_run

    def __init__(self, client: FakeClient, boom: Exception | None = None) -> None:
        self.reader = FakeReader(client)
        self.waiter = FakeWaiter()
        self._boom = boom
        self.rodou = False

    def _do_run(self) -> None:
        self.rodou = True
        if self._boom:
            raise self._boom


def test_gravacao_ja_parada_nao_explode():
    router = FakeRouter(FakeClient(gravando=False))
    router.run()  # não pode levantar
    assert router.waiter.limpezas == 1


def test_gravacao_ativa_ainda_e_parada():
    """A limpeza do upstream continua valendo quando ela é de fato necessária."""
    client = FakeClient(gravando=True)
    router = FakeRouter(client)
    router.run()
    assert client.paradas == 1
    assert router.waiter.limpezas == 1


def test_erro_no_loop_vai_para_o_reader_e_nao_sobe():
    boom = ValueError("frame ruim")
    router = FakeRouter(FakeClient(gravando=True), boom=boom)
    router.run()
    assert router.reader.error is boom
    assert router.waiter.limpezas == 1


def test_waiter_e_limpo_mesmo_se_o_stop_falhar():
    class ClientRuim(FakeClient):
        def stop_recording(self):
            raise RuntimeError("outro problema qualquer")

    router = FakeRouter(ClientRuim(gravando=True))
    router.run()  # não pode levantar
    assert router.waiter.limpezas == 1


def test_apply_instala_o_patch_no_pycord():
    router = pytest.importorskip("discord.voice.receive.router")
    patch.apply()
    # PacketRouter e quem tem o `finally` sem checagem (router.py:124)
    assert router.PacketRouter.run is patch._fixed_router_run
    assert router.SinkEventRouter.run is patch._fixed_sink_event_router_run
