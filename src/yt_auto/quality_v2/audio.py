from __future__ import annotations

"""Narration pacing and FFmpeg mix policy for the free-only pipeline."""

from array import array
from dataclasses import dataclass
import math
from pathlib import Path
import re
import sys
from typing import Sequence
import wave


_WORDS = re.compile(r"[A-Za-z0-9']+")


@dataclass(frozen=True)
class NarrationChunk:
    text: str
    pause_after_seconds: float


@dataclass(frozen=True)
class PaceReport:
    acceptable: bool
    words_per_minute: float
    pause_ratio: float
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class PcmWavMetrics:
    """Lightweight narration measurements from an uncompressed PCM WAV.

    ``dynamic_range_db`` is the 95th-to-10th percentile spread of active
    20 ms frame RMS levels.  It is deliberately an inexpensive dynamics proxy,
    not an EBU R128 loudness-range measurement.
    """

    duration_seconds: float
    active_speech_seconds: float
    silence_seconds: float
    silence_ratio: float
    active_speech_wpm: float
    rms_dbfs: float
    active_rms_dbfs: float
    peak_dbfs: float
    dynamic_range_db: float
    crest_factor_db: float
    silent_region_count: int
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class MixPolicy:
    dialogue_lufs: float = -18.0
    final_lufs: float = -16.5
    true_peak: float = -1.0
    music_gain: float = 0.08
    sidechain_ratio: float = 8.0
    sidechain_threshold: float = 0.03
    attack_ms: int = 20
    release_ms: int = 420


def chunk_narration(text: str, max_words: int = 54) -> list[NarrationChunk]:
    """Make small semantic TTS chunks with punctuation-aware breathing room."""
    source = re.sub(r"\s+", " ", str(text or "")).strip()
    if not source:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", source)
    chunks: list[NarrationChunk] = []
    buffer: list[str] = []
    count = 0
    for sentence in sentences:
        sentence = sentence.strip()
        words = _WORDS.findall(sentence)
        if not words:
            continue
        if buffer and count + len(words) > max_words:
            joined = " ".join(buffer)
            chunks.append(NarrationChunk(joined, _pause_for(joined)))
            buffer, count = [], 0
        buffer.append(sentence)
        count += len(words)
    if buffer:
        joined = " ".join(buffer)
        chunks.append(NarrationChunk(joined, _pause_for(joined)))
    return chunks


def _pause_for(text: str) -> float:
    text = text.rstrip()
    if text.endswith(("?", "!")):
        return 0.34
    if text.endswith((":", ";")):
        return 0.30
    return 0.24


def review_pacing(word_count: int, duration_seconds: float, pause_seconds: float = 0.0) -> PaceReport:
    duration = max(0.001, float(duration_seconds))
    spoken = max(0.001, duration - max(0.0, float(pause_seconds)))
    wpm = float(word_count) * 60.0 / spoken
    pause_ratio = max(0.0, float(pause_seconds)) / duration
    issues: list[str] = []
    if wpm < 115:
        issues.append("narration is too slow")
    if wpm > 165:
        issues.append("narration is too fast")
    if pause_ratio < 0.025:
        issues.append("narration lacks natural pauses")
    if pause_ratio > 0.30:
        issues.append("narration has excessive silence")
    return PaceReport(not issues, round(wpm, 2), round(pause_ratio, 4), tuple(issues))


def _dbfs_from_mean_square(mean_square: float, floor_db: float = -120.0) -> float:
    if mean_square <= 0:
        return float(floor_db)
    return max(float(floor_db), 10.0 * math.log10(mean_square))


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(fraction))) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * weight)


def _pcm_energy(raw: bytes, sample_width: int) -> tuple[float, int, float]:
    """Return normalized squared energy, sample count, and normalized peak."""

    if not raw:
        return 0.0, 0, 0.0

    if sample_width == 1:
        count = len(raw)
        values = (sample - 128 for sample in raw)
        scale = 128.0
    elif sample_width == 2:
        decoded = array("h")
        decoded.frombytes(raw)
        if sys.byteorder != "little":
            decoded.byteswap()
        count = len(decoded)
        values = decoded
        scale = 32768.0
    elif sample_width == 3:
        count = len(raw) // 3

        def pcm24_values():
            for index in range(0, count * 3, 3):
                value = raw[index] | (raw[index + 1] << 8) | (raw[index + 2] << 16)
                yield value - 0x1000000 if value & 0x800000 else value

        values = pcm24_values()
        scale = 8388608.0
    elif sample_width == 4:
        decoded = array("i")
        decoded.frombytes(raw)
        if sys.byteorder != "little":
            decoded.byteswap()
        count = len(decoded)
        values = decoded
        scale = 2147483648.0
    else:
        raise ValueError(f"unsupported PCM WAV sample width: {sample_width} bytes")

    squared = 0.0
    peak = 0.0
    for value in values:
        normalized = float(value) / scale
        squared += normalized * normalized
        peak = max(peak, abs(normalized))
    return squared, count, peak


