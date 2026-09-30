"""O keepalive UDP da recepção de voz tem de sair a cada segundos, não a cada 83 minutos.

py-cord 2.8.1, `discord/voice/receive/reader.py`: `UDPKeepAlive.delay = 5000`
vai direto para `time.sleep(self.delay)` — 5000 SEGUNDOS (issue upstream #3388,
aberta em 13/09/2026). Sem keepalive o Discord para de mandar voz depois de
alguns minutos. Com música tocando o próprio áudio mantém o caminho UDP vivo;
com o bot PARADO e só ouvindo (/ouvir sem música) é o cenário em que ele
"para de escutar" sem uma linha no journal.
"""
import pytest

from core import pycord_voice_patch as patch


def test_keepalive_sai_a_cada_poucos_segundos():
    reader = pytest.importorskip("discord.voice.receive.reader")
    patch.apply()

    assert reader.UDPKeepAlive.delay == patch.KEEPALIVE_UDP_S
    assert 1 <= patch.KEEPALIVE_UDP_S <= 15


def test_o_valor_de_fabrica_era_mesmo_o_bug():
    """Se o upstream consertar (delay pequeno), este teste avisa que o item 8
    do patch pode sair."""
    pytest.importorskip("discord.voice.receive.reader")
    patch.apply()

    assert patch.keepalive_de_fabrica() >= 1000, (
        "o py-cord mudou o keepalive — reveja o item 8 do patch"
    )


def test_apply_continua_idempotente():
    reader = pytest.importorskip("discord.voice.receive.reader")
    patch.apply()
    patch.apply()

    assert reader.UDPKeepAlive.delay == patch.KEEPALIVE_UDP_S
