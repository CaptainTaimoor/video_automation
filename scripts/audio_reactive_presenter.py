from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import cv2
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sadtalker-root", required=True)
    parser.add_argument("--landmark-cache", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--rhubarb", default="")
    parser.add_argument("--dialog-text", default="")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--fps", type=int, default=25)
    return parser.parse_args()


def square_portrait(image: np.ndarray, size: int) -> np.ndarray:
    height, width = image.shape[:2]
    crop_size = min(width, height)
    left = max(0, (width - crop_size) // 2)
    top = max(0, int((height - crop_size) * 0.34))
    top = min(top, height - crop_size)
    crop = image[top : top + crop_size, left : left + crop_size]
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_LANCZOS4)


def heuristic_landmarks(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    faces = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=5, minSize=(120, 120))
    if len(faces):
        face_x, face_y, face_w, face_h = max(faces, key=lambda item: item[2] * item[3])
    else:
        size = image.shape[0]
        face_x, face_y, face_w, face_h = int(size * 0.20), int(size * 0.12), int(size * 0.60), int(size * 0.66)
    landmarks = np.zeros((68, 2), dtype=np.float32)
    mouth_center_x = face_x + face_w * 0.50
    mouth_center_y = face_y + face_h * 0.73
    mouth_radius_x = face_w * 0.20
    mouth_radius_y = face_h * 0.055
    for offset in range(12):
        angle = math.pi + (2 * math.pi * offset / 12)
        landmarks[48 + offset] = (
            mouth_center_x + math.cos(angle) * mouth_radius_x,
            mouth_center_y + math.sin(angle) * mouth_radius_y,
        )
    for offset in range(8):
        angle = math.pi + (2 * math.pi * offset / 8)
        landmarks[60 + offset] = (
            mouth_center_x + math.cos(angle) * mouth_radius_x * 0.56,
            mouth_center_y + math.sin(angle) * mouth_radius_y * 0.54,
        )
    landmarks[36:42] = np.array([
        [face_x + face_w * 0.27, face_y + face_h * 0.39],
        [face_x + face_w * 0.32, face_y + face_h * 0.36],
        [face_x + face_w * 0.39, face_y + face_h * 0.37],
        [face_x + face_w * 0.42, face_y + face_h * 0.40],
        [face_x + face_w * 0.37, face_y + face_h * 0.42],
        [face_x + face_w * 0.31, face_y + face_h * 0.42],
    ])
    landmarks[42:48] = np.array([
        [face_x + face_w * 0.58, face_y + face_h * 0.40],
        [face_x + face_w * 0.61, face_y + face_h * 0.37],
        [face_x + face_w * 0.68, face_y + face_h * 0.36],
        [face_x + face_w * 0.73, face_y + face_h * 0.39],
        [face_x + face_w * 0.69, face_y + face_h * 0.42],
        [face_x + face_w * 0.63, face_y + face_h * 0.42],
    ])
    return landmarks


def detect_landmarks(image: np.ndarray, source_path: Path, cache_path: Path, sadtalker_root: Path) -> np.ndarray:
    fingerprint = {
        "source": str(source_path.resolve()),
        "size": source_path.stat().st_size,
        "mtime_ns": source_path.stat().st_mtime_ns,
        "frame_size": image.shape[0],
    }
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint:
                points = np.asarray(cached.get("landmarks"), dtype=np.float32)
                if points.shape == (68, 2):
                    return points
        except Exception:
            pass
    landmarks = None
    try:
        sys.path.insert(0, str(sadtalker_root))
        from src.utils.croper import Preprocesser

        detector = Preprocesser(device="cpu")
        landmarks = detector.get_landmark(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    except Exception:
        landmarks = None
    if landmarks is None or np.asarray(landmarks).shape != (68, 2):
        landmarks = heuristic_landmarks(image)
    landmarks = np.asarray(landmarks, dtype=np.float32)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"fingerprint": fingerprint, "landmarks": landmarks.tolist()}, indent=2),
        encoding="utf-8",
    )
    return landmarks


