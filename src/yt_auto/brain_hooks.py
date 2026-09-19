"""Brain Lens line sources, and the one rule set that judges them.

Copies of these rules used to sit in both the script writer and the quality
gate. Two copies drift: a hand-written opener could satisfy the composer and
then be thrown out by the gate for a rule the composer never applied. Every
decision about whether a Brain Lens line is good enough now reads from here,
so the gate and the composer cannot disagree.
"""

from __future__ import annotations

import re

# Openers. Each one puts a visible action in the first sentence -- something a
# camera could film -- rather than opening on an abstraction.
OPENERS: tuple[str, ...] = (
    "They text one dry word, and suddenly you want them more.",
    "You felt the chemistry, then one silence made your stomach drop.",
    "You stare at their name, pretending you are not waiting.",
    "They pull back for one day, and your brain turns it into a chase.",
    "You laugh in front of them, then replay every tiny glance later.",
    "You say you are over it, but your thumb still checks their story.",
    "One almost-kiss can feel louder than a whole conversation.",
    "You call it chemistry, but your nervous system may be reading uncertainty.",
    "They give mixed signals, and your brain mistakes the stress for sparks.",
    "You want to look calm, but your body already exposed the crush.",
    "You delete the message, rewrite it, then make it sound less interested.",
    "The person who feels addictive is not always the person who feels safe.",
    "You notice their voice change, and suddenly your confidence changes too.",
    "You walk into the room and instantly check if they noticed you.",
    "You think you want closure, but your brain may want one more hit of hope.",
    "They hold eye contact half a second longer, and your brain writes a whole story.",
    "You are not falling for them yet; you are falling for the tension.",
    "One late-night reply can feel more intimate than an actual conversation.",
    "They smile, look away, and suddenly you are analyzing the whole room.",
    "You feel calm around the right person, but addicted around the confusing one.",
)

# The middle beat: why the opening moment happens. Kept separate from the
# openers so a Short can pair any opener with any explanation.
RELATIONSHIP_FACTS: tuple[str, ...] = (
    "Unpredictable replies train the brain harder than steady ones, because uncertainty keeps the reward loop open.",
    "The body reads nervousness and attraction with the same raised heart rate, so tension gets misfiled as chemistry.",
    "Attention that arrives on a schedule feels safe, but attention that arrives at random feels urgent.",
    "Chasing releases more dopamine than receiving, which is why the pursuit can outshine the person.",
    "A pause reads as rejection because the brain fills the gap long before the facts arrive.",
    "Intermittent warmth builds a stronger habit than constant warmth, so the inconsistency is the hook.",
    "The brain fills an ambiguous silence with the story it most fears, which is rarely the likeliest one.",
)

# Takeaway endings: one plain sentence the viewer can act on.
CLOSERS: tuple[str, ...] = (
    "Chemistry gets attention; consistency earns access.",
    "The spark starts the story, but the pattern tells you whether to stay.",
    "Judge the ordinary pattern, not the most exciting moment.",
    "Watch what someone does on an ordinary day, not on their best one.",
    "If it only feels alive when it is uncertain, the feeling is the uncertainty.",
)

FRAMES: tuple[str, ...] = (
    "hot relationship micro-moment, brain explanation, emotionally mature power move",
    "phone tension, attraction loop, one calm next action",
    "body cue, romantic misread, practical confidence reset",
    "almost-flirt moment, nervous system reaction, safer interpretation",
    "mixed signal, dopamine loop, clean boundary payoff",
    "crush behavior, hidden insecurity, attractive self-control",
    "dating scene, body language clue, non-toxic confidence advice",
    "late-night text, chemistry spike, self-respect reset",
    "eye contact tension, attraction cue, calm confidence payoff",
    "almost-kiss suspense, nervous system cue, mature next move",
)

