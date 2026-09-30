"""Chamar o bot pelo nome: o nome tolerante a erro de transcrição, e o que é
comando (instantâneo) ou pergunta (vai para o Gemini)."""
import pytest

from core import intents
from core.intents import (
    chamou_de_frente,
    first_word,
    is_stt_noise,
    parse_command,
    registra_nome,
)


@pytest.fixture(autouse=True)
def jarvis(monkeypatch):
    monkeypatch.setattr(intents, "_NOMES", set())
    registra_nome("Jarvis")


# ── o nome ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("grafia", ["Jarvis", "Jarves", "Jardis", "Járis", "Járaves", "Javis", "Já arviz"])
def test_nome_tolera_as_grafias_do_whisper(grafia):
    """As grafias reais do Whisper local para "Jarvis", medidas em 27/09."""
    assert parse_command(f"{grafia}, qual a capital da França?") == (
        "pergunta", "qual a capital da franca",
    )


def test_palavra_parecida_mas_outra_nao_acorda():
    assert parse_command("Chaves, qual a capital da França?") is None
    assert parse_command("Jarras de vidro custam quanto?") is None


def test_frase_sem_o_nome_nao_e_com_o_bot():
    assert parse_command("qual a capital da França?") is None
    assert parse_command("") is None


def test_enchimento_antes_do_nome_nao_atrapalha():
    assert parse_command("Ei, Jarvis, que horas são?") == ("pergunta", "que horas sao")
    assert parse_command("Opa Jarvis") == ("chamou", "")


def test_eco_do_nome_e_ignorado():
    assert parse_command("Jarvis, Jarvis, para") == ("cala", "")


def test_nome_de_varias_palavras_vale_pela_primeira(monkeypatch):
    monkeypatch.setattr(intents, "_NOMES", set())
    assert registra_nome("Jarvis Stark") == "jarvis"
    assert parse_command("Jarvis, bom dia") == ("pergunta", "bom dia")
    assert registra_nome("") == ""


def test_sem_nome_registrado_nada_acorda(monkeypatch):
    monkeypatch.setattr(intents, "_NOMES", set())
    assert parse_command("Jarvis, qual a capital?") is None


def test_outro_nome_configurado(monkeypatch):
    monkeypatch.setattr(intents, "_NOMES", set())
    registra_nome("Sexta")
    assert parse_command("Sexta, que dia é hoje?") == ("pergunta", "que dia e hoje")
    assert parse_command("Jarvis, que dia é hoje?") is None


# ── comandos (só a frase curta e exata) ──────────────────────────────────────


def test_so_o_nome_e_chamar():
    assert parse_command("Jarvis.") == ("chamou", "")
    assert parse_command("Jarvis?") == ("chamou", "")


@pytest.mark.parametrize("frase", [
    "Jarvis, para", "Jarvis, para aí", "Jarvis, chega", "Jarvis, cala a boca",
    "Jarvis, silêncio", "Jarvis, pode parar", "Jarvis, para por favor", "Jarvis, já deu",
    # desistir do pedido, do jeito que se fala
    "Jarvis, cancela", "Jarvis, esquece", "Jarvis, deixa pra lá", "Jarvis, tá bom",
    "Jarvis, já chega", "Jarvis, você pode parar?", "Jarvis, nada não", "Jarvis, entendi",
    "Jarvis, beleza", "Jarvis, só para",
])
def test_cala(frase):
    assert parse_command(frase) == ("cala", "")


@pytest.mark.parametrize("frase", [
    "Jarvis, sai da call", "Jarvis, tchau", "Jarvis, pode sair", "Jarvis, vai embora",
    "Jarvis, sai", "Jarvis, sai agora",
    # o verbo com o lugar, a cortesia e as despedidas
    "Jarvis, pode sair da call", "Jarvis, você pode sair da call?", "Jarvis, sai do canal de voz",
    "Jarvis, vai embora daqui", "Jarvis, desconecta da chamada", "Jarvis, pode ir embora",
    "Jarvis, desliga aí", "Jarvis, até mais", "Jarvis, tchau tchau", "Jarvis, sai dessa call",
    # expulso, com gíria — não só "sai da call"
    "Jarvis, cai fora", "Jarvis, cai fora daqui", "Jarvis, vaza daqui", "Jarvis, some daqui",
    "Jarvis, se manda", "Jarvis, dá o fora", "Jarvis, sai fora", "Jarvis, fora daqui",
    "Jarvis, fora!", "Jarvis, xô", "Jarvis, rala", "Jarvis, você tá dispensado",
    "Jarvis, tá expulso", "Jarvis, pode se retirar", "Jarvis, vai pra casa", "Jarvis, vai dormir",
    "Jarvis, ninguém te chamou", "Jarvis, sai da nossa call", "Jarvis, vaza agora da call",
    "Jarvis, some", "Jarvis, pode cair fora por favor",
    # 30/09, na call: estas duas iam ao Gemini (e ele acertou, mas 2-3s depois)
    "Jarvis. Bye-bye.", "Jarvis, poderia sair da call, por favor?",
])
def test_sai(frase):
    assert parse_command(frase) == ("sai", "")


