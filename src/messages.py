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

# ── Quem manda (o dono é quem deu o /entrar) ─────────────────────────────────
OWNER_ONLY = "🔒 Só {dono} (quem me chamou para a call) pode fazer isso."
OWNER_ONLY_MOVE = "🔒 Já estou na call com {dono}, que me chamou: só ele pode me levar para outra."
NOT_ALLOWED = "🔒 Agora eu só converso com {dono} e com quem ele liberar."
MODE_OPEN = "🔓 **Modo aberto:** todo mundo na call pode falar comigo."
MODE_OWNER = "🔒 **Modo só o dono:** agora eu só converso com {dono} (e com quem ele liberar no `/permitir`)."
ALLOWED = "✅ {pessoa} pode falar comigo."
BLOCKED = "🚫 Vou ignorar {pessoa}."
CANT_BLOCK_OWNER = "Não dá para bloquear quem manda em mim 😅"
OWNER_TRANSFERRED = "👑 Quem me chamou saiu da call: agora quem manda em mim é {novo}."
VOICE_CHANGED = "🎙️ Pronto, agora eu falo com a voz **{voz}** ({estilo}). Me chama pra ouvir!"
VOICE_RESET = "🎙️ Voltei para a minha voz de sempre."
FORGOT = "🧽 Pronto, esqueci a nossa conversa. A próxima pergunta começa do zero."

# ── Respostas ────────────────────────────────────────────────────────────────
VOICE_ANSWER = '🗣️ **{quem}:** *"{pergunta}"*\n>>> {resposta}'
VOICE_ANSWER_FAILED = "😵 Não consegui responder agora. Pergunta de novo daqui a pouco?"
POLL = "📊 **Enquete** (pedida por {quem}): **{pergunta}**\n{opcoes}\n-# Vote na reação!"
POLL_NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
REMINDER = "⏰ {mencao}, lembrete: **{texto}**"
REMINDER_SEM_TEXTO = "deu a hora que você pediu"
REMINDERS_NONE = f'Você não tem lembretes pendentes. Pede um: **"{NOME}, me avisa em 10 minutos"** ⏰'
REMINDERS_LIST = "⏰ **Seus lembretes:**\n{lista}"
REMINDERS_ITEM = "• **{texto}**, <t:{quando}:R>"
REMINDERS_CANCELED = "🗑️ Cancelei {n} lembrete(s)."

# ── /notas ───────────────────────────────────────────────────────────────────
NOTES_NONE = f'Você não tem anotações. Pede uma: **"{NOME}, anota: comprar pão"** 📝'
NOTES_LIST = "📝 **Suas anotações:**\n{lista}"
NOTES_DELETED = "🗑️ Apaguei {n} anotação(ões)."

# ── /status ──────────────────────────────────────────────────────────────────
STATUS = (
    "📊 **Status**\n"
    "🎧 {call}\n"
    "🎙️ Voz: **{voz}**\n"
    "🧠 Memória: {trocas} troca(s) da conversa\n"
    "⏰ Lembretes pendentes aqui: {lembretes}\n"
    "{jogo}"
    "{dono}"
    "📶 Ping: {ping} ms · no ar há {no_ar}\n"
    "-# Modelo: {modelo}"
)
STATUS_IN_CALL = "Na call em {canal}"
STATUS_OUT_OF_CALL = "Fora da call (use `/entrar`)"
STATUS_OWNER = "👑 Quem manda: {dono} · modo **{modo}**\n"
STATUS_STOPWATCH = "⏱️ Cronômetro rodando: {tempo}\n"
STATUS_SCORE = "🏆 Placar: {placar}\n"

# ── Geral ────────────────────────────────────────────────────────────────────
COMMAND_ERROR = "😵 Deu um erro aqui do meu lado. Tenta de novo?"
GUILD_WELCOME = (
    f"👋 Oi! Eu sou o **{NOME}**, um assistente de voz.\n"
    "Entra num canal de voz e manda `/entrar`: eu fico na call e respondo falando"
    " quando alguém me chamar pelo nome.\n"
    f'Tipo assim: **"{NOME}, quanto é 15% de 80?"** — o `/ajuda` explica o resto.'
)
