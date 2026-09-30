"""O /voz (troca a voz do bot no servidor) e o /lembretes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import messages as m
from cogs.voice import _PADRAO, Voice
from core import lembretes, listen
from services import gemini


def _ctx(guild_id=42, user_id=7):
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id), author=SimpleNamespace(id=user_id), respond=AsyncMock(),
    )


def test_voz_troca_e_confirma():
    cog, ctx = Voice(MagicMock()), _ctx()
    try:
        asyncio.run(Voice.voz.callback(cog, ctx, "Charon"))
        assert gemini.voz_de(42) == "Charon"
        ctx.respond.assert_awaited_once_with(
            m.VOICE_CHANGED.format(voz="Charon", estilo=gemini.VOZES["Charon"]),
        )
    finally:
        gemini.escolhe_voz(42, None)


def test_esquecer_apaga_a_memoria_do_servidor_e_so_dele():
    gemini._lembra(42, "capital da França?", "Paris.")
    gemini._lembra(43, "e a do Japão?", "Tóquio.")
    ctx = _ctx()

    asyncio.run(Voice.esquecer.callback(Voice(MagicMock()), ctx))

    assert gemini._memoria(42) == [] and gemini._memoria(43)
    ctx.respond.assert_awaited_once_with(m.FORGOT)
    gemini.esquece(43)


def test_voz_volta_ao_padrao():
    cog, ctx = Voice(MagicMock()), _ctx()
    gemini.escolhe_voz(42, "Puck")

    asyncio.run(Voice.voz.callback(cog, ctx, _PADRAO))

    assert 42 not in gemini._voz_do_servidor
    ctx.respond.assert_awaited_once_with(m.VOICE_RESET)


def _com_lembretes(roda):
    """Roda `roda(escuta)` num loop com dois lembretes do 7 e um do 8 agendados."""
    escuta = listen.VoiceListener(MagicMock(), MagicMock())
    escuta.assistente.guild.id = 42

    async def tudo():
        escuta._agenda_lembrete(7, 600, "tirar a pizza")
        escuta._agenda_lembrete(7, 60, "")
        escuta._agenda_lembrete(8, 60, "do outro")
        try:
            await roda()
        finally:
            for tarefa in list(lembretes.pendentes):
                tarefa.cancel()
            lembretes.pendentes.clear()

    asyncio.run(tudo())


def test_lembretes_lista_so_os_seus_do_mais_proximo_ao_mais_longe():
    ctx = _ctx()

    _com_lembretes(lambda: Voice.lembretes.callback(Voice(MagicMock()), ctx, False))

    texto = ctx.respond.await_args.args[0]
    assert ctx.respond.await_args.kwargs["ephemeral"] is True
    assert texto.index(m.REMINDER_SEM_TEXTO) < texto.index("tirar a pizza")
    assert "do outro" not in texto and "<t:" in texto


def test_lembretes_cancelar_apaga_so_os_seus():
    ctx = _ctx()
    sobraram = []

    async def roda():
        await Voice.lembretes.callback(Voice(MagicMock()), ctx, True)
        sobraram.extend(lem.texto for lem in lembretes.pendentes.values())

    _com_lembretes(roda)

    ctx.respond.assert_awaited_once_with(m.REMINDERS_CANCELED.format(n=2), ephemeral=True)
    assert sobraram == ["do outro"]


def test_sem_lembretes_ensina_a_pedir():
    ctx = _ctx(user_id=99)

    _com_lembretes(lambda: Voice.lembretes.callback(Voice(MagicMock()), ctx, False))

    ctx.respond.assert_awaited_once_with(m.REMINDERS_NONE, ephemeral=True)


def test_voz_escolhida_volta_depois_de_reiniciar(monkeypatch):
    gemini.escolhe_voz(42, "Orus")
    gemini.escolhe_voz(43, "Kore")
    gemini.escolhe_voz(43, None)
    monkeypatch.setattr(gemini, "_voz_do_servidor", {})  # o bot reiniciou

    gemini.carrega_vozes()

    assert gemini._voz_do_servidor == {42: "Orus"}


def test_voz_que_saiu_da_lista_e_arquivo_estragado_nao_quebram(monkeypatch):
    monkeypatch.setattr(gemini, "_voz_do_servidor", {})
    with open(gemini._ARQUIVO_VOZES, "w", encoding="utf-8") as f:
        f.write('{"42": "VozQueSumiu", "44": "Charon"}')
    gemini.carrega_vozes()
    assert gemini._voz_do_servidor == {44: "Charon"}

    with open(gemini._ARQUIVO_VOZES, "w", encoding="utf-8") as f:
        f.write("[1, 2")
    gemini.carrega_vozes()  # só loga
