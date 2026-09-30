"""Nome indefinido não pode chegar em produção.

Em 12/09 `is_algorithmic_playlist` foi usado em cogs/music.py sem import; o
suite passou (ninguém exercitava aquele ramo) e o erro só apareceria para a
pessoa, como NameError, na hora em que uma playlist do Spotify falhasse. O
ruff pega isso em menos de um segundo — e, ao contrário do pyflakes, não se
confunde com as descrições em string do `discord.Option(...)`. Só roda se
ele estiver instalado (requirements-dev.txt).
"""
import os
import subprocess
import sys

import pytest

pytest.importorskip("ruff")

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_sem_nomes_indefinidos_em_src():
    saida = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821,F822,F823,E9",
         "--output-format", "concise", "src"],
        capture_output=True, text=True, cwd=_RAIZ, check=False,
    )
    assert saida.returncode == 0, saida.stdout + saida.stderr
