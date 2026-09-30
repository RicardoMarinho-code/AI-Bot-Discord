"""Chamando o bot pelo nome: "Jarvis, que horas são?" → o que fazer.

Lógica pura (texto → intenção), sem Discord nem áudio. O Whisper transcreve o
nome de jeitos criativos, então ele é comparado pelo SOM, não pela grafia
(ver `fonetica`), inteiro ou partido em duas palavras, nas primeiras palavras
da frase. Medido em 27/09 com 4 vozes e o áudio comprimido como no Discord: o
Whisper tiny escreveu "Jávez", "Jardes", "Já arbise", "Ei, Jardes", e a
comparação pela grafia achou o nome em 9 de 24 frases; pelo som, em 20 de 24
— e nenhum alarme falso nas 15 frases sem o nome ("Já vi esse filme" era um).

Quase tudo depois do nome é pergunta, e quem entende é o Gemini (ouvindo o
áudio, não este texto). Só três coisas são decididas aqui, na hora, porque
precisam ser instantâneas: o nome sozinho (a pessoa vai completar), "para"
(cala o bot) e "sai da call". E só a frase CURTA e exata: "para que serve o
bicarbonato?" é pergunta, "fui ao médico, o que é gastrite?" também — no bot
de música que serviu de base, as duas viravam comando.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

_WAKE_THRESHOLD = 0.75  # semelhança (pelo som) de UMA palavra com o nome
# o nome partido em duas ("já arviz") precisa de mais: colando palavras soltas
# "já" + "vi" dava 0,80 — e "Já vi esse filme" acordava o bot
_PAR_THRESHOLD = 0.85
_WAKE_SCAN_WORDS = 3  # o nome é procurado nas primeiras N palavras
_NOMES: set[str] = set()  # os nomes que acordam o bot, já pelo som (registra_nome)

_FILLERS = {
    "o", "a", "e", "ai", "com", "ei", "hey", "oi", "meu", "fala", "opa",
    "alo", "ola", "ok", "eae", "eai", "entao", "ta",
    "em",  # o Whisper escreve "Ei, Jarvis" como "Em Járbice"
}
# a saudação ANTES do nome ("Bom dia, Jarvis, ...") — só lá: depois do nome,
# "bom dia" é o próprio pedido, e o Gemini devolve o bom-dia
_ANTES_DO_NOME = _FILLERS | {"bom", "boa", "dia", "tarde", "noite", "salve", "coe", "psiu", "hello", "hi"}
# o Whisper às vezes escreve o som do "J" como uma palavra solta antes do resto
# do nome: "D'Arvis", "de Arves" — "d" + "arvis" vale como "jarvis"
_SOM_DE_J = {"d", "de"}
# o que sobra no fim de um comando sem mudar o sentido: "para aí", "sai agora"
_SOBRA = {"ai", "agora", "ja", "por", "favor", "pfv", "pf", "logo", "mesmo", "entao"}
# a cortesia no começo de um comando: "você pode sair da call?", "só para"
_CORTESIA = {"voce", "vc", "por", "favor", "pf", "pfv", "agora", "ja", "entao", "so", "poderia", "podia"}
_CALA = {
    "para", "pare", "parou", "chega", "cala", "calado", "cala boca", "cala a boca",
    "silencio", "shh", "xiu", "stop", "quieto", "fica quieto", "basta", "ja deu",
    "pode parar", "para de falar", "para para", "espera", "perai",
    "parah",  # 27/09: a Groq escreveu "Jarvis. Parah." e virou pergunta ao Gemini
    # desistir do pedido, do jeito que se fala ("tá bom" chega aqui como "bom":
    # o "tá" é enchimento)
    "cancela", "cancelar", "esquece", "esqueca", "deixa", "deixa pra la", "deixa para la",
    "deixa quieto", "nada", "nada nao", "nao precisa", "nao precisa mais", "bom",
    "beleza", "blz", "certo", "entendi", "ja entendi", "ja chega", "suficiente",
    "para com isso", "pare de falar", "fica calado", "fica em silencio", "para tudo",
}
# sair da call: a despedida sozinha, ou o verbo seguido só de "da call", "do
# canal de voz", "daqui"... — "desliga o forno em quanto tempo?" continua
# pergunta ("o forno" não é lugar). Aqui só o jeito curto e comum de mandar o
# bot embora; o resto ("a gente quer conversar a sós, dá licença") o Gemini
# entende e sai pela ferramenta (services/gemini.py)
_SAI_DESPEDIDA = {
    "tchau", "xau", "tchau tchau", "falou", "flw", "ate mais", "ate logo", "adeus",
    "ate depois", "ate amanha", "bye", "bye bye",
    # expulso, do jeito que se fala ("tá" chega aqui como enchimento, e "você"
    # como cortesia: "você tá dispensado" → "dispensado")
    "dispensado", "ta dispensado", "esta dispensado", "expulso", "ta expulso", "esta expulso",
    "liberado", "ta liberado", "esta liberado", "xo", "xispa", "rala", "rala peito",
    "ninguem te chamou", "ninguem te convidou", "ninguem chamou voce",
}
_SAI_VERBO = tuple(sorted((frase.split() for frase in (
    "pode ir embora", "pode ir", "pode sair", "pode vazar", "pode desligar", "pode desconectar",
    "vai embora", "sai", "sair", "saia", "vaza", "vazar", "some", "sume", "desconecta",
    "desconectar", "desliga", "desligar",
    # as gírias de expulsar
    "cai fora", "sai fora", "da o fora", "da no pe", "rapa fora", "fora", "se manda",
    "se retira", "se retire", "pode se retirar", "pode se mandar", "pode cair fora",
    "pode dar o fora", "vai nessa", "pode ir nessa", "vai pra casa", "vai para casa",
    "vai dormir", "vai descansar", "pode descansar", "pode ir dormir", "pode ir descansar",
    "encerra", "encerrar", "pode encerrar",
)), key=len, reverse=True))  # a mais longa primeiro: "sai fora" antes de "sai"
_SAI_LUGAR = {
    "da", "do", "de", "dessa", "desse", "call", "chamada", "ligacao", "canal", "voz",
    "sala", "conversa", "aqui", "dai", "daqui", "nossa", "nosso", "minha", "meu",
    "servidor", "discord", "papo", "fora", "agora", "ja", "logo",
}
# O que o Whisper devolve sobre ruído (respiração, clique, tosse). No journal
# de 30 dias do bot de música, "Tchau." sozinho apareceu 36 vezes sem ninguém
# falar com ele. Texto igual ao de quem fala de verdade. No log de 27/09 (Groq,
# call brasileira) vieram também os clássicos em outras línguas — "Thank you."
# três vezes, "Субтитры создавал DimaTorzok" — e um deles, na janela do modo
# conversa, foi parar no Gemini. Texto só em outro alfabeto normaliza para "".
_STT_NOISE = {
    "", "tchau", "tchau tchau", "obrigado", "obrigada", "valeu", "e ai",
    "obrigado por assistir", "obrigada por assistir", "legendas pela comunidade amara org",
    "inscreva se no canal", "ate a proxima",
    "thank you", "thank you very much", "thanks", "thanks for watching",
    "thank you for watching", "you", "bye", "bye bye", "dimatorzok",
    # o murmúrio de quem só concorda ("uhum", "tá bom") não é pergunta
    "hum", "hmm", "humm", "uhum", "hum hum", "mm hmm", "mhm", "ahn", "ah", "oh",
    "eh", "e", "ok", "okay", "ta", "ta bom",
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", text).strip()


def fonetica(palavra: str) -> str:
    """A palavra (já normalizada) pelo SOM, com as trocas que o Whisper faz em
    português: b/v ("arbise"), z/s ("Jávez"), ge/je ("Gerves"), l/r antes de
    consoante ("Jalves"), letra dobrada ("já arvisse"), e/i no fim ("Jardes") e
    o "Ei" colado antes do J ("Enjardis", "Ejarbici").
    """
    p = re.sub(r"^(?:e[mn]?|i)(?=j)", "", palavra)
    p = re.sub(r"g(?=[ei])", "j", p)
    p = p.replace("w", "v").replace("b", "v")
    p = re.sub(r"c(?=[ei])", "s", p)
    p = p.replace("z", "s")
    p = re.sub(r"l(?=[^aeiou])", "r", p)
    p = re.sub(r"(.)\1+", r"\1", p)
    p = re.sub(r"se$", "s", p)
    p = re.sub(r"e(s?)$", r"i\1", p)
    return re.sub(r"(.)\1+", r"\1", p)


def registra_nome(nome: str) -> str:
    """Faz `nome` acordar o bot. Nome de várias palavras vale pela primeira
    ("Jarvis Stark" → "jarvis"). Devolve o nome como ficou registrado."""
    palavras = _normalize(nome).split()
    if not palavras:
        return ""
    _NOMES.add(fonetica(palavras[0]))
    return palavras[0]


def first_word(text: str) -> str:
    """Primeira palavra normalizada — o que a pessoa tentou usar como nome. É
    só isso que vai para o log quando nada é reconhecido (nunca o conteúdo)."""
    words = _normalize(text).split()
    return words[0] if words else ""


def is_stt_noise(text: str) -> bool:
    """A transcrição INTEIRA é uma das frases que o Whisper inventa sobre ruído?"""
    return " ".join(_normalize(text).split()) in _STT_NOISE


def _parecido(palavra: str, limiar: float) -> bool:
    """Parecida com um dos nomes PELO SOM — e começando com o mesmo som: sem
    isso "Alves" (vira "arvis") acordava o bot no meio de um papo de futebol."""
    som = fonetica(palavra)
    return som in _NOMES or any(
        som[:1] == nome[:1] and difflib.SequenceMatcher(None, som, nome).ratio() >= limiar
        for nome in _NOMES
    )


def _parece_nome(palavra: str) -> bool:
    return _parecido(palavra, _WAKE_THRESHOLD)


def _is_wake_pair(a: str, b: str) -> bool:
    """O nome partido em dois pela transcrição ("já arviz", "d'arvis")."""
    return _parecido(("j" if a in _SOM_DE_J else a) + b, _PAR_THRESHOLD)


def _find_wake(words: list[str]) -> list[str] | None:
    """Resto da frase depois do nome (procurado no começo), ou None."""
    for i in range(min(_WAKE_SCAN_WORDS, len(words))):
        if _parece_nome(words[i]):
            return words[i + 1 :]
        if i + 1 < len(words) and _is_wake_pair(words[i], words[i + 1]):
            return words[i + 2 :]
    return None


def chamou_de_frente(text: str) -> bool:
    """A frase COMEÇA pelo nome? ("Jarvis, ..." sim; "não, do jarvis lá" não)

    O nome é procurado nas 3 primeiras palavras porque o STT às vezes põe lixo
    na frente — mas nome no meio de uma conversa entre as pessoas não é pedido.
    """
    words = _normalize(text).split()
    while words and words[0] in _ANTES_DO_NOME:
        words.pop(0)
    if not words:
        return False
    return _parece_nome(words[0]) or (len(words) > 1 and _is_wake_pair(words[0], words[1]))


def _pedido(text: str) -> list[str] | None:
    """As palavras depois do nome (sem ecos dele nem enchimento), ou None se a
    frase não chamou o bot."""
    words = _normalize(text).split()
    while words and words[0] in _ANTES_DO_NOME:
        words.pop(0)
    rest = _find_wake(words) if words else None
    if rest is None:
        return None
    # o Whisper às vezes ecoa o nome ("jarvis, jarvis, jarvis, para")
    while rest and _parece_nome(rest[0]):
        rest = rest[1:]
    while rest and rest[0] in _FILLERS:
        rest = rest[1:]
    return rest


def parse_command(text: str) -> tuple[str, str] | None:
    """Texto transcrito → (ação, frase), ou None se não chamou o bot.

    Ações: "chamou" (só o nome), "cala", "sai" e "pergunta" (o resto).
    """
    rest = _pedido(text)
    if rest is None:
        return None
    if not rest:
        return ("chamou", "")
    frase = " ".join(rest)
    enxuta = list(rest)
    while len(enxuta) > 1 and enxuta[-1] in _SOBRA:
        enxuta.pop()
    sem_cortesia = list(enxuta)
    while len(sem_cortesia) > 1 and sem_cortesia[0] in _CORTESIA:
        sem_cortesia.pop(0)
    # com e sem a cortesia: "já deu" é comando inteiro, "já chega" sem o "já" também
    formas = (enxuta, sem_cortesia)
    if any(" ".join(forma) in _CALA for forma in formas):
        return ("cala", "")
    if any(_e_saida(forma) for forma in formas):
        return ("sai", "")
    return ("pergunta", frase)


def _e_saida(palavras: list[str]) -> bool:
    """"sai da call", "pode sair do canal de voz", "cai fora daqui", "tchau"."""
    if " ".join(palavras) in _SAI_DESPEDIDA:
        return True
    for verbo in _SAI_VERBO:
        if palavras[: len(verbo)] == verbo:
            return all(palavra in _SAI_LUGAR for palavra in palavras[len(verbo):])
    return False
