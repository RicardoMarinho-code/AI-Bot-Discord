"""Pacotes de voz do flush não podem ir para o lixo.

`PacketDecoder._get_next_packet` do py-cord 2.8.1: quando o jitter buffer
estoura o timeout, ele dá `flush()`, devolve só `packets[0]` e DESCARTA o
resto — logando "N packets were lost being flushed". Em produção foram 269
ocorrências, de 1 a 8 pacotes cada: até 160ms de fala jogados fora, quase
sempre no fim do trecho, que é exatamente onde está a última palavra do
comando que o Whisper precisa ouvir.
"""
import pytest

from core import pycord_voice_patch as patch


class FakeBuffer:
    """Buffer que estoura o timeout com N pacotes ainda dentro."""

    def __init__(self, conteudo: list) -> None:
        self.conteudo = list(conteudo)

    def __bool__(self) -> bool:
        return bool(self.conteudo)

    def pop(self, timeout=None):
        return None  # sempre estoura: é o caso que perde pacotes

    def flush(self):
        itens, self.conteudo = self.conteudo, []
        return itens


class FakeDecoder:
    _get_next_packet = patch._fixed_get_next_packet

    def __init__(self, conteudo: list) -> None:
        self._buffer = FakeBuffer(conteudo)
        self.ssrc = 1
        self.fakes = 0

    def _make_fakepacket(self):
        self.fakes += 1
        return "fake"


def _drena(dec: FakeDecoder, n: int) -> list:
    return [dec._get_next_packet(0.02) for _ in range(n)]


def test_pacotes_do_flush_sao_entregues_e_nao_descartados():
    dec = FakeDecoder(["p1", "p2", "p3", "p4"])
    assert _drena(dec, 4) == ["p1", "p2", "p3", "p4"]


def test_ordem_e_preservada():
    dec = FakeDecoder([f"p{i}" for i in range(8)])
    assert _drena(dec, 8) == [f"p{i}" for i in range(8)]


def test_buffer_vazio_devolve_none_como_antes():
    dec = FakeDecoder([])
    assert dec._get_next_packet(0.02) is None


def test_depois_de_drenar_volta_a_devolver_none():
    dec = FakeDecoder(["p1", "p2"])
    _drena(dec, 2)
    assert dec._get_next_packet(0.02) is None


def test_lacuna_no_flush_e_preservada():
    """None no meio = pacote perdido de verdade; o decoder trata com FEC."""
    dec = FakeDecoder(["p1", None, "p3"])
    assert _drena(dec, 3) == ["p1", None, "p3"]


def test_reset_descarta_pendentes():
    """Sem isso, um trecho antigo vazaria para dentro do próximo."""
    dec = FakeDecoder(["p1", "p2", "p3"])
    dec._get_next_packet(0.02)  # deixa p2 e p3 pendentes
    patch._limpa_pendentes(dec)
    assert dec._get_next_packet(0.02) is None


def test_contador_de_diagnostico_soma_os_recuperados():
    antes = patch.DIAG["flush_recuperados"]
    dec = FakeDecoder(["p1", "p2", "p3"])
    _drena(dec, 3)
    assert patch.DIAG["flush_recuperados"] == antes + 2


def test_apply_instala_o_patch():
    opus = pytest.importorskip("discord.opus")
    patch.apply()
    assert opus.PacketDecoder._get_next_packet is patch._fixed_get_next_packet
