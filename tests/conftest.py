import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# O config.py carrega o .env de quem roda os testes: com a chave do Gemini lá,
# o suite mudava de comportamento (e podia ir à rede) na máquina de quem tem a
# chave e não no CI. O load_dotenv não sobrescreve o que já está no ambiente.
os.environ["GEMINI_API_KEY"] = ""
os.environ["BOT_NAME"] = ""
os.environ["GEMINI_VOZ"] = ""
