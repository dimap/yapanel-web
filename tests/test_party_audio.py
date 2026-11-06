"""Тесты автономного аудиодетектора ``party_audio``."""

import math

import pytest

from party_audio import BeatDetector, BiquadBandPass

FS = 48000.0
CHUNK = 2048


def _sine(freq, count, amp=1000.0, fade=0.005):
    """Синус с короткими fade-in/fade-out, чтобы не было щелчков на границах."""
    fade_n = min(int(FS * fade), count // 2)
    out = []
    for i in range(count):
        env = 1.0
        if fade_n:
            if i < fade_n:
                env = i / fade_n
            elif i >= count - fade_n:
                env = (count - 1 - i) / fade_n
        out.append(amp * env * math.sin(2.0 * math.pi * freq * i / FS))
    return out


def _track(freq, bursts=6, burst_s=0.25, gap_s=0.30, amp=6000.0, lead_s=0.5):
    """Синтетический трек: тишина, серия тональных всплесков, тишина."""
    data = [0.0] * int(FS * lead_s)
    burst_n = int(FS * burst_s)
    gap_n = int(FS * gap_s)
    for _ in range(bursts):
        data.extend(_sine(freq, burst_n, amp=amp))
        data.extend([0.0] * gap_n)
    data.extend([0.0] * int(FS * 0.25))
    return data


def _run(detector, samples, chunk=CHUNK):
    """Прогнать трек чанками, вернуть список событий с временем начала чанка."""
    events = []
    for start in range(0, len(samples) - chunk + 1, chunk):
        result = detector.process(samples[start:start + chunk])
        if result["kick"] or result["snare"]:
            events.append((start / FS, result))
    return events


def test_biquad_impulse_and_sine_positive_silence_zero():
    impulse = [1000.0] + [0.0] * 511
    assert BiquadBandPass(fc=85.0, fs=FS, q=1.2).process(impulse) > 0.0

    tone = _sine(1000.0, 1024, amp=1000.0)
    assert BiquadBandPass(fc=1000.0, fs=FS, q=1.2).process(tone) > 0.0

    # Свежий фильтр на тишине даёт ровно ноль.
    assert BiquadBandPass(fc=85.0, fs=FS, q=1.2).process([0.0] * 512) == 0.0
    assert BiquadBandPass(fc=1000.0, fs=FS, q=1.2).process([]) == 0.0


def test_low_frequency_bursts_produce_kick_not_snare():
    detector = BeatDetector(fs=FS)
    events = _run(detector, _track(60.0))

    kicks = [ev for _, ev in events if ev["kick"]]
    snares = [ev for _, ev in events if ev["snare"]]
    assert len(kicks) >= 4, f"expected kicks, got {len(kicks)} events"
    assert snares == []
    assert all(ev["kick_level"] > 0.0 for ev in kicks)


def test_high_frequency_bursts_produce_snare_not_kick():
    detector = BeatDetector(fs=FS)
    # Амплитуда выбрана так, чтобы просачивание 1 кГц в полосу 85 Гц
    # оставалось ниже минимального порога kick (250).
    events = _run(detector, _track(1000.0, amp=3000.0))

    snares = [ev for _, ev in events if ev["snare"]]
    kicks = [ev for _, ev in events if ev["kick"]]
    assert len(snares) >= 4, f"expected snares, got {len(snares)} events"
    assert kicks == []
    assert all(ev["snare_level"] > 0.0 for ev in snares)


def test_silence_produces_no_events():
    detector = BeatDetector(fs=FS)
    for _ in range(200):
        result = detector.process([0.0] * CHUNK)
        assert result["kick"] is False
        assert result["snare"] is False
        assert result["kick_level"] == 0.0
        assert result["snare_level"] == 0.0


def test_process_contract():
    detector = BeatDetector()
    result = detector.process([0.0] * 64)
    assert set(result) == {"kick", "snare", "kick_level", "snare_level"}
    assert isinstance(result["kick"], bool)
    assert isinstance(result["snare"], bool)
    assert isinstance(result["kick_level"], float)
    assert isinstance(result["snare_level"], float)


def test_reset_clears_state_and_no_false_event_afterwards():
    detector = BeatDetector(fs=FS)
    primed = _run(detector, _track(60.0, bursts=2))
    assert any(ev["kick"] for _, ev in primed)
    assert detector.p1_kick > 0.0 or detector.p2_kick > 0.0

    detector.reset()

    assert detector.moving_avg_kick == pytest.approx(BeatDetector.KICK_AVG_START)
    assert detector.moving_avg_snare == pytest.approx(BeatDetector.SNARE_AVG_START)
    assert (detector.p1_kick, detector.p2_kick) == (0.0, 0.0)
    assert (detector.p1_snare, detector.p2_snare) == (0.0, 0.0)
    assert detector.last_kick == -math.inf
    assert detector.last_snare == -math.inf
    assert (detector.bpf_kick.x1, detector.bpf_kick.y1) == (0.0, 0.0)
    assert (detector.bpf_snare.x1, detector.bpf_snare.y1) == (0.0, 0.0)

    result = detector.process([0.0] * CHUNK)
    assert result == {
        "kick": False,
        "snare": False,
        "kick_level": 0.0,
        "snare_level": 0.0,
    }

    # После сброса антидребезг тоже очищен: следующий всплеск детектируется.
    events = _run(detector, _track(60.0, bursts=2))
    assert any(ev["kick"] for _, ev in events)
