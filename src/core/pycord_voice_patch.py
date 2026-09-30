"""Curativo para a recepção de voz do py-cord 2.8.1 (upstream issue #3139).

O py-cord 2.8.1 reescreveu a recepção de voz para suportar DAVE (E2EE do
Discord) e deixou a migração pela metade — o próprio start_recording avisa
"Voice reception is currently broken". Os problemas concretos:

1. ``PacketDecryptor.decrypt_rtp`` descriptografa o áudio da camada de
   transporte (``raw_payload``) mas, quando a call NÃO usa DAVE, joga o
   resultado fora: nunca atribui ``packet.decrypted_data`` e retorna None,
   então o reader descarta todos os pacotes. Corrigido aqui via monkeypatch.

2. A base ``discord.sinks.Sink`` antiga não tem o que o novo
   SinkEventRouter/PacketDecoder esperam (``__sink_listeners__``,
   ``walk_children``, ``is_opus``) e ``start_recording`` não chama mais
   ``sink.init(vc)``. Isso é resolvido no ``CommandSink`` (core/listen.py).

3. ``PacketDecoder._decode_packet`` tem dois defeitos: (a) um único frame
   inválido (comum durante transições de epoch DAVE) mata a thread do
   PacketRouter inteira — e com ela a gravação; (b) quando o usuário está
   em "passthrough", ele aplica ``dave.decrypt`` sobre o PCM JÁ
   DECODIFICADO (ordem invertida — a descriptografia DAVE tem que vir
   antes do decode opus, e já acontece no item 1). Substituído por uma
   versão correta e tolerante: frame ruim = frame descartado.

4. ``VoiceClient.stop()`` para o playback E derruba o ``AudioReader`` (a
   recepção do modo de voz). Não existe forma pública de parar só a música,
   então todo skip deixava o bot surdo até a escuta religar. O ``apply()``
   injeta ``VoiceClient.stop_playing()`` — o mesmo código do ``stop()``
   upstream menos a parte do reader.

5. ``PacketDecoder._get_next_packet``, quando o jitter buffer estoura o
   timeout, dá ``flush()``, devolve só o primeiro pacote e DESCARTA o
   resto (269x no journal de produção, de 1 a 8 pacotes = até 160ms de
   fala perdidos no fim do trecho, onde está a última palavra do comando).
   Aqui o resto vira fila e sai nas chamadas seguintes.

6. O ``finally`` de ``PacketRouter.run`` chama ``client.stop_recording()``
   sem checar se ainda está gravando. Como o bot para a gravação primeiro,
   a thread do router morria com ``RecordingException: You are not
   recording`` a cada desligamento da escuta (36x no journal de produção).

7. ``VoiceClient._remove_ssrc`` usa ``self._reader.speaking_timer`` sem
   checar se há reader — e só há enquanto o bot grava. Com a escuta
   desligada, alguém sair da call levanta AttributeError dentro de
   ``_poll_ws``, que só trata queda de conexão: a task do poller morre e os
   eventos do gateway de voz daquela conexão param de ser processados
   (journal de 12/09 e 20/09). Corrigido, e o ``_recv_hook`` inteiro ganhou
   uma rede: exceção nele vira log em vez de poller morto.

8. ``UDPKeepAlive.delay = 5000`` vai direto para ``time.sleep()``: o
   keepalive UDP da recepção sai a cada 83 MINUTOS (issue upstream #3388, de
   13/09/2026 — lá, 7 de 7 gravações >10min ficaram mudas em 5–9min). Com
   música tocando o próprio áudio mantém o caminho UDP vivo; com o bot parado
   e só ouvindo é o "parou de me escutar" sem uma linha no journal. Aqui: 5s.

NÃO tente anunciar ``max_dave_protocol_version: 0`` para escapar do E2EE:
o gateway de voz derruba o identify com close code 4017 em loop infinito
(testado em 2026-08-01). A versão da call é negociada pelo servidor — se
ele responder 0 na session_description, cai no caminho sem DAVE corrigido
aqui; se responder 1, vale o caminho DAVE do item 1.

Remover este módulo quando o py-cord fechar o issue #3139.
"""
from __future__ import annotations

import functools
import logging

from discord import opus as _opus
from discord.voice import client as _voice_client
from discord.voice.receive import reader as _reader
from discord.voice.receive import router as _router

log = logging.getLogger(__name__)

# contadores de diagnóstico, exibidos periodicamente pelo VoiceListener
DIAG = {
    "transport": 0,  # pacotes descriptografados sem DAVE
    "dave_ok": 0,  # DAVE descriptografou
    "dave_fail": 0,  # DAVE falhou (vira silêncio)
    "dave_nouid": 0,  # DAVE ativo mas ssrc sem usuário mapeado
    "decode_ok": 0,  # opus decodificou
    "decode_fail": 0,  # opus falhou (frame descartado)
    "decode_fail_ext": 0,  # falhas em frames com extensão RTP
    "flush_recuperados": 0,  # pacotes do flush que o upstream jogaria fora
}
# o DAVE por pessoa: user_id → [descriptografados, falhas]. O total do DIAG não
# diz de QUEM é o áudio virando silêncio — em 27/09 eram ~50 falhas por segundo,
# sem parar (o ritmo de uma transmissão contínua: outro bot, ou a voz de alguém?)
DIAG_POR_USUARIO: dict[int, list[int]] = {}


