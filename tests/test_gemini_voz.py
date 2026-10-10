"""A conversa com o Gemini Live — sem rede e sem Discord.

O serviço (services/gemini.py) com uma sessão Live falsa: a resposta chega aos
pedaços e cada pedaço é repassado na hora; a memória das últimas trocas vai
no começo da sessão seguinte; o Gemini sabe com quem fala e que horas são. E a
escuta (core/listen.py): a resposta toca enquanto chega, vai escrita para o
chat, e uma pergunta nova ou um "para" interrompe a anterior.
"""
import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import config
from core import lembretes, listen
from core.audio import FRAME_BYTES, SILENCIO, FalaAoVivo, FonteDaFala
from core.listen import VoiceListener
from services import gemini

UM_SEGUNDO = b"\x00" * listen.BYTES_PER_SECOND


def _frame(valor: int) -> bytes:
    return np.full(FRAME_BYTES // 2, valor, dtype=np.int16).tobytes()


# ── conversão de áudio ───────────────────────────────────────────────────────


def test_discord_para_16k_mono():
    um_segundo_48k_estereo = np.full(48000 * 2, 1000, dtype=np.int16).tobytes()

    amostras = np.frombuffer(gemini.pcm_discord_para_16k(um_segundo_48k_estereo), dtype=np.int16)

    assert len(amostras) == 16000
    assert abs(int(amostras[100]) - 1000) <= 1


def test_24k_para_discord_dobra_a_taxa_e_duplica_o_canal():
    saida = gemini.pcm_24k_para_discord(np.full(24000, 500, dtype=np.int16).tobytes())

    assert len(saida) == listen.BYTES_PER_SECOND  # 1s de 48 kHz estéreo
    estereo = np.frombuffer(saida, dtype=np.int16).reshape(-1, 2)
    assert (estereo[:, 0] == estereo[:, 1]).all()
    assert estereo[10, 0] == 500


def test_pedaco_do_streaming_nao_e_completado_com_zeros():
    """Completar cada pedaço até o frame poria silêncio NO MEIO da fala (estalos)."""
    pedaco = gemini.pcm_24k_para_48k_estereo(np.full(1000, 7, dtype=np.int16).tobytes())
    assert len(pedaco) == 1000 * 2 * 2 * 2
    assert gemini.pcm_24k_para_48k_estereo(b"") == b""


# ── data e hora ──────────────────────────────────────────────────────────────


def test_data_e_hora_por_extenso():
    assert gemini.descreve_agora(datetime(2026, 9, 27, 14, 5)) == "domingo, 27 de setembro de 2026, 14:05"


def test_fuso_invalido_cai_na_hora_do_computador(monkeypatch):
    monkeypatch.setattr(config, "FUSO", "Nao/Existe")
    assert gemini.agora().tzinfo is not None


def test_instrucao_diz_quem_fala_e_que_horas_sao():
    texto = gemini._instrucao("Ricardo")
    assert "Ricardo" in texto and "Agora é" in texto and config.BOT_NAME in texto
    assert "Quem está falando" not in gemini._instrucao("")


def test_instrucao_pede_boa_vontade_e_ensina_a_sair():
    """"Se o áudio vier cortado, peça para repetir" fazia o Gemini pedir para
    repetir o que dava para entender; e ele dizia "saindo!" sem sair — agora
    ele sai de verdade, pela ferramenta."""
    texto = gemini._instrucao()
    assert "boa vontade" in texto and "diga o que você entendeu" in texto
    assert gemini._SAIR in texto and "expulsando" in texto


# ── memória da conversa ──────────────────────────────────────────────────────


def test_memoria_guarda_as_trocas_e_expira(monkeypatch):
    gemini.esquece(99)
    gemini._lembra(99, "capital da França?", "Paris.")
    assert len(gemini._memoria(99)) == 2  # pergunta + resposta

    agora = time.monotonic()
    monkeypatch.setattr(gemini.time, "monotonic", lambda: agora + gemini._MEMORIA_S + 1)
    assert gemini._memoria(99) == []


def test_memoria_tem_teto():
    gemini.esquece(98)
    for i in range(gemini._MEMORIA_TROCAS + 3):
        gemini._lembra(98, f"p{i}", f"r{i}")
    assert len(gemini._memoria(98)) == 2 * gemini._MEMORIA_TROCAS


def test_troca_sem_transcricao_nao_entra_na_memoria():
    gemini.esquece(97)
    gemini._lembra(97, "", "Paris.")
    assert gemini._memoria(97) == []


# ── a sessão Live (falsa) ────────────────────────────────────────────────────


def _msg(*, data=b"", ouviu="", falou="", fim=False):
    conteudo = SimpleNamespace(
        input_transcription=SimpleNamespace(text=ouviu) if ouviu else None,
        output_transcription=SimpleNamespace(text=falou) if falou else None,
        turn_complete=fim,
    )
    return SimpleNamespace(data=data or None, server_content=conteudo, tool_call=None)


def _chama_ferramenta(nome, args=None):
    chamada = SimpleNamespace(id="c1", name=nome, args=args or {})
    return SimpleNamespace(
        data=None, server_content=None, tool_call=SimpleNamespace(function_calls=[chamada]),
    )


class _Sessao:
    def __init__(self) -> None:
        self.mensagens = []
        self.enviado = []
        self.configs = []

    async def send_client_content(self, **kw):
        self.enviado.append(("memoria", kw))

    async def send_tool_response(self, **kw):
        self.enviado.append(("ferramenta", kw))

    async def send_realtime_input(self, **kw):
        self.enviado.append(("tempo_real", kw))

    async def receive(self):
        """Como o SDK: cada chamada entrega UM turno (até o turn_complete); o que
        chega depois do fim do turno sai na chamada seguinte."""
        while self.mensagens:
            msg = self.mensagens.pop(0)
            yield msg
            if msg.server_content is not None and msg.server_content.turn_complete:
                return


class _Conexao:
    def __init__(self, sessao) -> None:
        self.sessao = sessao

    async def __aenter__(self):
        return self.sessao

    async def __aexit__(self, *_):
        return False


@pytest.fixture
def sessao(monkeypatch):
    """Instala um cliente Live falso; o teste preenche `sessao.mensagens`."""
    s = _Sessao()

    def conecta(model, config):
        s.configs.append(config)
        return _Conexao(s)

    cliente = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=conecta)))
    monkeypatch.setattr(gemini, "_cliente", cliente)
    return s