# Subject-keyed openers, tried before the general pool.
TOPIC_OPENERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("decision fatigue",), "You open the menu, scan it twice, and suddenly choosing lunch feels exhausting."),
    (("almost relationships", "situationship"), "They text like a partner, avoid the label, and somehow still control your whole mood."),
    (("love bombing",), "They plan a future with you on day three, and the intensity feels impossible to ignore."),
    (("breadcrumb",), "They disappear, send one perfect message, and your hope resets instantly."),
    (("push pull",), "They pull you close, go cold, then return just as you start moving on."),
    (("mixed signals", "mixed attachment"), "They act deeply interested, disappear, then return before your hope has time to switch off."),
    (("emotional availability",), "They can name what they feel without making you guess where you stand."),
    (("self respect in dating", "fear of being too much"), "You really like them, so you start editing every need that might make you harder to keep."),
    (("flirting anxiety",), "You know what you want to say, then eye contact makes every word disappear."),
    (("eye contact",), "They hold your gaze one beat longer, and your body reacts before you think."),
    (("almost kiss",), "They lean closer, pause, and that unfinished second becomes the whole memory."),
    (("late night texting",), "One midnight message feels more intimate than the plans they still have not made."),
    (("texting anxiety", "reply time anxiety", "attachment anxiety after texting"), "Their reply slows down. Suddenly, the timestamp feels brutal. The whole connection seems at risk."),
    (("voice change attraction",), "Their voice shifts when you walk over, and you wonder whether that tiny change means attraction."),
    (("micro flirting", "flirting body language", "body language"), "They lean in, mirror your smile, and find one more reason not to end the conversation."),
    (("chemistry versus compatibility", "chemistry anxiety"), "The date feels electric, but your body is still asking whether this is attraction or alarm."),
    (("romantic uncertainty",), "Nothing is defined, so every text feels like new evidence for the relationship you hope is forming."),
    (("exclusivity conversation", "talking stage anxiety"), "You talk every day, but asking what this is suddenly feels riskier than staying confused."),
    (("orbiting", "checking an ex"), "They stop talking to you, keep watching every story, and one view reopens the whole question."),
    (("ghosting", "closure seeking"), "They disappear without an answer, and your mind keeps drafting the explanation they never gave."),
    (("slow fading", "dry texting"), "Their replies get shorter, the plans get vaguer, and you start working harder for less connection."),
    (("double texting", "canceled date"), "One unanswered message or canceled plan makes your thumb hover over a second explanation."),
    (("voice note intimacy", "late night vulnerability"), "Their voice lands in your headphones after midnight, and the message feels more intimate than the relationship is."),
    (("attraction to unavailable", "fear of intimacy"), "Closeness feels magnetic until it becomes real, then distance suddenly looks safer."),
    (("apology consistency", "conflict repair"), "The apology sounds perfect, but the next conflict reveals whether anything actually changed."),
    (("vulnerability reciprocity", "emotional intimacy versus oversharing"), "You share one honest thing, then watch whether they meet you with care or leave you exposed."),
    (("friends with benefits", "consent and chemistry"), "The chemistry is real, but one clear question decides whether the arrangement is still mutual."),
    (("sexual tension versus compatibility", "kissing chemistry", "first kiss nerves"), "The space closes and your body says yes, while compatibility is still an unanswered question."),
    (("crush idealization", "limerence"), "You know a few magnetic details, and your mind writes the rest of the person for you."),
    (("future faking",), "They describe a beautiful future, but the next real plan never makes it onto the calendar."),
    (("benching",), "They keep you warm enough to stay available, but never close enough to build anything real."),
    (("social media jealousy",), "One story, like, or follow turns a fragment online into a full relationship threat."),
    (("rebound chemistry",), "The new connection feels electric, but part of the spark may be relief from the loss before it."),
    (("relationship pacing", "mutual effort", "emotional safety"), "You stop measuring promises and start noticing whether the pace, effort, and care are actually mutual."),
    (("silent treatment",), "They stop replying after conflict, and your body starts chasing connection before you know what happened."),
    (("rejection sensitivity",), "You hear one plan change, and your brain searches the whole relationship for proof you did something wrong."),
    (("fear conditioning",), "Your body tenses before anything happens because it learned the warning first."),
    (("fear of abandonment",), "They take longer to reply, and your body treats the silence like a warning."),
    (("anxious attachment",), "One delayed text turns a calm evening into a search for reassurance."),
    (("avoidant attachment",), "They get close, then need distance the moment the connection starts feeling real."),
    (("attachment styles",), "One person asks for closeness while the other suddenly needs distance."),
    (("emotional contagion",), "You walk into a tense room, and your mood changes before anyone explains why."),
    (("emotional regulation",), "One sharp message hits, and your body wants to reply before your judgment catches up."),
    (("attention span",), "You reach for your phone while someone you care about is still talking."),
    (("fawn response",), "Someone sounds disappointed, and you agree before checking what you actually want."),
    (("halo effect", "first impression"), "One electric date can make you defend red flags you would instantly notice in your best friend's relationship."),
    (("impostor syndrome",), "You get praised, then immediately search for the mistake everyone missed."),
    (("learned helplessness",), "You stop trying before the next attempt because the last failures still feel like proof."),
    (("memory distortion",), "One good memory gets replayed until the whole relationship looks safer than it was."),
    (("mirror neurons",), "They tense their jaw, and your body copies the mood before you notice."),
    (("analysis paralysis",), "You reopen the same options, hoping one more comparison will finally make the choice feel safe."),
    (("anchoring effect",), "The first number you see quietly becomes the standard for every choice that follows."),
    (("comparison trap",), "You check one polished life online, then your own ordinary day suddenly feels smaller."),
    (("attachment",), "One slow reply changes your mood, even when nothing else changed."),
    (("dopamine", "reward loop"), "You check again without deciding to because your brain still expects a reward."),
)