def _fixed_decrypt_rtp(self, packet):
    state = self.client._connection
    dave = state.dave_session
    raw_payload = self._decryptor_rtp(packet)

    if dave is not None and dave.ready:
        user_id = state.ssrc_user_map.get(packet.ssrc)
        if user_id:
            try:
                import davey

                # a extensão RTP já foi tratada pelo _decrypt_rtp_<mode>; o
                # plaintext DAVE é opus puro. O upstream fazia um SEGUNDO
                # update_extended_header aqui, comendo bytes do áudio dos
                # frames com extensão (~40% deles) — não repetir!
                packet.decrypted_data = dave.decrypt(
                    user_id, davey.MediaType.audio, raw_payload
                )
                DIAG["dave_ok"] += 1
                DIAG_POR_USUARIO.setdefault(user_id, [0, 0])[0] += 1
            except Exception:  # noqa: BLE001 — upstream faz o mesmo
                packet.decrypted_data = _reader.OPUS_SILENCE
                DIAG["dave_fail"] += 1
                DIAG_POR_USUARIO.setdefault(user_id, [0, 0])[1] += 1
        else:
            DIAG["dave_nouid"] += 1
        return packet.decrypted_data

    # o upstream esquece esta atribuição no caminho sem DAVE (o áudio ia
    # para o lixo e o reader descartava todos os pacotes):
    packet.decrypted_data = raw_payload
    DIAG["transport"] += 1
    return packet.decrypted_data


def _fixed_decode_packet(self, packet):
    assert self._decoder is not None
    assert self.sink.client
    try:
        if packet:
            pcm = self._decoder.decode(packet.decrypted_data, fec=False)
        else:  # pacote perdido: tenta FEC do próximo, senão concealment
            next_packet = self._buffer.peek_next()
            if next_packet is not None:
                pcm = self._decoder.decode(next_packet.decrypted_data, fec=True)
            else:
                pcm = self._decoder.decode(None, fec=False)
    except Exception:  # noqa: BLE001 — frame ruim não pode matar o router
        DIAG["decode_fail"] += 1
        if getattr(packet, "extended", False):
            DIAG["decode_fail_ext"] += 1
        return packet, b""
    DIAG["decode_ok"] += 1
    return packet, pcm


def _limpa_pendentes(decoder) -> None:
    """Esquece os pacotes de flush guardados (troca de trecho / decoder morto)."""
    decoder.__dict__.pop("_pendentes_flush", None)


def _fixed_get_next_packet(self, timeout):
    """Entrega os pacotes do flush um a um em vez de jogar fora todos menos o 1º.

    Quando o jitter buffer estoura o timeout, o upstream faz ``flush()``,
    devolve ``packets[0]`` e descarta ``packets[1:]`` — só logando quantos
    perdeu. Em produção foram 269 avisos desses, de 1 a 8 pacotes: até 160ms
    de fala no lixo, quase sempre no fim do trecho, que é onde está a última
    palavra do comando. Aqui o resto fica pendente e sai nas chamadas
    seguintes, na ordem, com os ``None`` (lacunas reais) preservados para o
    tratamento de FEC/concealment do decoder.
    """
    pendentes = self.__dict__.get("_pendentes_flush")
    if pendentes:
        return pendentes.pop(0)

    packet = self._buffer.pop(timeout=timeout)
    if packet is None:
        if self._buffer:
            packets = list(self._buffer.flush())
            if len(packets) > 1:
                self.__dict__["_pendentes_flush"] = packets[1:]
                DIAG["flush_recuperados"] += len(packets) - 1
            return packets[0]
        return None
    if not packet:
        return self._make_fakepacket()
    return packet


def _envolve_limpando_pendentes(original):
    def wrapper(self, *args, **kwargs):
        _limpa_pendentes(self)
        return original(self, *args, **kwargs)

    return wrapper


def _parar_gravacao_se_precisar(reader) -> None:
    """``stop_recording()`` só quando ainda está gravando, e nunca levantando.

    Rodamos dentro de threads de teardown do py-cord: uma exceção aqui mata a
    thread com traceback no journal (e enterra erro de verdade no ruído).
    """
    try:
        client = reader.client
        if client.is_recording():  # a checagem que falta no upstream
            client.stop_recording()
    except Exception:  # noqa: BLE001 — teardown nunca pode levantar na thread
        log.debug("stop_recording no teardown do router falhou", exc_info=True)


