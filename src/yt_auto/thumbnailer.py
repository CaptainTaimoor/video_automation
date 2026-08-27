from __future__ import annotations

import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps


class ThumbnailMaker:
    _CHANNEL_BADGES = {
        "ancient_history": "SECRETS OF TIME",
        "brain_lens": "BRAIN LENS",
    }
    _CORE_STOP_WORDS = {
        "a", "an", "and", "about", "behind", "can", "could", "does", "for",
        "from", "how", "in", "is", "it", "of", "on", "reveals", "reveal",
        "says", "secret", "that", "the", "this", "tiny", "to", "what", "when",
        "why", "with", "your", "keep", "keeps", "pulling", "attention", "back",
        "relationship", "moment", "makes", "visible", "watch", "invest",
    }

    # YouTube Shorts portrait format
    def __init__(self, width: int = 1080, height: int = 1920) -> None:
        self.width = width
        self.height = height

    def _font(self, size: int) -> ImageFont.ImageFont:
        # Try the premium Montserrat font we downloaded, then fall back to system fonts
        for candidate in (
            "assets/fonts/Montserrat-Bold.ttf",
            "C:/Windows/Fonts/impact.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
        ):
            try:
                return ImageFont.truetype(str(Path(candidate).resolve()), size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _badge_text(self, channel_id: str | None) -> str:
        return self._CHANNEL_BADGES.get(channel_id or "", "STORY SPOTLIGHT")

    def _core_topic_terms(self, title: str, channel_id: str | None) -> list[str]:
        clean = re.sub(r"\s+", " ", title or "").strip(" .!?:;-")
        lowered = clean.lower()
        if channel_id == "ancient_history":
            known_phrases = (
                "rosetta stone", "göbekli tepe", "gobekli tepe", "cadaver synod",
                "kingdom of kush", "sogdian merchants", "machu picchu", "angkor wat", "tikal",
                "great zimbabwe", "axum obelisks", "axum's giant stelae", "olmec colossal heads",
                "minoan eruption", "silk road", "stonehenge", "terracotta army", "mausoleum of the first qin emperor",
                "late bronze age collapse", "nubian pyramids", "chichen itza", "petra", "pompeii",
                "lachish",
            )
            matched = [phrase for phrase in known_phrases if phrase in lowered]
            if matched:
                clean = matched[0]
            else:
                match = re.search(r"\b(?:about|behind|of)\s+(.+)$", clean, re.IGNORECASE)
                if match:
                    clean = match.group(1).strip(" .!?:;-")
        elif channel_id == "brain_lens":
            if "late repl" in lowered and "attachment anxiety" in lowered:
                clean = "reply"
                matched = ["reply"]
            else:
                matched = []
            known_phrases = (
                "mixed attachment signals", "voice change", "eye contact", "mixed signals", "love bombing",
                "anxious attachment", "avoidant attachment", "texting anxiety",
                "romantic attraction", "body language", "sexual tension",
                "kissing chemistry", "dating anxiety", "decision fatigue",
                "crush idealization", "limerence", "micro flirting",
                "almost relationships", "almost relationship",
                "future faking", "slow fading", "ghosting", "orbiting", "benching",
                "breadcrumbing", "breadcrumb", "push pull", "silent treatment",
                "friends with benefits", "social media jealousy", "fear of abandonment",
                "relationship pacing", "mutual effort", "emotional safety",
            )
            matched = matched or [phrase for phrase in known_phrases if phrase in lowered]
            if matched:
                if matched[0] == "mixed attachment signals":
                    clean = "mixed signals"
                    matched = ["mixed signals"]
                extra = "attraction" if "attraction" in lowered and "attraction" not in matched[0] else ""
                clean = " ".join([matched[0], extra]).strip()

        words = [
            word
            for word in re.findall(r"[A-Za-z0-9']+", clean)
            if word.lower() not in self._CORE_STOP_WORDS
        ]
        return words[:3] if channel_id == "brain_lens" else words[-3:]

    def _ensure_core_topic(self, title: str, display_text: str, channel_id: str | None) -> str:
        core_terms = self._core_topic_terms(title, channel_id)
        if not core_terms:
            return display_text
        displayed = set(re.findall(r"[a-z0-9']+", display_text.lower()))
        required = [term for term in core_terms if len(term) > 2]
        if required and all(term.lower() in displayed for term in required):
            return display_text

        core = " ".join(core_terms).upper()
        lowered = title.lower()
        if channel_id == "ancient_history":
            suffix = "WHAT SURVIVED?" if "surviv" in lowered or "record" in lowered else "THE KEY EVIDENCE"
            return f"{core}: {suffix}"
        if channel_id == "brain_lens":
            if "attraction" in lowered:
                attraction_core = " ".join(term for term in core_terms if term.lower() != "attraction").upper()
                return f"{attraction_core or core}: ATTRACTION?"
            return f"{core}: THE REAL CUE"
        return core

    def thumbnail_copy_issues(
        self,
        title: str,
        channel_id: str | None,
        display_text: str | None = None,
        badge_text: str | None = None,
    ) -> list[str]:
        """Deterministic checks usable by a pre-upload quality gate."""
        display = display_text or self._display_title(title, channel_id)
        badge = badge_text or self._badge_text(channel_id)
        issues: list[str] = []
        expected_badge = self._CHANNEL_BADGES.get(channel_id or "")
        if expected_badge and badge != expected_badge:
            issues.append(f"thumbnail badge must use channel brand '{expected_badge}'")
        display_words = set(re.findall(r"[a-z0-9']+", display.lower()))
        missing = [
            term
            for term in self._core_topic_terms(title, channel_id)
            if len(term) > 2 and term.lower() not in display_words
        ]
        if missing:
            issues.append("thumbnail omits core topic term(s): " + ", ".join(missing))
        word_limit = 4 if channel_id == "brain_lens" else 8
        if len(display.split()) > word_limit:
            issues.append(f"thumbnail copy exceeds {word_limit} words")
        return issues

    def _display_title(self, title: str, channel_id: str | None) -> str:
        cleaned = re.sub(r"\s+", " ", title.replace("â€”", "—").replace("â€™", "'")).strip()
        lowered = cleaned.lower()
        if channel_id == "brain_lens":
            if "late repl" in lowered or "reply time" in lowered:
                return "ONE REPLY PROVES NOTHING"
            core_terms = self._core_topic_terms(cleaned, channel_id)
            core = " ".join(core_terms).upper()
            if "micro flirting" in lowered:
                return "MICRO FLIRTING OR FRIENDLINESS?"
            if "almost relationship" in lowered:
                return "ALMOST RELATIONSHIPS: THE LOOP"
            if "future faking" in lowered:
                return "FUTURE FAKING: THE TRAP"
            if "friends with benefits" in lowered:
                return "FRIENDS WITH BENEFITS?"
            if "mixed" in core.lower() and "signal" in core.lower():
                return "MIXED SIGNALS LOOP"
            if "attraction" in core.lower():
                attraction_core = " ".join(
                    term for term in core_terms if term.lower() != "attraction"
                ).upper()
                return f"{attraction_core or 'ATTRACTION'}: ATTRACTION?"
            if core:
                return core
        if channel_id == "ancient_history":
            hooks = (
                (("carthage",), "WHY CARTHAGE FELL"),
                (("angkor wat",), "ANGKOR WAT: SACRED MOUNTAIN"),
                (("tikal",), "TIKAL: KINGS ABOVE THE JUNGLE"),
                (("chichen itza",), "CHICHEN ITZA: RITUAL POWER"),
                (("great zimbabwe",), "GREAT ZIMBABWE: AFRICAN ORIGINS CONFIRMED"),
                (("axum", "aksum"), "AXUM'S GIANT STELAE: STONE PALACES"),
                (("olmec", "colossal head"), "OLMEC COLOSSAL HEADS: BASALT RULERS"),
                (("indus", "mohenjo", "harappa"), "THE INDUS MYSTERY"),
                (("justinian", "plague"), "ROME'S DEADLIEST PLAGUE"),
                (("bronze age",), "WHY AN AGE COLLAPSED"),
                (("delphi", "pythia"), "THE ORACLE'S REAL POWER"),
                (("hammurabi",), "THE LAW BEHIND THE STONE"),
                (("qin shi huang", "terracotta"), "THE EMPEROR'S BURIED ARMY"),
                (("machu picchu",), "MACHU PICCHU'S SECRET"),
                (("stonehenge",), "STONEHENGE: WHAT SURVIVED?"),
                (("nazca", "nasca"), "WHO MADE THE NAZCA LINES?"),
                (("greek fire",), "BYZANTIUM'S SECRET WEAPON"),
                (("teutoburg",), "ROME'S LOST LEGIONS"),
                (("thermopylae",), "THERMOPYLAE BEYOND THE LEGEND"),
                (("petra",), "HOW PETRA BEAT THE DESERT"),
                (("antikythera",), "THE ANCIENT COMPUTER"),
                (("cyrus cylinder",), "THE CYRUS CYLINDER MYTH"),
                (("persepolis",), "PERSIA BUILT IN STONE"),
                (("rosetta stone",), "ROSETTA STONE: HIEROGLYPHS UNLOCKED"),
                (("göbekli tepe", "gobekli tepe"), "GÖBEKLI TEPE: OLDER THAN STONEHENGE"),
                (("cadaver synod",), "CADAVER SYNOD: DEAD POPE ON TRIAL"),
                (("kingdom of kush",), "KINGDOM OF KUSH: RULED EGYPT"),
                (("lachish",), "LACHISH: ASSYRIA'S SIEGE MACHINE"),
                (("nubian pyramid",), "NUBIAN PYRAMIDS: KUSH IN STONE"),
                (("sogdian merchant",), "SOGDIAN MERCHANTS: SILK ROAD NETWORK"),
                (("petra",), "PETRA: TRADE IN STONE"),
            )
            for markers, hook in hooks:
                if any(marker in lowered for marker in markers):
                    return self._ensure_core_topic(cleaned, hook, channel_id)
        short_clause = re.split(r"[:—|]", cleaned, maxsplit=1)[0].strip()
        words = short_clause.split()
        if len(words) > 7:
            words = words[:7]
        return self._ensure_core_topic(cleaned, " ".join(words).upper(), channel_id)

    def _wrap_title(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        lines: list[str] = []
        current = ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if current and bbox[2] - bbox[0] > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    def _fit_title(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        landscape: bool,
        max_width: int,
        max_height: int,
    ) -> tuple[ImageFont.ImageFont, list[str], int]:
        start_size = 104 if landscape else 138
        minimum_size = 62 if landscape else 78
        max_lines = 3 if landscape else 4
        for size in range(start_size, minimum_size - 1, -4):
            font = self._font(size)
            lines = self._wrap_title(draw, text, font, max_width)
            line_height = size + (10 if landscape else 16)
            if len(lines) <= max_lines and len(lines) * line_height <= max_height:
                return font, lines, line_height
        font = self._font(minimum_size)
        lines = self._wrap_title(draw, text, font, max_width)[:max_lines]
        return font, lines, minimum_size + (10 if landscape else 16)

    def generate(
        self,
        image_path: Path,
        title: str,
        out_path: Path,
        channel_id: str | None = None,
        logo_path: Path | None = None,
    ) -> None:
        w, h = self.width, self.height

        # Full-bleed background image, cropped to portrait.
        # If the lead asset is a video clip, grab a frame instead of failing.
        if image_path.suffix.lower() == '.mp4':
            from moviepy.editor import VideoFileClip
            with VideoFileClip(str(image_path)) as clip:
                frame = clip.get_frame(min(0.4, max(0.0, (clip.duration or 1.0) * 0.12)))
            image = Image.fromarray(frame).convert('RGB')
        else:
            image = Image.open(image_path).convert("RGB")
        image = ImageOps.fit(image, (w, h), method=Image.Resampling.LANCZOS)
        # Subtle sharpening for vibrancy
        image = image.filter(ImageFilter.SHARPEN)

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        gradient_top = int(h * 0.24)
        for y in range(gradient_top, h):
            ratio = (y - gradient_top) / max(1, h - gradient_top)
            alpha = int(ratio ** 1.18 * 225)
            draw.line((0, y, w, y), fill=(0, 0, 0, min(225, alpha)))

        if w > h:
            for x in range(int(w * 0.8)):
                ratio = 1.0 - x / max(1, int(w * 0.8))
                draw.line((x, 0, x, h), fill=(0, 0, 0, int(ratio ** 1.7 * 135)))

        if w <= h:
            for y in range(int(h * 0.25)):
                ratio = 1.0 - (y / max(1, int(h * 0.25)))
                draw.line((0, y, w, y), fill=(0, 0, 0, int(ratio ** 2 * 120)))

        merged = Image.alpha_composite(image.convert("RGBA"), overlay)
        draw2 = ImageDraw.Draw(merged)

        accent = (220, 166, 82, 255) if channel_id == "ancient_history" else (82, 202, 255, 255)
        badge_font = self._font(27 if w > h else 38)
        badge_text = self._badge_text(channel_id)
        badge_box = draw2.textbbox((0, 0), badge_text, font=badge_font)
        badge_width = badge_box[2] - badge_box[0]
        badge_height = badge_box[3] - badge_box[1]
        draw2.rounded_rectangle(
            (48, 38, 48 + badge_width + 34, 38 + badge_height + 24),
            radius=14,
            fill=(7, 10, 14, 205),
            outline=accent,
            width=2,
        )
        draw2.text((65, 48), badge_text, font=badge_font, fill=(248, 244, 230, 255))
        if logo_path and logo_path.exists():
            try:
                logo_size = 120 if w > h else 150
                logo = Image.open(logo_path).convert("RGBA")
                logo.thumbnail((logo_size, logo_size), Image.Resampling.LANCZOS)
                logo_canvas = Image.new("RGBA", (logo_size, logo_size), (0, 0, 0, 0))
                logo_canvas.alpha_composite(logo, ((logo_size - logo.width) // 2, (logo_size - logo.height) // 2))
                merged.alpha_composite(logo_canvas, (w - logo_size - 54, 42))
            except Exception:
                pass

        title_text = self._display_title(title, channel_id)
        side_padding = 110 if w > h else 84
        title_font, lines, line_height = self._fit_title(
            draw2,
            title_text,
            w > h,
            w - side_padding,
            int(h * (0.52 if w > h else 0.46)),
        )
        total_text_h = len(lines) * line_height
        text_top = h - total_text_h - (64 if w > h else 110)
        draw2.rounded_rectangle(
            (48, text_top - 34, min(w - 48, 48 + int(w * 0.22)), text_top - 22),
            radius=5,
            fill=accent,
        )

        for i, line in enumerate(lines):
            ly = text_top + i * line_height
            draw2.text(
                (48, ly),
                line,
                font=title_font,
                fill=(255, 248, 226, 255),
                stroke_width=4 if w > h else 5,
                stroke_fill=(0, 0, 0, 220),
            )

        merged.convert("RGB").save(out_path, format="JPEG", quality=95)

