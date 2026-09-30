"""Smoke: o entrypoint e todos os cogs importam sem erro.

Pega na hora quebras de boot (import circular, símbolo renomeado, extensão
nova com typo) que só apareceriam ao subir o bot de verdade.
"""
import importlib
import pathlib
import re

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# lidos do main.py: cog novo que entra lá é testado aqui sem ninguém lembrar
_COGS = re.findall(r'"(cogs\.\w+)"', (_SRC / "main.py").read_text(encoding="utf-8"))


@pytest.mark.parametrize("module", ["main", *_COGS])
def test_module_imports(module):
    assert importlib.import_module(module) is not None


def test_todo_cog_do_disco_esta_carregado_no_main():
    """Cog escrito mas nunca carregado é bug silencioso — o comando some."""
    no_disco = {f"cogs.{p.stem}" for p in (_SRC / "cogs").glob("*.py") if p.stem != "__init__"}
    assert no_disco == set(_COGS)