def test_resposta_chega_aos_pedacos_e_e_repassada_na_hora(sessao):
    gemini.esquece(1)
    pedaco_24k = np.full(2400, 300, dtype=np.int16).tobytes()  # 0,1s
    sessao.mensagens = [
        _msg(ouviu="capital da "), _msg(ouviu="França?"),
        _msg(data=pedaco_24k, falou="É "), _msg(data=pedaco_24k, falou="Paris."),
        _msg(fim=True),
    ]
    recebidos = []

    resposta = asyncio.run(gemini.responde(1, UM_SEGUNDO, quem="Ricardo", ao_falar=recebidos.append))

    assert len(recebidos) == 2 and all(len(p) == 2400 * 8 for p in recebidos)
    assert resposta.texto == "É Paris." and resposta.pergunta == "capital da França?"
    assert resposta.segundos_de_fala == pytest.approx(0.2)
    assert "Ricardo" in sessao.configs[0].system_instruction
    # as ferramentas: pesquisa Google (internet) e as nossas — todas declaradas,
    # sem repetir nome (a ordem não importa ao Gemini)
    [busca, ferramenta] = sessao.configs[0].tools
    assert busca.google_search is not None
    nomes = [f.name for f in ferramenta.function_declarations]
    assert len(nomes) == len(set(nomes))
    assert set(nomes) == {
        gemini._SAIR, gemini._MODO, gemini._LIBERAR, gemini._BLOQUEAR,
        gemini._SORTEAR, gemini._ESCOLHER, gemini._HORA_EM, gemini._SOBRE_A_DATA, gemini._CALCULAR,
        gemini._NA_CALL, gemini._MEUS_LEMBRETES, gemini._CANCELA_LEMBRETES, gemini._ENQUETE,
        gemini._CRONOMETRO, gemini._PLACAR, gemini._ANOTAR, gemini._MINHAS_NOTAS, gemini._APAGAR_NOTAS,
        gemini._TIMES, gemini._LEMBRETE, gemini._NOVIDADES, gemini._MEU_CODIGO,
    }
    assert not resposta.quer_sair
    # a pergunta vai marcada: início, áudio 16 kHz, fim
    tipos = [next(iter(kw)) for _, kw in sessao.enviado]
    assert tipos[0] == "activity_start" and tipos[-1] == "activity_end"
    assert gemini._memoria(1)  # a troca ficou para a próxima pergunta


def test_mandado_embora_o_gemini_chama_a_ferramenta_e_se_despede(sessao):
    """"Jarvis, ninguém te chamou, vaza": a lista curta não pega, o Gemini sim."""
    sessao.mensagens = [
        _msg(ouviu="Jarvis, ninguém te chamou, vaza"),
        _chama_ferramenta(gemini._SAIR),
        _msg(falou="Tá bom, fui!"),
        _msg(fim=True),
    ]

    resposta = asyncio.run(gemini.responde(4, UM_SEGUNDO))

    assert resposta.quer_sair and resposta.texto == "Tá bom, fui!"
    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    [retorno] = kw["function_responses"]
    assert retorno.id == "c1" and retorno.name == gemini._SAIR  # o Gemini continua a falar


def test_despedida_que_vem_num_turno_novo_depois_da_ferramenta_toca(sessao):
    """30/09, na call: o turno da ferramenta fechava sem fala e o bot saía mudo —
    a despedida vem num turno novo, depois da resposta da ferramenta."""
    pedaco_24k = np.full(2400, 300, dtype=np.int16).tobytes()
    sessao.mensagens = [
        _msg(ouviu="Jarvis, bye-bye."), _chama_ferramenta(gemini._SAIR), _msg(fim=True),
        _msg(data=pedaco_24k, falou="Tchau, até mais!"), _msg(fim=True),
    ]
    recebidos = []

    resposta = asyncio.run(gemini.responde(5, UM_SEGUNDO, ao_falar=recebidos.append))

    assert resposta.quer_sair and resposta.texto == "Tchau, até mais!" and len(recebidos) == 1


def test_sem_despedida_depois_da_ferramenta_nao_espera_para_sempre(sessao, monkeypatch):
    monkeypatch.setattr(gemini, "_DESPEDIDA_S", 0.05)
    sessao.mensagens = [_chama_ferramenta(gemini._SAIR), _msg(fim=True)]

    resposta = asyncio.run(gemini.responde(5, UM_SEGUNDO))

    assert resposta.quer_sair and not resposta.segundos_de_fala


def test_sorteio_de_verdade_e_a_fala_que_vem_depois(sessao):
    """"Jarvis, joga um dado": o número sai da ferramenta, não da cabeça do modelo,
    e a fala com o resultado vem num turno novo."""
    pedaco_24k = np.full(2400, 300, dtype=np.int16).tobytes()
    sessao.mensagens = [
        _chama_ferramenta(gemini._SORTEAR, {"minimo": 1, "maximo": 6}), _msg(fim=True),
        _msg(data=pedaco_24k, falou="Deu quatro!"), _msg(fim=True),
    ]

    resposta = asyncio.run(gemini.responde(7, UM_SEGUNDO))

    assert resposta.texto == "Deu quatro!" and not resposta.quer_sair
    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    [numero] = kw["function_responses"][0].response["numeros"]
    assert 1 <= numero <= 6


