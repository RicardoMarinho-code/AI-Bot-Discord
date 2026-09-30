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
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import numpy as np

import config
from core.audio import BYTES_POR_S, FRAME_BYTES
from services import speech

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

# o jeito de o Gemini tirar o bot da call quando o mandam embora com palavras
# que a lista de core/intents não prevê
_SAIR = "sair_da_call"
# sorteios de verdade: o modelo "sorteando" de cabeça repete sempre os mesmos
# números (o 7, o 42) e a moeda quase sempre dá cara
_SORTEAR = "sortear_numero"
_ESCOLHER = "escolher_entre"
_SORTEIO_MAX = 20  # números por pedido: "sorteia 3 números de 1 a 60"
_aleatorio = random.SystemRandom()


@dataclass
class Resposta:
    texto: str = ""  # o que o bot falou (transcrição)
    pergunta: str = ""  # o que a pessoa disse (transcrição do Gemini)
    primeiro_audio_s: float | None = None  # do fim da pergunta ao primeiro pedaço de fala
    segundos_de_fala: float = 0.0
    quer_sair: bool = False  # o Gemini entendeu que mandaram o bot embora (_SAIR)


def agora() -> datetime:
    """A hora no fuso do .env (FUSO); sem a base de fusos, a hora do computador."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(config.FUSO))
    except Exception:  # noqa: BLE001 — fuso inválido ou sem tzdata: segue com a local
        return datetime.now().astimezone()


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
        " Você só conversa: não toca músicas, não manda mensagens nem recados, não"
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
        f"\n\nAgora é {descreve_agora(agora())} (horário de {config.FUSO})."
    )
    if quem:
        texto += f"\nQuem está falando com você: {quem}."
    return texto


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


def _config(quem: str = ""):
    from google.genai import types

    fala = None
    if config.GEMINI_VOZ:
        fala = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.GEMINI_VOZ)
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
) -> Resposta:
    """A pergunta falada (PCM do Discord) → a resposta, falada e transcrita.

    `ao_falar(pcm)`: recebe cada pedaço da resposta (PCM 48 kHz estéreo) assim
    que ele chega. `quem`: o nome de quem perguntou, para o Gemini saber com
    quem fala.
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
        _get_cliente().aio.live.connect(model=config.GEMINI_LIVE_MODEL, config=_config(quem)) as sessao,
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
                    chamadas_no_turno.extend(chamada.name for chamada in chamadas)
                    await sessao.send_tool_response(function_responses=[
                        types.FunctionResponse(
                            id=chamada.id, name=chamada.name,
                            response=executa_ferramenta(chamada.name, chamada.args),
                        )
                        for chamada in chamadas
                    ])
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
