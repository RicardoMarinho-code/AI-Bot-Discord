"""O bot conhecendo a si mesmo: as próprias novidades e o próprio código.

"Jarvis, o que tem de novo?" sai do histórico do git (os commits são
escritos em português, para gente). "Como você sabe quem está na call?" sai
do código-fonte: o Gemini lista, busca e lê os arquivos do próprio bot.

Só o que está no repositório e é público: os .py de src/, o README e o
requirements.txt — nunca o .env nem o data/ (lembretes e notas das pessoas).
E o texto ainda passa pelo mascarador de segredos, por garantia.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from services.observabilidade import mascara

RAIZ = Path(__file__).resolve().parents[2]  # src/services/ → a raiz do projeto
_SOLTOS = ("README.md", "requirements.txt")
NOVIDADES_MAX = 30
_LER_MAX_LINHAS = 250
_LER_MAX_CARACTERES = 12_000  # a resposta da ferramenta vai inteira para o Gemini
_BUSCA_MAX = 40


def _arquivos() -> list[str]:
    """Os arquivos que o bot pode ler de si mesmo, como caminhos relativos (src/core/listen.py)."""
    codigo = sorted(
        p.relative_to(RAIZ).as_posix()
        for p in (RAIZ / "src").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    return codigo + [nome for nome in _SOLTOS if (RAIZ / nome).is_file()]


def _resolve(arquivo: str) -> str | None:
    """"listen.py", "core/listen.py" ou "src/core/listen.py" → o caminho na lista; None se não é dele."""
    pedido = arquivo.strip().replace("\\", "/").lstrip("./")
    todos = _arquivos()
    if pedido in todos:
        return pedido
    # pelo fim do caminho: "listen.py" ou "core/listen.py", se só um bate
    candidatos = [a for a in todos if a == f"src/{pedido}" or a.endswith(f"/{pedido}")]
    return candidatos[0] if len(candidatos) == 1 else None


def _resumo(caminho: Path) -> str:
    """A primeira linha da docstring do arquivo: o que ele faz, em uma frase."""
    if caminho.suffix != ".py":
        return ""
    try:
        doc = ast.get_docstring(ast.parse(caminho.read_text(encoding="utf-8")))
    except (OSError, SyntaxError, ValueError):
        return ""
    return (doc or "").strip().splitlines()[0][:160] if doc else ""


def novidades(quantidade: int = 10) -> dict:
    """As últimas mudanças no bot, do git: data e o que mudou, da mais nova à mais velha."""
    quantidade = min(max(int(quantidade), 1), NOVIDADES_MAX)
    try:
        saida = subprocess.run(
            ["git", "log", f"-{quantidade}", "--date=short", "--format=%h\t%ad\t%s"],
            cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        # instalado sem o git (o .tar.gz do README): o README conta o que o bot faz
        return {"erro": "sem o histórico do git nesta instalação", "dica": "leia o README.md com meu_codigo"}
    mudancas = []
    for linha in saida.splitlines():
        versao, data, assunto = (linha.split("\t", 2) + ["", ""])[:3]
        mudancas.append({"data": data, "versao": versao, "mudanca": mascara(assunto)})
    return {"versao_atual": mudancas[0]["versao"] if mudancas else "", "mudancas": mudancas}


def meu_codigo(acao: str, arquivo: str = "", texto: str = "", linha_inicial: int = 1, linha_final: int = 0) -> dict:
    """listar os arquivos, ler um (um trecho por vez) ou buscar um texto em todos."""
    acao = acao.strip().lower()
    if acao == "listar":
        return {"arquivos": [
            {"arquivo": a, "linhas": len((RAIZ / a).read_text(encoding="utf-8").splitlines()),
             "resumo": _resumo(RAIZ / a)}
            for a in _arquivos()
        ]}
    if acao == "ler":
        return _le(arquivo, linha_inicial, linha_final)
    if acao == "buscar":
        return _busca(texto)
    return {"erro": f"ação desconhecida: {acao} (use listar, ler ou buscar)"}


def _le(arquivo: str, linha_inicial: int, linha_final: int) -> dict:
    caminho = _resolve(arquivo)
    if caminho is None:
        return {"erro": f"não é um arquivo meu: {arquivo} (use listar para ver quais são)"}
    linhas = (RAIZ / caminho).read_text(encoding="utf-8").splitlines()
    inicio = max(int(linha_inicial or 1), 1)
    fim = min(int(linha_final or 0) or inicio + _LER_MAX_LINHAS - 1, inicio + _LER_MAX_LINHAS - 1, len(linhas))
    trecho, tamanho = [], 0
    for numero in range(inicio, fim + 1):
        linha = f"{numero}: {linhas[numero - 1]}"
        if tamanho + len(linha) > _LER_MAX_CARACTERES:
            fim = numero - 1
            break
        trecho.append(linha)
        tamanho += len(linha) + 1
    resultado = {
        "arquivo": caminho, "total_de_linhas": len(linhas),
        "de": inicio, "ate": fim, "conteudo": mascara("\n".join(trecho)),
    }
    if fim < len(linhas):
        resultado["continua"] = f"o resto começa na linha {fim + 1}"
    return resultado


def _busca(texto: str) -> dict:
    procurado = texto.strip().casefold()
    if len(procurado) < 2:
        return {"erro": "busque pelo menos 2 letras"}
    achados = []
    for arquivo in _arquivos():
        for numero, linha in enumerate((RAIZ / arquivo).read_text(encoding="utf-8").splitlines(), 1):
            if procurado in linha.casefold():
                achados.append(f"{arquivo}:{numero}: {mascara(linha.strip())[:160]}")
                if len(achados) >= _BUSCA_MAX:
                    return {"achados": achados, "aviso": f"parei nos primeiros {_BUSCA_MAX}"}
    return {"achados": achados}
