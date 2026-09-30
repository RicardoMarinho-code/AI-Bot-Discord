"""Comando /ajuda: o guia do assistente num embed organizado."""
import discord
from discord.ext import commands

import messages as m

_SECTIONS = [
    (
        "🎧 Entrar e sair",
        (
            "`/entrar` — entro no canal de voz em que você está e fico ouvindo\n"
            "`/sair` — saio da call (ou fale "
            f'*"{m.NOME}, sai da call"*, *"{m.NOME}, pode ir embora"* ou *"{m.NOME}, tchau"*)\n'
            "Se todo mundo sair, eu saio sozinho depois de 2 minutos."
        ),
    ),
    (
        "🗣️ Perguntar",
        (
            f'Comece pelo meu nome: *"{m.NOME}, quanto tempo leva pra cozinhar um ovo?"*.'
            " Eu respondo falando, e a pergunta e a resposta também aparecem aqui no chat.\n"
            f'Pode chamar e esperar: *"{m.NOME}…"* (toca um estalo) e aí fazer a pergunta.'
            " Pausa no meio da pergunta tudo bem, eu espero você terminar.\n"
            "Depois de eu responder, você tem uns segundos para continuar SEM repetir"
            ' o meu nome: *"e a de Portugal?"* depois da capital da França.'
        ),
    ),
    (
        "🧰 O que eu sei fazer",
        (
            "🌐 Notícias, placares, cotações e clima: pesquiso no Google antes de responder.\n"
            f'🧮 Contas certas: *"{m.NOME}, quanto é 15% de 1250?"*\n'
            '🕒 Hora e datas: *"que horas são em Tóquio?"*, *"quantos dias faltam pro Natal?"*\n'
            '🎲 Sorteios: *"joga um dado"*, *"cara ou coroa?"*, *"sorteia alguém da call"*\n'
            '📊 Enquetes no chat: *"faz uma enquete: pizza ou hambúrguer?"*\n'
            '⏱️ Cronômetro: *"inicia o cronômetro"*, *"quanto tempo deu?"*\n'
            '👥 *"Quem tá na call?"*, *"divide a call em dois times"*'
        ),
    ),
    (
        "⏰ Lembretes e 📝 anotações",
        (
            f'*"{m.NOME}, me avisa em 10 minutos pra tirar a pizza"*: na hora, eu te marco'
            ' aqui no chat. *"Quais são meus lembretes?"* e *"cancela meus lembretes"* também.\n'
            f'*"{m.NOME}, anota: comprar pão"*, *"o que eu anotei?"*, *"apaga minhas notas"*.\n'
            "`/lembretes` e `/notas` mostram (ou apagam) os seus — só você vê."
        ),
    ),
    (
        "⚙️ Comandos",
        (
            "`/voz` — troco a minha voz neste servidor (masculinas e femininas)\n"
            "`/status` — como eu estou: call, voz, memória, lembretes\n"
            "`/esquecer` — apago a memória da conversa na hora"
        ),
    ),
    (
        "✋ Interromper",
        (
            f'*"{m.NOME}, para"*, *"{m.NOME}, esquece"*, *"{m.NOME}, tá bom"* ou só'
            f' *"{m.NOME}"* corta a minha resposta. Uma pergunta nova também.'
        ),
    ),
    (
        "🔒 Privacidade",
        (
            "Eu escuto a call o tempo todo para ouvir o meu nome, mas só a fala de"
            " quem me chamou vai para a IA (Google Gemini). O resto fica no computador"
            " onde eu rodo. Eu lembro das últimas perguntas por 10 minutos; `/esquecer`"
            " apaga isso na hora."
        ),
    ),
]


class Help(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(name="ajuda", description="O que eu sei fazer, explicadinho")
    async def ajuda(self, ctx: discord.ApplicationContext) -> None:
        embed = discord.Embed(
            title=f"🤖 {m.NOME} — assistente de voz",
            description="Fico na call e respondo o que você perguntar, falando.",
            color=0x5865F2,
        )
        for name, value in _SECTIONS:
            embed.add_field(name=name, value=value, inline=False)
        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Help(bot))
