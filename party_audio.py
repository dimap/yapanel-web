"""Автономный аудиодетектор для Party Mode.

Модуль содержит только чистую обработку PCM-сэмплов и не зависит от Android,
``tinycap2``, ``subprocess`` или каких-либо потоков. Благодаря этому его можно
использовать и тестировать на любой платформе: вызывающая сторона сама читает
аудио, конвертирует стерео в моно и передаёт сэмплы в :class:`BeatDetector`.

Пример::

    detector = BeatDetector(fs=48000.0)
    events = detector.process(mono_samples, now=time.monotonic())
    if events["kick"]:
        ...

``mono_samples`` — последовательность чисел (обычно int16 или float).
Возвращаемый ``kick_level`` / ``snare_level`` — RMS соответствующей полосы
на текущем чанке.
"""

import math


class BiquadBandPass:
    """Полосовой фильтр Biquad (RBJ, constant 0 dB peak gain).

    Перенесён из ``party.py`` без изменения поведения.
    """

    def __init__(self, fc=800.0, fs=48000.0, q=1.0):
        w0 = 2.0 * math.pi * fc / fs
        alpha = math.sin(w0) / (2.0 * q)
        cos_w0 = math.cos(w0)
        b0 = alpha
        b1 = 0.0
        b2 = -alpha
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0
        self.x1, self.x2 = 0.0, 0.0
        self.y1, self.y2 = 0.0, 0.0

    def reset(self):
        """Сбросить внутреннюю историю фильтра."""
        self.x1, self.x2 = 0.0, 0.0
        self.y1, self.y2 = 0.0, 0.0

    def process(self, samples):
        """Пропустить сэмплы через фильтр и вернуть RMS выходного сигнала."""
        sq = 0.0
        n = len(samples)
        if n == 0:
            return 0.0
        b0, b1, b2 = self.b0, self.b1, self.b2
        a1, a2 = self.a1, self.a2
        x1, x2 = self.x1, self.x2
        y1, y2 = self.y1, self.y2
        for s in samples:
            x = float(s)
            y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, x
            y2, y1 = y1, y
            sq += y * y
        self.x1, self.x2 = x1, x2
        self.y1, self.y2 = y1, y2
        return math.sqrt(sq / n)


class BeatDetector:
    """Детектор kick/snare по моно PCM.

    Логика повторяет ``PartyManager._audio_loop`` из ``party.py``:
    два полосовых фильтра (85 Гц и 1 кГц), адаптивное скользящее среднее
    (alpha=0.02, старт 400/600), порог ``avg * 1.30`` / ``avg * 1.25`` с
    нижними границами 250/350, антидребезг 0.22 с / 0.18 с и детекция
    локального максимума (``p1 > p2 and p1 >= curr``).

    Args:
        fs: частота дискретизации в герцах.

    Note:
        ``process(..., now=None)`` использует внутренние часы, которые
        продвигаются на длительность каждого чанка. Если передаётся ``now``,
        ожидается монотонное время в секундах (например, ``time.monotonic()``);
        смешивать оба режима в одном детекторе не следует.
    """

    KICK_FC = 85.0
    SNARE_FC = 1000.0
    Q = 1.2

    ALPHA = 0.02
    KICK_AVG_START = 400.0
    SNARE_AVG_START = 600.0
    KICK_THRESHOLD = 1.30
    SNARE_THRESHOLD = 1.25
    KICK_MIN_LEVEL = 250.0
    SNARE_MIN_LEVEL = 350.0
    KICK_DEBOUNCE = 0.22
    SNARE_DEBOUNCE = 0.18

    def __init__(self, fs=48000.0):
        fs = float(fs)
        if fs <= 0:
            raise ValueError("fs must be positive")
        self.fs = fs
        self.bpf_kick = BiquadBandPass(fc=self.KICK_FC, fs=fs, q=self.Q)
        self.bpf_snare = BiquadBandPass(fc=self.SNARE_FC, fs=fs, q=self.Q)
        self.reset()

    def reset(self):
        """Сбросить фильтры, средние, локальные максимумы и таймеры."""
        self.bpf_kick.reset()
        self.bpf_snare.reset()
        self.moving_avg_kick = self.KICK_AVG_START
        self.moving_avg_snare = self.SNARE_AVG_START
        self.p2_kick, self.p1_kick = 0.0, 0.0
        self.p2_snare, self.p1_snare = 0.0, 0.0
        self.last_kick = -math.inf
        self.last_snare = -math.inf
        self._clock = 0.0

    def process(self, mono_samples, now=None):
        """Обработать один чанк моно-сэмплов.

        Args:
            mono_samples: последовательность чисел (int16/float).
            now: время в секундах; при ``None`` используется внутренний счётчик.

        Returns:
            dict с ключами ``kick`` (bool), ``snare`` (bool),
            ``kick_level`` (float), ``snare_level`` (float).
        """
        if now is None:
            now = self._clock
            self._clock += len(mono_samples) / self.fs
        else:
            now = float(now)
            self._clock = now

        curr_kick = self.bpf_kick.process(mono_samples)
        thresh_kick = max(self.moving_avg_kick * self.KICK_THRESHOLD, self.KICK_MIN_LEVEL)
        kick = False
        if self.p1_kick > self.p2_kick and self.p1_kick >= curr_kick:
            if self.p1_kick > thresh_kick and (now - self.last_kick > self.KICK_DEBOUNCE):
                kick = True
                self.last_kick = now
        self.moving_avg_kick = (1.0 - self.ALPHA) * self.moving_avg_kick + self.ALPHA * curr_kick
        self.p2_kick, self.p1_kick = self.p1_kick, curr_kick

        curr_snare = self.bpf_snare.process(mono_samples)
        thresh_snare = max(self.moving_avg_snare * self.SNARE_THRESHOLD, self.SNARE_MIN_LEVEL)
        snare = False
        if self.p1_snare > self.p2_snare and self.p1_snare >= curr_snare:
            if self.p1_snare > thresh_snare and (now - self.last_snare > self.SNARE_DEBOUNCE):
                snare = True
                self.last_snare = now
        self.moving_avg_snare = (1.0 - self.ALPHA) * self.moving_avg_snare + self.ALPHA * curr_snare
        self.p2_snare, self.p1_snare = self.p1_snare, curr_snare

        return {
            "kick": kick,
            "snare": snare,
            "kick_level": curr_kick,
            "snare_level": curr_snare,
        }
