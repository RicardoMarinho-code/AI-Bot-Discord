"""Perguntas faladas ao bot: o áudio vai ao Gemini Live e a resposta volta em voz.

Só o trecho de quem chamou o bot pelo nome sai da máquina — a escuta
(core/listen.py) já achou o nome e descartou o resto da call. Uma sessão Live
por pergunta: a conexão abre em ~0,6s e não há sessão velha para reconectar
(a do Live cai a cada ~10min). A memória da conversa são as últimas trocas em
TEXTO (transcrições que o próprio Gemini devolve), reenviadas no começo de
cada sessão.

A resposta chega aos pedaços, no ritmo da fala, e cada pedaço vai para a call
assim que chega (`ao_falar`): medido em 27/09, a de "o que é gastrite?" levou
13,1s para chegar inteira — tocando enquanto chega, o bot começa em ~2,9s.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

import config
from core import lembretes, notas
from core.audio import BYTES_POR_S, FRAME_BYTES
from services import calculadora, speech

log = logging.getLogger(__name__)

_TAXA_ENTRADA = 16000  # o que mandamos: PCM mono 16 kHz
_PEDACO_ENTRADA = _TAXA_ENTRADA  # bytes por mensagem no websocket: 0,5s (2 bytes/amostra)
# o Live entrega a fala no ritmo dela: uma resposta de 12s leva ~12s para
# chegar inteira. Passou disto, algo travou.
_TEMPO_MAX_S = 45.0
_TRANSCRICAO_ATRASADA_S = 0.6
# mandado embora: quanto esperar pela despedida, que vem depois da ferramenta
_DESPEDIDA_S = 5.0
# turnos só de ferramenta seguidos, antes de desistir de esperar a fala
_TURNOS_DE_FERRAMENTA = 3
# o tier grátis aceita poucas sessões Live simultâneas por chave
_sessoes = asyncio.Semaphore(2)
# memória curta por servidor: "e a de Portugal?" depois de "capital da França"
_MEMORIA_TROCAS = 5
_MEMORIA_S = 600.0
_historico: dict[int, deque[tuple[float, str, str]]] = {}

_DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
_MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
    "setembro", "outubro", "novembro", "dezembro",
)

_cliente = None

# as vozes prontas do Live que o /voz oferece (o Discord aceita até 25 opções;
# a 25ª é "voltar ao padrão"): nome → como soa
VOZES = {
    "Algieba": "masculina, suave",
    "Charon": "masculina, grave e séria",
    "Orus": "masculina, firme",
    "Iapetus": "masculina, clara",
    "Algenib": "masculina, rouca",
    "Alnilam": "masculina, firme",
    "Schedar": "masculina, equilibrada",
    "Rasalgethi": "masculina, informativa",
    "Sadaltager": "masculina, de quem entende",
    "Enceladus": "masculina, calma",
    "Umbriel": "masculina, tranquila",
    "Achird": "masculina, amigável",
    "Zubenelgenubi": "masculina, casual",
    "Puck": "masculina, animada",
    "Fenrir": "masculina, empolgada",
    "Sadachbia": "masculina, viva",
    "Zephyr": "feminina, luminosa",
    "Kore": "feminina, firme",
    "Leda": "feminina, jovem",
    "Aoede": "feminina, leve",
    "Callirrhoe": "feminina, tranquila",
    "Autonoe": "feminina, clara",
    "Despina": "feminina, suave",
    "Erinome": "feminina, nítida",
}
# a voz escolhida no /voz, por servidor; guardada em arquivo para valer depois
# de um reinício (sem escolha, vale a do .env: GEMINI_VOZ)
_voz_do_servidor: dict[int, str] = {}
_ARQUIVO_VOZES = os.path.join("data", "vozes.json")

# o jeito de o Gemini tirar o bot da call quando o mandam embora com palavras
# que a lista de core/intents não prevê
_SAIR = "sair_da_call"
# sorteios de verdade: o modelo "sorteando" de cabeça repete sempre os mesmos
# números (o 7, o 42) e a moeda quase sempre dá cara
_SORTEAR = "sortear_numero"
_ESCOLHER = "escolher_entre"
_SORTEIO_MAX = 20  # números por pedido: "sorteia 3 números de 1 a 60"
_aleatorio = random.SystemRandom()
# "me avisa em 10 minutos": o aviso vai para o chat, marcando quem pediu
_LEMBRETE = "criar_lembrete"
_LEMBRETE_MIN_S = 5
# "faz uma enquete: pizza ou hambúrguer?": quem chamou posta no chat, com reações
_ENQUETE = "criar_enquete"
ENQUETE_MAX_OPCOES = 10  # uma reação numerada por opção: 1️⃣ a 🔟
_LEMBRETE_MAX_S = 24 * 3600  # o bot reinicia de vez em quando: mais que um dia se perderia
# "quem tá na call?", "sorteia alguém daqui": o Gemini só conhece quem fala
_NA_CALL = "quem_esta_na_call"
# "quais são meus lembretes?", "cancela meus lembretes": sem ir ao /lembretes
_MEUS_LEMBRETES = "meus_lembretes"
_CANCELA_LEMBRETES = "cancelar_meus_lembretes"
# "anota: comprar pão", "o que eu anotei?": bloco de notas de quem fala
_ANOTAR = "anotar"
_MINHAS_NOTAS = "minhas_notas"
_APAGAR_NOTAS = "apagar_minhas_notas"
# o modelo erra conta de cabeça: "quanto é 15% de 80?" vai para a calculadora
_CALCULAR = "calcular"
# "que horas são em Tóquio?": conversão de fuso de cabeça erra o horário de verão
_HORA_EM = "hora_em"
# "quantos dias faltam pro Natal?", "que dia da semana cai 15/11?": o modelo erra
_SOBRE_A_DATA = "sobre_a_data"


@dataclass
class Resposta:
    texto: str = ""  # o que o bot falou (transcrição)
    pergunta: str = ""  # o que a pessoa disse (transcrição do Gemini)
    primeiro_audio_s: float | None = None  # do fim da pergunta ao primeiro pedaço de fala
    segundos_de_fala: float = 0.0
    quer_sair: bool = False  # o Gemini entendeu que mandaram o bot embora (_SAIR)
    # (daqui a quantos segundos, o quê): quem chamou agenda (core/listen)
    lembretes: list[tuple[int, str]] = field(default_factory=list)
    # (pergunta, opções): quem chamou posta no chat (core/listen)
    enquetes: list[tuple[str, list[str]]] = field(default_factory=list)


def agora() -> datetime:
    """A hora no fuso do .env (FUSO); sem a base de fusos, a hora do computador."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(config.FUSO))
    except Exception:  # noqa: BLE001 — fuso inválido ou sem tzdata: segue com a local
        return datetime.now().astimezone()


