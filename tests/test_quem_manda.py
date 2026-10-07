"""Quem manda no bot: o dono (quem deu o /entrar), o modo, liberar e bloquear."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.assistente as assistente_mod
import messages as m
from cogs.chat import Chat
from cogs.lifecycle import Lifecycle
from cogs.sessao import Sessao
from cogs.voice import Voice
from core.assistente import MODO_ABERTO, MODO_DONO, Assistente
from services import gemini

DONO, ANA, BETO, BOT = 1, 2, 3, 100


def _assistente(monkeypatch, na_call=(DONO, ANA, BETO), conectado=True):
    """Um Assistente de verdade numa call de mentira: na_call são os user_ids lá dentro."""
    guild = MagicMock()
    guild.id = 50
    guild.me = SimpleNamespace(id=BOT)
    canal = SimpleNamespace(
        voice_states={uid: None for uid in (*na_call, BOT)},
        members=[SimpleNamespace(id=uid, bot=uid == BOT) for uid in (*na_call, BOT)],
        mention="<#9>",
    )
    voz = MagicMock()
    voz.channel = canal
    voz.is_connected.return_value = conectado
    voz.disconnect = AsyncMock()
    monkeypatch.setattr(Assistente, "voice", property(lambda self: voz))
    sessao = Assistente(MagicMock(), guild)
    nomes = {DONO: "Ricardo", ANA: "Ana Júlia", BETO: "Beto"}
    sessao.nome_de = AsyncMock(side_effect=lambda uid: nomes.get(uid, ""))
    sessao.assume(DONO)
    return sessao


# ── as regras ────────────────────────────────────────────────────────────────


def test_modo_aberto_todo_mundo_fala_e_so_o_dono_manda(monkeypatch):
    sessao = _assistente(monkeypatch)

    assert sessao.modo == MODO_ABERTO
    assert all(sessao.pode_falar(uid) for uid in (DONO, ANA, BETO))
    assert sessao.manda(DONO) and not sessao.manda(ANA)


def test_modo_dono_so_o_dono_e_os_convidados(monkeypatch):
    sessao = _assistente(monkeypatch)
    sessao.define_modo(MODO_DONO)

    assert sessao.pode_falar(DONO) and not sessao.pode_falar(ANA)
    sessao.libera(ANA)
    assert sessao.pode_falar(ANA) and not sessao.pode_falar(BETO)


def test_bloqueado_e_ignorado_em_qualquer_modo_e_o_dono_nao_se_bloqueia(monkeypatch):
    sessao = _assistente(monkeypatch)
    sessao.bloqueia(BETO)

    assert not sessao.pode_falar(BETO)
    sessao.libera(BETO)  # liberar desfaz o bloqueio
    assert sessao.pode_falar(BETO)
    with pytest.raises(ValueError):
        sessao.bloqueia(DONO)


def test_sem_o_dono_na_call_ninguem_fica_sem_controle(monkeypatch):
    sessao = _assistente(monkeypatch, na_call=(ANA, BETO))
    sessao.dono_id = DONO  # o dono saiu e o evento ainda não chegou
    sessao.define_modo(MODO_DONO)

    assert sessao.manda(ANA) and sessao.pode_falar(BETO)


def test_outro_entrar_nao_rouba_o_dono(monkeypatch):
    sessao = _assistente(monkeypatch)
    sessao.assume(ANA)
    assert sessao.dono_id == DONO


def test_dono_saiu_quem_ficou_assume(monkeypatch):
    sessao = _assistente(monkeypatch, na_call=(ANA, BETO))
    sessao.dono_id = DONO
    sessao.bloqueia(ANA)

    assert sessao.passa_o_controle() == BETO  # bloqueado não herda o controle
    assert sessao.dono_id == BETO


def test_sair_da_call_zera_tudo(monkeypatch):
    sessao = _assistente(monkeypatch)
    sessao.define_modo(MODO_DONO)
    sessao.libera(ANA)
    sessao.bloqueia(BETO)

    asyncio.run(sessao.sai("teste"))

    assert (sessao.dono_id, sessao.modo, sessao.convidados, sessao.bloqueados) == (None, MODO_ABERTO, set(), set())


# ── por voz (o Gemini chama sessao.controle) ─────────────────────────────────


def test_controle_por_voz_acha_o_nome_sem_acento_e_em_parte(monkeypatch):
    sessao = _assistente(monkeypatch)

    assert asyncio.run(sessao.controle("modo", "Dono")) == {"resultado": "modo dono"}
    assert asyncio.run(sessao.controle("liberar", "ana julia")) == {"resultado": "Ana Júlia liberado"}
    assert ANA in sessao.convidados
    assert asyncio.run(sessao.controle("bloquear", "beto")) == {"resultado": "Beto bloqueado"}


def test_controle_por_voz_com_nome_que_nao_esta_na_call_ou_ambiguo(monkeypatch):
    sessao = _assistente(monkeypatch)
    sessao.nome_de = AsyncMock(side_effect=lambda uid: {DONO: "Ricardo", ANA: "Ana", BETO: "Ana Paula"}[uid])

    nao_esta = asyncio.run(sessao.controle("liberar", "Pedro"))
    assert "erro" in nao_esta and "Ana" in nao_esta["na_call"]
    assert asyncio.run(sessao.controle("liberar", "Ana")) == {"resultado": "Ana liberado"}  # o exato ganha
    assert "opcoes" in asyncio.run(sessao.controle("bloquear", "an"))
    assert "erro" in asyncio.run(sessao.controle("bloquear", "Ricardo"))  # o dono
    assert "erro" in asyncio.run(sessao.controle("modo", "fechado"))


# ── o Gemini respeita quem manda ─────────────────────────────────────────────


class _Sessao:
    def __init__(self, mensagens):
        self.mensagens, self.enviado = list(mensagens), []

    async def send_client_content(self, **kw):
        self.enviado.append(("conteudo", kw))

    async def send_tool_response(self, **kw):
        self.enviado.append(("ferramenta", kw))

    async def send_realtime_input(self, **kw):
        self.enviado.append(("tempo_real", kw))

    async def receive(self):
        while self.mensagens:
            msg = self.mensagens.pop(0)
            yield msg
            if msg.server_content is not None and msg.server_content.turn_complete:
                return


class _Conexao:
    def __init__(self, sessao):
        self.sessao = sessao

    async def __aenter__(self):
        return self.sessao

    async def __aexit__(self, *_):
        return False


def _msg(falou="", fim=False):
    conteudo = SimpleNamespace(
        input_transcription=None,
        output_transcription=SimpleNamespace(text=falou) if falou else None,
        turn_complete=fim,
    )
    return SimpleNamespace(data=None, server_content=conteudo, tool_call=None)


def _ferramenta(nome, args=None):
    chamada = SimpleNamespace(id="c1", name=nome, args=args or {})
    return SimpleNamespace(data=None, server_content=None, tool_call=SimpleNamespace(function_calls=[chamada]))


@pytest.fixture
def live(monkeypatch):
    def instala(*mensagens):
        sessao = _Sessao(mensagens)
        cliente = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(
            connect=lambda model, config: _Conexao(sessao),
        )))
        monkeypatch.setattr(gemini, "_cliente", cliente)
        return sessao

    gemini.esquece(50)
    return instala


def _retorno(sessao):
    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    return kw["function_responses"][0].response


def test_quem_nao_manda_nao_tira_o_bot_da_call(live):
    sessao = live(_ferramenta(gemini._SAIR), _msg(falou="Só o Ricardo pode."), _msg(fim=True))

    resposta = asyncio.run(gemini.responde(50, b"\x00" * 1920, manda=False))

    assert not resposta.quer_sair and "erro" in _retorno(sessao)


def test_ferramentas_de_controle_pedem_o_dono_e_chamam_o_controle(live):
    sessao = live(_ferramenta(gemini._LIBERAR, {"nome": "Ana"}), _msg(falou="Feito."), _msg(fim=True))
    controle = AsyncMock(return_value={"resultado": "Ana liberado"})

    asyncio.run(gemini.responde(50, b"\x00" * 1920, manda=True, controle=controle))

    controle.assert_awaited_once_with("liberar", "Ana")
    assert _retorno(sessao) == {"resultado": "Ana liberado"}

    sessao = live(_ferramenta(gemini._MODO, {"modo": "dono"}), _msg(falou="Não."), _msg(fim=True))
    controle.reset_mock()
    asyncio.run(gemini.responde(50, b"\x00" * 1920, manda=False, controle=controle))
    controle.assert_not_awaited()
    assert "erro" in _retorno(sessao)


def test_pergunta_escrita_vai_como_texto_e_sem_audio(live):
    sessao = live(_msg(falou="Camberra."), _msg(fim=True))

    resposta = asyncio.run(gemini.responde(50, b"", texto="capital da Austrália?"))

    assert resposta.pergunta == "capital da Austrália?" and resposta.texto == "Camberra."
    [(_, kw)] = [e for e in sessao.enviado if e[0] == "conteudo"]
    assert kw["turn_complete"] is True and kw["turns"][-1].parts[0].text == "capital da Austrália?"
    assert not [e for e in sessao.enviado if e[0] == "tempo_real"]  # nada de áudio
    assert gemini._memoria(50)  # entrou na conversa, como a voz


# ── a escuta ignora quem o bot não escuta ────────────────────────────────────


def test_escuta_nem_transcreve_quem_nao_pode_falar(monkeypatch):
    from core import listen

    transcritos = []

    async def transcreve(pcm):
        transcritos.append(pcm)
        return "Jarvis, que horas são?"

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    assistente = MagicMock()
    assistente.pode_falar.side_effect = lambda uid: uid == DONO
    escuta = listen.VoiceListener(assistente, MagicMock())
    escuta.despachadas = []
    escuta._comeca_pergunta = lambda uid, *a, **k: escuta.despachadas.append(uid)
    um_segundo = b"\x01\x00" * (listen.BYTES_PER_SECOND // 2)

    asyncio.run(escuta._processa(ANA, um_segundo, listen.time.monotonic()))
    assert transcritos == [] and escuta.despachadas == []


def test_sai_da_call_de_quem_nao_manda_so_avisa(monkeypatch):
    from core import intents, listen

    monkeypatch.setattr(intents, "_NOMES", set())
    intents.registra_nome("Jarvis")

    async def transcreve(pcm):
        return "Jarvis, sai da call"

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    assistente = MagicMock()
    assistente.pode_falar.return_value = True
    assistente.manda.return_value = False
    assistente.dono_id = DONO
    assistente.sai = AsyncMock()
    canal = MagicMock()
    canal.send = AsyncMock()
    escuta = listen.VoiceListener(assistente, canal)
    um_segundo = b"\x01\x00" * (listen.BYTES_PER_SECOND // 2)

    asyncio.run(escuta._processa(ANA, um_segundo, listen.time.monotonic()))

    assistente.sai.assert_not_awaited()
    assert canal.send.await_args.args[0] == m.OWNER_ONLY.format(dono=f"<@{DONO}>")


# ── os comandos ──────────────────────────────────────────────────────────────


def _ctx(autor, guild_id=50):
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id), author=SimpleNamespace(id=autor, display_name="Ana"),
        channel=MagicMock(), respond=AsyncMock(), defer=AsyncMock(),
    )


@pytest.fixture
def registrada(monkeypatch):
    def registra(sessao):
        monkeypatch.setattr(assistente_mod, "_sessoes", {50: sessao})
        for modulo in ("cogs.sessao", "cogs.voice", "cogs.chat", "cogs.status"):
            monkeypatch.setattr(f"{modulo}.peek_assistente", lambda gid: assistente_mod._sessoes.get(gid))
        return sessao

    return registra


def test_modo_e_permitir_so_pelo_dono(monkeypatch, registrada):
    sessao = registrada(_assistente(monkeypatch))
    cog = Sessao(MagicMock())

    ctx = _ctx(ANA)
    asyncio.run(Sessao.modo.callback(cog, ctx, MODO_DONO))
    assert sessao.modo == MODO_ABERTO and ctx.respond.await_args.kwargs["ephemeral"] is True

    ctx = _ctx(DONO)
    asyncio.run(Sessao.modo.callback(cog, ctx, MODO_DONO))
    assert sessao.modo == MODO_DONO and "só o dono" in ctx.respond.await_args.args[0]

    asyncio.run(Sessao.permitir.callback(cog, _ctx(DONO), SimpleNamespace(id=ANA, mention="<@2>")))
    asyncio.run(Sessao.bloquear.callback(cog, _ctx(DONO), SimpleNamespace(id=BETO, mention="<@3>")))
    assert sessao.pode_falar(ANA) and not sessao.pode_falar(BETO)

    ctx = _ctx(DONO)
    asyncio.run(Sessao.bloquear.callback(cog, ctx, SimpleNamespace(id=DONO, mention="<@1>")))
    ctx.respond.assert_awaited_once_with(m.CANT_BLOCK_OWNER, ephemeral=True)


def test_comandos_de_dono_fora_da_call(monkeypatch, registrada):
    registrada(_assistente(monkeypatch, conectado=False))
    ctx = _ctx(DONO)

    asyncio.run(Sessao.modo.callback(Sessao(MagicMock()), ctx, MODO_DONO))

    ctx.respond.assert_awaited_once_with(m.NOT_IN_CALL, ephemeral=True)


def test_sair_e_entrar_de_quem_nao_manda_sao_recusados(monkeypatch, registrada):
    sessao = registrada(_assistente(monkeypatch))
    sessao.sai = AsyncMock()
    cog = Voice(MagicMock())

    ctx = _ctx(ANA)
    asyncio.run(Voice.sair.callback(cog, ctx))
    sessao.sai.assert_not_awaited()
    assert ctx.respond.await_args.args[0] == m.OWNER_ONLY.format(dono=f"<@{DONO}>")

    ctx = _ctx(ANA)
    asyncio.run(Voice.entrar.callback(cog, ctx))
    ctx.defer.assert_not_awaited()  # nem tentou conectar
    assert ctx.respond.await_args.args[0] == m.OWNER_ONLY_MOVE.format(dono=f"<@{DONO}>")


def test_dono_saiu_da_call_o_chat_fica_sabendo(monkeypatch, registrada):
    sessao = registrada(_assistente(monkeypatch, na_call=(ANA,)))
    sessao.dono_id = DONO
    sessao.text_channel = MagicMock()
    sessao.text_channel.send = AsyncMock()
    bot = MagicMock()
    bot.user.id = BOT
    canal = sessao.voice.channel

    asyncio.run(Lifecycle(bot).on_voice_state_update(
        SimpleNamespace(id=DONO, guild=SimpleNamespace(id=50)),
        SimpleNamespace(channel=canal), SimpleNamespace(channel=None),
    ))

    assert sessao.dono_id == ANA
    assert sessao.text_channel.send.await_args.args[0] == m.OWNER_TRANSFERRED.format(novo=f"<@{ANA}>")


# ── o /perguntar ─────────────────────────────────────────────────────────────


def _responde_falso(monkeypatch, resposta):
    chamadas = []

    async def responde(guild_id, pcm, **kw):
        chamadas.append(kw)
        if kw.get("ao_falar"):
            kw["ao_falar"](b"\x00" * 3840)
        return resposta

    monkeypatch.setattr(gemini, "responde", responde)
    return chamadas


def test_perguntar_fora_da_call_responde_so_no_chat(monkeypatch):
    monkeypatch.setattr("cogs.chat.peek_assistente", lambda gid: None)
    chamadas = _responde_falso(monkeypatch, gemini.Resposta(texto="Camberra.", pergunta="capital?"))
    ctx = _ctx(ANA)

    asyncio.run(Chat.perguntar.callback(Chat(MagicMock()), ctx, "capital da Austrália?"))

    assert chamadas[0]["texto"] == "capital da Austrália?" and chamadas[0]["ao_falar"] is None
    texto = ctx.respond.await_args.args[0]
    assert "Camberra." in texto and "capital da Austrália?" in texto


def test_perguntar_na_call_fala_e_respeita_o_modo(monkeypatch, registrada):
    sessao = registrada(_assistente(monkeypatch))
    fala = MagicMock()
    sessao.fala_ao_vivo = MagicMock(return_value=fala)
    chamadas = _responde_falso(monkeypatch, gemini.Resposta(texto="Oi!", segundos_de_fala=1.0))

    asyncio.run(Chat.perguntar.callback(Chat(MagicMock()), _ctx(ANA), "oi"))
    fala.escreve.assert_called_once()
    fala.termina.assert_called_once()
    assert chamadas[0]["manda"] is False  # a Ana pergunta, mas não manda

    sessao.define_modo(MODO_DONO)
    ctx = _ctx(BETO)
    asyncio.run(Chat.perguntar.callback(Chat(MagicMock()), ctx, "oi"))
    assert len(chamadas) == 1  # o Beto nem chegou ao Gemini
    assert ctx.respond.await_args.kwargs["ephemeral"] is True


def test_perguntar_que_falha_avisa(monkeypatch):
    monkeypatch.setattr("cogs.chat.peek_assistente", lambda gid: None)

    async def quebra(*a, **k):
        raise RuntimeError("rede")

    monkeypatch.setattr(gemini, "responde", quebra)
    ctx = _ctx(ANA)

    asyncio.run(Chat.perguntar.callback(Chat(MagicMock()), ctx, "oi"))

    ctx.respond.assert_awaited_once_with(m.VOICE_ANSWER_FAILED)