def test_sorteio_de_numeros():
    for _ in range(50):
        [n] = gemini.executa_ferramenta(gemini._SORTEAR, {"minimo": 1, "maximo": 6})["numeros"]
        assert 1 <= n <= 6
    tres = gemini.executa_ferramenta(gemini._SORTEAR, {"minimo": 1, "maximo": 60, "quantidade": 3})
    assert len(tres["numeros"]) == 3


def test_sorteio_aceita_limites_invertidos_e_poe_teto_na_quantidade():
    """O modelo manda "maximo": 1, "minimo": 10 às vezes; e ninguém quer ouvir 1000 números."""
    numeros = gemini.executa_ferramenta(
        gemini._SORTEAR, {"minimo": 10, "maximo": 1, "quantidade": 1000},
    )["numeros"]
    assert len(numeros) == gemini._SORTEIO_MAX and all(1 <= n <= 10 for n in numeros)


def test_escolhe_entre_as_opcoes():
    escolhas = {
        gemini.executa_ferramenta(gemini._ESCOLHER, {"opcoes": ["cara", "coroa"]})["escolhido"]
        for _ in range(100)
    }
    assert escolhas == {"cara", "coroa"}


def test_ferramenta_com_argumentos_ruins_nao_quebra_a_conversa():
    assert "erro" in gemini.executa_ferramenta(gemini._ESCOLHER, {"opcoes": ["", " "]})
    assert "erro" in gemini.executa_ferramenta(gemini._SORTEAR, {"minimo": "um", "maximo": 6})
    assert "erro" in gemini.executa_ferramenta("nao_existe", None)


def test_lembrete_pedido_por_voz_volta_na_resposta(sessao):
    """"Jarvis, me avisa em 10 minutos pra tirar a pizza": quem chamou agenda."""
    sessao.mensagens = [
        _chama_ferramenta(gemini._LEMBRETE, {"segundos": 600, "texto": "tirar a pizza"}),
        _msg(falou="Combinado, te aviso."), _msg(fim=True),
    ]

    resposta = asyncio.run(gemini.responde(8, UM_SEGUNDO))

    assert resposta.lembretes == [(600, "tirar a pizza")]
    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    assert "ok" in kw["function_responses"][0].response["resultado"]


def test_lembrete_fora_dos_limites_volta_como_erro_para_o_gemini(sessao):
    sessao.mensagens = [
        _chama_ferramenta(gemini._LEMBRETE, {"segundos": 7 * 24 * 3600}),
        _msg(falou="Não consigo lembrar por tanto tempo."), _msg(fim=True),
    ]

    resposta = asyncio.run(gemini.responde(8, UM_SEGUNDO))

    assert resposta.lembretes == []
    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    assert "erro" in kw["function_responses"][0].response


def test_quem_esta_na_call_so_busca_os_nomes_se_o_gemini_pedir(sessao):
    buscas = []

    async def na_call():
        buscas.append(1)
        return ["Ricardo", "Pedro"]

    sessao.mensagens = [
        _chama_ferramenta(gemini._NA_CALL), _msg(fim=True),
        _msg(falou="Deu o Pedro!"), _msg(fim=True),
    ]
    resposta = asyncio.run(gemini.responde(9, UM_SEGUNDO, na_call=na_call))

    assert resposta.texto == "Deu o Pedro!" and buscas == [1]
    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    assert kw["function_responses"][0].response == {"pessoas": ["Ricardo", "Pedro"]}

    sessao.mensagens = [_msg(falou="Paris."), _msg(fim=True)]
    asyncio.run(gemini.responde(9, UM_SEGUNDO, na_call=na_call))
    assert buscas == [1]  # sem a ferramenta, nada de ir atrás dos nomes


def test_quem_esta_na_call_sem_quem_saiba_responde_lista_vazia(sessao):
    sessao.mensagens = [_chama_ferramenta(gemini._NA_CALL), _msg(falou="Não sei."), _msg(fim=True)]

    asyncio.run(gemini.responde(9, UM_SEGUNDO))

    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    assert kw["function_responses"][0].response == {"pessoas": []}


def _pendente(monkeypatch, user_id, texto, daqui_s):
    lem = lembretes.Lembrete(guild_id=10, user_id=user_id, channel_id=5, texto=texto, vence_em=time.time() + daqui_s)
    monkeypatch.setitem(lembretes.pendentes, MagicMock(), lem)


def test_meus_lembretes_por_voz_so_os_de_quem_fala(sessao, monkeypatch):
    _pendente(monkeypatch, 7, "tirar a pizza", 600)
    _pendente(monkeypatch, 8, "do outro", 60)
    sessao.mensagens = [
        _chama_ferramenta(gemini._MEUS_LEMBRETES), _msg(falou="Tirar a pizza, daqui a 10 minutos."),
        _msg(fim=True),
    ]

    asyncio.run(gemini.responde(10, UM_SEGUNDO, user_id=7))

    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    assert kw["function_responses"][0].response == {
        "lembretes": [{"texto": "tirar a pizza", "faltam_minutos": 10}],
    }


def test_cancelar_lembretes_por_voz_so_os_de_quem_fala(sessao, monkeypatch):
    _pendente(monkeypatch, 7, "tirar a pizza", 600)
    _pendente(monkeypatch, 7, "ligar pra mãe", 60)
    _pendente(monkeypatch, 8, "do outro", 60)
    sessao.mensagens = [_chama_ferramenta(gemini._CANCELA_LEMBRETES), _msg(falou="Cancelados."), _msg(fim=True)]

    asyncio.run(gemini.responde(10, UM_SEGUNDO, user_id=7))

    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    assert kw["function_responses"][0].response == {"cancelados": 2}
    assert [lem.texto for lem in lembretes.pendentes.values()] == ["do outro"]