def read_audio_envelope(audio_path: Path, fps: int) -> tuple[np.ndarray, float]:
    with wave.open(str(audio_path), "rb") as audio_file:
        sample_rate = audio_file.getframerate()
        channels = audio_file.getnchannels()
        sample_width = audio_file.getsampwidth()
        samples = np.frombuffer(audio_file.readframes(audio_file.getnframes()), dtype=np.int16)
    if sample_width != 2:
        raise ValueError("Presenter audio must be 16-bit PCM WAV")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    samples_float = samples.astype(np.float32) / 32768.0
    frame_count = max(1, int(math.ceil(len(samples_float) / sample_rate * fps)))
    window = max(1, int(sample_rate / fps))
    envelope = np.zeros(frame_count, dtype=np.float32)
    articulation = np.zeros(frame_count, dtype=np.float32)
    for frame_index in range(frame_count):
        start = frame_index * window
        chunk = samples_float[start : start + window]
        if chunk.size:
            envelope[frame_index] = float(np.sqrt(np.mean(chunk * chunk)))
            articulation[frame_index] = float(np.mean(np.abs(np.diff(chunk)))) if chunk.size > 1 else 0.0
    reference = float(np.percentile(envelope[envelope > 0], 88)) if np.any(envelope > 0) else 1.0
    envelope = np.clip(envelope / max(reference, 1e-4), 0.0, 1.0)
    if np.any(articulation > 0):
        articulation /= max(float(np.percentile(articulation, 90)), 1e-5)
    articulation = np.clip(articulation, 0.0, 1.0)
    combined = np.clip(envelope * 0.82 + articulation * 0.18, 0.0, 1.0)
    smoothed = np.zeros_like(combined)
    for frame_index, value in enumerate(combined):
        previous = smoothed[frame_index - 1] if frame_index else 0.0
        rate = 0.62 if value > previous else 0.30
        smoothed[frame_index] = previous + (value - previous) * rate
    return smoothed, len(samples_float) / sample_rate