def hora_em(fuso: str, momento: datetime | None = None) -> dict:
    """A data e a hora num fuso da base IANA ("Asia/Tokyo"), para o Gemini falar."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zona = ZoneInfo(fuso.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return {"erro": f"fuso desconhecido: {fuso} (use o nome IANA, ex.: Europe/Lisbon)"}
    aqui = agora()
    base = momento or aqui
    la = base.astimezone(zona)
    # o mesmo instante nos dois lugares: o horário de verão de cada um conta
    zero = timedelta(0)
    diferenca = (la.utcoffset() or zero) - (base.astimezone(aqui.tzinfo).utcoffset() or zero)
    diferenca_h = diferenca.total_seconds() / 3600
    return {
        "agora_la": descreve_agora(la),
        "diferenca_para_ca_em_horas": int(diferenca_h) if diferenca_h.is_integer() else diferenca_h,
    }


def sobre_a_data(data: str, hoje: date | None = None) -> dict:
    """Quantos dias faltam (negativo: passaram) até `data` (AAAA-MM-DD) e o dia da semana."""
    try:
        alvo = date.fromisoformat(data.strip())
    except ValueError:
        return {"erro": f"data inválida: {data} (use AAAA-MM-DD, ex.: 2026-12-25)"}
    dias = (alvo - (hoje or agora().date())).days
    return {
        "dias_ate_la": dias,
        "semanas_e_dias": [dias // 7, dias % 7] if dias >= 0 else None,
        "dia_da_semana": _DIAS[alvo.weekday()],
    }


def descreve_agora(momento: datetime) -> str:
    """"sábado, 27 de setembro de 2026, 14:05" — o Gemini não sabe que horas são."""
    return (
        f"{_DIAS[momento.weekday()]}, {momento.day} de {_MESES[momento.month - 1]}"
        f" de {momento.year}, {momento:%H:%M}"
    )


def _instrucao(quem: str = "") -> str:
    # "Se o áudio vier cortado, peça para repetir" (a versão anterior) fazia o
    # Gemini pedir para repetir perguntas que dava para entender: o áudio de uma
    # call sempre tem o nome no começo, pausas, ruído e compressão
    texto = (
        f"Você é {config.BOT_NAME}, um assistente de voz numa call do Discord. A"
        " pessoa te chamou pelo nome e fez uma pergunta ou um pedido em voz alta."
        " Responda sempre em português do Brasil, de forma curta e natural, como"
        " numa conversa: uma a três frases, sem listas, sem markdown e sem"
        " emojis — sua resposta vai ser ouvida, não lida."
        " Quem fala com você é brasileiro: fale só português do Brasil, nunca"
        " espanhol, mesmo que alguma palavra soe estrangeira."
        f" O áudio começa com o seu nome ({config.BOT_NAME}) e pode ter pausas, ruído"
        " e a qualidade de uma call; palavras em inglês (jogos, tecnologia) e"
        " gírias são comuns. Entenda com boa vontade: se der para entender a"
        " intenção, responda. Só se não der mesmo para entender, diga o que você"
        " entendeu e peça para a pessoa repetir."
        # a pesquisa Google (ferramenta nativa do Live) cobre o que muda com o tempo
        " Para fatos recentes ou que mudam com o tempo (notícias, placares, preços,"
        " clima, lançamentos), pesquise no Google antes de responder. Se mesmo"
        " assim não souber, diga que não sabe."
        # 27/09: "posso colocar a música pra tocar", "mandado o recado pro Olavo"
        " Você só conversa: não toca músicas, não manda mensagens nem recados para outras pessoas, não"
        " chama ninguém e não faz nada no Discord ou no computador — se pedirem,"
        " diga com naturalidade que isso você não consegue fazer."
        ' Se pedirem para você parar, esquecer ou ficar quieto, responda só "Tudo bem".'
        # o "sai da call" curto a escuta já resolve sozinha (core/intents); aqui
        # chega o resto: "ninguém te chamou, vaza", "dá licença que o papo é nosso"
        " Se a pessoa quiser que você saia da call — de qualquer jeito: pedindo,"
        " te mandando embora, te expulsando, dizendo que ninguém te chamou ou que"
        f" quer conversar sem você —, use a ferramenta {_SAIR} e se despeça numa"
        " frase bem curta. Só quando for um pedido para VOCÊ ir embora: \"sai mais"
        ' barato?" ou "como se diz tchau em inglês?" são perguntas.'
        f" Para sortear números, jogar dados, cara ou coroa ou escolher entre opções,"
        f" use as ferramentas {_SORTEAR} e {_ESCOLHER} e diga o resultado que elas"
        " derem — nunca invente um sorteio de cabeça."
        f" Para a hora em outra cidade ou país, use {_HORA_EM}; para quantos dias"
        f" faltam (ou passaram) até uma data e o dia da semana dela, {_SOBRE_A_DATA}."
        f" Para qualquer conta que não seja trivial, use {_CALCULAR} e fale o"
        " resultado dela (arredondado de um jeito natural para ouvir)."
        f" Para lembretes e timers de quem está falando (\"me avisa em 10 minutos\"),"
        f" use a ferramenta {_LEMBRETE}: na hora, você marca a pessoa no chat."
        f" Para saber quem está na call (ou sortear alguém daqui), use {_NA_CALL}."
        f" Se perguntarem pelos lembretes de quem fala, use {_MEUS_LEMBRETES}; para"
        f" cancelá-los, {_CANCELA_LEMBRETES}."
        f" Para votações no grupo (\"faz uma enquete\"), use {_ENQUETE}: ela vai para o"
        " chat com reações para votar."
        f" Quem fala tem um bloco de notas: {_ANOTAR} (\"anota: ...\"), {_MINHAS_NOTAS}"
        f" e {_APAGAR_NOTAS}; as notas também aparecem no /notas."
        f"\n\nAgora é {descreve_agora(agora())} (horário de {config.FUSO})."
    )
    if quem:
        texto += f"\nQuem está falando com você: {quem}."
    return texto


def le_enquete(args: dict | None) -> tuple[str, list[str]]:
    """(pergunta, opções) de um criar_enquete; ValueError se não der enquete."""
    args = args or {}
    pergunta = " ".join(str(args.get("pergunta") or "").split())[:200]
    opcoes = [" ".join(str(o).split())[:80] for o in args.get("opcoes") or []]
    opcoes = [o for o in opcoes if o]
    if not pergunta:
        raise ValueError("a enquete precisa de uma pergunta")
    if not 2 <= len(opcoes) <= ENQUETE_MAX_OPCOES:
        raise ValueError(f"a enquete precisa de 2 a {ENQUETE_MAX_OPCOES} opções")
    return pergunta, opcoes


def le_lembrete(args: dict | None) -> tuple[int, str]:
    """(segundos, texto) de um criar_lembrete; ValueError se fora dos limites."""
    args = args or {}
    segundos = round(float(args.get("segundos", 0)))
    if not _LEMBRETE_MIN_S <= segundos <= _LEMBRETE_MAX_S:
        raise ValueError(f"o lembrete precisa ser de {_LEMBRETE_MIN_S}s a 24h")
    return segundos, str(args.get("texto") or "").strip()[:200]


def bloco_de_notas(nome: str, args: dict | None, guild_id: int, user_id: int) -> dict:
    """As ferramentas de anotação, sempre de quem está falando."""
    if not user_id:  # sem saber quem fala, a nota iria para o dono errado
        return {"erro": "não sei quem está falando"}
    if nome == _ANOTAR:
        try:
            return {"resultado": "ok, anotado", "total": notas.anota(guild_id, user_id, str((args or {}).get("texto", "")))}
        except ValueError as erro:
            return {"erro": str(erro)}
    if nome == _MINHAS_NOTAS:
        return {"notas": notas.de(guild_id, user_id)}
    return {"apagadas": notas.apaga(guild_id, user_id)}


def meus_lembretes(guild_id: int, user_id: int) -> dict:
    """Os lembretes de alguém como o Gemini entende: texto e minutos que faltam."""
    agora_s = time.time()
    return {"lembretes": [
        {"texto": lem.texto, "faltam_minutos": max(0, round((lem.vence_em - agora_s) / 60))}
        for lem in lembretes.de(guild_id, user_id)
    ]}


def executa_ferramenta(nome: str, args: dict | None) -> dict:
    """O que o bot responde a uma chamada de ferramenta do Gemini."""
    args = args or {}
    if nome == _SAIR:
        return {"resultado": "ok: você sai da call assim que terminar de falar"}
    try:
        if nome == _SORTEAR:
            minimo, maximo = sorted((int(args.get("minimo", 1)), int(args.get("maximo", 6))))
            quantidade = min(max(int(args.get("quantidade", 1)), 1), _SORTEIO_MAX)
            return {"numeros": [_aleatorio.randint(minimo, maximo) for _ in range(quantidade)]}
        if nome == _ENQUETE:
            le_enquete(args)
            return {"resultado": "ok: a enquete vai para o chat agora, com reações para votar"}
        if nome == _LEMBRETE:
            segundos, _texto = le_lembrete(args)
            return {"resultado": f"ok: o aviso vai para o chat daqui a {segundos} segundos"}
        if nome == _SOBRE_A_DATA:
            return sobre_a_data(str(args.get("data", "")))
        if nome == _HORA_EM:
            return hora_em(str(args.get("fuso", "")))
        if nome == _CALCULAR:
            return {"resultado": calculadora.calcula(str(args.get("expressao", "")))}
        if nome == _ESCOLHER:
            opcoes = [str(o).strip() for o in args.get("opcoes") or [] if str(o).strip()]
            if not opcoes:
                return {"erro": "nenhuma opção para escolher"}
            return {"escolhido": _aleatorio.choice(opcoes)}
    except (TypeError, ValueError) as erro:
        return {"erro": f"argumentos inválidos: {erro}"}
    return {"erro": f"ferramenta desconhecida: {nome}"}


def _ferramentas() -> list:
    from google.genai import types

    inteiro = types.Type.INTEGER
    return [
        # acesso à internet: a busca roda no Google e não gera tool_call para o bot
        types.Tool(google_search=types.GoogleSearch()),
        types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=_SAIR,
                description=(
                    "Sai da call de voz do Discord. Use quando a pessoa pedir ou mandar"
                    " você ir embora, te expulsar ou dispensar da call."
                ),
            ),
            types.FunctionDeclaration(
                name=_SORTEAR,
                description=(
                    "Sorteia números inteiros ao acaso, de minimo a maximo (inclusive)."
                    " Dado comum: 1 a 6. Use sempre que pedirem um sorteio ou um dado."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "minimo": types.Schema(type=inteiro, description="menor número possível"),
                    "maximo": types.Schema(type=inteiro, description="maior número possível"),
                    "quantidade": types.Schema(
                        type=inteiro, description=f"quantos números sortear (1 a {_SORTEIO_MAX})",
                    ),
                }, required=["minimo", "maximo"]),
            ),
            types.FunctionDeclaration(
                name=_ESCOLHER,
                description="Escolhe uma opção ao acaso: cara ou coroa, quem começa, onde comer.",
                parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "opcoes": types.Schema(
                        type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING),
                        description='as opções, ex.: ["cara", "coroa"]',
                    ),
                }, required=["opcoes"]),
            ),
            types.FunctionDeclaration(
                name=_HORA_EM,
                description=(
                    "A data e a hora agora num fuso horário, e a diferença para o daqui."
                    " Use o nome IANA do fuso: Asia/Tokyo, Europe/Lisbon, America/New_York."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "fuso": types.Schema(type=types.Type.STRING, description="nome IANA, ex.: Asia/Tokyo"),
                }, required=["fuso"]),
            ),
            types.FunctionDeclaration(
                name=_SOBRE_A_DATA,
                description=(
                    "Quantos dias faltam até uma data (negativo se já passou) e o dia da"
                    " semana dela. Se a pessoa não disser o ano, use a próxima ocorrência."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "data": types.Schema(type=types.Type.STRING, description="AAAA-MM-DD, ex.: 2026-12-25"),
                }, required=["data"]),
            ),
            types.FunctionDeclaration(
                name=_CALCULAR,
                description=(
                    "Faz uma conta exata. Expressão com números, + - * / // % ** e"
                    " parênteses; funções sqrt, abs, round, log, log10, exp, sin, cos,"
                    " tan, floor, ceil; constantes pi e e. Ex.: 15% de 80 = 0.15*80."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "expressao": types.Schema(type=types.Type.STRING, description="ex.: (1250*1.08)/12"),
                }, required=["expressao"]),
            ),
            types.FunctionDeclaration(
                name=_NA_CALL,
                description=(
                    "Os nomes de quem está agora na call de voz com você (sem contar"
                    " você). Use para dizer quem está aqui ou sortear alguém da call."
                ),
            ),
            types.FunctionDeclaration(
                name=_MEUS_LEMBRETES,
                description="Os lembretes pendentes de quem está falando: o texto e quantos minutos faltam.",
            ),
            types.FunctionDeclaration(
                name=_CANCELA_LEMBRETES,
                description="Cancela TODOS os lembretes pendentes de quem está falando.",
            ),
            types.FunctionDeclaration(
                name=_ENQUETE,
                description=(
                    "Posta uma enquete no chat do Discord, com uma reação numerada por"
                    f" opção para o pessoal votar. De 2 a {ENQUETE_MAX_OPCOES} opções."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "pergunta": types.Schema(type=types.Type.STRING, description="ex.: O que vamos jantar?"),
                    "opcoes": types.Schema(
                        type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING),
                        description='ex.: ["pizza", "hambúrguer", "japonês"]',
                    ),
                }, required=["pergunta", "opcoes"]),
            ),
            types.FunctionDeclaration(
                name=_ANOTAR,
                description="Guarda uma anotação no bloco de notas de quem está falando.",
                parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "texto": types.Schema(type=types.Type.STRING, description='a nota, ex.: "comprar pão"'),
                }, required=["texto"]),
            ),
            types.FunctionDeclaration(
                name=_MINHAS_NOTAS,
                description="As anotações de quem está falando, da mais antiga à mais nova.",
            ),
            types.FunctionDeclaration(
                name=_APAGAR_NOTAS,
                description="Apaga TODAS as anotações de quem está falando.",
            ),
            types.FunctionDeclaration(
                name=_LEMBRETE,
                description=(
                    "Agenda um lembrete para quem está falando: daqui a tantos segundos,"
                    " o bot marca a pessoa no chat do Discord com o texto. Até 24 horas."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={
                    "segundos": types.Schema(
                        type=inteiro, description="daqui a quantos segundos (10 minutos = 600)",
                    ),
                    "texto": types.Schema(
                        type=types.Type.STRING, description='do que lembrar, ex.: "tirar a pizza do forno"',
                    ),
                }, required=["segundos"]),
            ),
        ]),
    ]


def _get_cliente():
    global _cliente
    if _cliente is None:
        from google import genai

        _cliente = genai.Client(api_key=config.GEMINI_API_KEY)
    return _cliente


def aquece() -> None:
    """Importa o SDK e cria o cliente — no boot, numa thread. O import do
    google-genai leva ~1,4s num Ryzen (medido em 27/09) e bem mais na VM: feito
    na primeira pergunta, ele travava o event loop (e a escuta junto) e a
    primeira resposta demorava 4,1s para começar."""
    from google.genai import types  # noqa: F401 — o import é o aquecimento

    _get_cliente()


def pcm_discord_para_16k(pcm: bytes) -> bytes:
    """PCM 48 kHz estéreo do Discord → PCM mono 16 kHz s16 (entrada do Live)."""
    mono = speech.pcm_to_float_mono_16k(pcm)
    return (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def pcm_24k_para_48k_estereo(pcm: bytes) -> bytes:
    """PCM mono 24 kHz do Live → PCM 48 kHz estéreo (um pedaço, sem completar frame).

    2x por interpolação linear (repetir cada amostra soa metálico) e o mesmo
    sinal nos dois canais.
    """
    amostras = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32)
    if len(amostras) == 0:
        return b""
    x = np.arange(len(amostras) * 2) / 2
    dobro = np.interp(x, np.arange(len(amostras)), amostras)
    return np.repeat(dobro, 2).astype(np.int16).tobytes()


def pcm_24k_para_discord(pcm: bytes) -> bytes:
    """Como pcm_24k_para_48k_estereo, em frames inteiros (o PCMAudio do py-cord
    descarta o último pedaço incompleto)."""
    estereo = pcm_24k_para_48k_estereo(pcm)
    return estereo + b"\x00" * (-len(estereo) % FRAME_BYTES)


def _memoria(guild_id: int) -> list:
    """As trocas recentes do servidor como turnos do Gemini (as velhas saem)."""
    from google.genai import types

    trocas = _historico.get(guild_id)
    if not trocas:
        return []
    limite = time.monotonic() - _MEMORIA_S
    while trocas and trocas[0][0] < limite:
        trocas.popleft()
    turnos = []
    for _quando, pergunta, resposta in trocas:
        turnos.append(types.Content(role="user", parts=[types.Part(text=pergunta)]))
        turnos.append(types.Content(role="model", parts=[types.Part(text=resposta)]))
    return turnos


def _lembra(guild_id: int, pergunta: str, resposta: str) -> None:
    if not pergunta or not resposta:
        return
    trocas = _historico.setdefault(guild_id, deque(maxlen=_MEMORIA_TROCAS))
    trocas.append((time.monotonic(), pergunta, resposta))


def esquece(guild_id: int) -> None:
    """A conversa do servidor recomeça do zero (o bot saiu da call)."""
    _historico.pop(guild_id, None)


def escolhe_voz(guild_id: int, voz: str | None) -> None:
    """A voz do bot neste servidor; None volta para a do .env."""
    if voz is None:
        _voz_do_servidor.pop(guild_id, None)
    elif voz not in VOZES:
        raise ValueError(f"voz desconhecida: {voz}")
    else:
        _voz_do_servidor[guild_id] = voz
    _salva_vozes()


def _salva_vozes() -> None:
    try:
        os.makedirs(os.path.dirname(_ARQUIVO_VOZES), exist_ok=True)
        temporario = _ARQUIVO_VOZES + ".tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump({str(g): v for g, v in _voz_do_servidor.items()}, f)
        os.replace(temporario, _ARQUIVO_VOZES)
    except OSError:
        log.warning("🎙️ não consegui salvar as vozes do /voz", exc_info=True)


def carrega_vozes() -> None:
    """No boot: as vozes que cada servidor escolheu no /voz."""
    try:
        with open(_ARQUIVO_VOZES, encoding="utf-8") as f:
            salvas = json.load(f)
        # uma voz que saiu da lista (o Google tirou) volta para a do .env
        _voz_do_servidor.update({int(g): v for g, v in salvas.items() if v in VOZES})
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError):
        log.warning("🎙️ o arquivo de vozes está estragado — ignorando", exc_info=True)


def voz_de(guild_id: int) -> str:
    """A voz que o servidor ouve: a do /voz, senão a do .env ("" = a do Google)."""
    return _voz_do_servidor.get(guild_id, config.GEMINI_VOZ)


def _config(quem: str = "", voz: str = ""):
    from google.genai import types

    fala = None
    if voz:
        fala = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voz)
            )
        )
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=_instrucao(quem),
        speech_config=fala,
        tools=_ferramentas(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # quem decide onde a fala começa e termina é a escuta do bot: a pergunta
        # inteira vai de uma vez, marcada, e o Live responde logo no fim dela
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
    )


async def responde(
    guild_id: int,
    pcm: bytes,
    *,
    quem: str = "",
    ao_falar: Callable[[bytes], None] | None = None,
    na_call: Callable[[], Awaitable[list[str]]] | None = None,
    user_id: int = 0,
) -> Resposta:
    """A pergunta falada (PCM do Discord) → a resposta, falada e transcrita.

    `ao_falar(pcm)`: recebe cada pedaço da resposta (PCM 48 kHz estéreo) assim
    que ele chega. `quem`: o nome de quem perguntou, para o Gemini saber com
    quem fala. `na_call()`: os nomes de quem está na call — só chamada se o
    Gemini perguntar (buscar os nomes pode ir à API do Discord). `user_id`: de
    quem são os lembretes que o Gemini lista ou cancela.
    """
    from google.genai import types

    entrada = await asyncio.to_thread(pcm_discord_para_16k, pcm)
    resposta = Resposta()
    texto: list[str] = []
    pergunta: list[str] = []
    chamadas_no_turno: list[str] = []
    async with (
        _sessoes,
        asyncio.timeout(_TEMPO_MAX_S),
        _get_cliente().aio.live.connect(model=config.GEMINI_LIVE_MODEL, config=_config(quem, voz_de(guild_id))) as sessao,
    ):
        memoria = _memoria(guild_id)
        if memoria:
            await sessao.send_client_content(turns=memoria, turn_complete=False)
        await sessao.send_realtime_input(activity_start=types.ActivityStart())
        for i in range(0, len(entrada), _PEDACO_ENTRADA):
            await sessao.send_realtime_input(
                audio=types.Blob(
                    data=entrada[i : i + _PEDACO_ENTRADA],
                    mime_type=f"audio/pcm;rate={_TAXA_ENTRADA}",
                )
            )
        await sessao.send_realtime_input(activity_end=types.ActivityEnd())
        enviado = time.monotonic()

        async def ouve_turno() -> None:
            async for msg in sessao.receive():
                if msg.data:
                    if resposta.primeiro_audio_s is None:
                        resposta.primeiro_audio_s = time.monotonic() - enviado
                    pedaco = pcm_24k_para_48k_estereo(msg.data)
                    resposta.segundos_de_fala += len(pedaco) / BYTES_POR_S
                    if ao_falar is not None:
                        ao_falar(pedaco)
                if msg.tool_call:
                    # a saída de fato fica para quem chamou, depois de a fala tocar
                    chamadas = msg.tool_call.function_calls or []
                    resposta.quer_sair |= any(chamada.name == _SAIR for chamada in chamadas)
                    chamadas_no_turno.extend(chamada.name or "" for chamada in chamadas)
                    retornos = []
                    for chamada in chamadas:
                        if chamada.name == _NA_CALL:
                            retorno = {"pessoas": await na_call() if na_call is not None else []}
                        elif chamada.name == _MEUS_LEMBRETES:
                            retorno = meus_lembretes(guild_id, user_id)
                        elif chamada.name == _CANCELA_LEMBRETES:
                            retorno = {"cancelados": lembretes.cancela(guild_id, user_id)}
                        elif chamada.name in (_ANOTAR, _MINHAS_NOTAS, _APAGAR_NOTAS):
                            retorno = bloco_de_notas(chamada.name, chamada.args, guild_id, user_id)
                        else:
                            retorno = executa_ferramenta(chamada.name or "", chamada.args)
                        if chamada.name == _LEMBRETE and "erro" not in retorno:
                            resposta.lembretes.append(le_lembrete(chamada.args))
                        if chamada.name == _ENQUETE and "erro" not in retorno:
                            resposta.enquetes.append(le_enquete(chamada.args))
                        retornos.append(types.FunctionResponse(
                            id=chamada.id, name=chamada.name, response=retorno,
                        ))
                    await sessao.send_tool_response(function_responses=retornos)
                conteudo = msg.server_content
                if conteudo is None:
                    continue
                if conteudo.input_transcription and conteudo.input_transcription.text:
                    pergunta.append(conteudo.input_transcription.text)
                if conteudo.output_transcription and conteudo.output_transcription.text:
                    texto.append(conteudo.output_transcription.text)
                if conteudo.turn_complete:
                    return

        await ouve_turno()
        # 30/09: o turno da ferramenta fecha sem fala, e a fala vem num turno
        # NOVO, depois da resposta dela — sem esperar, o bot saía mudo. Um
        # sorteio pode puxar outro ("dois dados"): no máximo alguns turnos.
        for _ in range(_TURNOS_DE_FERRAMENTA):
            if not chamadas_no_turno or resposta.segundos_de_fala:
                break
            chamadas_no_turno.clear()
            if not resposta.quer_sair:
                await ouve_turno()
                continue
            try:
                async with asyncio.timeout(_DESPEDIDA_S):
                    await ouve_turno()
            except TimeoutError:
                break
        # a transcrição da PERGUNTA às vezes chega em pedaços e o último depois
        # do fim do turno (visto em 27/09: o chat mostrou "que dia é hoje e",
        # sem o resto). O áudio já tocou; só a mensagem do chat espera isto.
        try:
            async with asyncio.timeout(_TRANSCRICAO_ATRASADA_S):
                async for msg in sessao.receive():
                    conteudo = msg.server_content
                    if conteudo and conteudo.input_transcription and conteudo.input_transcription.text:
                        pergunta.append(conteudo.input_transcription.text)
        except TimeoutError:
            pass
    resposta.texto = "".join(texto).strip()
    resposta.pergunta = "".join(pergunta).strip()
    _lembra(guild_id, resposta.pergunta, resposta.texto)
    return resposta
