import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# O config.py carrega o .env de quem roda os testes: com a chave do Gemini lá,
# o suite mudava de comportamento (e podia ir à rede) na máquina de quem tem a
# chave e não no CI. O load_dotenv não sobrescreve o que já está no ambiente.
os.environ["GEMINI_API_KEY"] = ""
os.environ["BOT_NAME"] = ""
os.environ["GEMINI_VOZ"] = ""



@pytest.fixture(autouse=True)
def _dados_num_arquivo_temporario(tmp_path, monkeypatch):
    """Lembretes e vozes vão para data/: nos testes, para arquivos de mentira."""
    from core import lembretes
    from services import gemini

    monkeypatch.setattr(lembretes, "_ARQUIVO", str(tmp_path / "lembretes.json"))
    monkeypatch.setattr(gemini, "_ARQUIVO_VOZES", str(tmp_path / "vozes.json"))