def analyze_pcm_wav(
    path: str | Path,
    *,
    word_count: int = 0,
    silence_threshold_dbfs: float = -45.0,
    minimum_silence_seconds: float = 0.08,
    frame_ms: float = 20.0,
) -> PcmWavMetrics:
    """Measure active delivery, silence, and dynamics without ML dependencies.

    Frames below ``silence_threshold_dbfs`` count as silence only when they are
    part of a contiguous run at least ``minimum_silence_seconds`` long.  Short
    zero crossings and stop consonants therefore remain part of active speech.
    """

    wav_path = Path(path)
    if int(word_count) < 0:
        raise ValueError("word_count cannot be negative")
    if float(minimum_silence_seconds) < 0:
        raise ValueError("minimum_silence_seconds cannot be negative")
    if float(frame_ms) <= 0:
        raise ValueError("frame_ms must be positive")

    records: list[tuple[float, float, int, float, float]] = []
    total_frames = 0
    with wave.open(str(wav_path), "rb") as source:
        if source.getcomptype() != "NONE":
            raise ValueError("analyze_pcm_wav requires an uncompressed PCM WAV")
        sample_rate = int(source.getframerate())
        channels = int(source.getnchannels())
        sample_width = int(source.getsampwidth())
        if sample_rate <= 0 or channels <= 0:
            raise ValueError("PCM WAV has invalid stream metadata")
        frames_per_window = max(1, int(round(sample_rate * float(frame_ms) / 1000.0)))
        while True:
            raw = source.readframes(frames_per_window)
            if not raw:
                break
            squared, sample_count, peak = _pcm_energy(raw, sample_width)
            if sample_count <= 0:
                continue
            frame_count = sample_count // channels
            if frame_count <= 0:
                continue
            total_frames += frame_count
            duration = frame_count / float(sample_rate)
            level_dbfs = _dbfs_from_mean_square(squared / sample_count)
            records.append((duration, squared, sample_count, peak, level_dbfs))

    if not records or total_frames <= 0:
        raise ValueError("PCM WAV contains no audio samples")

    qualifying_silence = [False] * len(records)
    silent_region_count = 0
    index = 0
    while index < len(records):
        if records[index][4] > float(silence_threshold_dbfs):
            index += 1
            continue
        end = index
        run_seconds = 0.0
        while end < len(records) and records[end][4] <= float(silence_threshold_dbfs):
            run_seconds += records[end][0]
            end += 1
        if run_seconds + 1e-9 >= float(minimum_silence_seconds):
            silent_region_count += 1
            for silent_index in range(index, end):
                qualifying_silence[silent_index] = True
        index = end

    duration_seconds = total_frames / float(sample_rate)
    silence_seconds = sum(
        record[0]
        for record, is_silence in zip(records, qualifying_silence)
        if is_silence
    )
    active_speech_seconds = max(0.0, duration_seconds - silence_seconds)

    total_squared = sum(record[1] for record in records)
    total_samples = sum(record[2] for record in records)
    active_squared = sum(
        record[1]
        for record, is_silence in zip(records, qualifying_silence)
        if not is_silence
    )
    active_samples = sum(
        record[2]
        for record, is_silence in zip(records, qualifying_silence)
        if not is_silence
    )
    peak = max(record[3] for record in records)
    active_levels = [
        record[4]
        for record in records
        if record[4] > float(silence_threshold_dbfs)
    ]

    rms_dbfs = _dbfs_from_mean_square(total_squared / max(1, total_samples))
    active_rms_dbfs = _dbfs_from_mean_square(active_squared / max(1, active_samples))
    peak_dbfs = max(-120.0, 20.0 * math.log10(peak)) if peak > 0 else -120.0
    dynamic_range_db = max(
        0.0,
        _percentile(active_levels, 0.95) - _percentile(active_levels, 0.10),
    )
    active_speech_wpm = (
        float(word_count) * 60.0 / active_speech_seconds
        if int(word_count) > 0 and active_speech_seconds > 0
        else 0.0
    )

    return PcmWavMetrics(
        duration_seconds=round(duration_seconds, 4),
        active_speech_seconds=round(active_speech_seconds, 4),
        silence_seconds=round(silence_seconds, 4),
        silence_ratio=round(silence_seconds / duration_seconds, 4),
        active_speech_wpm=round(active_speech_wpm, 2),
        rms_dbfs=round(rms_dbfs, 2),
        active_rms_dbfs=round(active_rms_dbfs, 2),
        peak_dbfs=round(peak_dbfs, 2),
        dynamic_range_db=round(dynamic_range_db, 2),
        crest_factor_db=round(max(0.0, peak_dbfs - active_rms_dbfs), 2),
        silent_region_count=silent_region_count,
        sample_rate=sample_rate,
        channels=channels,
    )


def bounded_atempo(current_seconds: float, target_seconds: float) -> float | None:
    """Return a safe FFmpeg atempo factor, otherwise require re-synthesis."""
    if current_seconds <= 0 or target_seconds <= 0:
        return None
    factor = float(current_seconds) / float(target_seconds)
    return round(factor, 4) if 0.95 <= factor <= 1.05 else None


def should_resynthesize(report: PaceReport, critical_transcript_match: bool) -> bool:
    return not critical_transcript_match or not report.acceptable


def ffmpeg_audio_filter(policy: MixPolicy = MixPolicy()) -> str:
    """Mix voice and music with dialogue-led ducking and final loudness control.

    Inputs are labelled [0:a] (narration) and [1:a] (music); output is [aout].
    """
    return (
        f"[0:a]loudnorm=I={policy.dialogue_lufs}:TP={policy.true_peak}:LRA=7[voice];"
        f"[1:a]volume={policy.music_gain}[bed];"
        f"[bed][voice]sidechaincompress=threshold={policy.sidechain_threshold}:ratio={policy.sidechain_ratio}:"
        f"attack={policy.attack_ms}:release={policy.release_ms}[ducked];"
        f"[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0,"
        f"loudnorm=I={policy.final_lufs}:TP={policy.true_peak}:LRA=7[aout]"
    )
