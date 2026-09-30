"""O logging do bot: segredos mascarados, ruído resumido, erros de thread no log."""
import logging
import threading

from services import observabilidade as obs


def _registro(msg: str, *args, exc_info=None, nome: str = "x") -> logging.LogRecord:
    return logging.LogRecord(nome, logging.WARNING, __file__, 1, msg, args, exc_info)


# ── segredos ─────────────────────────────────────────────────────────────────


def test_chave_da_groq_no_repr_de_um_erro_sai_mascarada():
    """Foi assim que vazou 25 vezes: o repr do ClientResponseError traz os headers."""
    erro = "ClientResponseError(RequestInfo(headers={'Authorization': 'Bearer gsk_OhMvAbCdEf1234567890xyz'}))"
    linha = obs.Redator(obs.FORMATO).format(_registro("LLM do Groq falhou: %s", erro))

    assert "gsk_OhMv" not in linha
    assert "LLM do Groq falhou" in linha


def test_segredo_dentro_do_traceback_tambem_e_mascarado():
    try:
        raise RuntimeError("falhou com Bearer gsk_SegredoNoTraceback123456")
    except RuntimeError:
        import sys

        linha = obs.Redator(obs.FORMATO).format(_registro("deu ruim", exc_info=sys.exc_info()))

    assert "SegredoNoTraceback" not in linha
    assert "RuntimeError" in linha


def test_valor_do_env_e_mascarado_mesmo_sem_formato_conhecido(monkeypatch):
    monkeypatch.setattr(obs.config, "GROQ_API_KEY", "segredo-sem-padrao-42")
    assert "segredo-sem-padrao-42" not in obs.mascara("auth: segredo-sem-padrao-42 recusado")


def test_token_do_discord_e_mascarado():
    # falso, montado em pedaços: inteiro, o push protection do GitHub o toma por real
    token = ".".join(["MTUzMzE4MTA0NzA3OTA0NzE5OA", "GabcdE", "abcdefghijklmnopqrstuvwxyz0123456789AB"])
    assert token not in obs.mascara(f"login com {token} falhou")


def test_token_e_sessao_da_conexao_de_voz_sao_mascarados():
    """O py-cord loga isto em INFO a cada conexão (visto no journal de 20/09)."""
    linha = (
        "Identifying ourselves: {'op': 0, 'd': {'server_id': '1248787627956637726',"
        " 'session_id': 'd68c4691d8efc905fe8702fce4992203', 'token': 'ef3d431ee18454e1',"
        " 'max_dave_protocol_version': 1}}"
    )
    limpa = obs.mascara(linha)

    assert "ef3d431ee18454e1" not in limpa and "d68c4691d8efc905" not in limpa
    # a chave fica, só o valor some (uma 1ª versão trocava o par inteiro por
    # bytes de controle: o grupo do regex se perdeu num heredoc)
    assert "'token': '***'" in limpa and "'session_id': '***'" in limpa
    assert limpa.isprintable()
    assert "'server_id': '1248787627956637726'" in limpa  # o resto continua legível
    assert "'max_dave_protocol_version': 1" in limpa


def test_texto_comum_passa_intacto():
    linha = "[123] Gemini respondeu: começou a falar em 2.4s, 6.3s de fala"
    assert obs.mascara(linha) == linha


def test_chave_do_gemini_no_formato_do_ai_studio_sai_mascarada():
    """O formato das chaves de 2026 ("AQ." + ~50 caracteres), mesmo sem estar no .env."""
    linha = obs.mascara("conectando com a chave AQ.ChaveFalsaDeTeste_nao_e_real_0123456789abcdef agora")

    assert "ChaveFalsa" not in linha and "AQ.***" in linha


def test_chave_do_gemini_no_formato_antigo_sai_mascarada():
    assert "AIzaSyA1" not in obs.mascara("key=AIzaSyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW")


def test_chave_do_gemini_do_env_sai_mascarada(monkeypatch):
    monkeypatch.setattr(obs.config, "GEMINI_API_KEY", "chave-do-gemini-sem-formato-1234")
    assert "chave-do-gemini" not in obs.mascara("erro com chave-do-gemini-sem-formato-1234")


# ── ruído ────────────────────────────────────────────────────────────────────


def test_rajada_de_cryptoerror_vira_um_aviso_com_contagem():
    filtro = obs.FiltroDeRajada(("Decryption failed",), janela_s=60.0)
    agora = [100.0]
    filtro._relogio = lambda: agora[0]

    primeiro = _registro("Critical error at AEAD: Decryption failed.")
    assert filtro.filter(primeiro) is True
    for _ in range(40):
        assert filtro.filter(_registro("Critical error at AEAD: Decryption failed.")) is False

    agora[0] += 61.0
    depois = _registro("Critical error at AEAD: Decryption failed.")
    assert filtro.filter(depois) is True
    assert "+40 iguais" in depois.getMessage()


def test_rajada_perde_o_traceback_de_15_linhas():
    import sys

    filtro = obs.FiltroDeRajada(("CryptoError",))
    try:
        raise ValueError("x")
    except ValueError:
        registro = _registro("CryptoError while decoding a voice packet", exc_info=sys.exc_info())

    assert filtro.filter(registro) is True
    assert registro.exc_info is None


def test_rotina_do_leitor_de_voz_nao_vai_para_o_espelho_de_erros():
    filtro = obs.FiltroDeRotina(("is waiting to be set as running",))
    rotina = _registro("Socket reader voice-socket-event-reader:0x7be1 is waiting to be set as running")

    assert filtro.filter(rotina) is False
    assert filtro.filter(_registro("Disconnected from voice... Reconnecting in 1.2s")) is True


def test_filtro_nao_toca_no_que_nao_e_ruido():
    filtro = obs.FiltroDeRajada(("Decryption failed",))
    for _ in range(5):
        assert filtro.filter(_registro("Disconnected from voice... Reconnecting in 1.2s")) is True


# ── erros fora do logging ────────────────────────────────────────────────────


def test_excecao_em_thread_passa_pelo_logging(caplog):
    """Eram 37 tracebacks crus no journal — e fora do espelho que vai ao dono."""
    anterior = threading.excepthook
    threading.excepthook = obs._excecao_em_thread
    try:
        with caplog.at_level(logging.ERROR, logger="services.observabilidade"):
            t = threading.Thread(target=lambda: 1 / 0, name="voice-receiver-packet-router")
            t.start()
            t.join()
    finally:
        threading.excepthook = anterior

    assert "voice-receiver-packet-router" in caplog.text
    assert "ZeroDivisionError" in caplog.text