# --- the shared rule set -----------------------------------------------------

# Openings that state a topic instead of showing a moment.
GENERIC_OPENER_STARTS: tuple[str, ...] = (
    "to understand ",
    "there is a fascinating reason",
    "if you've ever felt",
    "modern psychology has a lot to say",
    "is one of those moments",
    "a psychology pattern like",
    "once you understand",
    "one tiny trigger can make",
    "the weirdest part of",
    "this is the split second where",
    "the trap in ",
    "your brain can turn",
    "this is where ",
)

GENERIC_OPENER_BITS: tuple[str, ...] = (
    "shapes everyday behavior",
    "starts to make sense",
    "you're definitely not alone",
    "does before you notice it",
)

# A Short opener has about one breath to land, so it is bounded on both ends.
MIN_OPENER_WORDS = 6
MAX_OPENER_WORDS = 26

# There is deliberately no "must contain a visible action" rule here.
#
# Two versions of one were tried and both rejected lines that are plainly fine:
# a whitelist of verbs threw out "you start editing" and "your thumb hovers"
# because those verbs were not on it, and looking for a person in the sentence
# threw out "One late-night reply can feel more intimate than an actual
# conversation." A rule that fails good work is worse than no rule -- it is
# what made hand-written openers unusable in the first place. What is left
# below is only what can be decided precisely.

# Wording that signals a definition rather than a scene.
ABSTRACT_MARKERS: tuple[str, ...] = (
    "represents", "refers to", "is defined as", "is a phenomenon",
    "is a concept", "is the tendency", "can be understood as",
)


def is_generic_opener(text: str) -> bool:
    """True when the line states a topic instead of showing a moment."""
    cleaned = (text or "").lower().strip()
    if not cleaned:
        return True
    if cleaned.startswith(GENERIC_OPENER_STARTS):
        return True
    return any(bit in cleaned for bit in GENERIC_OPENER_BITS)


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", (text or "").lower()) if w]


def opener_issues(text: str) -> list[str]:
    """Every reason this opener is not good enough, in plain words.

    This is the single check. The gate rejects on it and the composer repairs
    against it, so a line can no longer pass one and fail the other.
    """
    issues: list[str] = []
    cleaned = (text or "").strip()
    if not cleaned:
        return ["opener is empty"]
    if is_generic_opener(cleaned):
        issues.append("opener states the topic instead of showing a moment")
    count = len(_words(cleaned))
    if count < MIN_OPENER_WORDS:
        issues.append(f"opener is too short ({count} words, needs {MIN_OPENER_WORDS})")
    elif count > MAX_OPENER_WORDS:
        issues.append(f"opener is too long ({count} words, allow {MAX_OPENER_WORDS})")
    if any(marker in cleaned.lower() for marker in ABSTRACT_MARKERS):
        issues.append("opener defines the topic instead of showing it")
    return issues


def joined_opener(text: str) -> str:
    """Fold a two-sentence opener into one sentence.

    Composed Shorts split beats on sentence boundaries, so an opener that
    buries its cue in a second sentence loses it. Joining on a comma keeps the
    cue in the beat that actually gets spoken first.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", cleaned) if p.strip()]
    if len(parts) < 2:
        return cleaned
    head = parts[0].rstrip(".!?")
    tail = parts[1][0].lower() + parts[1][1:] if parts[1] else ""
    joined = f"{head}, and {tail}"
    rest = " ".join(parts[2:])
    return f"{joined} {rest}".strip() if rest else joined