@pytest.mark.parametrize("frase", [
    # no bot de música, TODAS estas viravam comando (fui = sair, passa = pular…)
    "Jarvis, para que serve o bicarbonato?",
    "Jarvis, fui ao médico hoje, o que é gastrite?",
    "Jarvis, desliga o forno em quanto tempo?",
    "Jarvis, passa a receita de bolo de cenoura",
    "Jarvis, repete o que você disse",
    "Jarvis, sai mais barato ir de ônibus ou de metrô?",
    "Jarvis, tchau em inglês é como?",
    "Jarvis, chega de papo e me conta uma piada",
    # o verbo de sair com algo que não é lugar, e os pedidos que começam
    # com as palavras de desistir
    "Jarvis, pode desligar a luz?",
    "Jarvis, sai da frente",
    "Jarvis, esquece o que eu falei e me conta uma piada",
    "Jarvis, deixa eu te perguntar uma coisa",
    "Jarvis, nada a ver isso, né?",
    "Jarvis, certo ou errado: a Terra é redonda?",
    "Jarvis, bom dia",
    # as gírias de expulsar com algo que não é lugar
    "Jarvis, fora isso, o que mais tem pra fazer?",
    "Jarvis, vai dormir que horas hoje?",
    "Jarvis, encerra quando o jogo?",
    "Jarvis, se manda mensagem pro Pedro chega na hora?",
])
def test_pergunta_que_comeca_com_palavra_de_comando_continua_pergunta(frase):
    assert parse_command(frase)[0] == "pergunta"


# ── nome no começo x no meio ─────────────────────────────────────────────────


def test_chamou_de_frente():
    assert chamou_de_frente("Jarvis, qual a capital?")
    assert chamou_de_frente("Ei Jarvis, qual a capital?")
    assert not chamou_de_frente("não, do jarvis lá")
    assert not chamou_de_frente("")


@pytest.mark.parametrize(("frase", "esperado"), [
    ("Bom dia, Jarvis, que horas são?", ("pergunta", "que horas sao")),
    ("Boa noite Jarvis", ("chamou", "")),
    ("Salve Jarvis, tudo bem?", ("pergunta", "tudo bem")),
])
def test_saudacao_antes_do_nome_e_chamar_de_frente(frase, esperado):
    """Antes, "Bom dia, Jarvis" era "nome no meio da frase" — conversa, ignorada."""
    assert parse_command(frase) == esperado
    assert chamou_de_frente(frase)


def test_saudacao_sem_o_nome_nao_acorda():
    assert parse_command("Bom dia a todos") is None
    assert parse_command("Boa tarde, pessoal") is None


# ── utilitários ──────────────────────────────────────────────────────────────


def test_ruido_do_whisper():
    for ruido in ("Tchau.", "Tchau, tchau.", "Obrigado.", "Valeu", "E aí", "."):
        assert is_stt_noise(ruido), ruido
    assert not is_stt_noise("tchau pessoal")


def test_first_word_normaliza():
    assert first_word("Járvis, qual…") == "jarvis"
    assert first_word("") == ""


# ── o nome comparado pelo SOM ────────────────────────────────────────────────


@pytest.mark.parametrize(("escrito", "som"), [
    ("javez", "javis"), ("jardes", "jardis"), ("jalves", "jarvis"), ("jaarbisse", "jarvis"),
    ("gerves", "jervis"), ("jarvis", "jarvis"),
])
def test_fonetica(escrito, som):
    assert intents.fonetica(escrito) == som


@pytest.mark.parametrize("frase", [
    # o que o Whisper escreveu no lugar de "Jarvis" nos testes de 27/09 (4 vozes,
    # áudio comprimido como no Discord) — pela grafia, quase nenhuma passava
    "Jávez, que horas são?", "Jardes, que horas são?", "Jalves, com o...",
    "Já arbise, me conta", "Já arvesse.", "Já, bis.", "Gerves.",
    "D'Arvis, que horas são agora?", "de Arves me conta uma curiosidade",
    "D'Arvies, qual é a raiz", "Ei, Jardes.", "Jáaves para.", "Jervis, bota",
    "Já arbisse.",
])
def test_nome_como_o_whisper_escreve(frase):
    assert parse_command(frase) is not None and chamou_de_frente(frase)


@pytest.mark.parametrize("frase", [
    "Já vi esse filme ontem.",  # "já"+"vi" acordava o bot
    "Alves marcou o gol",  # pelo som vira "arvis": só vale com o mesmo som no começo
    "De vez em quando", "De árvore em árvore",  # "d"+palavra como som do J
    "Já vai começar o jogo", "Já volto, pessoal", "Já disse que não", "Já viu isso?",
    "Jair, passa o sal", "Jardim tá bonito", "Davi, olha isso", "Travis Scott lançou",
    "Chaves é o melhor", "Jorge, vem cá", "Álvaro chegou", "Servidor caiu",
    "Harvard é cara", "Aviso importante", "Já avisei vocês", "Java é chato",
])
def test_frase_sem_o_nome_nao_acorda(frase):
    assert not (parse_command(frase) is not None and chamou_de_frente(frase))
    # limitação conhecida: "Jarbas" e "Juarez" soam como "Jarvis" e acordam o bot


@pytest.mark.parametrize("frase", ["Em Járbice, como se diz", "Enjardis, como se disse?", "Ejarbici, que horas"])
def test_ei_colado_antes_do_nome(frase):
    """ "Ei, Jarvis" que o Whisper juntou: "Em Járbice", "Enjardis"."""
    assert parse_command(frase) is not None and chamou_de_frente(frase)
