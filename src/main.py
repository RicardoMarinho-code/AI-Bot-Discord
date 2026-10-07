"""Entrypoint do bot. Rode da raiz do projeto: python src/main.py"""
import asyncio
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord  # noqa: E402

import config  # noqa: E402
import messages as m  # noqa: E402
from core.intents import registra_nome  # noqa: E402
from core.pycord_voice_patch import apply as apply_voice_patch  # noqa: E402
from services import observabilidade  # noqa: E402

apply_voice_patch()  # recepção de voz do py-cord 2.8.1 (ver o módulo)

log = logging.getLogger("main")

_shutting_down = False


async def _shutdown(bot: discord.Bot) -> None:
    """Desligamento gracioso no SIGTERM (Linux): sai das calls antes de fechar."""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    log.info("🛑 SIGTERM — desligando...")
    from core.assistente import todos
    from services import http

    try:
        for sessao in todos():
            try:
                await asyncio.wait_for(sessao.sai("desligamento do bot"), timeout=3)
            except Exception:  # noqa: BLE001 — desligar nunca pode travar
                pass
        await http.close()
    finally:
        await bot.close()


def main() -> None:
    # níveis, segredos mascarados e tracebacks de thread DENTRO do logging
    observabilidade.configura_logging()

    if not config.DISCORD_TOKEN:
        sys.exit(
            "❌ DISCORD_TOKEN não configurado.\n"
            "   Copie .env.example para .env e preencha o token do bot."
        )
    if not config.GEMINI_API_KEY:
        sys.exit(
            "❌ GEMINI_API_KEY não configurada — é ela que responde as perguntas.\n"
            "   Pegue uma chave grátis em https://aistudio.google.com/apikey e coloque no .env."
        )

    nome = registra_nome(config.BOT_NAME)
    intents = discord.Intents.default()
    debug_guilds = [int(config.GUILD_ID)] if config.GUILD_ID else None
    bot = discord.Bot(intents=intents, debug_guilds=debug_guilds)

    @bot.event
    async def on_ready() -> None:
        log.info(
            "✅ Online como %s — em %d servidor(es). Chame por %r.",
            bot.user, len(bot.guilds), nome,
        )
        if hasattr(bot, "_preparado"):  # on_ready dispara de novo em reconexões
            return
        bot._preparado = True
        if sys.platform != "win32":  # no Windows o Ctrl+C já é tratado no bot.run()
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGTERM, lambda: asyncio.create_task(_shutdown(bot))
            )
        # aquece em background o que a primeira pergunta pagaria: o SDK do
        # Gemini, o detector do nome (é ele que faz o estalo sair na hora) e o
        # Whisper maior só quando é o motor principal (sem Groq)
        from core import lembretes
        from services import gemini, speech

        bot._restaura = asyncio.create_task(lembretes.restaura(bot))  # os de antes do reinício
        gemini.carrega_vozes()  # as do /voz
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, gemini.aquece)
        if speech.detector_ligado():
            loop.run_in_executor(None, speech.preload_detector)
        if not config.GROQ_API_KEY:
            loop.run_in_executor(None, speech.preload)

    @bot.event
    async def on_error(event: str, *args, **kwargs) -> None:
        # o padrão do py-cord imprime "Ignoring exception in on_x" + traceback
        # cru no stderr: sem horário, sem nível e sem os segredos mascarados
        log.exception("erro no evento %s", event)

    @bot.event
    async def on_application_command_error(
        ctx: discord.ApplicationContext, error: discord.DiscordException
    ) -> None:
        # sem isso, exceção em slash command virava "O aplicativo não
        # respondeu" para o usuário e sumia sem rastro no log
        original = getattr(error, "original", error)
        log.error(
            "erro no /%s: %r",
            getattr(ctx.command, "qualified_name", "?"),
            original,
            exc_info=original,
        )
        try:
            await ctx.respond(m.COMMAND_ERROR, ephemeral=True)
        except discord.HTTPException:
            pass

    for extension in (
        "cogs.voice",
        "cogs.help",
        "cogs.lifecycle",
        "cogs.registro",
        "cogs.status",
        "cogs.notas",
        "cogs.sessao",
        "cogs.chat",
    ):
        bot.load_extension(extension)

    try:
        bot.run(config.DISCORD_TOKEN)
    except RuntimeError as exc:
        # py-cord fecha a própria sessão HTTP dentro de close(), mas o loop de
        # reconexão do gateway ainda está de pé: a tentativa seguinte levanta
        # "Session is closed" num desligamento que deu certo. Só engolimos isso
        # quando o SIGTERM foi nosso — qualquer outro RuntimeError continua subindo.
        if not (_shutting_down and "Session is closed" in str(exc)):
            raise
        log.info("desligado")


if __name__ == "__main__":
    main()
