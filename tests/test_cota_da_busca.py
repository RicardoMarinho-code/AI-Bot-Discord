"""10/10: a cota da pesquisa Google acabou e TODA pergunta falhava.

Sem cota, a pergunta vai de novo na hora sem a busca, e a busca fica de fora
por um tempo — a conversa não para por causa da pesquisa.
"""
import asyncio
from types import SimpleNamespace

import pytest

import config
from services import gemini

_SEM_COTA = "1011 None. You exceeded your current quota, please check your plan and billing details."


def _tem_busca(cfg) -> bool:
    return any(getattr(t, "google_search", None) for t in cfg.tools)


class _Sessao:
    def __init__(self, falar_antes_de_quebrar=False):
        self.falar_antes = falar_antes_de_quebrar

    async def send_client_content(self, **kw):
        pass

    async def send_tool_response(self, **kw):
        pass

    async def send_realtime_input(self, **kw):
        pass

    async def receive(self):
        if self.falar_antes:
            yield SimpleNamespace(data=b"\x00" * 4800, tool_call=None, server_content=None)
            raise RuntimeError(_SEM_COTA)
        yield SimpleNamespace(data=None, tool_call=None, server_content=SimpleNamespace(
            input_transcription=None, output_transcription=SimpleNamespace(text="Oi."), turn_complete=True,
        ))


@pytest.fixture
def live(monkeypatch):
    """O Live falso: recusa por cota toda sessão COM a busca (como em 10/10)."""
    configs = []

    class Conexao:
        def __init__(self, cfg, erro, falar):
            self.cfg, self.erro, self.falar = cfg, erro, falar

        async def __aenter__(self):
            if _tem_busca(self.cfg) and not self.falar:
                raise RuntimeError(self.erro)
            return _Sessao(falar_antes_de_quebrar=self.falar and _tem_busca(self.cfg))

        async def __aexit__(self, *_):
            return False

    estado = SimpleNamespace(erro=_SEM_COTA, falar=False, configs=configs)

    def conecta(model, config):
        configs.append(config)
        return Conexao(config, estado.erro, estado.falar)

    monkeypatch.setattr(gemini, "_cliente", SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=conecta))))
    monkeypatch.setattr(gemini, "_busca_pausada_ate", 0.0)
    monkeypatch.setattr(gemini, "_TRANSCRICAO_ATRASADA_S", 0.01)
    monkeypatch.setattr(config, "GEMINI_BUSCA", True)
    gemini.esquece(70)
    return estado


def test_sem_cota_responde_sem_a_busca_e_pausa_a_busca(live):
    resposta = asyncio.run(gemini.responde(70, b"\x00" * 1920, texto="oi"))

    assert resposta.texto == "Oi."
    assert [_tem_busca(c) for c in live.configs] == [True, False]

    asyncio.run(gemini.responde(70, b"\x00" * 1920, texto="oi de novo"))
    assert [_tem_busca(c) for c in live.configs] == [True, False, False]  # nem tenta: pausada


def test_passada_a_pausa_a_busca_volta(live, monkeypatch):
    asyncio.run(gemini.responde(70, b"", texto="oi"))
    monkeypatch.setattr(gemini, "_busca_pausada_ate", 0.0)  # a pausa acabou

    asyncio.run(gemini.responde(70, b"", texto="oi"))

    assert _tem_busca(live.configs[2])


def test_outro_erro_nao_tira_a_busca(live):
    live.erro = "1007 Precondition check failed"

    with pytest.raises(RuntimeError):
        asyncio.run(gemini.responde(70, b"", texto="oi"))
    assert len(live.configs) == 1 and gemini._busca_pausada_ate == 0.0


def test_ja_falando_nao_repete_a_pergunta(live):
    """Cota que acaba no meio da fala: repetir faria o bot começar a resposta de novo."""
    live.falar = True
    pedacos = []

    with pytest.raises(RuntimeError):
        asyncio.run(gemini.responde(70, b"", texto="oi", ao_falar=pedacos.append))
    assert len(live.configs) == 1 and pedacos


def test_busca_desligada_no_env_nem_tenta(live, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_BUSCA", False)

    asyncio.run(gemini.responde(70, b"", texto="oi"))

    assert [_tem_busca(c) for c in live.configs] == [False]