def test_enquete_pedida_por_voz_volta_na_resposta(sessao):
    sessao.mensagens = [
        _chama_ferramenta(gemini._ENQUETE, {"pergunta": "O que jantar?", "opcoes": ["pizza", " ", "japonês"]}),
        _msg(falou="Enquete no chat!"), _msg(fim=True),
    ]

    resposta = asyncio.run(gemini.responde(11, UM_SEGUNDO))

    assert resposta.enquetes == [("O que jantar?", ["pizza", "japonês"])]


def test_enquete_sem_opcoes_suficientes_volta_como_erro():
    assert "erro" in gemini.executa_ferramenta(gemini._ENQUETE, {"pergunta": "Sim?", "opcoes": ["sim"]})
    assert "erro" in gemini.executa_ferramenta(gemini._ENQUETE, {"pergunta": "", "opcoes": ["a", "b"]})
    muitas = [str(i) for i in range(gemini.ENQUETE_MAX_OPCOES + 1)]
    assert "erro" in gemini.executa_ferramenta(gemini._ENQUETE, {"pergunta": "?", "opcoes": muitas})


def test_enquete_vai_para_o_chat_com_uma_reacao_por_opcao(monkeypatch):
    resposta = gemini.Resposta(
        texto="Pronto.", segundos_de_fala=1.0, enquetes=[("O que jantar?", ["pizza", "japonês"])],
    )
    _responde_com(monkeypatch, resposta)
    escuta = _listener()
    postada = MagicMock()
    postada.add_reaction = AsyncMock()
    escuta.text_channel.send = AsyncMock(return_value=postada)

    _conversa(escuta)

    textos = [c.args[0] for c in escuta.text_channel.send.await_args_list]
    enquete = next(t for t in textos if "Enquete" in t)
    assert "Ricardo" in enquete and "O que jantar?" in enquete and "2️⃣ japonês" in enquete
    assert [c.args[0] for c in postada.add_reaction.await_args_list] == ["1️⃣", "2️⃣"]
    chamada = next(c for c in escuta.text_channel.send.await_args_list if "Enquete" in c.args[0])
    mencoes = chamada.kwargs["allowed_mentions"]
    assert not mencoes.everyone and not mencoes.users and not mencoes.roles


def test_enquete_sem_permissao_de_reagir_nao_derruba_a_conversa(monkeypatch):
    resposta = gemini.Resposta(texto="Pronto.", segundos_de_fala=1.0, enquetes=[("?", ["a", "b"])])
    _responde_com(monkeypatch, resposta)
    escuta = _listener()
    postada = MagicMock()
    postada.add_reaction = AsyncMock(side_effect=RuntimeError("Missing Permissions"))
    escuta.text_channel.send = AsyncMock(return_value=postada)

    _conversa(escuta)  # não levanta

    assert escuta.text_channel.send.await_count >= 1


@pytest.mark.parametrize(("segundos", "texto"), [(7.9, "7s"), (125, "2min05s"), (3725, "1h02min05s")])
def test_duracao_falada(segundos, texto):
    assert gemini.duracao_falada(segundos) == texto


def test_cronometro_inicia_ve_e_para(monkeypatch):
    relogio = [1000.0]
    monkeypatch.setattr(gemini.time, "monotonic", lambda: relogio[0])
    monkeypatch.setattr(gemini, "_cronometros", {})

    assert "erro" in gemini.cronometro(1, "ver")  # nada rodando
    assert gemini.cronometro(1, "iniciar") == {"resultado": "cronômetro iniciado"}
    relogio[0] += 125
    assert gemini.cronometro(1, "ver") == {"parado": False, "tempo": "2min05s", "segundos": 125.0}
    assert "erro" in gemini.cronometro(2, "ver")  # cada servidor tem o seu
    relogio[0] += 10
    assert gemini.cronometro(1, "parar")["tempo"] == "2min15s"
    assert "erro" in gemini.cronometro(1, "parar")


def test_cronometro_reiniciar_zera_e_acao_invalida(monkeypatch):
    monkeypatch.setattr(gemini, "_cronometros", {})
    gemini.cronometro(1, "iniciar")
    assert "zerado" in gemini.cronometro(1, " Iniciar ")["resultado"]
    assert "erro" in gemini.cronometro(1, "pausar")


def test_cronometro_pela_sessao(sessao, monkeypatch):
    monkeypatch.setattr(gemini, "_cronometros", {})
    sessao.mensagens = [
        _chama_ferramenta(gemini._CRONOMETRO, {"acao": "iniciar"}), _msg(falou="Valendo!"), _msg(fim=True),
    ]

    asyncio.run(gemini.responde(12, UM_SEGUNDO))

    assert 12 in gemini._cronometros


def test_times_equilibrados_com_todo_mundo():
    pessoas = ["Ana", "Beto", "Caio", "Duda", "Edu", "Fê", "Gui"]

    times = gemini.divide_times(pessoas, 2)["times"]

    assert sorted(len(t) for t in times) == [3, 4]
    assert sorted([p for time in times for p in time]) == sorted(pessoas)


def test_times_mudam_de_uma_vez_para_outra():
    pessoas = [str(i) for i in range(10)]
    assert len({str(gemini.divide_times(pessoas, 2)["times"]) for _ in range(20)}) > 1


def test_times_impossiveis():
    assert "erro" in gemini.divide_times(["Ana"], 2)
    assert "erro" in gemini.divide_times(["Ana", "Beto"], 1)
    assert "erro" in gemini.divide_times(["Ana", " ", ""], 2)


