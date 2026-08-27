from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np


SAMPLE_RATE = 44_100


def _envelope(length: int, attack: float = 0.08, release: float = 0.18) -> np.ndarray:
    env = np.ones(length, dtype=np.float64)
    attack_samples = max(1, min(length, int(SAMPLE_RATE * attack)))
    release_samples = max(1, min(length, int(SAMPLE_RATE * release)))
    env[:attack_samples] = np.linspace(0.0, 1.0, attack_samples)
    env[-release_samples:] *= np.linspace(1.0, 0.0, release_samples)
    return env


def _add_tone(track: np.ndarray, start: float, duration: float, frequency: float, amplitude: float, pan: float) -> None:
    first = int(start * SAMPLE_RATE)
    length = min(int(duration * SAMPLE_RATE), len(track) - first)
    if first < 0 or length <= 0:
        return
    time = np.arange(length, dtype=np.float64) / SAMPLE_RATE
    tone = (
        np.sin(2 * np.pi * frequency * time)
        + 0.24 * np.sin(2 * np.pi * frequency * 2.0 * time)
        + 0.08 * np.sin(2 * np.pi * frequency * 3.0 * time)
    )
    tone *= amplitude * _envelope(length)
    left = np.sqrt((1.0 - pan) * 0.5)
    right = np.sqrt((1.0 + pan) * 0.5)
    track[first:first + length, 0] += tone * left
    track[first:first + length, 1] += tone * right


def _add_soft_hit(track: np.ndarray, start: float, frequency: float, amplitude: float, pan: float = 0.0) -> None:
    first = int(start * SAMPLE_RATE)
    length = min(int(0.34 * SAMPLE_RATE), len(track) - first)
    if first < 0 or length <= 0:
        return
    time = np.arange(length, dtype=np.float64) / SAMPLE_RATE
    decay = np.exp(-time * 13.0)
    hit = np.sin(2 * np.pi * (frequency * (1.0 - 0.3 * time)) * time) * decay * amplitude
    left = np.sqrt((1.0 - pan) * 0.5)
    right = np.sqrt((1.0 + pan) * 0.5)
    track[first:first + length, 0] += hit * left
    track[first:first + length, 1] += hit * right


def _build_track(duration: float, bpm: float, chords: list[tuple[float, ...]], mood: str, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    total_samples = int(duration * SAMPLE_RATE)
    track = np.zeros((total_samples, 2), dtype=np.float64)
    beat = 60.0 / bpm
    chord_span = beat * 8.0

    for chord_index, chord in enumerate(chords):
        start = chord_index * chord_span
        for note_index, frequency in enumerate(chord):
            pan = (-0.38 + note_index * 0.38) if len(chord) > 1 else 0.0
            _add_tone(track, start, chord_span + 0.25, frequency, 0.075, pan)
            _add_tone(track, start, chord_span + 0.25, frequency / 2.0, 0.045, -pan * 0.5)

    total_beats = int(duration / beat)
    for index in range(total_beats):
        start = index * beat
        if mood == "brain_lens":
            if index % 2 == 0:
                _add_soft_hit(track, start, 72.0, 0.12)
            if index % 4 == 2:
                _add_soft_hit(track, start, 158.0, 0.055, pan=0.35)
            chord = chords[(index // 8) % len(chords)]
            note = chord[index % len(chord)] * (2.0 if index % 4 in {1, 3} else 1.0)
            _add_tone(track, start + beat * 0.48, beat * 0.38, note, 0.045, rng.uniform(-0.5, 0.5))
        else:
            if index % 4 == 0:
                _add_soft_hit(track, start, 52.0, 0.16)
            if index % 8 == 6:
                _add_soft_hit(track, start, 96.0, 0.07, pan=-0.25)

    noise = rng.normal(0.0, 1.0, total_samples)
    window = 900
    cumulative = np.cumsum(np.insert(noise, 0, 0.0))
    smooth_noise = (cumulative[window:] - cumulative[:-window]) / window
    smooth_noise = np.pad(smooth_noise, (window // 2, window - (window // 2) - 1), mode="edge")[:total_samples]
    track[:, 0] += smooth_noise * 0.018
    track[:, 1] += np.roll(smooth_noise, 181) * 0.018
    fade = min(int(SAMPLE_RATE * 1.2), total_samples // 4)
    track[:fade] *= np.linspace(0.0, 1.0, fade)[:, None]
    track[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None]
    peak = float(np.max(np.abs(track))) or 1.0
    return np.clip(track * (0.88 / peak), -1.0, 1.0)


def _write_wave(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (audio * 32_767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("assets/music"))
    parser.add_argument("--duration", type=float, default=48.0)
    args = parser.parse_args()

    tracks = (
        ("brain_lens/warm_tension.wav", 88.0, [(220.0, 261.63, 329.63), (196.0, 246.94, 293.66), (174.61, 220.0, 261.63), (196.0, 246.94, 329.63)], "brain_lens", 11),
        ("brain_lens/after_hours_pulse.wav", 96.0, [(207.65, 246.94, 311.13), (185.0, 233.08, 277.18), (164.81, 207.65, 246.94), (185.0, 233.08, 311.13)], "brain_lens", 19),
        ("ancient_history/stone_and_dust.wav", 72.0, [(110.0, 130.81, 164.81), (98.0, 123.47, 146.83), (87.31, 110.0, 130.81), (98.0, 123.47, 164.81)], "ancient_history", 31),
        ("ancient_history/empire_echo.wav", 80.0, [(98.0, 123.47, 146.83), (110.0, 130.81, 164.81), (92.5, 116.54, 146.83), (82.41, 103.83, 130.81)], "ancient_history", 47),
    )
    repeats = max(1, int(np.ceil(args.duration / ((60.0 / min(item[1] for item in tracks)) * 8.0 * 4.0))))
    for relative_path, bpm, chord_cycle, mood, seed in tracks:
        chords = (chord_cycle * repeats)[: max(1, int(np.ceil(args.duration / ((60.0 / bpm) * 8.0))))]
        audio = _build_track(args.duration, bpm, chords, mood, seed)
        _write_wave(args.output / relative_path, audio)


if __name__ == "__main__":
    main()
