"""Registro de uso: o journal passa a saber quem pediu o quê.

Até 20/09/2026 não havia uma linha dizendo que alguém rodou /play, clicou num
botão ou saiu da call — só o que o player fez por dentro.
"""
import asyncio
import logging
from types import SimpleNamespace

import discord

from cogs.registro import Registro, descreve_opcoes


def _ctx(nome: str = "play", opcoes=None, interacao: int = 1):
    return SimpleNamespace(
        interaction=SimpleNamespace(id=interacao),
        selected_options=opcoes,
        guild=SimpleNamespace(id=77),
        command=SimpleNamespace(qualified_name=nome),
        author=SimpleNamespace(id=1234),
        channel=SimpleNamespace(name="musica"),
    )


def _cog() -> Registro:
    return Registro(SimpleNamespace(user=SimpleNamespace(id=999)))


def test_opcoes_do_comando_viram_texto():
    texto = descreve_opcoes([{"name": "busca", "value": "henrique e juliano"}, {"name": "modo", "value": "substituir"}])
    assert texto == "busca='henrique e juliano' modo='substituir'"


def test_link_gigante_e_cortado():
    texto = descreve_opcoes([{"name": "busca", "value": "https://open.spotify.com/" + "x" * 300}])
    assert len(texto) < 110 and texto.endswith("…'")


def test_comando_sem_opcoes():
    assert descreve_opcoes(None) == ""


def test_comando_sai_no_log_com_quem_e_onde(caplog):
    cog = _cog()
    with caplog.at_level(logging.INFO, logger="cogs.registro"):
        asyncio.run(cog.on_application_command(_ctx(opcoes=[{"name": "busca", "value": "sid"}])))

    assert "[77] /play busca='sid' — por 1234 em #musica" in caplog.text


def test_fim_do_comando_traz_a_duracao(caplog, monkeypatch):
    cog = _cog()
    relogio = iter([100.0, 102.5])
    monkeypatch.setattr("cogs.registro._agora", lambda: next(relogio))
    with caplog.at_level(logging.INFO, logger="cogs.registro"):
        asyncio.run(cog.on_application_command(_ctx()))
        asyncio.run(cog.on_application_command_completion(_ctx()))

    assert "/play terminou em 2.5s" in caplog.text
    assert cog._em_andamento == {}


def test_comando_que_falha_nao_fica_pendurado_na_memoria(caplog):
    cog = _cog()
    with caplog.at_level(logging.WARNING, logger="cogs.registro"):
        asyncio.run(cog.on_application_command(_ctx()))
        asyncio.run(cog.on_application_command_error(_ctx(), RuntimeError("x")))

    assert "/play FALHOU" in caplog.text
    assert cog._em_andamento == {}


def test_clique_em_botao_do_painel_sai_no_log(caplog):
    interacao = SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": "player:skip"},
        guild_id=77,
        user=SimpleNamespace(id=1234),
    )
    with caplog.at_level(logging.INFO, logger="cogs.registro"):
        asyncio.run(_cog().on_interaction(interacao))

    assert "clique em player:skip — por 1234" in caplog.text


def test_slash_command_nao_e_logado_duas_vezes(caplog):
    interacao = SimpleNamespace(type=discord.InteractionType.application_command, data={})
    with caplog.at_level(logging.INFO, logger="cogs.registro"):
        asyncio.run(_cog().on_interaction(interacao))

    assert caplog.text == ""


def _voz(canal):
    return SimpleNamespace(channel=canal)


def test_gente_saindo_do_canal_do_bot_diz_quantos_ficaram(caplog):
    canal = SimpleNamespace(members=[SimpleNamespace(bot=True), SimpleNamespace(bot=False)])
    guild = SimpleNamespace(id=77, voice_client=SimpleNamespace(channel=canal))
    membro = SimpleNamespace(id=1234, guild=guild)
    with caplog.at_level(logging.INFO, logger="cogs.registro"):
        asyncio.run(_cog().on_voice_state_update(membro, _voz(canal), _voz(None)))

    assert "1234 saiu do canal do bot — 1 ouvinte(s) agora" in caplog.text


def test_mutar_o_microfone_nao_e_evento(caplog):
    canal = SimpleNamespace(members=[])
    guild = SimpleNamespace(id=77, voice_client=SimpleNamespace(channel=canal))
    membro = SimpleNamespace(id=1234, guild=guild)
    with caplog.at_level(logging.INFO, logger="cogs.registro"):
        asyncio.run(_cog().on_voice_state_update(membro, _voz(canal), _voz(canal)))

    assert caplog.text == ""


def test_movimento_em_outro_canal_nao_interessa(caplog):
    canal_do_bot = SimpleNamespace(nome="do bot", members=[])
    outro = SimpleNamespace(nome="outro", members=[])
    guild = SimpleNamespace(id=77, voice_client=SimpleNamespace(channel=canal_do_bot))
    membro = SimpleNamespace(id=1234, guild=guild)
    with caplog.at_level(logging.INFO, logger="cogs.registro"):
        asyncio.run(_cog().on_voice_state_update(membro, _voz(None), _voz(outro)))

    assert caplog.text == ""


def test_o_proprio_bot_mudando_de_canal_sai_no_log(caplog):
    guild = SimpleNamespace(id=77, voice_client=None)
    bot_membro = SimpleNamespace(id=999, guild=guild)
    with caplog.at_level(logging.INFO, logger="cogs.registro"):
        asyncio.run(_cog().on_voice_state_update(bot_membro, _voz("Geral"), _voz(None)))

    assert "bot na voz: Geral → fora" in caplog.text
