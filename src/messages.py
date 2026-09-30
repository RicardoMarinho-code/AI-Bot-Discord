"""TODAS as mensagens do bot num lugar só — edite à vontade! 🎨

Regras de ouro:
- Mantenha os {placeholders} (ex.: {pergunta}, {error}) — o bot preenche na hora.
- Emojis, **negrito**, *itálico* e `código` do Discord funcionam normalmente.
- Depois de editar, reinicie o bot para as mudanças valerem.
- O guia do /ajuda fica em cogs/help.py.
"""
import config

NOME = config.BOT_NAME

# ── Entrar e sair da call ────────────────────────────────────────────────────
JOIN_VOICE_FIRST = "Entra num canal de voz primeiro, que eu vou atrás 🎧"
JOINED = (
    f"🎧 **Tô na call!** É só me chamar pelo nome e perguntar:\n"
    f'> **"{NOME}, qual a capital da Austrália?"** · **"{NOME}, me conta uma curiosidade"**\n'
    f'-# Pra me interromper: **"{NOME}, para"**. Pra eu sair: `/sair` ou **"{NOME}, sai da call"**.'
)
JOIN_FAILED = "Não consegui entrar na call 😖 (`{error}`)"
CONN_DROPPED = "A conexão com a call caiu antes de eu começar a ouvir 😖 Tenta o `/entrar` de novo."
LEFT = "👋 Saí da call. Quando precisar, é só `/entrar`."
NOT_IN_CALL = "Eu nem tô numa call 😅 Use `/entrar` num canal de voz."
AUTO_LEFT_EMPTY = "👋 Fiquei sozinho na call e saí. Chama de novo com `/entrar`!"
VOICE_BYE = "👋 Falou! Até a próxima."

# ── Respostas ────────────────────────────────────────────────────────────────
VOICE_ANSWER = '🗣️ **{quem}:** *"{pergunta}"*\n>>> {resposta}'
VOICE_ANSWER_FAILED = "😵 Não consegui responder agora. Pergunta de novo daqui a pouco?"

# ── Geral ────────────────────────────────────────────────────────────────────
COMMAND_ERROR = "😵 Deu um erro aqui do meu lado. Tenta de novo?"
GUILD_WELCOME = (
    f"👋 Oi! Eu sou o **{NOME}**, um assistente de voz.\n"
    "Entra num canal de voz e manda `/entrar`: eu fico na call e respondo falando"
    " quando alguém me chamar pelo nome.\n"
    f'Tipo assim: **"{NOME}, quanto é 15% de 80?"** — o `/ajuda` explica o resto.'
)
