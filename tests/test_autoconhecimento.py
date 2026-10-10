"""O bot conhecendo a si mesmo: as novidades (git) e o próprio código."""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from services import autoconhecimento as auto
from services import gemini


def test_novidades_vem_do_git(monkeypatch):
    def git_log(cmd, **kw):
        assert cmd[:2] == ["git", "log"] and "-3" in cmd
        return SimpleNamespace(stdout="114f5fd\t2026-10-07\tQuem manda no Jarvis\n69a7313\t2026-09-30\tPlacar por voz\n")

    monkeypatch.setattr(auto.subprocess, "run", git_log)

    assert auto.novidades(3) == {
        "versao_atual": "114f5fd",
        "mudancas": [
            {"data": "2026-10-07", "versao": "114f5fd", "mudanca": "Quem manda no Jarvis"},
            {"data": "2026-09-30", "versao": "69a7313", "mudanca": "Placar por voz"},
        ],
    }


def test_novidades_sem_git_aponta_o_readme(monkeypatch):
    def sem_git(*a, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(auto.subprocess, "run", sem_git)
    assert "README" in auto.novidades()["dica"]

    def falha(*a, **kw):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(auto.subprocess, "run", falha)
    assert "erro" in auto.novidades()


def test_sem_git_as_novidades_vem_do_pacote(monkeypatch, tmp_path):
    """Instalado pelo .tar.gz: o pacote traz o git log no NOVIDADES.txt."""
    def sem_git(*a, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(auto.subprocess, "run", sem_git)
    monkeypatch.setattr(auto, "RAIZ", tmp_path)
    (tmp_path / "NOVIDADES.txt").write_text(
        "0477230\t2026-10-10\tJarvis conhece a si mesmo\n114f5fd\t2026-10-07\tQuem manda no Jarvis\n",
        encoding="utf-8",
    )

    resultado = auto.novidades(1)

    assert resultado == {
        "versao_atual": "0477230",
        "mudancas": [{"data": "2026-10-10", "versao": "0477230", "mudanca": "Jarvis conhece a si mesmo"}],
    }


def test_novidades_no_repositorio_de_verdade():
    """Com o .git (o checkout do CI tem ao menos o último commit), sem simular nada."""
    resultado = auto.novidades(1)
    if "erro" not in resultado:
        assert len(resultado["mudancas"]) == 1 and resultado["versao_atual"]


def test_lista_os_proprios_arquivos_com_o_que_cada_um_faz():
    arquivos = {a["arquivo"]: a for a in auto.meu_codigo("listar")["arquivos"]}

    assert "src/core/listen.py" in arquivos and "README.md" in arquivos
    assert arquivos["src/services/autoconhecimento.py"]["resumo"].startswith("O bot conhecendo a si mesmo")
    assert not any("__pycache__" in a or a.endswith(".env") for a in arquivos)


def test_le_um_trecho_com_os_numeros_das_linhas():
    trecho = auto.meu_codigo("ler", arquivo="calculadora.py", linha_inicial=1, linha_final=3)

    assert trecho["arquivo"] == "src/services/calculadora.py" and (trecho["de"], trecho["ate"]) == (1, 3)
    assert trecho["conteudo"].startswith('1: """') and "continua" in trecho


def test_arquivo_grande_vem_aos_pedacos():
    trecho = auto.meu_codigo("ler", arquivo="src/core/listen.py")

    assert trecho["ate"] < trecho["total_de_linhas"] and len(trecho["conteudo"]) <= auto._LER_MAX_CARACTERES
    seguinte = auto.meu_codigo("ler", arquivo="src/core/listen.py", linha_inicial=trecho["ate"] + 1)
    assert seguinte["de"] == trecho["ate"] + 1


@pytest.mark.parametrize("proibido", [
    ".env", "../.env", "src/../.env", "..\\.env", "data/notas.json", "/etc/passwd",
    "C:/Windows/win.ini", "tests/conftest.py", "", "py",
])
def test_nao_le_nada_fora_do_proprio_codigo(proibido):
    assert "erro" in auto.meu_codigo("ler", arquivo=proibido)


def test_busca_acha_arquivo_e_linha():
    achados = auto.meu_codigo("buscar", texto="def pode_falar")["achados"]

    assert any(a.startswith("src/core/assistente.py:") for a in achados)
    assert "erro" in auto.meu_codigo("buscar", texto="x")
    assert "erro" in auto.meu_codigo("pular")


def test_segredo_no_codigo_sai_mascarado(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "vazou.py").write_text('CHAVE = "gsk_ABCDEFGHIJKLMNOP1234"\n', encoding="utf-8")
    monkeypatch.setattr(auto, "RAIZ", tmp_path)

    assert "gsk_ABCDEFGH" not in auto.meu_codigo("ler", arquivo="vazou.py")["conteudo"]
    assert "gsk_ABCDEFGH" not in str(auto.meu_codigo("buscar", texto="CHAVE"))


def test_ferramentas_pelo_gemini_nao_quebram_com_argumento_ruim():
    assert "erro" in gemini.sobre_mim(gemini._MEU_CODIGO, {"acao": "ler", "arquivo": "listen.py", "linha_inicial": "um"})
    assert "arquivos" in gemini.sobre_mim(gemini._MEU_CODIGO, {"acao": "listar"})


def test_instrucao_diz_que_ele_conhece_o_proprio_codigo():
    texto = gemini._instrucao()
    assert gemini._NOVIDADES in texto and gemini._MEU_CODIGO in texto and "nunca inventando" in texto


def test_pela_sessao_a_leitura_roda_fora_do_loop(monkeypatch):
    usadas = []

    async def em_thread(funcao, *args):
        usadas.append(funcao.__name__)
        return funcao(*args)

    monkeypatch.setattr(gemini.asyncio, "to_thread", em_thread)
    chamada = SimpleNamespace(id="c1", name=gemini._MEU_CODIGO, args={"acao": "buscar", "texto": "def calcula"})
    mensagens = [
        SimpleNamespace(data=None, server_content=None, tool_call=SimpleNamespace(function_calls=[chamada])),
        SimpleNamespace(data=None, tool_call=None, server_content=SimpleNamespace(
            input_transcription=None, output_transcription=SimpleNamespace(text="Fica na calculadora."),
            turn_complete=True,
        )),
    ]
    enviados = []

    class Sessao:
        async def send_client_content(self, **kw):
            pass

        async def send_tool_response(self, **kw):
            enviados.append(kw["function_responses"][0].response)

        async def receive(self):
            while mensagens:
                msg = mensagens.pop(0)
                yield msg
                if msg.server_content is not None and msg.server_content.turn_complete:
                    return

    class Conexao:
        async def __aenter__(self):
            return Sessao()

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(gemini, "_cliente", SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(
        connect=lambda model, config: Conexao(),
    ))))
    gemini.esquece(60)

    resposta = asyncio.run(gemini.responde(60, b"", texto="onde você faz contas?"))

    assert "sobre_mim" in usadas and resposta.texto == "Fica na calculadora."
    assert any("src/services/calculadora.py" in a for a in enviados[0]["achados"])
