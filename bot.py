import os
from collections import defaultdict, deque

import discord
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
NVIDIA_API_KEY = os.environ["NVIDIA_API_KEY"]
MODEL = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
BOT_NAME = os.getenv("BOT_NAME", "Jarvis")

SYSTEM_PROMPT = (
    f"Você é {BOT_NAME}, um assistente num servidor do Discord. "
    "Responda sempre em português do Brasil, de forma curta e natural, "
    "como numa conversa falada. Não use markdown, listas nem emojis, "
    "porque suas respostas depois serão lidas em voz alta."
)

llm = AsyncOpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_API_KEY)

# Memória curta por canal: últimas 10 mensagens (5 trocas)
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))


async def ask_llm(channel_id: int, question: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[channel_id]]
    messages.append({"role": "user", "content": question})

    completion = await llm.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.7,
        top_p=0.95,
        max_tokens=1024,
        # Sem "thinking": responde bem mais rápido, importante para a voz depois
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    answer = (completion.choices[0].message.content or "").strip()

    history[channel_id].append({"role": "user", "content": question})
    history[channel_id].append({"role": "assistant", "content": answer})
    return answer


bot = discord.Bot()


@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")


@bot.slash_command(name="pergunta", description=f"Faça uma pergunta para o {BOT_NAME}")
async def pergunta(ctx: discord.ApplicationContext, texto: str):
    await ctx.defer()  # o modelo pode demorar mais que os 3s que o Discord espera
    try:
        answer = await ask_llm(ctx.channel_id, texto)
    except Exception as e:
        await ctx.followup.send(f"Deu erro ao falar com o modelo: `{e}`")
        return
    # Discord limita mensagens a 2000 caracteres
    for i in range(0, len(answer) or 1, 2000):
        await ctx.followup.send(answer[i : i + 2000] or "(resposta vazia)")


@bot.slash_command(name="esquecer", description="Apaga a memória da conversa neste canal")
async def esquecer(ctx: discord.ApplicationContext):
    history.pop(ctx.channel_id, None)
    await ctx.respond("Memória apagada.")


bot.run(DISCORD_TOKEN)
