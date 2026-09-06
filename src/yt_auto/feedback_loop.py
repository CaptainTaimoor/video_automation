from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from yt_auto.utils import ensure_dir, read_json, write_json


class FeedbackLoop:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = ensure_dir(state_dir)
        self.state_file = self.state_dir / "feedback_state.json"
        self.experiment_file = self.state_dir / "experiments.jsonl"

    def load(self) -> Dict:
        return read_json(self.state_file, {"channels": {}, "videos": {}})

    def save(self, state: Dict) -> None:
        write_json(self.state_file, state)

    def _bucket(self, state: Dict, channel_id: str) -> Dict:
        channels = state.setdefault("channels", {})
        c = channels.setdefault(channel_id, {})
        c.setdefault("styles", {})
        c.setdefault("patterns", {})
        c.setdefault("terms", {})
        return c

    def _blend(self, old: float, incoming: float, alpha: float = 0.33) -> float:
        return round((old * (1 - alpha)) + (incoming * alpha), 4)

    def _update_metric(self, bucket: Dict, key: str, reward: float) -> None:
        item = bucket.setdefault(key, {"score": 1.0, "n": 0})
        item["score"] = self._blend(float(item.get("score", 1.0)), reward)
        item["n"] = int(item.get("n", 0)) + 1

    def record_build(self, payload: Dict) -> None:
        with self.experiment_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def apply_video_metrics(
        self,
        channel_id: str,
        style: str,
        pattern_id: str,
        terms: List[str],
        metrics: Dict,
        min_views: int,
    ) -> Dict:
        views = int(metrics.get("views", 0))
        likes = int(metrics.get("likes", 0))
        comments = int(metrics.get("comments", 0))

        if views < min_views:
            return {"applied": False, "reason": "insufficient_views", "views": views}

        like_rate = likes / max(views, 1)
        comment_rate = comments / max(views, 1)
        reward = max(0.35, min(2.5, 1.0 + (like_rate * 7.0) + (comment_rate * 6.0)))

        state = self.load()
        c = self._bucket(state, channel_id)
        self._update_metric(c["styles"], style, reward)
        self._update_metric(c["patterns"], pattern_id, reward)
        for term in terms[:6]:
            self._update_metric(c["terms"], term, reward)
        self.save(state)

        return {
            "applied": True,
            "views": views,
            "likes": likes,
            "comments": comments,
            "reward": round(reward, 4),
        }

    def style_bias(self, channel_id: str, styles: List[str]) -> Dict[str, float]:
        state = self.load()
        c = state.get("channels", {}).get(channel_id, {})
        raw = c.get("styles", {})
        return {style: float(raw.get(style, {}).get("score", 1.0)) for style in styles}

    def term_bias(self, channel_id: str) -> Dict[str, float]:
        state = self.load()
        c = state.get("channels", {}).get(channel_id, {})
        terms = c.get("terms", {})
        return {k: float(v.get("score", 1.0)) for k, v in terms.items()}

    def report(self, channel_id: str) -> Dict:
        state = self.load()
        c = state.get("channels", {}).get(channel_id, {})
        styles = c.get("styles", {})
        patterns = c.get("patterns", {})
        terms = c.get("terms", {})

        top_styles = sorted(styles.items(), key=lambda x: x[1].get("score", 1.0), reverse=True)[:5]
        top_patterns = sorted(patterns.items(), key=lambda x: x[1].get("score", 1.0), reverse=True)[:8]
        top_terms = sorted(terms.items(), key=lambda x: x[1].get("score", 1.0), reverse=True)[:12]

        return {
            "channel": channel_id,
            "top_styles": top_styles,
            "top_patterns": top_patterns,
            "top_terms": top_terms,
        }
