"""Configuração via variáveis de ambiente (.env na raiz do projeto)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _env_liga(name: str) -> bool:
    """Chave que vem LIGADA: só "0", "false", "nao"/"não" ou "off" desligam."""
    return os.getenv(name, "").strip().lower() not in ("0", "false", "nao", "não", "off", "no")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
# ID do servidor: os comandos aparecem na hora, mas SÓ nele. Vazio = comandos
# globais (a primeira propagação pode levar até 1h).
GUILD_ID = os.getenv("GUILD_ID", "").strip()

# ── O assistente (Gemini Live) ───────────────────────────────────────────────
# o nome é a wake word ("Jarvis, que horas são?") e como o Gemini se apresenta
BOT_NAME = os.getenv("BOT_NAME", "").strip() or "Jarvis"
# chave do Google AI Studio (aistudio.google.com/apikey)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live").strip()
# uma das vozes prontas do Gemini (Puck, Zephyr, Kore, Charon…); vazio = padrão
GEMINI_VOZ = os.getenv("GEMINI_VOZ", "").strip()
# o Gemini não sabe que horas são: o bot conta, neste fuso
FUSO = os.getenv("FUSO", "America/Sao_Paulo").strip()
# a pergunta e a resposta também vão escritas para o canal do /entrar
RESPOSTAS_NO_CHAT = _env_liga("RESPOSTAS_NO_CHAT")
# modo conversa: depois de responder, o bot espera a continuação de quem
# perguntou por estes segundos SEM precisar do nome. 0 desliga.
VOICE_CONVERSA_S = _env_float("VOICE_CONVERSA_S", 8.0)

# ── Ouvir a call ─────────────────────────────────────────────────────────────
# Whisper local (sem Groq): tiny = rápido, base/small = mais precisos. Num PC
# comum o base reconhece o nome bem melhor (0,6s por frase num Ryzen de 12 threads).
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base").strip()
# o Whisper que procura o nome no COMEÇO da frase, enquanto a pessoa ainda fala
# (é o que faz o estalo sair na hora). Sempre local; tiny = ~250 ms e o melhor
# em trechos curtos (medido em 27/09: achou o nome em 20 de 24, o base em 15).
# "off" desliga (máquina fraca); lento demais, ele se desliga sozinho.
WHISPER_DETECTOR = os.getenv("WHISPER_DETECTOR", "").strip() or "tiny"
# opcional: chave do Groq (console.groq.com) — transcrição na nuvem com o
# whisper-large-v3-turbo, bem melhor com o nome do bot, ~0,5s, grátis
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# o idioma da call para a Groq. Adivinhando sozinho (vazio), 20 de 74 falas de
# uma call brasileira saíram em outro idioma (27/09: russo, islandês, lituano,
# espanhol, inglês inventado) — e o "Jarvis" dito numa delas se perdia.
VOICE_IDIOMA = os.getenv("VOICE_IDIOMA", "pt").strip()
# diagnóstico: 1 = grava WAVs dos trechos em data/voice-debug e loga o texto
# transcrito. Deixe desligado no uso normal (é a fala das pessoas em disco).
VOICE_DEBUG = os.getenv("VOICE_DEBUG", "").strip().lower() in ("1", "true", "yes")
# segundos sem pacotes de áudio até considerar que a pessoa parou de falar.
# Pausa maior que isto no meio da pergunta não a corta: a escuta junta a
# continuação (core/listen.py).
VOICE_GAP_S = _env_float("VOICE_GAP_S", 0.8)
# energia mínima (RMS por janela de 20ms) para um trecho contar como fala
VOICE_MIN_RMS = _env_float("VOICE_MIN_RMS", 250.0)
# vocabulário passado ao Whisper do Groq como bias ("prompt") — ajuda a
# transcrever o nome do bot certo; defina vazio no .env para desligar
VOICE_STT_PROMPT = os.getenv("VOICE_STT_PROMPT", f"{BOT_NAME}.").strip()
# o estalo de "estou ouvindo" quando alguém chama o bot. VOICE_SOM_ESCUTA=0 desliga.
VOICE_SOM_ESCUTA = _env_liga("VOICE_SOM_ESCUTA")
# teto de requisições por minuto do Whisper na Groq (tier grátis: 20)
GROQ_STT_RPM = int(_env_float("GROQ_STT_RPM", 20))
# duração máxima (s) de um trecho enviado à Groq; 0 desliga. A escuta já só
# manda o COMEÇO de cada trecho (onde está o nome), então isto quase nunca age.
VOICE_TETO_S = _env_float("VOICE_TETO_S", 6.0)
# fallback para o Whisper LOCAL quando a Groq falha (0 desliga). Sem chave da
# Groq o local é o único motor e esta chave é ignorada.
VOICE_WHISPER_LOCAL = _env_liga("VOICE_WHISPER_LOCAL")