def read_mouth_cues(
    audio_path: Path,
    rhubarb_path: Path,
    dialog_text: str,
    output_dir: Path,
) -> list[dict]:
    if not rhubarb_path.exists() or not dialog_text.strip():
        return []
    dialog_path = output_dir / "presenter_dialog.txt"
    cues_path = output_dir / "presenter_mouth_cues.json"
    dialog_path.write_text(dialog_text.strip(), encoding="utf-8")
    command = [
        str(rhubarb_path),
        "-q",
        "-r",
        "pocketSphinx",
        "-f",
        "json",
        "-d",
        str(dialog_path),
        "-o",
        str(cues_path),
        str(audio_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        payload = json.loads(cues_path.read_text(encoding="utf-8"))
        cues = list(payload.get("mouthCues") or [])
        return [
            cue
            for cue in cues
            if str(cue.get("value") or "") in {"A", "B", "C", "D", "E", "F", "G", "H", "X"}
        ]
    except Exception:
        return []


def local_warp(frame: np.ndarray, center: tuple[float, float], radius: tuple[float, float], amount: float, mode: str) -> np.ndarray:
    height, width = frame.shape[:2]
    center_x, center_y = center
    radius_x, radius_y = max(radius[0], 1.0), max(radius[1], 1.0)
    left = max(0, int(center_x - radius_x))
    right = min(width, int(center_x + radius_x) + 1)
    top = max(0, int(center_y - radius_y))
    bottom = min(height, int(center_y + radius_y) + 1)
    if right - left < 4 or bottom - top < 4:
        return frame
    grid_x, grid_y = np.meshgrid(np.arange(left, right, dtype=np.float32), np.arange(top, bottom, dtype=np.float32))
    normalized_x = (grid_x - center_x) / radius_x
    normalized_y = (grid_y - center_y) / radius_y
    distance = normalized_x * normalized_x + normalized_y * normalized_y
    weight = np.clip(1.0 - distance, 0.0, 1.0) ** 2
    map_x = grid_x.astype(np.float32)
    map_y = grid_y.astype(np.float32)
    if mode == "open":
        direction = np.where(normalized_y < 0, -1.0, 1.0)
        map_y = grid_y - direction * amount * weight
    elif mode == "close":
        map_y = grid_y + normalized_y * amount * weight
    elif mode == "width":
        map_x = grid_x - normalized_x * amount * weight
    map_x = map_x.astype(np.float32)
    map_y = map_y.astype(np.float32)
    warped = cv2.remap(frame, map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)
    mask = cv2.GaussianBlur((weight * 255).astype(np.uint8), (0, 0), 3.0).astype(np.float32) / 255.0
    mask = mask[:, :, None]
    output = frame.copy()
    region = output[top:bottom, left:right].astype(np.float32)
    output[top:bottom, left:right] = np.clip(region * (1.0 - mask) + warped.astype(np.float32) * mask, 0, 255).astype(np.uint8)
    return output


def add_mouth_interior(
    frame: np.ndarray,
    center: tuple[float, float],
    mouth_width: float,
    openness: float,
    width_scale: float,
) -> np.ndarray:
    if openness < 0.10:
        return frame
    height, width = frame.shape[:2]
    center_x, center_y = int(center[0]), int(center[1] + mouth_width * 0.006)
    gap_width = max(5, int(mouth_width * (0.37 + openness * 0.09) * width_scale))
    gap_height = max(1, int(mouth_width * (0.008 + openness * 0.078)))
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.ellipse(mask, (center_x, center_y), (gap_width // 2, gap_height // 2), 0, 0, 360, 255, -1, cv2.LINE_AA)
    mask = cv2.GaussianBlur(mask, (0, 0), 1.2).astype(np.float32)[:, :, None] / 255.0
    interior = np.empty_like(frame)
    interior[:, :] = (37, 20, 31)
    return np.clip(
        frame.astype(np.float32) * (1.0 - mask * 0.76) + interior.astype(np.float32) * mask * 0.76,
        0,
        255,
    ).astype(np.uint8)


def animate_frame(
    base: np.ndarray,
    landmarks: np.ndarray,
    openness: float,
    width_scale: float,
    frame_index: int,
    fps: int,
) -> np.ndarray:
    mouth = landmarks[48:68]
    mouth_center = tuple(np.mean(mouth, axis=0))
    mouth_width = float(np.ptp(mouth[:, 0]))
    mouth_height = max(float(np.ptp(mouth[:, 1])), mouth_width * 0.12)
    mouth_amount = (-0.35 + openness * 6.6) * max(0.75, mouth_width / 95.0)
    frame = local_warp(
        base,
        mouth_center,
        (mouth_width * 0.72, mouth_height * 1.55),
        mouth_amount,
        "open",
    )
    width_amount = (width_scale - 1.0) * mouth_width * 0.52
    if abs(width_amount) > 0.15:
        frame = local_warp(
            frame,
            mouth_center,
            (mouth_width * 0.78, mouth_height * 1.75),
            width_amount,
            "width",
        )
    frame = add_mouth_interior(frame, mouth_center, mouth_width, openness, width_scale)
    time_seconds = frame_index / fps
    blink_phase = min(
        abs(time_seconds - 1.82),
        abs(time_seconds - 4.55),
    )
    blink_strength = max(0.0, 1.0 - blink_phase / 0.085)
    if blink_strength > 0:
        for eye in (landmarks[36:42], landmarks[42:48]):
            eye_center = tuple(np.mean(eye, axis=0))
            eye_width = max(float(np.ptp(eye[:, 0])), 10.0)
            eye_height = max(float(np.ptp(eye[:, 1])), eye_width * 0.16)
            frame = local_warp(
                frame,
                eye_center,
                (eye_width * 0.70, eye_height * 1.9),
                eye_height * 1.4 * blink_strength,
                "close",
            )
    height, width = frame.shape[:2]
    scale = 1.012 + 0.006 * math.sin(time_seconds * 1.15)
    shift_x = 1.5 * math.sin(time_seconds * 0.72)
    shift_y = 1.0 * math.sin(time_seconds * 0.93 + 0.8)
    transform = cv2.getRotationMatrix2D((width / 2, height / 2), 0.22 * math.sin(time_seconds * 0.55), scale)
    transform[0, 2] += shift_x
    transform[1, 2] += shift_y
    return cv2.warpAffine(frame, transform, (width, height), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT_101)


def render(args: argparse.Namespace) -> None:
    source_path = Path(args.source_image).resolve()
    audio_path = Path(args.audio).resolve()
    output_path = Path(args.output).resolve()
    sadtalker_root = Path(args.sadtalker_root).resolve()
    cache_path = Path(args.landmark_cache).resolve()
    ffmpeg_path = Path(args.ffmpeg).resolve()
    rhubarb_path = Path(args.rhubarb).resolve() if args.rhubarb else Path("__rhubarb_unavailable__")
    source = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
    if source is None:
        raise FileNotFoundError(source_path)
    base = square_portrait(source, args.size)
    landmarks = detect_landmarks(base, source_path, cache_path, sadtalker_root)
    envelope, duration = read_audio_envelope(audio_path, args.fps)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mouth_cues = read_mouth_cues(
        audio_path,
        rhubarb_path,
        args.dialog_text,
        output_path.parent,
    )
    silent_path = output_path.with_name(f"{output_path.stem}_silent.mp4")
    writer = cv2.VideoWriter(
        str(silent_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        args.fps,
        (args.size, args.size),
    )
    if not writer.isOpened():
        raise RuntimeError("Could not initialize presenter video writer")
    shape_targets = {
        "A": (0.03, 0.92),
        "B": (0.13, 1.05),
        "C": (0.31, 1.16),
        "D": (0.67, 1.02),
        "E": (0.57, 0.79),
        "F": (0.39, 0.70),
        "G": (0.16, 0.96),
        "H": (0.34, 1.01),
        "X": (0.00, 0.96),
    }
    cue_index = 0
    current_openness = 0.0
    current_width_scale = 0.96
    try:
        for frame_index, envelope_value in enumerate(envelope):
            frame_time = frame_index / args.fps
            if mouth_cues:
                while cue_index + 1 < len(mouth_cues) and frame_time >= float(mouth_cues[cue_index]["end"]):
                    cue_index += 1
                cue = mouth_cues[cue_index]
                shape = str(cue.get("value") or "X")
                target_openness, target_width_scale = shape_targets.get(shape, shape_targets["X"])
                if float(envelope_value) < 0.025:
                    target_openness = 0.0
                else:
                    target_openness *= 0.82 + float(envelope_value) * 0.18
            else:
                target_openness = float(envelope_value) * 0.74
                target_width_scale = 1.0

            openness_rate = 0.70 if target_openness > current_openness else 0.54
            current_openness += (target_openness - current_openness) * openness_rate
            current_width_scale += (target_width_scale - current_width_scale) * 0.58
            writer.write(
                animate_frame(
                    base,
                    landmarks,
                    current_openness,
                    current_width_scale,
                    frame_index,
                    args.fps,
                )
            )
    finally:
        writer.release()
    command = [
        str(ffmpeg_path),
        "-y",
        "-i",
        str(silent_path),
        "-i",
        str(audio_path),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "19",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    silent_path.unlink(missing_ok=True)
    if not output_path.exists() or output_path.stat().st_size < 100_000:
        raise RuntimeError("Presenter renderer did not produce a usable video")


if __name__ == "__main__":
    render(parse_args())
