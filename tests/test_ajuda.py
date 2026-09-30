"""O /ajuda: cabe nos limites do Discord e fala de todo comando que existe."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from cogs import help as ajuda

_SRC = Path(__file__).resolve().parent.parent / "src"


def test_cabe_nos_limites_do_embed():
    """Campo acima de 1024 caracteres (ou embed acima de 6000) e o Discord recusa o /ajuda inteiro."""
    assert len(ajuda._SECTIONS) <= 25
    for nome, valor in ajuda._SECTIONS:
        assert len(nome) <= 256 and len(valor) <= 1024, nome
    assert sum(len(n) + len(v) for n, v in ajuda._SECTIONS) < 5500


def test_todo_comando_aparece_na_ajuda():
    """Comando novo sem estar no /ajuda é comando que ninguém descobre."""
    comandos = set()
    for arquivo in (_SRC / "cogs").glob("*.py"):
        comandos |= set(re.findall(r'slash_command\(\s*name="(\w+)"', arquivo.read_text(encoding="utf-8")))
    texto = "".join(valor for _, valor in ajuda._SECTIONS)
    faltando = {c for c in comandos - {"ajuda"} if f"`/{c}`" not in texto}
    assert comandos and not faltando


def test_responde_so_para_quem_pediu():
    ctx = SimpleNamespace(respond=AsyncMock())

    asyncio.run(ajuda.Help.ajuda.callback(ajuda.Help(MagicMock()), ctx))

    embed = ctx.respond.await_args.kwargs["embed"]
    assert ctx.respond.await_args.kwargs["ephemeral"] is True
    assert len(embed.fields) == len(ajuda._SECTIONS)