def test_times_sem_lista_usam_quem_esta_na_call(sessao):
    async def na_call():
        return ["Ana", "Beto", "Caio", "Duda"]

    sessao.mensagens = [
        _chama_ferramenta(gemini._TIMES, {"quantidade": 2}), _msg(falou="Times prontos!"), _msg(fim=True),
    ]
    asyncio.run(gemini.responde(13, UM_SEGUNDO, na_call=na_call))

    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    times = kw["function_responses"][0].response["times"]
    assert sorted([p for time in times for p in time]) == ["Ana", "Beto", "Caio", "Duda"] and [len(t) for t in times] == [2, 2]


def test_times_com_quantidade_estranha_nao_quebram(sessao):
    sessao.mensagens = [
        _chama_ferramenta(gemini._TIMES, {"quantidade": "dois", "pessoas": ["a", "b"]}),
        _msg(falou="Hum."), _msg(fim=True),
    ]
    asyncio.run(gemini.responde(13, UM_SEGUNDO))

    [(_, kw)] = [e for e in sessao.enviado if e[0] == "ferramenta"]
    assert "erro" in kw["function_responses"][0].response


def test_placar_soma_ordena_e_zera(monkeypatch):
    monkeypatch.setattr(gemini, "_placares", {})

    gemini.placar(1, {"acao": "somar", "nome": "Pedro"})
    gemini.placar(1, {"acao": "somar", "nome": "Ana", "pontos": 3})
    gemini.placar(1, {"acao": "somar", "nome": "pedro", "pontos": 1})  # a mesma pessoa
    gemini.placar(2, {"acao": "somar", "nome": "Outro servidor"})
    assert gemini.placar(1, {"acao": "somar", "nome": "Ana", "pontos": -1}) == {"placar": {"Ana": 2, "Pedro": 2}}
    assert list(gemini.placar(1, {"acao": "ver"})["placar"]) == ["Ana", "Pedro"]

    assert gemini.placar(1, {"acao": "zerar"}) == {"resultado": "placar zerado"}
    assert gemini.placar(1, {"acao": "ver"}) == {"placar": {}}
    assert gemini.placar(2, {"acao": "ver"}) == {"placar": {"Outro servidor": 1}}


def test_placar_com_argumentos_ruins(monkeypatch):
    monkeypatch.setattr(gemini, "_placares", {})
    assert "erro" in gemini.placar(1, {"acao": "somar"})
    assert "erro" in gemini.placar(1, {"acao": "somar", "nome": "Ana", "pontos": "muitos"})
    assert "erro" in gemini.placar(1, {"acao": "dobrar"})
    assert "erro" in gemini.placar(1, None)


def test_placar_pela_sessao(sessao, monkeypatch):
    monkeypatch.setattr(gemini, "_placares", {})
    sessao.mensagens = [
        _chama_ferramenta(gemini._PLACAR, {"acao": "somar", "nome": "Time azul", "pontos": 3}),
        _msg(falou="Três pro azul!"), _msg(fim=True),
    ]

    asyncio.run(gemini.responde(14, UM_SEGUNDO))

    assert gemini._placares[14] == {"Time azul": 3}


def test_le_lembrete_arredonda_e_corta_o_texto():
    assert gemini.le_lembrete({"segundos": "90.4", "texto": "  x" * 200}) == (90, ("  x" * 200).strip()[:200])
    with pytest.raises(ValueError):
        gemini.le_lembrete({"segundos": 1})


def test_hora_em_outro_fuso(monkeypatch):
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(config, "FUSO", "America/Sao_Paulo")
    momento = datetime(2026, 9, 30, 20, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))

    toquio = gemini.hora_em("Asia/Tokyo", momento)

    assert toquio == {"agora_la": "quinta-feira, 1 de outubro de 2026, 08:00", "diferenca_para_ca_em_horas": 12}
    assert gemini.hora_em("Asia/Kolkata", momento)["diferenca_para_ca_em_horas"] == 8.5


def test_quantos_dias_faltam_e_o_dia_da_semana():
    from datetime import date

    natal = gemini.sobre_a_data("2026-12-25", hoje=date(2026, 9, 30))

    assert natal == {"dias_ate_la": 86, "semanas_e_dias": [12, 2], "dia_da_semana": "sexta-feira"}


def test_data_que_ja_passou_e_data_invalida():
    from datetime import date

    assert gemini.sobre_a_data("2026-09-01", hoje=date(2026, 9, 30))["dias_ate_la"] == -29
    assert "erro" in gemini.sobre_a_data("25/12")
    assert "erro" in gemini.executa_ferramenta(gemini._SOBRE_A_DATA, {"data": "2026-02-30"})


def test_hora_em_fuso_que_nao_existe():
    assert "erro" in gemini.hora_em("Terra/Media")
    assert "erro" in gemini.executa_ferramenta(gemini._HORA_EM, {"fuso": ""})


def test_conta_pela_ferramenta():
    assert gemini.executa_ferramenta(gemini._CALCULAR, {"expressao": "0.15*80"}) == {"resultado": 12}
    assert "erro" in gemini.executa_ferramenta(gemini._CALCULAR, {"expressao": "1/0"})
    assert "erro" in gemini.executa_ferramenta(gemini._CALCULAR, {"expressao": "__import__('os')"})


def test_instrucao_manda_sortear_com_a_ferramenta():
    texto = gemini._instrucao()
    assert gemini._SORTEAR in texto and gemini._ESCOLHER in texto


def test_pedaco_da_pergunta_que_chega_depois_do_fim_do_turno_nao_se_perde(sessao):
    """27/09: o chat mostrou "Jarvis, que dia é hoje e" — o resto da transcrição
    da pergunta chegou depois do turn_complete."""
    sessao.mensagens = [
        _msg(ouviu="Jarvis, que dia é hoje e"),
        _msg(falou="Hoje é domingo."),
        _msg(fim=True),
        _msg(ouviu=" qual é o meu nome?"),
    ]

    resposta = asyncio.run(gemini.responde(6, UM_SEGUNDO))

    assert resposta.pergunta == "Jarvis, que dia é hoje e qual é o meu nome?"


