"""Conversão de áudio Discord (48kHz stereo s16) → Whisper (16kHz mono f32)."""
import numpy as np

from core.listen import analyze_speech
from services.speech import _retry_delay, pcm_to_float_mono_16k


def _stereo_pcm(samples_48k: np.ndarray) -> bytes:
    stereo = np.repeat(samples_48k.astype(np.int16), 2)
    return stereo.tobytes()


def test_downsamples_48k_stereo_to_16k_mono():
    one_second = _stereo_pcm(np.ones(48000, dtype=np.int16) * 1000)
    out = pcm_to_float_mono_16k(one_second)
    assert out.dtype == np.float32
    assert len(out) == 16000


def test_output_range_is_normalized():
    loud = _stereo_pcm(np.full(4800, 32767, dtype=np.int16))
    out = pcm_to_float_mono_16k(loud)
    assert 0.99 <= float(out.max()) <= 1.0


def test_empty_and_odd_input_do_not_crash():
    assert len(pcm_to_float_mono_16k(b"")) == 0
    assert len(pcm_to_float_mono_16k(b"\x00\x01")) == 0


# ── analyze_speech: gate de energia por janelas de 20ms + trim ───────────────

_WIN_SAMPLES = 1920  # 20ms de PCM 48kHz stereo int16
_WIN_BYTES = _WIN_SAMPLES * 2


def _windows(*levels: int) -> bytes:
    """PCM sintético: uma janela de 20ms por nível de amplitude."""
    parts = [np.full(_WIN_SAMPLES, level, dtype=np.int16) for level in levels]
    return np.concatenate(parts).tobytes()


def test_counts_only_windows_above_threshold():
    pcm = _windows(0, 0, 3000, 3000, 3000, 0, 0)
    hot, _trimmed = analyze_speech(pcm, threshold=250.0)
    assert hot == 3


def test_short_burst_is_not_diluted_by_long_silence():
    # RMS global diluiria 3 janelas de fala em 2s de silêncio; por janela não
    pcm = _windows(*([0] * 50), 3000, 3000, 3000, *([0] * 50))
    hot, trimmed = analyze_speech(pcm, threshold=250.0)
    assert hot == 3
    # trim: fala + 1 janela de margem de cada lado (bem menor que o total)
    assert 3 * _WIN_BYTES <= len(trimmed) <= 5 * _WIN_BYTES


def test_pure_silence_and_low_noise_are_rejected():
    assert analyze_speech(_windows(*([0] * 20)), threshold=250.0) == (0, b"")
    hot, _ = analyze_speech(_windows(*([100] * 20)), threshold=250.0)
    assert hot == 0  # respiração/ruído baixo


def test_tiny_input_does_not_crash():
    assert analyze_speech(b"", threshold=250.0) == (0, b"")
    assert analyze_speech(b"\x00\x01\x02", threshold=250.0) == (0, b"")


# ── _retry_delay: retry único do Groq em 429/5xx ─────────────────────────────


def test_retry_delay_only_for_transient_statuses():
    assert _retry_delay(200, None) is None
    assert _retry_delay(400, None) is None
    assert _retry_delay(401, None) is None  # chave inválida: retentar não ajuda
    assert _retry_delay(429, None) == 1.0
    assert _retry_delay(503, None) == 1.0


def test_retry_delay_respects_retry_after_with_cap():
    assert _retry_delay(429, "0.5") == 0.5
    assert _retry_delay(429, "30") == 2.0  # cap: trecho >12s é descartado mesmo
    assert _retry_delay(429, "lixo") == 1.0


def test_peak_rms_de_silencio_e_zero_e_de_fala_e_alto():
    from core.listen import peak_rms

    assert peak_rms(_windows(0, 0, 0)) == 0.0
    assert peak_rms(_windows(0, 3000, 0)) > 2500
    assert peak_rms(b"") == 0.0