def _fixed_sink_event_router_run(self) -> None:
    """``SinkEventRouter.run`` sem o stop_recording cru no caminho de erro."""
    try:
        self._do_run()
    except Exception as exc:  # noqa: BLE001 — mesmo contrato do upstream
        log.exception("erro no roteador de eventos do sink")
        self.reader.error = exc
        _parar_gravacao_se_precisar(self.reader)


def _fixed_router_run(self) -> None:
    """``PacketRouter.run`` que não explode quando a gravação já parou.

    O ``finally`` do upstream chama ``client.stop_recording()`` sem checar se
    ainda está gravando. Como o bot para a gravação primeiro (core/listen.py),
    a thread do router morria com ``RecordingException: You are not
    recording`` — 36 tracebacks no journal de produção, um por desligamento da
    escuta. Funcionalmente inofensivo (a thread ia terminar de qualquer jeito),
    mas enterrava erros de verdade no meio do ruído.
    """
    try:
        self._do_run()
    except Exception as exc:  # noqa: BLE001 — mesmo contrato do upstream
        log.exception("erro no loop do %s", self)
        self.reader.error = exc
    finally:
        _parar_gravacao_se_precisar(self.reader)
        self.waiter.clear()


def _stop_playing(self) -> None:
    """Para só a música; NÃO toca no AudioReader (escuta do modo de voz).

    Cópia de ``VoiceClient.stop()`` (voice/client.py) sem o bloco do
    ``_reader``. O ``AudioPlayer.stop()`` seta o evento ``_end``; a thread
    do player sai do loop e o ``finally`` dispara o callback ``after``
    normalmente — o avanço de fila continua funcionando.
    """
    if self._player:
        self._player.stop()
    if self._player_future:
        self.loop.call_soon_threadsafe(
            self._set_future_result_if_pending, self._player_future, None
        )
    self._player = None
    self._player_future = None


def _fixed_remove_ssrc(self, *, user_id: int) -> None:
    """``VoiceClient._remove_ssrc`` que sobrevive à escuta desligada."""
    ssrc = self._id_to_ssrc.pop(user_id, None)
    if ssrc:
        reader = self._reader
        if reader:  # MISSING (falsy) sempre que o bot não está gravando
            reader.speaking_timer.drop_ssrc(ssrc)
        self._ssrc_to_id.pop(ssrc, None)


def _protege_hook(original):
    """Erro no hook do websocket de voz vira log — o poller continua vivo."""

    @functools.wraps(original)
    async def wrapper(self, ws, msg):
        try:
            return await original(self, ws, msg)
        except Exception:  # noqa: BLE001 — CancelledError não é Exception e passa
            log.exception(
                "erro no hook do gateway de voz (op %s) — evento ignorado, poller segue",
                (msg or {}).get("op") if isinstance(msg, dict) else "?",
            )

    wrapper._cabrunco_patched = True
    return wrapper


KEEPALIVE_UDP_S = 5  # item 8: o valor que o upstream quis escrever (era 5000 s)
_keepalive_de_fabrica: float | None = None


def keepalive_de_fabrica() -> float:
    """O `delay` que veio no py-cord, guardado na 1ª aplicação do patch (para o
    teste avisar quando o upstream consertar e o item 8 puder sair)."""
    return _keepalive_de_fabrica if _keepalive_de_fabrica is not None else 0.0


def apply() -> None:
    global _keepalive_de_fabrica
    if _keepalive_de_fabrica is None:
        _keepalive_de_fabrica = _reader.UDPKeepAlive.delay
    _reader.UDPKeepAlive.delay = KEEPALIVE_UDP_S
    _reader.PacketDecryptor.decrypt_rtp = _fixed_decrypt_rtp
    # item 7: alguém sair da call com a escuta desligada matava o poller
    _voice_client.VoiceClient._remove_ssrc = _fixed_remove_ssrc
    if not getattr(_voice_client.VoiceClient._recv_hook, "_cabrunco_patched", False):
        _voice_client.VoiceClient._recv_hook = _protege_hook(_voice_client.VoiceClient._recv_hook)
    _opus.PacketDecoder._decode_packet = _fixed_decode_packet
    _voice_client.VoiceClient.stop_playing = _stop_playing
    # PacketRouter, NAO SinkEventRouter: e o `finally` dele que chama
    # stop_recording sem checar (o traceback no journal aponta router.py:124,
    # dentro de PacketRouter.run). Errei essa classe na primeira tentativa e
    # o traceback continuou aparecendo em produção.
    _router.PacketRouter.run = _fixed_router_run
    _router.SinkEventRouter.run = _fixed_sink_event_router_run
    _opus.PacketDecoder._get_next_packet = _fixed_get_next_packet
    # reset/destroy precisam esquecer os pendentes, senão um trecho antigo
    # vazaria para dentro do próximo na mesma SSRC
    for nome in ("reset", "destroy"):
        original = getattr(_opus.PacketDecoder, nome)
        if not getattr(original, "_cabrunco_patched", False):
            novo = _envolve_limpando_pendentes(original)
            novo._cabrunco_patched = True
            setattr(_opus.PacketDecoder, nome, novo)
