"""Boas-vindas ao entrar num servidor novo: sem isso o bot entrava e ficava
mudo, e ninguém descobria que existe o /entrar."""
import asyncio
from unittest.mock import MagicMock

import messages as m
from cogs.lifecycle import Lifecycle


class FakeCanal:
    def __init__(self, pode_escrever: bool = True, nome: str = "geral", quebra: bool = False) -> None:
        self.pode_escrever = pode_escrever
        self.quebra = quebra
        self.name = nome
        self.enviadas: list = []

    def permissions_for(self, membro):
        p = MagicMock()
        p.send_messages = self.pode_escrever
        return p

    async def send(self, content=None, **kwargs):
        if self.quebra:
            raise RuntimeError("sem permissão de verdade")
        self.enviadas.append(content)


class FakeGuild:
    def __init__(self, canais, system_channel=None) -> None:
        self.id = 1
        self.name = "Servidor Novo"
        self.text_channels = canais
        self.system_channel = system_channel
        self.me = MagicMock()


def _cog() -> Lifecycle:
    return Lifecycle(MagicMock())


def test_manda_boas_vindas_no_canal_de_sistema():
    sistema = FakeCanal(nome="boas-vindas")
    outro = FakeCanal(nome="geral")

    asyncio.run(_cog().on_guild_join(FakeGuild([outro, sistema], system_channel=sistema)))

    assert sistema.enviadas == [m.GUILD_WELCOME]
    assert outro.enviadas == []


def test_sem_canal_de_sistema_usa_o_primeiro_que_da_para_escrever():
    mudo = FakeCanal(pode_escrever=False, nome="anuncios")
    bom = FakeCanal(nome="geral")

    asyncio.run(_cog().on_guild_join(FakeGuild([mudo, bom])))

    assert mudo.enviadas == [] and bom.enviadas == [m.GUILD_WELCOME]


def test_erro_num_canal_tenta_o_proximo_e_nunca_levanta():
    quebrado = FakeCanal(nome="quebrado", quebra=True)
    bom = FakeCanal(nome="geral")

    asyncio.run(_cog().on_guild_join(FakeGuild([quebrado, bom])))

    assert bom.enviadas == [m.GUILD_WELCOME]


def test_mensagem_ensina_o_basico():
    assert "/entrar" in m.GUILD_WELCOME
    assert "/ajuda" in m.GUILD_WELCOME
    assert m.NOME in m.GUILD_WELCOME