def test_memoria_vai_no_comeco_da_sessao_seguinte(sessao):
    gemini.esquece(2)
    gemini._lembra(2, "capital da França?", "Paris.")
    sessao.mensagens = [_msg(fim=True)]

    asyncio.run(gemini.responde(2, UM_SEGUNDO))

    assert sessao.enviado[0][0] == "memoria"
    assert sessao.enviado[0][1]["turn_complete"] is False


def test_voz_escolhida_no_env_vai_na_configuracao(sessao, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_VOZ", "Kore")
    sessao.mensagens = [_msg(fim=True)]

    asyncio.run(gemini.responde(3, UM_SEGUNDO))

    voz = sessao.configs[0].speech_config.voice_config.prebuilt_voice_config.voice_name
    assert voz == "Kore"


def test_voz_do_voz_vale_so_no_servidor_que_escolheu(sessao, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_VOZ", "Algieba")
    gemini.escolhe_voz(3, "Charon")
    sessao.mensagens = [_msg(fim=True), _msg(fim=True), _msg(fim=True)]
    try:
        asyncio.run(gemini.responde(3, UM_SEGUNDO))
        asyncio.run(gemini.responde(4, UM_SEGUNDO))
        gemini.escolhe_voz(3, None)
        asyncio.run(gemini.responde(3, UM_SEGUNDO))
    finally:
        gemini.escolhe_voz(3, None)

    vozes = [c.speech_config.voice_config.prebuilt_voice_config.voice_name for c in sessao.configs]
    assert vozes == ["Charon", "Algieba", "Algieba"]


def test_voz_desconhecida_e_recusada():
    with pytest.raises(ValueError):
        gemini.escolhe_voz(3, "Jarvis")
    assert gemini.voz_de(3) == config.GEMINI_VOZ


def test_as_vozes_cabem_nas_opcoes_do_discord():
    assert len(gemini.VOZES) + 1 <= 25  # + "a de sempre"


# ── a fala ao vivo ───────────────────────────────────────────────────────────


def test_fala_espera_o_pulmao_antes_de_comecar():
    fala = FalaAoVivo(pulmao_s=0.1)  # 5 frames
    fala.escreve(_frame(1) * 3)

    assert fala.proximo() == SILENCIO  # 3 < 5: ainda enchendo
    fala.escreve(_frame(2) * 2)
    assert fala.proximo() == _frame(1)


def test_fala_emenda_pedacos_que_nao_fecham_frame():
    fala = FalaAoVivo(pulmao_s=0.02)
    inteiro = _frame(5)
    fala.escreve(inteiro[:1000])
    fala.escreve(inteiro[1000:])

    assert fala.proximo() == inteiro


def test_fala_que_atrasa_vira_silencio_e_conta_o_engasgo():
    fala = FalaAoVivo(pulmao_s=0.02)
    fala.escreve(_frame(1))
    assert fala.proximo() == _frame(1)

    assert fala.proximo() == SILENCIO
    assert fala.proximo() == SILENCIO
    assert fala.engasgos == 1  # um buraco, não um por frame
    assert fala.ativa()


def test_fala_acaba_depois_de_tocar_tudo():
    fala = FalaAoVivo(pulmao_s=10)  # pulmão grande: termina() libera mesmo assim
    fala.escreve(_frame(1) + _frame(2)[:100])
    fala.termina()

    assert fala.proximo() == _frame(1)
    ultimo = fala.proximo()
    assert len(ultimo) == FRAME_BYTES and ultimo.endswith(b"\x00" * 100)
    assert fala.proximo() is None
    assert not fala.ativa()


def test_calar_corta_na_hora():
    fala = FalaAoVivo(pulmao_s=0.02)
    fala.escreve(_frame(1) * 10)

    assert fala.cala() is True
    assert fala.proximo() is None
    assert fala.cala() is False  # já calada


def test_fonte_da_fala_termina_com_a_fala():
    fala = FalaAoVivo(pulmao_s=0.02)
    fala.escreve(_frame(3))
    fala.termina()
    fonte = FonteDaFala(fala)

    assert fonte.is_opus() is False
    assert fonte.read() == _frame(3)
    assert fonte.read() == b""


# ── a escuta conversando com o Gemini ────────────────────────────────────────


def _listener():
    assistente = MagicMock()
    assistente.guild.id = 1
    assistente.cala.return_value = False
    assistente.nome_de = AsyncMock(return_value="Ricardo")
    canal = MagicMock()
    canal.send = AsyncMock()
    return VoiceListener(assistente, canal)


def _responde_com(monkeypatch, resposta, pedacos=()):
    async def responde(guild_id, pcm, *, quem="", ao_falar=None, na_call=None, **_):
        responde.chamadas.append((guild_id, pcm, quem))
        for pedaco in pedacos:
            ao_falar(pedaco)
        if isinstance(resposta, BaseException):
            raise resposta
        return resposta

    responde.chamadas = []
    monkeypatch.setattr(gemini, "responde", responde)
    return responde


def _conversa(escuta, user_id=7, texto="Jarvis, capital?", pcm=UM_SEGUNDO):
    async def roda():
        escuta._conversa_nova(user_id, texto, pcm)
        await escuta._conversa

    asyncio.run(roda())


def test_a_resposta_toca_enquanto_chega_e_vai_para_o_chat(monkeypatch):
    fala = FalaAoVivo(pulmao_s=0.02)
    resposta = gemini.Resposta(texto="Paris.", pergunta="capital da França?", segundos_de_fala=1.0)
    responde = _responde_com(monkeypatch, resposta, pedacos=[_frame(1), _frame(2)])
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = fala

    _conversa(escuta)

    assert responde.chamadas == [(1, UM_SEGUNDO, "Ricardo")]
    escuta.assistente.fala_ao_vivo.assert_called_once()  # uma fala para a resposta inteira
    assert fala.proximo() == _frame(1) and fala.proximo() == _frame(2)
    assert fala.proximo() is None  # terminada quando a resposta acabou
    enviado = escuta.text_channel.send.await_args.args[0]
    assert "Ricardo" in enviado and "capital da França?" in enviado and "Paris." in enviado


def test_mandado_embora_pelo_gemini_se_despede_e_sai(monkeypatch):
    fala = FalaAoVivo(pulmao_s=0.02)
    _responde_com(
        monkeypatch,
        gemini.Resposta(texto="Tá bom, fui!", pergunta="ninguém te chamou", segundos_de_fala=1.0, quer_sair=True),
        pedacos=[_frame(1)],
    )
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = fala
    escuta.assistente.espera_efeito = AsyncMock()
    escuta.assistente.sai = AsyncMock()

    async def roda():
        escuta._conversa_nova(7, "Jarvis, ninguém te chamou, vaza", UM_SEGUNDO)
        tarefa = escuta._conversa
        await asyncio.sleep(0.05)
        assert fala.proximo() == _frame(1)  # a despedida toca antes de sair
        await tarefa

    asyncio.run(roda())

    escuta.assistente.sai.assert_awaited_once()
    enviados = [c.args[0] for c in escuta.text_channel.send.await_args_list]
    assert "Tá bom, fui!" in enviados[0] and enviados[-1] == listen.m.VOICE_BYE
    assert 7 not in escuta._chamados  # sem modo conversa: o bot está indo embora


def test_sem_transcricao_do_gemini_o_chat_usa_a_do_whisper(monkeypatch):
    _responde_com(monkeypatch, gemini.Resposta(texto="Paris.", segundos_de_fala=1.0))
    escuta = _listener()

    _conversa(escuta, texto="Jarvis, capital da França?")

    assert "Jarvis, capital da França?" in escuta.text_channel.send.await_args.args[0]


def test_respostas_fora_do_chat_se_desligado(monkeypatch):
    monkeypatch.setattr(config, "RESPOSTAS_NO_CHAT", False)
    _responde_com(monkeypatch, gemini.Resposta(texto="Paris.", segundos_de_fala=1.0))
    escuta = _listener()

    _conversa(escuta)

    escuta.text_channel.send.assert_not_awaited()


def test_lembrete_marca_quem_pediu_no_chat_na_hora(monkeypatch):
    resposta = gemini.Resposta(
        texto="Te aviso.", pergunta="me avisa", segundos_de_fala=1.0, lembretes=[(0.01, "tirar a pizza")],
    )
    _responde_com(monkeypatch, resposta)
    escuta = _listener()

    async def roda():
        escuta._conversa_nova(7, "Jarvis, me avisa", UM_SEGUNDO)
        await escuta._conversa
        await asyncio.sleep(0.05)  # o lembrete vence

    asyncio.run(roda())

    enviados = [c.args[0] for c in escuta.text_channel.send.await_args_list]
    assert listen.m.REMINDER.format(mencao="<@7>", texto="tirar a pizza") in enviados
    assert not lembretes.pendentes  # vencido, sai da lista


def test_lembrete_sem_texto_ainda_avisa(monkeypatch):
    resposta = gemini.Resposta(texto="Ok.", segundos_de_fala=1.0, lembretes=[(0.01, "")])
    _responde_com(monkeypatch, resposta)
    escuta = _listener()

    async def roda():
        escuta._conversa_nova(7, "Jarvis, timer", UM_SEGUNDO)
        await escuta._conversa
        await asyncio.sleep(0.05)

    asyncio.run(roda())

    enviados = [c.args[0] for c in escuta.text_channel.send.await_args_list]
    assert listen.m.REMINDER.format(mencao="<@7>", texto=listen.m.REMINDER_SEM_TEXTO) in enviados


def test_falha_antes_de_falar_avisa_no_chat(monkeypatch):
    _responde_com(monkeypatch, TimeoutError())
    escuta = _listener()

    _conversa(escuta)

    assert escuta.text_channel.send.await_args.args[0] == listen.m.VOICE_ANSWER_FAILED


def test_recusa_de_cara_tenta_de_novo_sem_a_memoria(monkeypatch):
    """27/09: "1007 Precondition check failed" um segundo depois de pedir, logo
    após duas trocas sem sentido na memória — a pergunta se perdia."""
    tentativas = []

    async def responde(guild_id, pcm, *, quem="", ao_falar=None, na_call=None, **_):
        tentativas.append(bool(gemini._memoria(guild_id)))
        if len(tentativas) == 1:
            raise RuntimeError("1007 None. Precondition check failed.")
        ao_falar(_frame(1))
        return gemini.Resposta(texto="Paris.", pergunta="capital?", segundos_de_fala=1.0)

    monkeypatch.setattr(gemini, "responde", responde)
    gemini.esquece(1)
    gemini._lembra(1, "amanecer mi pene", "Não entendi.")
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = FalaAoVivo(pulmao_s=0.02)

    _conversa(escuta)

    assert tentativas == [True, False]  # a segunda já sem a memória
    assert listen.m.VOICE_ANSWER_FAILED not in [c.args[0] for c in escuta.text_channel.send.await_args_list]


def test_falha_depois_de_comecar_a_falar_nao_tenta_de_novo(monkeypatch):
    chamadas = _responde_com(monkeypatch, TimeoutError(), pedacos=[_frame(1)])
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = FalaAoVivo(pulmao_s=0.02)

    _conversa(escuta)

    assert len(chamadas.chamadas) == 1  # repetir agora dobraria a resposta


def test_falha_no_meio_da_fala_nao_manda_erro_e_termina_a_fala(monkeypatch):
    fala = FalaAoVivo(pulmao_s=0.02)
    _responde_com(monkeypatch, TimeoutError(), pedacos=[_frame(1)])
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = fala

    _conversa(escuta)

    escuta.text_channel.send.assert_not_awaited()
    assert fala.proximo() == _frame(1) and fala.proximo() is None


def test_resposta_vazia_avisa_no_chat(monkeypatch):
    _responde_com(monkeypatch, gemini.Resposta())
    escuta = _listener()

    _conversa(escuta)

    assert escuta.text_channel.send.await_args.args[0] == listen.m.VOICE_ANSWER_FAILED


def test_fora_da_call_a_resposta_fica_so_no_chat(monkeypatch):
    _responde_com(
        monkeypatch, gemini.Resposta(texto="Paris.", pergunta="capital?", segundos_de_fala=1.0),
        pedacos=[_frame(1), _frame(2)],
    )
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = None

    _conversa(escuta)

    escuta.assistente.fala_ao_vivo.assert_called_once()  # não tenta de novo a cada pedaço
    assert "Paris." in escuta.text_channel.send.await_args.args[0]


@pytest.mark.parametrize("debug", [False, True])
def test_o_que_o_gemini_ouviu_so_vai_para_o_log_no_diagnostico(monkeypatch, caplog, debug):
    monkeypatch.setattr(config, "VOICE_DEBUG", debug)
    _responde_com(monkeypatch, gemini.Resposta(texto="Paris.", pergunta="capital da França?", segundos_de_fala=1.0))
    escuta = _listener()

    with caplog.at_level("INFO", logger="core.listen"):
        _conversa(escuta)

    assert ("capital da França?" in caplog.text) is debug
    assert ("Paris." in caplog.text) is debug


def test_erro_inesperado_na_conversa_vai_para_o_log(monkeypatch, caplog):
    _responde_com(monkeypatch, gemini.Resposta(texto="Paris.", segundos_de_fala=1.0))
    escuta = _listener()
    escuta.text_channel.send = AsyncMock(side_effect=RuntimeError("discord fora"))

    with caplog.at_level("ERROR", logger="core.listen"):
        _conversa(escuta)

    assert "erro na conversa com o Gemini" in caplog.text


# ── interromper ──────────────────────────────────────────────────────────────


def _gemini_que_espera(monkeypatch, liberar: asyncio.Event):
    async def responde(guild_id, pcm, *, quem="", ao_falar=None, na_call=None, **_):
        responde.comecou = True
        await liberar.wait()
        return gemini.Resposta(texto="ok", segundos_de_fala=1.0)

    responde.comecou = False
    monkeypatch.setattr(gemini, "responde", responde)
    return responde


def test_a_escuta_fica_livre_enquanto_o_gemini_responde(monkeypatch):
    """A resposta chega no ritmo da fala (10s+): com o worker preso nela, um
    "Jarvis, para" no meio só seria ouvido depois do fim."""
    async def roda():
        liberar = asyncio.Event()
        responde = _gemini_que_espera(monkeypatch, liberar)
        escuta = _listener()
        escuta._conversa_nova(7, "Jarvis, conta uma história", UM_SEGUNDO)  # não bloqueia
        await asyncio.sleep(0)
        assert responde.comecou and not escuta._conversa.done()
        liberar.set()
        await escuta._conversa

    asyncio.run(roda())


def test_interromper_cancela_o_pedido_que_ainda_pensa(monkeypatch):
    async def roda():
        _gemini_que_espera(monkeypatch, asyncio.Event())  # nunca responde
        escuta = _listener()
        escuta._conversa_nova(7, "Jarvis, conta uma história", UM_SEGUNDO)
        await asyncio.sleep(0)
        conversa = escuta._conversa

        assert escuta._interrompe_conversa() is True
        await asyncio.sleep(0)
        assert conversa.cancelled()
        assert escuta._interrompe_conversa() is False  # nada mais em andamento

    asyncio.run(roda())


def test_pergunta_nova_interrompe_a_anterior(monkeypatch):
    async def roda():
        _gemini_que_espera(monkeypatch, asyncio.Event())
        escuta = _listener()
        escuta._conversa_nova(7, "Jarvis, primeira", UM_SEGUNDO)
        await asyncio.sleep(0)
        primeira = escuta._conversa
        escuta._conversa_nova(7, "Jarvis, segunda", UM_SEGUNDO)
        await asyncio.sleep(0)
        assert primeira.cancelled() and escuta._conversa is not primeira
        escuta._conversa.cancel()

    asyncio.run(roda())


# ── modo conversa ────────────────────────────────────────────────────────────


def test_depois_de_responder_quem_perguntou_continua_sem_o_nome(monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONVERSA_S", 8.0)
    _responde_com(monkeypatch, gemini.Resposta(texto="Paris.", segundos_de_fala=1.0), pedacos=[_frame(1)])
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = FalaAoVivo()

    _conversa(escuta, user_id=7)

    assert escuta._chamados[7] > time.monotonic() + 7  # janela aberta só para quem perguntou
    assert 8 not in escuta._chamados


def test_modo_conversa_desligado(monkeypatch):
    monkeypatch.setattr(config, "VOICE_CONVERSA_S", 0)
    _responde_com(monkeypatch, gemini.Resposta(texto="Paris.", segundos_de_fala=1.0), pedacos=[_frame(1)])
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = FalaAoVivo()

    _conversa(escuta)

    assert escuta._chamados == {}


def test_sem_voz_nao_abre_a_janela(monkeypatch):
    """Resposta que ninguém ouviu (bot fora da call) não convida continuação."""
    monkeypatch.setattr(config, "VOICE_CONVERSA_S", 8.0)
    _responde_com(monkeypatch, gemini.Resposta(texto="Paris.", segundos_de_fala=1.0), pedacos=[_frame(1)])
    escuta = _listener()
    escuta.assistente.fala_ao_vivo.return_value = None

    _conversa(escuta)

    assert escuta._chamados == {}
