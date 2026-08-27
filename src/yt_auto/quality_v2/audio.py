from __future__ import annotations

"""Narration pacing and FFmpeg mix policy for the free-only pipeline."""

from dataclasses import dataclass
import re
from typing import Sequence


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
