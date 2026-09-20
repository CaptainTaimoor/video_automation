from __future__ import annotations

import html
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, urlparse

import feedparser
import requests

from yt_auto.source_registry import (
    CURATED_TOPIC_SOURCE_PACKS,
    VERIFIED_CONTEXT_URLS,
    VERIFIED_CONTEXT_URL_SET,
)


# Words that carry no subject, so a headline search is not dragged off target
# by the framing around the thing the headline is actually about.
_HEADLINE_FILLER = {
    "why", "how", "what", "when", "where", "who", "the", "and", "for", "with",
    "still", "matters", "really", "actually", "explained", "inside", "about",
    "behind", "secrets", "story", "truth", "history", "historians", "reveals",
    "that", "this", "from", "into", "than", "then", "your", "you",
}


class SourceSafeResearchExhaustedError(RuntimeError):
    """Raised when no remaining topic has enough verified, concrete research."""


class ContentResearcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "yt-auto/1.0"})
        self._history_detail_cache: dict[str, List[str]] = {}
        self._source_validation_cache: dict[str, bool] = {}
        self.history_fact_overrides = {
            "antikythera mechanism": "The Antikythera mechanism was an ancient Greek device that tracked astronomical cycles with remarkable mechanical precision.",
            "battle of thermopylae": "The Battle of Thermopylae became famous because a small Greek force held off a much larger Persian army at a narrow pass.",
            "battle of teutoburg forest": "The Battle of the Teutoburg Forest was the ambush that destroyed three Roman legions in Germania.",
            "bronze age collapse": "The Bronze Age collapse was a period when major Mediterranean powers fell apart in a wave of war, migration and disruption.",
            "byzantine greek fire": "Greek fire was a feared Byzantine weapon because it could keep burning even on water.",
            "cadaver synod": "The Cadaver Synod was the infamous trial where a dead pope's body was put on display and judged in court.",
            "carthage": "Carthage was a powerful North African city that rivaled Rome for control of the western Mediterranean.",
            "code of hammurabi": "The Code of Hammurabi is one of the oldest surviving legal codes carved into stone for public display.",
            "dead sea scrolls": "The Dead Sea Scrolls are ancient Jewish manuscripts that transformed how historians study the biblical world.",
            "fall of constantinople": "The fall of Constantinople in 1453 ended the Byzantine Empire and reshaped trade, politics and warfare.",
            "justinianic plague": "The Justinianic Plague was a devastating pandemic that struck the Byzantine Empire in the sixth century.",
            "library of alexandria": "The Library of Alexandria was part archive, part research center and a symbol of ancient knowledge gathering.",
            "minoan eruption": "The Minoan eruption on Thera was one of the largest volcanic events of the ancient world.",
            "nazca lines": "The Nazca Lines are vast desert geoglyphs in Peru made by removing dark surface stones to reveal lighter ground beneath.",
            "oracle of delphi": "The Oracle of Delphi was one of the most influential religious institutions in the Greek world.",
            "petra": "Petra was a Nabataean city carved into rose-colored rock and positioned on major trade routes.",
            "pompeii": "Pompeii was preserved under volcanic ash after Mount Vesuvius erupted in AD 79.",
            "rosetta stone": "The Rosetta Stone helped scholars decipher Egyptian hieroglyphs by presenting the same text in multiple scripts.",
            "sack of rome (410)": "The sack of Rome in 410 shocked the ancient world because the city had seemed untouchable for centuries.",
            "sea peoples": "The Sea Peoples appear in ancient Egyptian records as raiders linked to the instability of the late Bronze Age.",
            "siege of masada": "Masada is remembered as the desert fortress where Jewish rebels made their final stand against Rome.",
            "terracotta army": "The Terracotta Army was buried to guard the tomb of China's first emperor in the afterlife.",
            "tomb of qin shi huang": "The tomb of Qin Shi Huang is the vast burial complex created for the first emperor of a unified China.",
            "mausoleum of qin shi huang": "The mausoleum of Qin Shi Huang is the vast burial complex created for the first emperor of a unified China.",
            "mausoleum of the first qin emperor": "China's first emperor ordered a vast burial complex guarded by thousands of life-sized terracotta soldiers.",
            "vindolanda tablets": "The Vindolanda tablets are Roman writing tablets that reveal everyday life on the empire's northern frontier.",
            "gobekli tepe": "Gobekli Tepe is a prehistoric site in southeastern Turkey with monumental stone circles older than Stonehenge.",
            "catalhoyuk": "Catalhoyuk was a dense Neolithic settlement in Anatolia where people lived in mudbrick houses entered from the roof.",
            "uruk": "Uruk was one of the world's earliest major cities and became a center of writing, trade and temple power in Mesopotamia.",
            "ziggurat of ur": "The Ziggurat of Ur was a massive stepped temple platform built in ancient Mesopotamia for the city's moon god.",
            "mohenjo-daro": "Mohenjo-daro was a major Indus Valley city known for planned streets, drainage systems and baked-brick architecture.",
            "indus valley civilization": "The Indus Valley Civilization built large planned cities with advanced drainage long before many later empires.",
            "epic of gilgamesh": "The Epic of Gilgamesh is one of the oldest surviving works of literature and preserves ancient Mesopotamian ideas about kingship and mortality.",
            "assyrian siege of lachish": "The Assyrian siege of Lachish is recorded in palace reliefs that show the machinery and brutality of imperial warfare.",
            "cyrus cylinder": "The Cyrus Cylinder records a Persian royal proclamation after Babylon fell to Cyrus the Great.",
            "persepolis": "Persepolis was the ceremonial heart of the Achaemenid Persian Empire, built to display imperial power and tribute.",
            "battle of marathon": "The Battle of Marathon became famous because an Athenian-led force defeated a larger Persian invasion army.",
            "peloponnesian war": "The Peloponnesian War was the long conflict between Athens and Sparta that weakened the Greek world.",
            "alexander siege of tyre": "Alexander's siege of Tyre used a massive causeway to attack an island city that had resisted conquest.",
            "ashoka edicts": "Ashoka's edicts were inscriptions across the Mauryan Empire that announced law, morality and royal policy.",
            "han dynasty silk road": "The Han dynasty helped expand Silk Road links that carried goods, ideas and diplomacy across Eurasia.",
            "roman concrete": "Roman concrete helped ancient builders create durable harbors, vaults and monuments that still survive.",
            "hadrian's wall": "Hadrian's Wall marked the northern edge of Roman Britain and controlled movement across the frontier.",
            "boudica revolt": "Boudica led a major revolt against Roman rule in Britain after her family and tribe were abused by imperial officials.",
            "tikal": "Tikal was a powerful Maya city whose temples and plazas reveal the scale of Classic Maya kingship.",
            "mayan calendar": "The Maya calendar system combined astronomy, ritual cycles and long historical counts with remarkable precision.",
            "olmec colossal heads": "The Olmec colossal heads are massive stone portraits that show the power and artistry of an early Mesoamerican culture.",
            "chichen itza": "Chichen Itza joined El Castillo, Mesoamerica's largest known ball court and a ritual cenote in one Maya center.",
            "stonehenge": "Stonehenge was a prehistoric monument aligned with solar events and built through generations of labor.",
            "lascaux cave paintings": "The Lascaux cave paintings preserve Ice Age animal art that shows the skill and imagination of prehistoric people.",
            "nubian pyramids": "The Nubian pyramids of Kush show how powerful Nile Valley kingdoms built their own royal burial traditions.",
            "kingdom of kush": "The Kingdom of Kush ruled from Nubia and at times controlled Egypt as the Twenty-Fifth Dynasty.",
            "axum obelisks": "Aksum's northern stelae field raised towering granite monuments, while underground chambers held elite tombs.",
            "great zimbabwe": "The stone-built city became a center. Trade and cattle wealth sustained power. Political authority shaped life there.",
            "terrace farming at machu picchu": "Machu Picchu's famous view depends on terraces hiding a drainage system beneath the steep Andean slopes.",
            "sogdian merchants": "Sogdian merchants helped connect Silk Road cities by carrying goods, languages and ideas across Central Asia.",
            "angkor wat": "Angkor Wat began as a Hindu state temple whose towers, galleries and moat modelled Mount Meru for Khmer royal power.",
        }
        self.brain_lens_fact_overrides = {
            "analysis paralysis": "Analysis paralysis happens when overthinking turns decision-making into a loop that blocks action.",
            "anchoring effect": "The anchoring effect happens when the first number or idea you see strongly shapes later judgment.",
            "anxious attachment": "Anxious attachment is a relationship pattern where uncertainty can trigger intense reassurance-seeking.",
            "almost relationships": "Almost relationships can feel intense because the brain keeps chasing possibility without receiving clear commitment.",
            "almost kiss tension": "An almost kiss can feel powerful because anticipation makes the brain amplify attention, memory and reward.",
            "attachment styles": "Attachment styles describe patterns people develop around closeness, trust and emotional connection.",
            "attention span": "Attention span describes how long someone can stay focused before their mind drifts to something else.",
            "avoidant attachment": "Avoidant attachment is a relationship pattern where closeness can feel overwhelming even when connection matters.",
            "body language": "Body language is the set of nonverbal signals people use through posture, facial expression and movement.",
            "chemistry versus compatibility": "Chemistry can feel exciting in the moment, while compatibility depends on respect, timing, and repeated behavior.",
            "brain fog": "Brain fog is a common way people describe mental sluggishness, forgetfulness and trouble concentrating.",
            "burnout": "Burnout is the state of emotional exhaustion, detachment and reduced effectiveness that follows chronic stress.",
            "choice overload": "Choice overload happens when too many options make decisions feel harder instead of easier.",
            "cognitive dissonance": "Cognitive dissonance is the mental discomfort people feel when their beliefs and actions clash.",
            "cognitive overload": "Cognitive overload happens when the brain is asked to process more information than it can handle efficiently.",
            "comparison trap": "The comparison trap happens when constant self-evaluation against others starts to damage confidence and mood.",
            "confirmation bias": "Confirmation bias is the habit of noticing and remembering evidence that supports what you already believe.",
            "decision fatigue": "Decision fatigue is the drop in judgment and self-control that can happen after making too many choices.",
            "default mode network": "The default mode network is a set of brain regions that becomes active during mind-wandering and self-focused thought.",
            "dopamine": "Dopamine is a brain chemical involved in motivation, learning and the feeling that something is worth chasing.",
            "eye contact attraction": "Eye contact can feel powerful because direct attention is a strong social cue linked to interest, trust and emotional intensity.",
            "late night texting": "Late-night texting can feel more intimate because tired brains read silence, speed and tone with extra emotion.",
            "doomscrolling": "Doomscrolling is the habit of continuing to consume negative content even when it leaves you feeling worse.",
            "emotional contagion": "Emotional contagion is the tendency to absorb and mirror the emotions of people around you.",
            "emotional intelligence": "Emotional intelligence is the ability to understand feelings in yourself and other people and respond well to them.",
            "emotional numbness": "Emotional numbness is when the mind dulls feelings as a way to reduce overwhelm or stress.",
            "emotional regulation": "Emotional regulation is the ability to notice, manage and respond to feelings without being overwhelmed by them.",
            "fear conditioning": "Fear conditioning is the process where the brain learns to link a neutral cue with danger or discomfort.",
            "fear of abandonment": "Fear of abandonment is the worry that important people may leave, reject, or pull away.",
            "flirting body language": "Flirting body language often works through small signals like eye contact, posture, mirroring and relaxed attention.",
            "fawn response": "The fawn response is a stress pattern where people try to stay safe by pleasing or appeasing others.",
            "first impressions": "First impressions are fast judgments the brain makes from limited social cues within seconds.",
            "freeze response": "The freeze response is a stress reaction where the body slows down or shuts down before action feels possible.",
            "habit loop": "A habit loop is the cycle of cue, routine and reward that helps repeated behaviors become automatic.",
            "halo effect": "The halo effect is the tendency to let one positive trait shape your whole impression of a person.",
            "high-functioning anxiety": "High-functioning anxiety is a common phrase for looking capable while feeling driven by worry inside.",
            "impostor syndrome": "Impostor syndrome is the feeling that your success is undeserved even when the evidence says otherwise.",
            "learned helplessness": "Learned helplessness happens when repeated setbacks train someone to expect failure even when change is possible.",
            "loneliness and the brain": "Loneliness changes how people process threat, connection and social attention.",
            "mixed signals": "Mixed signals can feel addictive because uncertainty keeps the brain searching for proof of interest.",
            "micro flirting": "Micro flirting works through tiny signals like playful timing, eye contact, posture and small changes in voice.",
            "memory distortion": "Memory distortion happens because recall is a reconstruction process, not a perfect replay.",
            "mirror neurons": "Mirror neurons are brain cells linked to empathy, imitation and reading other people's actions.",
            "negativity bias": "Negativity bias is the brain's tendency to notice and remember negative experiences more strongly than neutral ones.",
            "overthinking": "Overthinking happens when the mind keeps looping through possibilities instead of moving toward a decision.",
            "parasocial relationships": "Parasocial relationships are one-sided emotional bonds people form with media figures who do not actually know them.",
            "people pleasing": "People pleasing is the habit of prioritizing approval and harmony even when it costs personal boundaries.",
            "perfectionism": "Perfectionism is the pressure to avoid mistakes at all costs, even when it blocks progress.",
            "procrastination": "Procrastination is usually less about laziness and more about avoiding discomfort in the moment.",
            "rejection sensitivity": "Rejection sensitivity is the tendency to quickly expect, notice or overreact to signs of social rejection.",
            "situationship anxiety": "Situationship anxiety grows when closeness, attention and uncertainty arrive together without clear commitment.",
            "revenge bedtime procrastination": "Revenge bedtime procrastination is staying up late to reclaim free time even when you know it will hurt sleep.",
            "rumination": "Rumination is the repetitive habit of turning the same distressing thoughts over without moving toward relief.",
            "self sabotage": "Self-sabotage happens when fear, doubt or old habits push people to undermine goals they actually want.",
            "self abandonment": "Self-abandonment is ignoring your own needs or limits to keep approval, peace, or connection.",
            "self esteem": "Self-esteem is the overall sense of personal worth that shapes confidence, risk-taking and emotional resilience.",
            "texting anxiety": "Texting anxiety happens when delayed replies, tone, or silence become loaded with imagined meaning.",
            "voice change attraction": "People may shift pitch, pace, loudness, or expressiveness around someone they find attractive, but the pattern varies by person and context.",
            "sleep and memory": "Sleep helps the brain strengthen memories by sorting and consolidating what happened during the day.",
            "social anxiety": "Social anxiety is the fear of being judged, embarrassed or negatively evaluated in social situations.",
            "social battery": "Social battery is a casual way to describe how social interaction can drain or restore mental energy.",
            "social proof": "Social proof is the tendency to copy what others seem to be doing when you are unsure how to act.",
            "stress response": "The stress response is the body's built-in alarm system that prepares you to react to pressure or danger.",
            "sunk cost fallacy": "The sunk cost fallacy is staying invested just because you already spent time or effort.",
            "toxic positivity": "Toxic positivity is pressure to stay positive in a way that can dismiss real feelings or problems.",
            "trauma response": "A trauma response is the mind and body's protective reaction to overwhelming stress, even long after the event is over.",
            "attachment anxiety after texting": "Attachment anxiety after texting rises when the brain treats silence as a possible social threat.",
            "breadcrumbing": "Breadcrumbing keeps attention hooked through tiny bursts of interest without steady emotional investment.",
            "chemistry anxiety": "Chemistry anxiety happens when strong attraction makes ordinary uncertainty feel unusually urgent.",
            "emotional availability": "Emotional availability is the ability to stay present, responsive and honest during closeness.",
            "fear of being too much": "Fear of being too much can make people shrink their needs to protect a connection.",
            "flirting anxiety": "Flirting anxiety appears when the brain wants connection but scans every signal for rejection.",
            "love bombing": "Love bombing can feel intoxicating because intense attention quickly activates reward, hope and attachment.",
            "mixed attachment signals": "Mixed attachment signals confuse the brain because warmth and distance arrive close together.",
            "push pull dynamic": "A push pull dynamic keeps attention locked because closeness and distance alternate unpredictably.",
            "romantic uncertainty": "Romantic uncertainty can feel addictive because the brain keeps checking for the next sign of safety.",
            "self respect in dating": "Self respect in dating means noticing attraction without abandoning boundaries, timing or standards.",
            "silent treatment": "The silent treatment can trigger threat systems because sudden disconnection creates uncertainty and power imbalance.",
            "reply time anxiety": "Reply time anxiety happens when message speed becomes a false measure of interest or worth.",
            "talking stage anxiety": "Talking-stage anxiety grows when emotional closeness increases before expectations or commitment become clear.",
            "post date overthinking": "Post-date overthinking turns a few ambiguous moments into repeated guesses about interest, mistakes and future rejection.",
            "dry texting": "Dry texting can feel rejecting because short replies remove tone and leave the receiver to supply the missing meaning.",
            "double texting anxiety": "Double-texting anxiety appears when sending another message feels like a test of confidence, interest or social value.",
            "slow fading": "Slow fading creates uncertainty when contact gradually decreases without a direct conversation about the change.",
            "future faking": "Future faking uses exciting promises about later commitment without matching those promises with consistent action now.",
            "benching in dating": "Benching keeps someone available through occasional attention while avoiding the consistency needed to build a relationship.",
            "orbiting after breakup": "Orbiting after a breakup keeps emotional access alive through views, likes or reactions without direct repair or commitment.",
            "ghosting recovery": "Ghosting recovery becomes harder when the lack of explanation leaves the mind repeatedly searching for a missing ending.",
            "closure seeking": "Closure seeking can become a loop when another person's explanation is treated as the only way to regain emotional stability.",
            "limerence": "Limerence describes intense romantic preoccupation that can grow through uncertainty, fantasy and limited real-world information.",
            "crush idealization": "Crush idealization fills gaps in knowledge with desirable traits, making imagined compatibility feel more complete than the evidence.",
            "attraction to unavailable people": "Attraction to unavailable people can feel safer when longing allows intimacy in fantasy without the risks of mutual closeness.",
            "fear of intimacy": "Fear of intimacy can make genuine closeness trigger withdrawal even when connection is wanted and valued.",
            "first date nerves": "First-date nerves combine attraction, uncertainty and social evaluation, so ordinary pauses can feel unusually important.",
            "first kiss nerves": "First-kiss nerves intensify attention because desire, uncertainty and the need for mutual consent arrive in the same moment.",
            "dating app choice overload": "Dating-app choice overload can make each match feel replaceable and turn normal uncertainty into constant comparison.",
            "online dating burnout": "Online-dating burnout grows when repeated evaluation, small talk and disappointment start to outweigh curiosity and connection.",
            "exclusivity conversation anxiety": "Exclusivity-conversation anxiety appears when asking for clarity feels risky because the answer could change the connection.",
            "emotional intimacy versus oversharing": "Emotional intimacy grows through mutual trust and pacing, while oversharing can reveal a lot before safety or reciprocity exists.",
            "vulnerability reciprocity": "Vulnerability feels safer when openness is met with respect, care and comparable emotional effort over time.",
            "conflict repair": "Conflict repair depends less on never disagreeing and more on returning with accountability, respect and a workable next step.",
            "apology consistency": "An apology builds trust only when later behavior reduces the chance that the same harm will keep repeating.",
            "relationship pacing": "Relationship pacing lets attraction stay exciting while time reveals consistency, compatibility and respect for boundaries.",
            "mutual effort in dating": "Mutual effort becomes visible when planning, communication and emotional labor are returned rather than repeatedly chased.",
            "secure attraction": "Secure attraction can still feel exciting, but it is supported by clarity, reciprocity and room to remain yourself.",
            "emotional safety in dating": "Emotional safety in dating grows when honesty does not lead to punishment, ridicule, pressure or disappearing contact.",
            "social media jealousy": "Social-media jealousy intensifies when incomplete online signals are treated as complete evidence about a relationship.",
            "checking an ex online": "Checking an ex online can briefly reduce uncertainty while keeping attention attached to new fragments of their life.",
            "rebound chemistry": "Rebound chemistry can feel powerful because novelty and attention arrive while grief and identity are still unsettled.",
            "canceled date anxiety": "Canceled-date anxiety turns one change of plan into a larger rejection story before the follow-up behavior is known.",
            "voice note intimacy": "Voice notes can feel more intimate than text because tone, breath and timing carry emotional information words alone remove.",
            "late night vulnerability": "Late-night vulnerability can feel unusually intense because fatigue lowers restraint and makes private attention feel more exclusive.",
            "friends with benefits boundaries": "Friends-with-benefits arrangements need explicit consent, expectations and boundaries because emotional meaning can change over time.",
            "sexual tension versus compatibility": "Sexual tension can create powerful attention, while compatibility still depends on communication, values, timing and mutual respect.",
            "kissing chemistry": "Kissing chemistry is shaped by mutual comfort, timing, attention and consent rather than one universal technique.",
            "friendship to romance tension": "Friendship-to-romance tension grows when familiar emotional safety meets new uncertainty about mutual attraction.",
            "consent and chemistry": "Consent strengthens chemistry when both people can express interest, hesitation and boundaries without pressure.",
            "trauma bond": "A trauma bond can form when relief after stress feels like love because the nervous system is chasing safety.",
            "respect after rejection": "Respect after rejection means accepting another person's choice without guilt, bargaining, punishment or pressure.",
            "planning initiative": "Planning initiative makes interest visible through concrete suggestions, shared decisions and reliable follow-through.",
            "warmth after vulnerability": "Warmth after vulnerability protects openness through patient attention, privacy and respect rather than control.",
            "comfortable silence": "Comfortable silence can signal ease when warmth and connection remain present without constant performance.",
            "meeting friends": "Meeting friends can add social context to dating, but it does not by itself prove commitment or compatibility.",
            "playful teasing boundaries": "Playful teasing stays connective when both people can redirect it and discomfort is respected immediately.",
            "busy week consistency": "Busy-week consistency keeps communication understandable without requiring constant contact during a demanding schedule.",
            "follow-up questions": "Thoughtful follow-up questions can make listening visible when curiosity remains mutual and respectful.",
            "working memory": "Working memory is the brain's short-term workspace for holding and using information right now.",
        }
        self.history_wikipedia_aliases = {
            "angkor wat": "Angkor Wat",
            "alexander siege of tyre": "Siege of Tyre (332 BC)",
            "ashoka edicts": "Edicts of Ashoka",
            "assyrian siege of lachish": "Siege of Lachish",
            "axum obelisks": "Obelisk of Axum",
            "boudica revolt": "Boudican revolt",
            "bronze age collapse": "Late Bronze Age collapse",
            "chichen itza": "Chichen Itza",
            "cyrus cylinder": "Cyrus Cylinder",
            "byzantine greek fire": "Greek fire",
            "han dynasty silk road": "Silk Road",
            "justinianic plague": "Plague of Justinian",
            "great zimbabwe": "Great Zimbabwe",
            "lascaux cave paintings": "Lascaux",
            "lascaux": "Lascaux",
            "mausoleum of qin shi huang": "Mausoleum of the First Qin Emperor",
            "mausoleum of the first qin emperor": "Mausoleum of the First Qin Emperor",
            "mohenjo-daro": "Mohenjo-daro",
            "mayan calendar": "Maya calendar",
            "nubian pyramids": "Nubian pyramids",
            "olmec colossal heads": "Olmec colossal heads",
            "kingdom of kush": "Kingdom of Kush",
            "sogdian merchants": "Sogdia",
            "roman concrete": "Roman concrete",
            "terrace farming at machu picchu": "Machu Picchu",
            "tikal": "Tikal",
            "tomb of qin shi huang": "Mausoleum of the First Qin Emperor",
        }
        self.history_source_overrides = {
            "assyrian siege of lachish": [
                "https://www.britishmuseum.org/collection/galleries/assyria-lion-hunts",
                "https://www.britishmuseum.org/collection/object/W_1856-0909-14_7",
                "https://www.metmuseum.org/exhibitions/listings/2014/assyria-to-iberia/blog/posts/sennacherib-and-jerusalem",
                "https://pmc.ncbi.nlm.nih.gov/articles/PMC11090153/",
            ],
        }
        duplicate_subjects = {"tomb of qin shi huang", "mausoleum of qin shi huang"}
        self.history_subjects = [
            subject for subject in self.history_fact_overrides
            if subject not in duplicate_subjects
        ]

        self.true_story_subjects = [
            "Baader-Meinhof Phenomenon",
            "Schrödinger's Cat",
            "Quantum Entanglement",
            "Time Dilation",
            "Event Horizon",
            "Singularity",
        ]
        self.brain_lens_subjects = [
            subject for subject in self.brain_lens_fact_overrides.keys()
            if subject != "trauma bond"
        ]

        self.brain_lens_category_map = {
            "bias": {
                "anchoring effect", "cognitive dissonance", "comparison trap", "confirmation bias", "first impressions",
                "halo effect", "memory distortion", "negativity bias", "social proof", "sunk cost fallacy",
            },
            "focus": {
                "analysis paralysis", "attention span", "brain fog", "choice overload", "cognitive overload",
                "decision fatigue", "default mode network", "sleep and memory", "working memory",
            },
            "emotion": {
                "burnout", "emotional contagion", "emotional intelligence", "emotional numbness",
                "emotional regulation", "fear conditioning", "fawn response", "freeze response",
                "high-functioning anxiety", "learned helplessness", "loneliness and the brain",
                "rumination", "social anxiety", "stress response", "trauma response",
            },
            "habit": {
                "doomscrolling", "dopamine", "habit loop", "overthinking", "perfectionism", "procrastination",
                "revenge bedtime procrastination", "self abandonment", "self sabotage",
            },
            "social": {
                "almost relationships", "anxious attachment", "attachment styles", "avoidant attachment", "body language",
                "chemistry versus compatibility", "eye contact attraction", "fear of abandonment", "flirting body language",
                "impostor syndrome", "late night texting", "micro flirting", "mirror neurons", "mixed signals",
                "almost kiss tension", "parasocial relationships", "people pleasing", "rejection sensitivity",
                "self esteem", "situationship anxiety", "social battery", "texting anxiety", "toxic positivity",
                "voice change attraction", "attachment anxiety after texting", "breadcrumbing", "chemistry anxiety",
                "emotional availability", "fear of being too much", "flirting anxiety", "love bombing",
                "mixed attachment signals", "push pull dynamic", "romantic uncertainty", "self respect in dating",
                "silent treatment", "reply time anxiety", "talking stage anxiety", "post date overthinking",
                "dry texting", "double texting anxiety", "slow fading", "future faking", "benching in dating",
                "orbiting after breakup", "ghosting recovery", "closure seeking", "limerence", "crush idealization",
                "attraction to unavailable people", "fear of intimacy", "first date nerves", "first kiss nerves",
                "dating app choice overload", "online dating burnout", "exclusivity conversation anxiety",
                "emotional intimacy versus oversharing", "vulnerability reciprocity", "conflict repair",
                "apology consistency", "relationship pacing", "mutual effort in dating", "secure attraction",
                "emotional safety in dating", "social media jealousy", "checking an ex online", "rebound chemistry",
                "canceled date anxiety", "voice note intimacy", "late night vulnerability",
                "friends with benefits boundaries", "sexual tension versus compatibility", "kissing chemistry",
                "friendship to romance tension", "consent and chemistry", "trauma bond",
                "flirty banter", "relationship check-ins", "post-date anxiety", "growing attraction",
                "honest attraction", "respect after rejection", "planning initiative",
                "warmth after vulnerability", "comfortable silence", "meeting friends",
                "playful teasing boundaries", "busy week consistency", "follow-up questions",
            },
        }
        self.brain_lens_priority_subjects = {
            "almost relationships", "almost kiss tension", "anxious attachment", "attachment styles",
            "avoidant attachment", "body language", "chemistry versus compatibility", "eye contact attraction",
            "fear of abandonment", "flirting body language", "late night texting", "micro flirting",
            "mixed signals", "rejection sensitivity", "situationship anxiety", "texting anxiety",
            "voice change attraction", "attachment anxiety after texting", "breadcrumbing", "chemistry anxiety",
            "emotional availability", "fear of being too much", "flirting anxiety", "love bombing",
            "mixed attachment signals", "push pull dynamic", "romantic uncertainty", "self respect in dating",
            "silent treatment", "reply time anxiety", "talking stage anxiety", "post date overthinking",
            "dry texting", "double texting anxiety", "slow fading", "future faking", "benching in dating",
            "orbiting after breakup", "ghosting recovery", "closure seeking", "limerence", "crush idealization",
            "attraction to unavailable people", "fear of intimacy", "first date nerves", "first kiss nerves",
            "dating app choice overload", "online dating burnout", "exclusivity conversation anxiety",
            "emotional intimacy versus oversharing", "vulnerability reciprocity", "conflict repair",
            "apology consistency", "relationship pacing", "mutual effort in dating", "secure attraction",
            "emotional safety in dating", "social media jealousy", "checking an ex online", "rebound chemistry",
            "canceled date anxiety", "voice note intimacy", "late night vulnerability",
            "friends with benefits boundaries", "sexual tension versus compatibility", "kissing chemistry",
            "friendship to romance tension", "consent and chemistry", "trauma bond",
            "boundary response", "curiosity and interest", "consistency after intimacy",
            "jealousy and clarity", "direct interest", "apology follow-through",
            "flirty banter", "relationship check-ins", "post-date anxiety", "growing attraction",
            "honest attraction", "respect after rejection", "planning initiative",
            "warmth after vulnerability", "comfortable silence", "meeting friends",
            "playful teasing boundaries", "busy week consistency", "follow-up questions",
        }
        self.story_subjects = [
            "Operation Mincemeat",
            "Great Molasses Flood",
            "The Great Stink",
            "London Beer Flood",
            "The Halifax Explosion",
            "The Radium Girls",
            "Phineas Gage",
            "Donner Party",
            "Cadaver Synod",
            "Dancing plague of 1518",
            "Emu War",
            "Mary Toft",
            "The Third Man Factor",
            "The Yuba County Five",
            "Tarrare",
            "Toothbrush moustache",
            "Tunguska event",
            "Dyatlov Pass incident",
            "Cotard's delusion",
            "Krakatoa",
            "Voynich manuscript",
            "Antikythera mechanism",
            "Roanoke Colony",
            "Mary Celeste",
            "Chernobyl disaster",
            "Wojtek (bear)",
            "Balloon boy hoax",
            "Max Headroom signal hijacking",
            "DB Cooper",
            "Zodiac Killer",
            "Centralia mine fire",
            "Tsavo Man-Eaters",
            "Gef the talking mongoose",
            "The Wow! signal",
            "Sailing stones",
            "Kowloon Walled City",
            "Vela incident",
            "Oak Island mystery",
            "Bloop",
            "Kentucky meat shower",
            "Miracle of the Sun",
            "Spring-heeled Jack",
        ]

    def _clean_text(self, text: str) -> str:
        clean = html.unescape(text or "")
        # Repair common UTF-8-as-Windows-1252 corruption returned by a few
        # scraped encyclopedia mirrors. Unicode escapes keep this source itself
        # safe when Windows terminals use a legacy console code page.
        if any(marker in clean for marker in ("\u00e2\u20ac", "\u00c3", "\u00c2")):
            try:
                repaired = clean.encode("cp1252").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                repaired = clean
            if repaired:
                clean = repaired
        clean = (
            clean.replace("\u00e2\u20ac\u2122", "'")
            .replace("\u00e2\u20ac\u02dc", "'")
            .replace("\u00e2\u20ac\u0153", '"')
            .replace("\u00e2\u20ac\u009d", '"')
            .replace("\u00e2\u20ac\u201d", "-")
            .replace("\u00e2\u20ac\u201c", "-")
            .replace("\u2018", "'")
            .replace("\u2019", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u00c2", "")
        )
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\[[^\]]+\]", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    def _finalize_sentence(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip(" ,;:-")
        if not text:
            return ""
        if text[-1] not in ".!?":
            text += "."
        return text

    def _looks_incomplete(self, text: str) -> bool:
        text = re.sub(r"\s+", " ", text or "").strip(" ,;:-")
        if not text:
            return True
        lower = text.lower()
        bare = lower.rstrip(".!?")
        if not bare.strip():
            return True
        trailing_fragments = (
            "may refer to",
            "also known as",
            "which was",
            "that was",
            "because of",
            "due to",
            "in order to",
            "part of a larger",
            "can cause forward",
            "in a way",
            "other eastern",
            "other western",
            "other northern",
            "other southern",
        )
        if any(bare.endswith(fragment) for fragment in trailing_fragments):
            return True
        if re.search(
            r"\b(?:and|or)\s+(?:demonstrates?|describes?|indicates?|records?|reveals?|shows?|suggests?)$",
            bare,
        ):
            return True
        if re.match(r"^(?:when|while|although|because|unless|whereas)\b", bare) and not re.search(
            r"[,;:]",
            bare,
        ):
            # A leading dependent clause without a following main clause is a
            # scraped fragment, even when the source happened to add a period.
            return True
        last_word = bare.split()[-1]
        if last_word in {"the", "a", "an", "and", "or", "but", "to", "of", "for", "with", "by", "from", "into", "eastern", "western", "northern", "southern"}:
            return True
        return False

    def _trim_text(self, text: str, max_chars: int = 170, sentence_mode: bool = True) -> str:
        text = self._clean_text(text)
        if not text:
            return ""

        if len(text) <= max_chars:
            return self._finalize_sentence(text) if sentence_mode else text

        clause_parts = re.split(r"(?<=[,;:])\s+", text)
        candidate = ""
        for part in clause_parts:
            proposed = f"{candidate} {part}".strip()
            if proposed and len(proposed) <= max_chars:
                candidate = proposed
            else:
                break

        if len(candidate) < 50:
            candidate = text[:max_chars].rsplit(" ", 1)[0]
            break_points = [candidate.rfind(token) for token in (", ", "; ", " and ", " which ", " with ", " that ")]
            best_break = max(break_points)
            if best_break >= 65:
                candidate = candidate[:best_break]

        if re.search(
            r",\s*(?:\w+\s+)?(?:feet|foot|metres?|meters?)\s*\([^)]*\)\s*$",
            candidate,
            flags=re.IGNORECASE,
        ):
            candidate = candidate.rsplit(",", 1)[0]

        return self._finalize_sentence(candidate) if sentence_mode else candidate.strip()

    def _split_sentences(self, text: str) -> List[str]:
        raw = self._clean_text(text)
        parts = re.split(r"(?<=[.!?])\s+", raw)
        cleaned = []
        ends_cleanly = bool(re.search(r"[.!?]\s*$", raw))
        for idx, part in enumerate(parts):
            p = re.sub(r"\s+", " ", part).strip()
            if idx == len(parts) - 1 and not ends_cleanly:
                continue
            if self._looks_incomplete(p):
                continue
            if len(p) >= 25:
                cleaned.append(self._finalize_sentence(p))
        return cleaned

    def _override_fact_for_title(self, title: str) -> str:
        normalized = self._normalize_subject(title)
        for mapping in (self.history_fact_overrides, self.brain_lens_fact_overrides):
            if normalized in mapping:
                return mapping[normalized]
            norm_words = {w for w in re.findall(r"\w{4,}", normalized)}
            for key, fact in mapping.items():
                key_words = {w for w in re.findall(r"\w{4,}", key)}
                if normalized in key or key in normalized or len(norm_words & key_words) >= 2:
                    return fact
        return ""

    def _first_clean_fact(self, item: dict | None) -> str:
        if not item:
            return ""
        override = self._override_fact_for_title(str(item.get("title", "")))
        if override:
            return override
        candidates = []
        summary = item.get("summary", "")
        if summary:
            candidates.extend(self._split_sentences(summary))
        candidates.extend(item.get("bullets", []) or [])
        for fact in candidates:
            cleaned = self._trim_text(fact, max_chars=170, sentence_mode=True)
            low = cleaned.lower()
            if not cleaned or self._looks_incomplete(cleaned):
                continue
            if any(flag in low for flag in ("may refer to", "also known as", "list of", "category")):
                continue
            return cleaned
        return ""

    def _clip(self, items: List[str], limit: int = 4, max_chars: int = 170, sentence_mode: bool = True) -> List[str]:
        out = []
        for item in items:
            text = self._trim_text(item, max_chars=max_chars, sentence_mode=sentence_mode)
            if not text:
                continue
            out.append(text)
            if len(out) >= limit:
                break
        return out

    def _source_url_is_live(self, value: str) -> bool:
        """Reject malformed, invented, and currently missing research URLs."""
        url = str(value or "").strip()
        if url in VERIFIED_CONTEXT_URL_SET:
            self._source_validation_cache[url] = True
            return True
        if url in self._source_validation_cache:
            return self._source_validation_cache[url]
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or "." not in parsed.netloc:
                self._source_validation_cache[url] = False
                return False
            response = self.session.get(url, timeout=(5, 10), allow_redirects=True, stream=True)
            live = 200 <= response.status_code < 400
            response.close()
        except Exception:
            live = False
        self._source_validation_cache[url] = live
        return live

    def _validated_sources(self, values: List[str], limit: int = 4) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for value in values:
            url = str(value or "").strip()
            key = url.lower()
            if not url or key in seen or not self._source_url_is_live(url):
                continue
            seen.add(key)
            out.append(url)
            if len(out) >= limit:
                break
        return out

    def enrich_source_urls(
        self,
        subject: str,
        values: List[str] | None = None,
        limit: int = 6,
    ) -> List[str]:
        """Merge authoritative topic packs, then run the normal URL validator.

        Discovered URLs keep their original order. Curated institutional and
        peer-reviewed links supplement them, with case-insensitive deduplication
        and the same live/registry validation used by every other source.
        """
        normalized = self._normalize_subject(subject)
        curated: list[str] = []
        for topic_key, source_pack in CURATED_TOPIC_SOURCE_PACKS.items():
            if normalized == topic_key or topic_key in normalized:
                curated.extend(source_pack)
        return self._validated_sources([*(values or []), *curated], limit=limit)

    def _default_source_url(self, title: str) -> str:
        """Resolve an exact Wikipedia page; never manufacture a /wiki/<slug> URL."""
        normalized = self._normalize_subject(title)
        lookup_term = self.history_wikipedia_aliases.get(normalized, title)
        brain_lookup = ""
        if "love bombing" in normalized:
            brain_lookup = "Love bombing"
        elif "breadcrumb" in normalized:
            brain_lookup = "Breadcrumbing"
        elif any(term in normalized for term in ("ghosting", "slow fading", "orbiting", "closure")):
            brain_lookup = "Ghosting (behavior)"
        elif any(term in normalized for term in ("limerence", "crush idealization")):
            brain_lookup = "Limerence"
        elif "jealous" in normalized or "checking an ex" in normalized:
            brain_lookup = "Jealousy"
        elif any(term in normalized for term in ("choice overload", "dating app", "online dating burnout")):
            brain_lookup = "Overchoice"
        elif any(term in normalized for term in ("consent", "friends with benefits")):
            brain_lookup = "Consent"
        elif any(
            term in normalized
            for term in (
                "attachment", "fear of abandonment", "fear of intimacy", "emotionally unavailable",
                "attraction to unavailable", "trauma bond", "secure attraction",
            )
        ):
            brain_lookup = "Attachment theory"
        elif any(
            term in normalized
            for term in (
                "mixed signals", "push pull", "breadcrumb", "future faking", "benching",
                "romantic uncertainty", "reply time", "canceled date",
            )
        ):
            brain_lookup = "Operant conditioning"
        elif any(
            term in normalized
            for term in (
                "texting", "voice note", "late night", "dry texting", "double texting", "social media",
            )
        ):
            brain_lookup = "Computer-mediated communication"
        elif any(
            term in normalized
            for term in (
                "eye contact", "body language", "flirting", "kiss", "chemistry", "sexual tension",
                "voice change", "micro flirting", "friendship to romance", "first date",
                "flirty banter", "growing attraction", "honest attraction",
            )
        ):
            brain_lookup = "Interpersonal attraction"
        elif any(
            term in normalized
            for term in (
                "relationship", "mutual effort", "conflict repair", "apology", "vulnerability",
                "emotional safety", "emotional availability", "exclusivity", "situationship",
                "talking stage", "relationship pacing", "self respect in dating",
                "relationship check-ins", "post-date anxiety",
            )
        ):
            brain_lookup = "Interpersonal relationship"
        elif normalized in self.brain_lens_priority_subjects:
            brain_lookup = "Interpersonal relationship"
        registry_lookup = brain_lookup or lookup_term
        if registry_lookup in VERIFIED_CONTEXT_URLS:
            resolved = VERIFIED_CONTEXT_URLS[registry_lookup]
            self._source_validation_cache[resolved] = True
            return resolved
        # A topic title is a headline -- "Why Nazca Lines still matters to
        # historians" -- and no article is named that. Asked only for exact
        # titles, this returned "" and the planner rejected the subject as
        # source-less, then proposed the same headline again. Searching for
        # the words inside it finds the page the headline is about.
        searched = self._wikipedia_search_title(lookup_term)
        lookup_terms = list(
            dict.fromkeys(term for term in (lookup_term, brain_lookup, searched) if term)
        )
        endpoint = "https://en.wikipedia.org/w/api.php"
        for candidate_term in lookup_terms:
            params = {
                "action": "query",
                "format": "json",
                "redirects": "1",
                "titles": candidate_term,
                "prop": "extracts|info",
                "exintro": "1",
                "explaintext": "1",
                "inprop": "url",
            }
            try:
                response = self.session.get(endpoint, params=params, timeout=(5, 15))
                response.raise_for_status()
                pages = response.json().get("query", {}).get("pages", {})
                page = next(
                    (
                        candidate
                        for candidate in pages.values()
                        if "missing" not in candidate and candidate.get("extract") and candidate.get("fullurl")
                    ),
                    None,
                )
                if not page:
                    continue
                resolved = str(page["fullurl"])
                self._source_validation_cache[resolved] = True
                return resolved
            except Exception:
                continue
        return ""

    def _dedupe_bullets(self, items: List[str], limit: int = 5) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for item in items:
            cleaned = self._trim_text(item, max_chars=170, sentence_mode=True)
            if not cleaned or self._looks_incomplete(cleaned):
                continue
            key = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned)
            if len(out) >= limit:
                break
        return out

    _GENERIC_HISTORY_SUPPORT_MARKERS = (
        "changed daily life through",
        "archaeologists read",
        "the real stakes",
        "evidence from",
        "the key question",
        "what makes",
        "researchers still return",
        "archaeologists still study",
        "the strongest evidence around",
        "the layout of",
        "the decisive detail in",
        "ancient accounts of",
    )

    _HISTORY_SHORT_MIN_FACT_WORDS = 70
    _HISTORY_SHORT_VISUAL_READY_SUBJECTS = frozenset(
        {
            "angkor wat",
            "tikal",
            "chichen itza",
            "great zimbabwe",
            "axum obelisks",
            "olmec colossal heads",
            # Expanded Phase-1 pool (curated facts already pass short budget).
            "nubian pyramids",
            "terrace farming at machu picchu",
            "roman concrete",
            "petra",
            "nazca lines",
            "gobekli tepe",
            "cahokia",
            "teotihuacan",
            "knossos",
            "rosetta stone",
            "pompeii plaster casts",
            "carthage harbor",
            "sogdian merchants",
        }
    )

    def _history_short_specific_bullets(self, items: List[str], limit: int = 6) -> List[str]:
        """Keep only complete, concrete facts suitable for a sourced History Short.

        The generic supporting templates are useful as research prompts, but the
        ScriptWriter deliberately removes them from narration. Counting those
        templates toward the research pack's minimum used to produce 30-63 word
        Shorts after that filtering step. This method makes the research layer
        measure the same factual material that can actually survive editorial QA.
        """
        concrete = [
            item
            for item in items
            if item
            and not any(
                marker in self._clean_text(item).lower()
                for marker in self._GENERIC_HISTORY_SUPPORT_MARKERS
            )
        ]
        return self._dedupe_bullets(concrete, limit=limit)

    def _history_short_fact_budget_ok(self, bullets: List[str]) -> bool:
        # The retention opener is selected from these facts, rather than added on
        # top of them. Requiring 70 factual words leaves enough distinct evidence
        # for an 82+ word narration after that de-duplication and a concise payoff.
        # Thin packs must be rejected here instead of being padded downstream.
        word_count = sum(len(re.findall(r"[A-Za-z0-9']+", item or "")) for item in bullets)
        return len(bullets) >= 4 and word_count >= self._HISTORY_SHORT_MIN_FACT_WORDS

    def _curated_history_short_facts(self, subject: str) -> List[str]:
        return self._history_short_specific_bullets(
            [
                self._override_fact_for_title(subject),
                *self._history_supporting_bullets(subject),
            ],
            limit=6,
        )

    def _history_supporting_bullets(self, title: str) -> List[str]:
        normalized = self._normalize_subject(title)
        display_title = self._clean_text(title).title()
        if "nubian pyramid" in normalized:
            return [
                "Kushite rulers built royal pyramids at sites including El-Kurru, Nuri and Meroe in what is now Sudan.",
                "Many Nubian pyramids have steep, narrow sides, while their burial chambers were cut underground beneath the monuments.",
                "The cemeteries preserve Napatan and Meroitic royal burial traditions that developed south of Egypt along the Nile.",
                "Chapel walls and surviving reliefs connect the monuments with Kushite rulers, deities and funerary ritual.",
            ]
        if "stonehenge" in normalized:
            return [
                "Stonehenge's builders raised circles of sarsen stones and smaller bluestones on Salisbury Plain over many generations.",
                "Some bluestones came from western Wales, showing that moving the monument's material required long-distance planning and labor.",
                "The monument's alignment with the solstices links its architecture to seasonal gatherings, ritual and the movement of the sun.",
            ]
        if "angkor wat" in normalized:
            return [
                "King Suryavarman II commissioned Angkor Wat in early twelfth-century Cambodia as a state temple dedicated to Vishnu.",
                "Five towers, concentric galleries and a broad moat transformed the complex into an architectural image of sacred Mount Meru.",
                "Long sandstone bas-reliefs show court processions and warfare, while Hindu epics connect royal propaganda with religious storytelling.",
                "Buddhist worship continued after the Khmer court changed, keeping Angkor Wat active instead of leaving it an abandoned ruin.",
            ]
        if "mausoleum of the first qin emperor" in normalized or "qin shi huang" in normalized:
            return [
                "The Terracotta Army fills outer burial pits, while the emperor's central tomb chamber remains unexcavated.",
                "Bronze chariots, weapons and thousands of individually modeled soldiers reveal the labor organized around the burial complex.",
                "Archaeologists can map the surrounding pits and walls without opening the tomb mound, preserving evidence that cannot be replaced.",
            ]
        if "masada" in normalized:
            return [
                "Roman camps, a siege wall and a massive assault ramp still surround the fortress at Masada.",
                "The historian Josephus provides the main written account, so archaeologists compare his story with the surviving siegeworks.",
                "Rome's army isolated the plateau, organized thousands of troops and built the ramp that made the final assault possible.",
            ]
        if "machu picchu" in normalized:
            return [
                "Built in the 15th century high in Peru's Andes, Machu Picchu had to survive steep slopes and heavy seasonal rain.",
                "Beneath each terrace, graded layers of stone, gravel and soil drained water before it could destabilize the slope.",
                "Canals carried runoff away while carefully graded surfaces protected buildings from erosion.",
                "The terraces show that the site's dramatic appearance depended on hidden engineering beneath the visible landscape.",
            ]
        if normalized == "tikal" or "tikal " in normalized:
            return [
                "Ancient inscriptions identify the city as Yax Mutal, while modern archaeologists know the ruins as Tikal.",
                "Temples, palaces, reservoirs and causeways supported one of the largest urban centers in the Classic Maya world.",
                "Carved stelae name rulers, date wars and record dynastic claims, allowing comparison between royal messages and excavated evidence.",
                "Temple I beside the Great Plaza became Jasaw Chan K'awiil I's funerary monument, tying royal memory to Tikal's skyline.",
            ]
        if "chichen itza" in normalized:
            return [
                "El Castillo dominated the plaza, turning stairways, terraces and a summit temple into a visible sacred monument.",
                "The Great Ball Court—the largest known in Mesoamerica—linked play with elite ceremony, while carved panels depicted sacrifice.",
                "Human remains and offerings from the Sacred Cenote show that pilgrims brought valuables and performed rituals at the sinkhole.",
                "The Temple of the Warriors fronts a forest of carved columns, linking military imagery with ceremony in the central precinct.",
            ]
        if "great zimbabwe" in normalized:
            return [
                "Builders raised stone walls without mortar. They enclosed homes and elite spaces. Rituals unfolded there too.",
                "Imported beads and ceramics crossed oceans. Indian Ocean trade reached the city. Cattle and gold supported local power.",
                "Its architecture and trade goods disprove colonial claims that African builders could not have created the city.",
                "The Hill Complex, Great Enclosure and Valley Ruins changed between the eleventh and fifteenth centuries as the settlement expanded.",
                "Carved soapstone birds found at the site became powerful symbols of the kingdom and later of modern Zimbabwe.",
            ]
        if "axum" in normalized or "aksum" in normalized:
            return [
                "Aksumite builders raised granite stelae above elite tombs, shaping false doors and windows to imitate multi-storey palaces.",
                "The standing Obelisk of Axum belongs to a fourth-century monument field and shows remarkable precision in one block of granite.",
                "The larger Great Stele fell and shattered in antiquity, while its fragments remain beside underground chambers in Axum's northern field.",
                "Italy removed one major stela in 1937; its later return made the monument part of modern Ethiopian heritage politics.",
            ]
        if "lascaux" in normalized:
            return [
                "Teenagers discovered Lascaux Cave in southwestern France in 1940, revealing chambers covered with Ice Age animal paintings and engravings.",
                "The art was created roughly seventeen thousand years ago and depicts horses, aurochs, deer, bison and other animals with mineral pigments and confident outlines.",
                "Artists used the cave's curves and ledges to give painted bodies volume, showing that the rock surface was part of each composition.",
                "Authorities closed the original cave to the public in 1963 after heat, carbon dioxide and microorganisms linked to heavy visitation threatened the paintings.",
            ]
        if "cyrus cylinder" in normalized:
            return [
                "The Cyrus Cylinder is a barrel-shaped clay inscription written in Akkadian cuneiform after Cyrus captured Babylon in 539 BCE.",
                "Its text presents the Babylonian god Marduk as choosing Cyrus and describes restoring sanctuaries, cult images and displaced communities.",
                "Hormuzd Rassam excavated the cylinder at Babylon in 1879, and the surviving fragments are now held by the British Museum.",
                "Calling it the first human-rights charter projects a modern category onto an ancient royal proclamation written in a long Mesopotamian political tradition.",
            ]
        if "olmec" in normalized and ("head" in normalized or "colossal" in normalized):
            return [
                "Olmec sculptors carved basalt heads between roughly 1200 and 400 BCE at San Lorenzo, La Venta and Tres Zapotes.",
                "Faces and fitted headgear differ, while many archaeologists interpret the monuments as portraits of powerful rulers.",
                "Basalt came from the Tuxtla Mountains, so moving multi-ton boulders to lowland centers required organized labor over long distances.",
                "Seventeen heads are known; distinct faces and helmets make each monument an individual portrait, not a repeated stock image.",
            ]
        if "roman concrete" in normalized or "opus caementicium" in normalized:
            return [
                "Roman builders mixed lime mortar with rubble aggregate, and volcanic ash created hydraulic concrete that could harden in wet conditions.",
                "Harbor works at sites around the Mediterranean used concrete in seawater, allowing piers and breakwaters to extend beyond the shoreline.",
                "The Pantheon's unreinforced dome becomes lighter toward the top as builders changed the aggregate and reduced the structure's thickness.",
                "Roman concrete had no universal recipe: builders adjusted aggregate, ash, and methods to local materials and each structure's purpose.",
            ]
        if "persepolis" in normalized:
            return [
                "Reliefs at Persepolis show delegations bringing gifts from across the Achaemenid Persian Empire.",
                "Workers' tablets record rations and labor, revealing the administration behind the palace terraces and ceremonial display.",
                "Alexander's forces burned parts of Persepolis, but its inscriptions and reliefs still preserve the empire's political message.",
            ]
        if "battle of marathon" in normalized or normalized == "marathon":
            return [
                "The plain at Marathon limited Persian cavalry movement and gave the Athenian-led infantry a decisive tactical opportunity.",
                "Greek forces strengthened their wings, closed quickly and enveloped the Persian center during the battle.",
                "The victory stopped the first Persian invasion of mainland Greece and reshaped the choices both sides made afterward.",
            ]
        if "mohenjo daro" in normalized or "mohenjo-daro" in normalized:
            return [
                "Mohenjo-daro became one of the largest cities of the Indus civilization during the Mature Harappan period in the third millennium BCE.",
                "Its baked-brick streets, neighborhood wells and covered drains show sustained planning for water and waste across a dense urban settlement.",
                "The Great Bath was waterproofed with bitumen inside a major public complex, although its exact civic or ritual purpose remains uncertain.",
                "Standardized weights, seals and specialized craft goods connect the city with wider Indus trade, while its short inscriptions remain undeciphered.",
                "No obvious royal palace or giant named tomb has been identified, so political authority must be reconstructed from buildings, standards and material evidence.",
            ]
        if "indus valley" in normalized or "harappa" in normalized:
            return [
                "The Mature Harappan phase flourished roughly between 2600 and 1900 BCE across parts of present-day Pakistan, northwest India and Afghanistan.",
                "Mohenjo-daro and Harappa show baked-brick streets, wells and drainage channels built for dense urban life.",
                "Dholavira used large reservoirs, channels and carefully planned stone architecture to manage water in the dry landscape of Kutch.",
                "The Great Bath at Mohenjo-daro was sealed with bitumen and placed inside a major public complex, although its exact ritual or civic purpose remains uncertain.",
                "Standard brick proportions appear across distant settlements, suggesting shared building practices without proving a single centralized ruler controlled every city.",
                "Cubical stone weights followed consistent ratios and helped merchants measure goods across a wide trading network.",
                "Indus seals depict animals, symbols and short inscriptions, and some were used to mark goods or identity in trade and administration.",
                "The undeciphered Indus script is a major limit: archaeologists can study objects and cities, but not full written voices.",
                "Mesopotamian texts mention trade with a region called Meluhha, which many scholars connect with the Indus world, while imported materials confirm long-distance exchange.",
                "Carnelian beads, shell objects, copper tools and specialized workshops reveal skilled production moving between cities and coastal routes.",
                "Unlike Egypt or Mesopotamia, the largest Indus cities have not produced obvious royal tombs or giant statues naming individual kings.",
                "That absence does not prove the society had no rulers; it means political organization must be reconstructed from streets, standards, storage, craft and settlement patterns.",
                "After about 1900 BCE, many large cities declined as populations shifted toward smaller settlements and different regional traditions.",
                "Changing rivers, weaker monsoons, disrupted trade and local adaptation all contribute to current explanations, replacing the old idea of one sudden invasion.",
                "Human remains and settlement evidence show regional change rather than one identical collapse happening everywhere at the same moment.",
                "The civilization's planned cities, craft standards and trade links survive clearly, but its undeciphered writing keeps names, offices and political debates beyond direct recovery.",
            ]
        if "kingdom of kush" in normalized or "nubian" in normalized:
            return [
                "Kush grew along the Nile south of Egypt, with capitals such as Kerma, Napata and later Meroe.",
                "Kushite rulers once controlled Egypt as the Twenty-Fifth Dynasty, leaving pyramids, temples and royal inscriptions.",
                "Iron production, Nile trade and local burial traditions made Kush more than a shadow of Egypt.",
            ]
        if "alexander" in normalized and "tyre" in normalized:
            return [
                "Tyre resisted Alexander because the city sat offshore, protected by water and strong walls.",
                "Alexander's army built a causeway from the mainland, turning an island defense into a brutal engineering problem.",
                "The siege mattered because controlling Tyre helped Alexander secure the eastern Mediterranean coast.",
            ]
        if "lachish" in normalized:
            return [
                "Sennacherib captured Lachish in 701 BCE during an Assyrian campaign against the fortified cities of Judah.",
                "Lachish stood on a prominent mound in the Shephelah, where routes linked Judah's hill country with the coastal plain and the road toward Egypt.",
                "The city was one of Judah's chief fortified centers, so taking it damaged both regional defense and control of movement through the lowlands.",
                "Assyrian troops built a stone-and-earth siege ramp against the southwest corner of the city wall so heavy assault engines could reach the defenses.",
                "The Judean defenders raised a counter-ramp inside the wall opposite the Assyrian ramp, turning the assault point into an engineering contest.",
                "Excavations in one attack area recovered 859 arrowheads together with sling stones, armor scales, an iron chain and perforated stones.",
                "Archaeomagnetic study of a burned mudbrick tower supports intense fire during the 701 BCE attack, although who set that fire remains uncertain.",
                "After the conquest, sculptors covered a room in Sennacherib's Southwest Palace at Nineveh with gypsum reliefs narrating the victory.",
                "The Lachish reliefs show archers, slingers, shield bearers and wheeled siege engines advancing while defenders throw stones and burning torches.",
                "Other panels show families leaving with possessions and animals, while captives and executions make Assyrian deportation and terror part of the royal message.",
                "One palace panel places Sennacherib on a throne receiving prisoners and booty, with cuneiform inscriptions identifying the royal victory scene.",
                "The reliefs are detailed evidence for Assyrian tactics, but they were also palace propaganda designed to turn conquest into a permanent image of royal power.",
                "A royal prism, Assyrian campaign records and the Hebrew Bible preserve different accounts of the wider campaign, so agreement and disagreement must be compared claim by claim.",
                "The Level III destruction at Lachish belongs to Sennacherib's campaign, while a later rebuilt city was destroyed by Babylonia in 587 or 586 BCE.",
                "Keeping those destruction layers separate prevents the later Lachish Letters from being mistaken for eyewitness records of the Assyrian siege.",
                "Lachish was rebuilt after the Assyrian conquest, but Judah survived with reduced power and heavier obligations to the empire.",
            ]
        if "oracle of delphi" in normalized or "delphi" in normalized or "pythia" in normalized:
            return [
                "At Delphi, the Pythia delivered Apollo's oracle from a sanctuary that Greek cities treated as politically powerful.",
                "Rulers and city-states consulted Delphi before wars, colonies and reforms, turning prophecy into strategy.",
                "Treasuries, offerings and inscriptions at Delphi show how religion, prestige and competition met in one sacred place.",
            ]
        if "justinianic plague" in normalized or "plague of justinian" in normalized:
            return [
                "The first documented wave reached the Mediterranean world in the 540s during the reign of the emperor Justinian I.",
                "Writers including Procopius and John of Ephesus described severe mortality, disrupted burial and fear in affected cities.",
                "Ancient DNA recovered from early medieval burials identifies Yersinia pestis as the pathogen behind the pandemic.",
                "Historians still debate its total demographic impact because surviving reports, burial evidence and regional records are uneven.",
            ]
        if "bronze age collapse" in normalized or "late bronze age collapse" in normalized:
            return [
                "Around 1200 BCE, several palace-centered states in the eastern Mediterranean were destroyed, abandoned or radically reorganized.",
                "Egyptian inscriptions describe conflicts with groups later called the Sea Peoples, but those records do not explain every regional collapse.",
                "Destruction layers and surviving letters show warfare and disrupted communication, yet the sequence differed from one city to another.",
                "Drought, migration, rebellion, trade disruption and fragile political systems remain competing parts of a multi-cause explanation.",
            ]
        if "boudica" in normalized:
            return [
                "Boudica was queen of the Iceni, a people in Roman Britain pushed into revolt after imperial abuse and seizure of wealth.",
                "Her forces destroyed Roman centers including Camulodunum and Londinium before the rebellion was crushed.",
                "The revolt exposed how quickly Roman control could fracture when taxation, violence and humiliation hit local elites.",
            ]
        if "sogdian" in normalized:
            return [
                "The early fourth-century Ancient Letters were found west of Dunhuang and preserve messages written by merchants and family members in western China.",
                "Their Iranian language became a lingua franca at trading posts stretching deep into China and Mongolia.",
                "Seventh-century murals at Afrasiab in Samarkand depict foreign envoys bringing gifts to the local ruler.",
                "Commercial messages name traded goods including gold, silver, pepper, musk, wheat, silk and other cloth.",
                "Sogdian communities carried religious traditions and artistic styles as well as merchandise between Central Asia and China.",
            ]
        if normalized == "petra" or "petra " in normalized:
            return [
                "Nabataean engineers cut channels, dams and cisterns into Petra's sandstone cliffs to capture rare desert rain.",
                "The Siq canyon controlled every approach to the city, while carved tombs and temples faced the visitor route.",
                "Incense and caravan trade funded the stonework that made Petra a hydraulic and commercial capital.",
                "Plaster-lined pipes and overflow paths show that water control was planned as carefully as monumental façades.",
            ]
        if "gobekli" in normalized or "göbekli" in normalized:
            return [
                "Göbekli Tepe in southeastern Turkey preserves T-shaped limestone pillars carved with animals inside circular enclosures.",
                "Radiocarbon dates place major construction in the Pre-Pottery Neolithic, earlier than farming villages were once expected.",
                "Builders later deliberately buried the enclosures, sealing pillars and fill that excavators now reconstruct phase by phase.",
                "The scale of quarrying and carving suggests organized ritual gatherings before permanent agricultural towns dominated the region.",
            ]
        if "cahokia" in normalized:
            return [
                "Cahokia near present-day St. Louis raised Monks Mound and dozens of earthen platforms beside the Mississippi floodplain.",
                "Woodhenge posts and plaza layouts mark ceremonial calendars while residential neighborhoods spread across a huge urban footprint.",
                "Trade in copper, shell and other goods connected Cahokia to distant regions of North America.",
                "Population decline after about 1200 CE left the mounds as the clearest surviving map of Mississippian political power.",
            ]
        if "teotihuacan" in normalized:
            return [
                "Teotihuacan's Avenue of the Dead aligns the Pyramid of the Sun and Pyramid of the Moon inside a planned urban grid.",
                "Apartment compounds housed thousands of residents while specialized workshops produced obsidian tools and craft goods.",
                "No long king list survives for the city, so scholars debate collective or palace-centered rule from architecture and murals.",
                "Later burning damaged major buildings, but the street plan still preserves one of Mesoamerica's largest planned cities.",
            ]
        if "knossos" in normalized:
            return [
                "The palace complex at Knossos on Crete includes storage magazines, courtyards and frescoed rooms excavated by Arthur Evans.",
                "Evans rebuilt parts in concrete and named spaces boldly, shaping the modern labyrinth image beyond the original ashlar remains.",
                "Linear A tablets from the site remain undeciphered, while later Mycenaean Linear B shows a changed administrative phase.",
                "Earthquake damage and rebuilding phases remind researchers to separate archaeological evidence from reconstructed tourist corridors.",
            ]
        if "rosetta stone" in normalized:
            return [
                "The Rosetta Stone carries the same Ptolemaic decree in hieroglyphic, Demotic and Greek scripts.",
                "French soldiers found the slab near Rashid in 1799, and scholars used repeated royal names in cartouches as reading keys.",
                "Jean-François Champollion's phonetic readings opened Egyptian hieroglyphs to modern historical study.",
                "The decree itself records priestly privileges under Ptolemy V, so the stone is both a political text and a linguistic key.",
            ]
        if "pompeii" in normalized:
            return [
                "When Vesuvius buried Pompeii in 79 CE, ash hardened around bodies and later left hollow cavities excavators could fill with plaster.",
                "The resulting casts preserve final postures, clothing folds and group scenes that ordinary skeletons rarely show so clearly.",
                "Houses nearby still hold bread, tools, graffiti and furniture, tying the casts to everyday Roman life interrupted mid-action.",
                "Modern conservation treats the casts as reconstructions that require ethical display choices as much as scientific care.",
            ]
        if "ziggurat of ur" in normalized or normalized == "ur":
            return [
                "King Ur-Nammu began the ziggurat around 2100 BCE, using a mudbrick core protected by fired-brick facing.",
                "Its height made religious authority visible across the city and tied royal power to the moon god Nanna.",
                "The structure survived because later rulers restored it, leaving inscriptions that link politics to sacred architecture.",
            ]
        if "greek fire" in normalized:
            return [
                "Byzantine sources connect Greek fire to naval warfare, especially ships defending Constantinople from attack.",
                "Its exact formula was guarded closely, but accounts describe a burning liquid projected through siphons.",
                "The weapon mattered because fire that kept burning on water changed how enemies approached Byzantine ships.",
            ]
        if "carthage" in normalized:
            return [
                "Ancient tradition dated Carthage's foundation to 814 BCE, while archaeology places a Phoenician settlement on the Gulf of Tunis by the late ninth century BCE.",
                "Its position in present-day Tunisia connected North African farmland with sea routes through Sicily, Sardinia, Iberia and the central Mediterranean.",
                "The cothon combined a rectangular commercial harbor with a circular military basin and an island command center, turning naval organization into visible architecture.",
                "Carthaginian power rested on shipping, tribute, allied communities, North African agriculture and access to metals and markets farther west.",
                "Competition over Sicily began the First Punic War in 264 BCE and ended with Carthage losing Sicily, paying an indemnity and surrendering much of its naval advantage.",
                "Unpaid troops and rebel communities then pushed Carthage into the Mercenary War, exposing how quickly an overseas military system could become a threat at home.",
                "The Barcid family expanded Carthaginian power in Iberia, building the resources and army that later carried Hannibal's campaign toward Italy.",
                "Hannibal crossed the Alps and defeated several Roman armies, but battlefield victories did not break Rome's alliances or end its ability to raise new forces.",
                "Scipio Africanus forced the war back to North Africa and defeated Hannibal at Zama in 202 BCE, leaving Carthage wealthy enough to recover but politically constrained.",
                "Roman writers later emphasized Carthage's renewed prosperity and the repeated demand that the city be destroyed, but those accounts also served Roman political memory.",
                "The Third Punic War became a siege from 149 to 146 BCE, ending with the city captured, burned and its surviving population killed or enslaved.",
                "The famous claim that Rome salted Carthage's soil is a much later story and is not supported by the surviving ancient accounts.",
                "Rome eventually rebuilt Carthage as a colony, and the new city became one of the largest urban centers in the western Roman Empire.",
                "Modern excavation must separate Punic remains from Roman rebuilding because later streets and monuments transformed the same ground.",
                "Burials in the Tophet remain fiercely debated: some scholars read them as evidence of child sacrifice, while others stress infant mortality and funerary practice.",
                "Ports, inscriptions, imported goods, workshops and domestic remains reveal a society larger and more complicated than the Roman image of a single enemy city.",
            ]
        if "nazca" in normalized or "nasca" in normalized:
            return [
                "The Nazca Lines sit in the desert of southern Peru, where dark surface stones were moved to reveal pale ground.",
                "Geometric paths and animal figures stretch for hundreds of meters across the pampa while nearby pottery helps date Nazca activity.",
                "Survey work shows many lines functioned as walking routes, so ritual procession is a stronger explanation than sky-only viewing.",
                "Extreme aridity preserved the geoglyphs, while modern tracks and tourism still threaten edges that archaeologists must protect.",
            ]
        if any(token in normalized for token in ("battle", "siege", "sack")):
            return [
                f"The decisive detail in {title} was the mix of engineering, supply, terrain and military pressure.",
                f"Ancient accounts of {title} preserve tactics and leadership choices, but the physical setting explains why it mattered.",
            ]
        if any(token in normalized for token in ("tablets", "scrolls", "stone", "code", "library")):
            return [
                f"What makes {title} valuable is the direct evidence preserved from its own time instead of later legend.",
                f"Researchers still return to {title} because the evidence shows how people wrote, ruled, traded or remembered their world.",
            ]
        if any(token in normalized for token in ("tomb", "mausoleum", "army", "city", "petra", "pompeii", "mechanism", "lines")):
            return [
                f"Archaeologists still study {title} because new finds keep adding clues about how it was built, used or preserved.",
                f"The strongest evidence around {title} comes from the physical remains, so each discovery can change the story.",
                f"The layout of {title} matters because it turns power into architecture, guarded space and buried labor.",
            ]
        options = [
            f"{display_title} changed daily life through food, trade, labor, belief, warfare, or written records.",
            f"Archaeologists read {display_title} through traces people left behind: tools, buildings, routes, burials, or inscriptions.",
            f"The real stakes of {display_title} were practical: safety, work, wealth, status, and survival.",
            f"Evidence from {display_title} shows what people built, moved, buried, recorded, or tried to protect.",
            f"The key question in {display_title} is who benefited, who paid the cost, and what changed afterward.",
        ]
        return random.sample(options, k=2)

    def _history_detail_sentences(self, title: str, limit: int = 8) -> List[str]:
        cache_key = f"{self._normalize_subject(title)}:{limit}"
        if cache_key in self._history_detail_cache:
            return list(self._history_detail_cache[cache_key])
        endpoint = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "format": "json",
            "redirects": "1",
            "titles": title,
            "prop": "extracts",
            "explaintext": "1",
        }
        extract = ""
        for _ in range(3):
            try:
                response = self.session.get(endpoint, params=params, timeout=30)
                response.raise_for_status()
                pages = response.json().get("query", {}).get("pages", {})
                extract = str(next(iter(pages.values()), {}).get("extract", ""))
                if extract:
                    break
            except Exception:
                continue
        if not extract:
            return []
        extract = re.sub(r"=+[^=]+?=+", " ", extract)

        subject_tokens = {
            token
            for token in re.findall(r"[a-z]{4,}", self._normalize_subject(title))
            if token not in {"ancient", "battle", "city", "empire", "kingdom", "siege"}
        }
        low_value = (
            "is located", "is part of", "is one of", "was declared", "world heritage site",
            "national park", "department in", "province of", "municipality", "coordinates",
            "may refer to", "disambiguation",
            "a recent study using",
            "museum was founded", "museum has exhibits", "open air museum", "was first surveyed",
            "excavations were performed", "under the auspices of unesco",
        )
        evidence_terms = (
            "archaeolog", "artifact", "burial", "built", "carved", "constructed", "destroyed",
            "discovered", "excavat", "inscription", "king", "queen", "ruler", "dynasty", "army",
            "battle", "siege", "trade", "temple", "palace", "stone", "bronze", "manuscript",
            "restored", "evidence", "relief", "tomb", "wall", "road", "harbor", "harbour",
        )
        transition_terms = ("because", "but", "however", "after", "before", "survived", "until")
        ranked: List[tuple[int, int, str]] = []
        long_form = limit > 8
        sentence_limit = 120 if long_form else 60
        minimum_score = 2 if long_form else 4
        for index, sentence in enumerate(self._split_sentences(extract)[:sentence_limit]):
            if long_form:
                cleaned = self._clean_text(sentence)
                if len(cleaned) > 260:
                    continue
                cleaned = self._finalize_sentence(cleaned)
            else:
                cleaned = self._trim_text(sentence, max_chars=185, sentence_mode=True)
            lowered = cleaned.lower()
            if not cleaned or self._looks_incomplete(cleaned) or any(term in lowered for term in low_value):
                continue
            if re.search(r"[\u0370-\u03ff\u0400-\u052f\u0590-\u08ff\u0900-\u0fff\u3040-\u30ff\u3400-\u9fff]", cleaned):
                continue
            if re.search(r"\b(?:in|at|from|within) what is now\.?$", lowered):
                continue
            if "child sacrifice" in lowered and not any(term in lowered for term in ("debate", "disput", "controvers")):
                continue
            if re.search(
                r"\b(?:achaemenid|roman|persian|byzantine|maya|mauryan|assyrian|nabataean|punic|phoenician)\.?$",
                lowered,
            ):
                continue
            tokens = set(re.findall(r"[a-z]{4,}", lowered))
            score = 0
            score += 4 if re.search(r"\b\d{3,4}\b|\b\d{1,2}(?:st|nd|rd|th) century\b", lowered) else 0
            score += sum(2 for term in evidence_terms if term in lowered)
            score += sum(1 for term in transition_terms if term in lowered)
            score += 2 if not subject_tokens or tokens & subject_tokens else 0
            if score >= minimum_score:
                ranked.append((score, index, cleaned))

        selected = sorted(sorted(ranked, key=lambda item: (-item[0], item[1]))[:limit], key=lambda item: item[1])
        result = self._dedupe_bullets([item[2] for item in selected], limit=limit)
        if result:
            self._history_detail_cache[cache_key] = list(result)
        return result

    def _history_visual_queries(self, title: str, subject: str = "") -> List[str]:
        normalized = self._normalize_subject(" ".join([title, subject]))
        if "nubian pyramid" in normalized:
            return [
                "Nubian pyramids Meroe Sudan archaeological site",
                "Pyramids of Meroe Sudan Kush royal tombs",
                "Nuri pyramids Sudan ancient Kush",
                "El Kurru pyramids Sudan archaeology",
            ]
        if "angkor wat" in normalized:
            return [
                "Angkor Wat Cambodia temple towers",
                "Angkor Wat sandstone bas relief",
                "Angkor Wat moat causeway Cambodia",
                "Angkor Wat Vishnu gallery archaeology",
            ]
        if normalized == "tikal" or " tikal" in normalized:
            return [
                "Tikal Guatemala Maya Great Plaza",
                "Tikal Temple I Guatemala",
                "Tikal Maya stela inscription",
                "Tikal Temple IV rainforest",
            ]
        if "chichen itza" in normalized:
            return [
                "Chichen Itza El Castillo Yucatan",
                "Chichen Itza Great Ball Court",
                "Chichen Itza Sacred Cenote",
                "Chichen Itza Maya temple columns",
            ]
        if "great zimbabwe" in normalized:
            return [
                "Great Zimbabwe Great Enclosure walls",
                "Great Zimbabwe Hill Complex",
                "Great Zimbabwe conical tower",
                "Zimbabwe Bird soapstone sculpture",
            ]
        if "mohenjo" in normalized or "indus valley" in normalized:
            return [
                "Mohenjo-daro Pakistan Great Bath",
                "Mohenjo-daro baked brick streets drains",
                "Mohenjo-daro Indus seal artifact",
                "Mohenjo-daro archaeological ruins",
            ]
        if "axum" in normalized or "aksum" in normalized:
            return [
                "Obelisk of Axum Ethiopia stelae field",
                "Aksum northern stelae park tombs",
                "Great Stele Axum Ethiopia",
                "Axum stela carved false doors windows",
            ]
        if "lascaux" in normalized:
            return [
                "Lascaux cave paintings Hall of Bulls",
                "Lascaux horse painting Paleolithic",
                "Lascaux cave bison painting",
                "Lascaux cave replica France",
            ]
        if "cyrus cylinder" in normalized:
            return [
                "Cyrus Cylinder British Museum cuneiform",
                "Cyrus Cylinder clay inscription Babylon",
                "Cyrus Cylinder Akkadian cuneiform detail",
                "Cyrus the Great Babylon relief artifact",
            ]
        if "olmec" in normalized and ("head" in normalized or "colossal" in normalized):
            return [
                "Olmec colossal head San Lorenzo",
                "Olmec colossal head La Venta",
                "Olmec colossal head Tres Zapotes",
                "Olmec basalt head Mexico museum",
            ]
        if "roman concrete" in normalized:
            return [
                "Roman concrete Pantheon dome construction",
                "opus caementicium Roman wall concrete",
                "Roman concrete harbor archaeology",
                "Roman pozzolana concrete vault",
            ]
        if "nazca" in normalized or "nasca" in normalized:
            return [
                "Nazca Lines Peru aerial geoglyph desert",
                "Nazca Lines Peru UNESCO geoglyphs",
                "Nazca desert Peru archaeology South America",
                "Nasca Lines Peru pampas geoglyphs",
            ]
        if "carthage" in normalized:
            return [
                "Carthage Tunisia archaeological site",
                "Carthage ruins Tunisia Punic harbor",
                "Carthage Phoenician Punic archaeology",
                "Hannibal Carthage ancient history",
            ]
        if "teutoburg" in normalized:
            return [
                "Teutoburg Forest Germany Roman legions battle",
                "Battle of Teutoburg Forest Germania archaeology",
                "Varus battle Teutoburg Forest Roman army",
                "Kalkriese museum Teutoburg Forest finds",
            ]
        if "thermopylae" in normalized:
            return [
                "Thermopylae Greece mountain pass Spartans Persians",
                "Battle of Thermopylae Greece archaeology",
                "Spartan hoplite Persian war Thermopylae",
                "Thermopylae pass Greece historical site",
            ]
        if "greek fire" in normalized:
            return [
                "Byzantine Greek fire manuscript",
                "Greek fire Byzantine navy illustration",
                "Byzantine ship medieval fire siphon",
                "Constantinople Byzantine navy historical art",
            ]
        if "justinianic plague" in normalized or "plague of justinian" in normalized:
            return [
                "Plague of Justinian Byzantine manuscript",
                "Emperor Justinian mosaic Ravenna",
                "Yersinia pestis ancient DNA archaeology",
                "Procopius Byzantine manuscript plague",
                "Byzantine Empire map sixth century",
            ]
        if "petra" in normalized:
            return [
                "Petra Jordan Nabataean rock city",
                "Petra Treasury Jordan archaeology",
                "Nabataean Petra desert ruins",
                "Petra ancient trade route",
            ]
        if "dead sea scrolls" in normalized or "qumran" in normalized:
            return [
                "Dead Sea Scrolls Qumran caves manuscripts",
                "Qumran caves Dead Sea Scrolls parchment",
                "Dead Sea Scrolls museum manuscript fragments",
                "Judean Desert Qumran archaeology",
            ]
        if "antikythera" in normalized:
            return [
                "Antikythera mechanism ancient Greek bronze gears",
                "Antikythera mechanism museum artifact",
                "ancient Greek astronomical calculator Antikythera",
                "Antikythera shipwreck mechanism fragments",
            ]
        if "hammurabi" in normalized:
            return [
                "Code of Hammurabi stele Louvre basalt law code",
                "Hammurabi Babylonian law stele",
                "ancient Babylon Code of Hammurabi artifact",
                "Mesopotamia Babylon Hammurabi relief",
            ]
        if "alexandria" in normalized:
            return [
                "Library of Alexandria ancient Egypt illustration",
                "Alexandria Egypt ancient library scholarship",
                "ancient Alexandria lighthouse manuscript",
                "Hellenistic Alexandria Egypt archaeology",
            ]
        return [title, f"{title} archaeology", f"{title} artifact", "historical documentary"]

    def _brain_lens_fact_extras(self, item: dict | None, expected_subject: str = "") -> List[str]:
        if not item:
            return []
        expected = self._normalize_subject(expected_subject)
        item_title = self._normalize_subject(str(item.get("title", "")))
        if expected and item_title and expected not in item_title and item_title not in expected:
            return []
        extras: List[str] = []
        for fact in self._split_sentences(item.get("summary", "")):
            cleaned = self._trim_text(fact, max_chars=165, sentence_mode=True)
            if cleaned and not self._looks_incomplete(cleaned):
                extras.append(cleaned)
        return self._dedupe_bullets(extras, limit=2)

    def wikipedia_page_images(self, title: str, limit: int = 6) -> List[str]:
        endpoint = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "format": "json",
            "redirects": "1",
            "titles": title,
            "prop": "images",
            # Article pages often list logos, maps and navigation graphics before
            # documentary photographs. Scan a deeper, still-bounded page set so a
            # visually rich subject is not reduced to the first one or two files.
            "imlimit": str(min(200, max(limit * 8, 50))),
        }
        try:
            r = self.session.get(endpoint, params=params, timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []

        _SKIP = (
            "icon", "logo", "map", "seal", "symbol", "svg", "flag",
            "coat", "arms", "emblem", "crest", "badge", "border", "locator",
            "portrait", "headshot", "official", "thumb", "commons-logo",
            "signature", "stamp", "medal", "ribbon", "button", "blank",
            "graphic", "diagram", "chart", "schematic", "route", "plan",
        )
        image_titles = []
        for page in data.get("query", {}).get("pages", {}).values():
            for item in page.get("images", []) or []:
                img_title = str(item.get("title", ""))
                low = img_title.lower()
                if not low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    continue
                if any(skip in low for skip in _SKIP):
                    continue
                image_titles.append(img_title)

        if not image_titles:
            return []

        image_titles = list(dict.fromkeys(image_titles))[: min(50, max(limit * 4, 24))]
        params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(image_titles),
            "prop": "imageinfo",
            "iiprop": "url|size",
        }
        try:
            r = self.session.get(endpoint, params=params, timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []

        urls: List[str] = []
        for page in data.get("query", {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            url = str(info.get("url", ""))
            low = url.lower()
            if not low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            # Reject portrait-ratio very-wide images (flags, banners)
            w = info.get("width", 0) or 0
            h = info.get("height", 0) or 0
            if h > 0 and w > 0:
                aspect = w / h
                if aspect > 2.5 or aspect < 0.3:  # skip panoramas and thin banners
                    continue
            urls.append(url)
            if len(urls) >= limit:
                break
        return urls

    def _wikipedia_search_title(self, term: str) -> str:
        """The closest real article title, or "".

        Topic titles are written as headlines -- "Why Nazca Lines still matters
        to historians" -- and no encyclopedia has a page under that name. Asked
        for the exact title and told no, the planner concluded the subject had
        no source, rejected it, and proposed the same headline again: twelve
        times in a row on one build. Searching for the words inside the
        headline finds the page the headline is about.
        """
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", str(term or "")) if w.lower() not in _HEADLINE_FILLER]
        query = " ".join(words[:6]).strip()
        if not query:
            return ""
        try:
            response = self.session.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "format": "json", "list": "search", "srsearch": query, "srlimit": 1},
                timeout=20,
            )
            response.raise_for_status()
            hits = response.json().get("query", {}).get("search", []) or []
        except Exception:
            return ""
        return str(hits[0].get("title") or "") if hits else ""

    def wikipedia_summary(self, term: str) -> dict | None:
        data: dict = {}
        lookup_term = self.history_wikipedia_aliases.get(self._normalize_subject(term), term)
        term_variants = list(dict.fromkeys([lookup_term, self._display_subject_title(lookup_term)]))
        for candidate in term_variants:
            url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(candidate)}"
            try:
                r = self.session.get(url, timeout=20)
                r.raise_for_status()
                data = r.json()
                if data.get("extract"):
                    break
            except Exception:
                continue

        if not data.get("extract"):
            # Last resort: the title may be a headline rather than a page name.
            searched = self._wikipedia_search_title(lookup_term)
            if searched and searched.lower() != str(lookup_term or "").lower():
                lookup_term = searched
            endpoint = "https://en.wikipedia.org/w/api.php"
            params = {
                "action": "query",
                "format": "json",
                "redirects": "1",
                "titles": lookup_term,
                "prop": "extracts|info",
                "explaintext": "1",
                "exintro": "1",
                "inprop": "url",
            }
            try:
                r = self.session.get(endpoint, params=params, timeout=25)
                r.raise_for_status()
                pages = r.json().get("query", {}).get("pages", {})
                page = next((item for item in pages.values() if item.get("extract")), {})
                data = {
                    "title": page.get("title", lookup_term),
                    "extract": page.get("extract", ""),
                    "content_urls": {"desktop": {"page": page.get("fullurl", "")}},
                }
            except Exception:
                return None

        extract = self._clean_text(str(data.get("extract", "")))
        title = self._clean_text(str(data.get("title", lookup_term))) or lookup_term
        if not extract:
            return None

        image_urls = []
        lead_image = data.get("originalimage", {}).get("source") or data.get("thumbnail", {}).get("source", "")
        if lead_image:
            image_urls.append(str(lead_image))
        image_urls.extend(self.wikipedia_page_images(title, limit=14))
        image_urls = list(dict.fromkeys([u for u in image_urls if u]))
        resolved_page_url = str(data.get("content_urls", {}).get("desktop", {}).get("page", "") or "")
        if resolved_page_url:
            self._source_validation_cache[resolved_page_url] = True

        return {
            "title": title,
            "summary": extract,
            "url": resolved_page_url,
            "image_url": image_urls[0] if image_urls else "",
            "image_urls": image_urls[:14],
            "bullets": self._clip(self._split_sentences(extract), limit=4),
        }

    def history_pack(
        self,
        terms: List[str],
        avoid_subjects: set[str] | None = None,
        content_kind: str = "short",
    ) -> dict:
        """Keep the history channel locked to curated historical subjects instead of generic trend pages."""
        used = self._load_used_topics("ancient_history")
        avoid_subjects = {self._normalize_subject(item) for item in (avoid_subjects or set()) if item}
        pool = [
            subject
            for subject in self.history_subjects
            if not self._is_duplicate_subject(subject, used)
            and not self._is_duplicate_subject(subject, avoid_subjects)
        ]
        if not pool:
            pool = [subject for subject in self.history_subjects if not self._is_duplicate_subject(subject, avoid_subjects)]
        if not pool:
            pool = self.history_subjects[:]

        random.shuffle(pool)
        forced_subject = self._normalize_subject(
            os.getenv("YT_FORCE_HISTORY_SUBJECT", "")
        )
        if forced_subject:
            forced_match = next(
                (
                    subject
                    for subject in self.history_subjects
                    if self._normalize_subject(subject) == forced_subject
                ),
                "",
            )
            if forced_match and not self._is_duplicate_subject(
                forced_match,
                avoid_subjects,
            ):
                # Local QA can request one deterministic subject without changing
                # normal scheduled randomness. Later planning attempts receive
                # the first subject in ``avoid_subjects`` and continue normally.
                pool = [
                    forced_match,
                    *[
                        subject
                        for subject in pool
                        if self._normalize_subject(subject) != forced_subject
                    ],
                ]
        if content_kind == "short":
            pool = [
                subject
                for subject in pool
                if self._normalize_subject(subject) in self._HISTORY_SHORT_VISUAL_READY_SUBJECTS
            ]
            if not pool:
                pool = [
                    subject
                    for subject in self.history_subjects
                    if self._normalize_subject(subject) in self._HISTORY_SHORT_VISUAL_READY_SUBJECTS
                    and not self._is_duplicate_subject(subject, avoid_subjects)
                ]
                random.shuffle(pool)
            # Prefer subjects with enough locally curated facts to remain useful
            # even when Wikipedia's full-extract endpoint is temporarily down.
            # The shuffled order is preserved inside each group.
            pool = sorted(
                pool,
                key=lambda subject: not self._history_short_fact_budget_ok(
                    self._curated_history_short_facts(subject)
                ),
            )
        for subject in pool[:15]:
            item = self.wikipedia_summary(subject)
            if not item:
                continue
            title = item.get("title", subject)
            if self._is_duplicate_subject(title, used) or self._is_duplicate_subject(title, avoid_subjects):
                continue
            bullets = []
            first_fact = self._override_fact_for_title(subject) or self._override_fact_for_title(title) or self._first_clean_fact(item)
            if first_fact:
                bullets.append(first_fact)
            supporting = self._history_supporting_bullets(title)
            generic_support = any(
                marker in " ".join(supporting).lower()
                for marker in (
                    "changed daily life through", "archaeologists read", "the real stakes",
                    "evidence from", "the key question", "archaeologists still study",
                    "the strongest evidence around", "the layout of", "the decisive detail in",
                    "ancient accounts of",
                )
            )
            detail_limit = 18 if content_kind == "video" else 8
            bullet_limit = 16 if content_kind == "video" else 5
            details = self._history_detail_sentences(title, limit=detail_limit)
            if not generic_support:
                bullets.extend(supporting)
            bullets.extend(details)
            bullets.extend(item.get("bullets", []))
            if content_kind == "video" and len(bullets) < 10:
                bullets.extend(
                    self._split_sentences(str(item.get("summary") or ""))[:12]
                )
            # Generic context prompts are never researched facts. Long videos
            # previously appended them after the Wikipedia details, allowing a
            # filler line such as "the real stakes ... status" to reach the
            # editorial gate and reject otherwise source-rich subjects.
            if generic_support and content_kind != "video":
                bullets.extend(supporting)
            if content_kind == "short":
                bullets = self._history_short_specific_bullets(bullets, limit=6)
                if not self._history_short_fact_budget_ok(bullets):
                    # A verified URL alone is not enough: if the page summary
                    # yielded too little concrete detail, try another researched
                    # subject instead of handing generic filler to ScriptWriter.
                    continue
            else:
                bullets = self._dedupe_bullets(bullets, limit=bullet_limit)
            if len(bullets) < 3:
                continue
            return {
                "headline": self._display_subject_title(title),
                "bullets": bullets,
                "sources": self.enrich_source_urls(
                    subject or title,
                    [
                        item.get("url") or self._default_source_url(title),
                        *self.history_source_overrides.get(
                            self._normalize_subject(subject),
                            self.history_source_overrides.get(
                                self._normalize_subject(title),
                                [],
                            ),
                        ),
                    ],
                    limit=4,
                ),
                "visual_queries": self._history_visual_queries(title, subject),
                "image_urls": item.get("image_urls", [])[:14 if content_kind == "video" else 8],
                "subject": subject,
            }

        fallback_subject = pool[0] if pool else (terms[0].title() if terms else "Ancient history")
        if content_kind == "short":
            # The live pool may contain only unused subjects whose curated packs are
            # too thin. Recycle an older source-safe subject if needed, but never
            # append the raw catalog without reapplying this build's avoid set; doing
            # that caused the first ready subject to repeat on every planning attempt.
            # When in-session avoids exhausted every visual-ready subject, allow one
            # recycle pass against published/used subjects only so caption rejects
            # do not force continuity-only builds.
            fallback_candidates = [
                subject
                for subject in dict.fromkeys([*pool, *self.history_subjects])
                if not self._is_duplicate_subject(subject, avoid_subjects)
                and self._normalize_subject(subject) in self._HISTORY_SHORT_VISUAL_READY_SUBJECTS
            ]
            if not fallback_candidates:
                # In-session caption rejects should not exhaust the whole short
                # pool into continuity-only mode. Recycle unused visual-ready
                # subjects even if this build already rejected their first draft.
                fallback_candidates = [
                    subject
                    for subject in dict.fromkeys([*pool, *self.history_subjects])
                    if not self._is_duplicate_subject(subject, used)
                    and self._normalize_subject(subject)
                    in self._HISTORY_SHORT_VISUAL_READY_SUBJECTS
                ]
            ready_fallback = next(
                (
                    (subject, facts)
                    for subject in fallback_candidates
                    if self._history_short_fact_budget_ok(
                        facts := self._curated_history_short_facts(subject)
                    )
                ),
                None,
            )
            if ready_fallback is None:
                raise SourceSafeResearchExhaustedError(
                    "No source-safe Ancient History Short has enough concrete facts"
                )
            fallback_subject, fallback_bullets = ready_fallback
        else:
            fallback_fact = self._override_fact_for_title(fallback_subject) or f"{fallback_subject} is still remembered because historians treat it as an important piece of the ancient record."
            fallback_bullets = self._dedupe_bullets(
                [fallback_fact, *self._history_supporting_bullets(fallback_subject)],
                limit=4,
            )
        return {
            "headline": fallback_subject.title(),
            "bullets": fallback_bullets,
            "sources": self.enrich_source_urls(
                fallback_subject,
                [self._default_source_url(fallback_subject)],
                limit=4,
            ),
            "visual_queries": self._history_visual_queries(fallback_subject, fallback_subject),
            "image_urls": [],
            "subject": fallback_subject,
        }

    def _story_score(self, title: str) -> int:
        low = title.lower()
        good = ["bizarre", "weird", "funny", "viral", "shocking", "strange", "caught", "true", "real", "mystery", "odd"]
        bad = ["practice", "association", "doctor", "medical", "workflow", "dispatch", "preview", "guide", "review", "market"]
        score = sum(2 for w in good if w in low) - sum(3 for w in bad if w in low)
        if 35 <= len(title) <= 110:
            score += 1
        return score

    def _brain_lens_category(self, subject: str) -> str:
        normalized = self._normalize_subject(subject)
        for category, subjects in self.brain_lens_category_map.items():
            if normalized in subjects:
                return category
        return "general"

    def _display_subject_title(self, subject: str) -> str:
        words = re.split(r"\s+", self._normalize_subject(subject))
        small = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}
        out = []
        for idx, word in enumerate(words):
            if not word:
                continue
            if idx > 0 and idx < len(words) - 1 and word in small:
                out.append(word)
            else:
                out.append(word.capitalize())
        return " ".join(out).strip() or subject.title()

    def _brain_lens_headline_options(self, title: str, category: str) -> List[str]:
        subject = re.sub(r"\s+versus\s+", " vs ", (title or "").lower()).strip()
        topic_headlines = {
            "almost relationships": ["Why an almost-relationship can hurt more than a breakup", "The unfinished relationship your brain keeps replaying"],
            "almost kiss tension": ["Why the almost-kiss stays in your head", "The unfinished second that makes attraction feel electric"],
            "anxious attachment": ["Why one late reply can overpower a week of care", "The texting moment that wakes up anxious attachment"],
            "attachment anxiety after texting": ["Why a slow reply can trigger attachment anxiety", "The texting loop that makes silence feel like rejection"],
            "avoidant attachment": ["Why closeness can trigger distance after a great date", "When a perfect date is followed by sudden distance"],
            "body language": ["The body-language cue that separates warmth from attraction", "The attraction cue people notice before they think"],
            "chemistry vs compatibility": ["The chemistry test that predicts nothing about compatibility", "Why instant chemistry can hide a bad match"],
            "eye contact attraction": ["Why one extra second of eye contact feels electric", "The eye-contact cue that makes attraction feel mutual"],
            "flirting body language": ["The flirting cue that appears before either person speaks", "The body-language shift that quietly signals interest"],
            "late night texting": ["Why late-night texts feel more intimate than daytime plans", "The midnight texting effect that makes feelings feel bigger"],
            "micro flirting": ["The micro-flirting cue people notice before they think", "The tiny flirting signal that changes the whole conversation"],
            "mixed signals": ["The mixed signal that keeps your attention hooked", "Why hot-and-cold attention is so hard to ignore"],
            "situationship anxiety": ["Why a situationship can control your mood so quickly", "The situationship loop that keeps you checking for clarity"],
            "texting anxiety": ["Why one slow reply can change your whole mood", "The texting habit that turns silence into a threat"],
            "voice change attraction": [
                "What voice changes can signal about attraction",
                "Why a voice may shift around someone you like",
            ],
            "breadcrumbing": ["Why one perfect text can restart your hope", "The breadcrumb that keeps you waiting for a relationship"],
            "chemistry anxiety": ["When chemistry feels exciting and quietly makes you anxious", "The difference between chemistry and an anxiety spike"],
            "emotional availability": ["The green flag that makes attraction feel calm", "The emotional-availability cue that ends the guessing"],
            "fear of being too much": ["Why you shrink your needs when you really like someone", "The dating fear that makes honesty feel dangerous"],
            "flirting anxiety": ["Why eye contact can erase every clever line", "The confident flirting reset that is not a pickup line"],
            "love bombing": ["Why love bombing feels like instant compatibility", "The intensity test that separates romance from love bombing"],
            "mixed attachment signals": ["Why warmth followed by distance feels so addictive", "The attachment signal that keeps changing the answer"],
            "push pull dynamic": ["Why hot-and-cold attention feels so hard to leave", "The push-pull loop that makes relief feel like romance"],
            "romantic uncertainty": ["Why uncertainty can feel stronger than actual intimacy", "The romantic unknown that keeps your brain checking"],
            "self respect in dating": ["The dating move that protects attraction and self-respect", "How to stay interested without abandoning your standards"],
            "silent treatment": ["Why sudden silence can feel physically threatening", "Silent treatment or healthy space? One signal tells you"],
            "reply time anxiety": ["Why reply time starts feeling like a measure of your worth", "The reply-time trap that changes your whole mood"],
            "rejection sensitivity": ["Why one canceled plan can feel like rejection", "The rejection alarm that turns silence into proof"],
            "talking stage anxiety": ["Why the talking stage can control your whole mood", "The undefined connection that keeps your brain checking"],
            "post date overthinking": ["Why you replay every second after a great date", "The post-date spiral that turns chemistry into doubt"],
            "dry texting": ["When dry texts feel colder than they really are", "The short reply that suddenly changes your whole mood"],
            "double texting anxiety": ["Why sending a second text can feel so exposing", "The double-texting fear that makes confidence disappear"],
            "slow fading": ["The slow fade that keeps you hoping for one more date", "Why disappearing gradually can hurt more than a clear no"],
            "future faking": ["The future promise that feels romantic but proves nothing", "Future faking or real intention? Watch what happens next"],
            "benching in dating": ["The dating pattern that keeps you available but never chosen", "How occasional attention quietly puts you on the bench"],
            "orbiting after breakup": ["Why an ex watching every story can restart your hope", "The post-breakup signal that looks intimate but changes nothing"],
            "ghosting recovery": ["Why ghosting keeps your brain searching for an ending", "The missing explanation that makes ghosting hard to release"],
            "closure seeking": ["Why one final conversation rarely creates perfect closure", "The closure trap that keeps emotional access open"],
            "limerence": ["When a crush becomes the story your brain cannot stop writing", "Why uncertainty can turn attraction into limerence"],
            "crush idealization": ["Why your crush can seem perfect before you know them", "The missing information your fantasy fills in for a crush"],
            "attraction to unavailable people": ["Why unavailable people can feel safer to desire", "The distance that makes attraction feel more intense"],
            "fear of intimacy": ["Why real closeness can trigger distance after a perfect date", "The intimacy alarm that appears when feelings become mutual"],
            "first date nerves": ["Why one pause on a first date feels so important", "The first-date reset that makes nervousness look confident"],
            "first kiss nerves": ["Why the second before a first kiss feels electric", "The first-kiss cue that protects chemistry and consent"],
            "dating app choice overload": ["Why more dating matches can make connection feel harder", "The dating-app loop that makes everyone feel replaceable"],
            "online dating burnout": ["Why dating apps make everyone feel replaceable", "Dating-app burnout: the reset that brings curiosity back"],
            "exclusivity conversation anxiety": ["Why asking for exclusivity can feel like risking everything", "The clarity conversation that ends the talking-stage guessing"],
            "emotional intimacy versus oversharing": ["Intimacy or oversharing? The difference is what happens next", "Why instant vulnerability can feel deeper than the trust"],
            "vulnerability reciprocity": ["The response that tells you vulnerability is emotionally safe", "Why mutual openness feels intimate without becoming a test"],
            "conflict repair": ["The five minutes after conflict that predict relationship safety", "Why repair matters more than never arguing"],
            "apology consistency": ["The apology test that reveals whether anything will change", "Why perfect words mean little without different behavior"],
            "relationship pacing": ["Why slowing the pace can make attraction stronger", "The dating pace that protects chemistry from fantasy"],
            "mutual effort in dating": ["The effort gap that reveals who is carrying the connection", "Why returned effort feels calmer than being chased"],
            "secure attraction": ["Why secure attraction can feel quieter but grow stronger", "The green flag that keeps chemistry without the guessing"],
            "emotional safety in dating": ["The dating signal that makes honesty feel emotionally safe", "Why emotional safety can be more attractive than intensity"],
            "social media jealousy": ["The social-media clue your jealousy may be misreading", "Why one story view can trigger a whole relationship fear"],
            "checking an ex online": ["Why checking an ex gives relief and then pulls you back", "The online clue that keeps an old relationship emotionally open"],
            "rebound chemistry": ["Why rebound chemistry can feel stronger than expected", "The attraction test that separates healing from escape"],
            "canceled date anxiety": ["Why one canceled date can feel like total rejection", "The follow-up signal that matters more than the cancellation"],
            "voice note intimacy": ["Why one voice note can feel more intimate than fifty texts", "The tiny vocal cue that makes a message feel personal"],
            "late night vulnerability": ["Why late-night honesty can feel like instant intimacy", "The midnight conversation that makes feelings feel bigger"],
            "friends with benefits boundaries": ["The boundary talk friends with benefits should not skip", "When casual chemistry starts changing the emotional rules"],
            "sexual tension versus compatibility": ["Why sexual tension can hide a compatibility problem", "The chemistry test desire alone cannot pass"],
            "kissing chemistry": ["What kissing chemistry reveals—and what it cannot", "Why a great kiss does not automatically mean a great match"],
            "friendship to romance tension": ["The moment friendship starts feeling like romantic tension", "How to read chemistry when a friendship begins to change"],
            "consent and chemistry": ["Why clear consent can make chemistry feel stronger", "The confident question that protects attraction and trust"],
        }
        if subject in topic_headlines:
            return topic_headlines[subject]
        options = [
            f"Why {subject} feels so hard to ignore",
            f"The tiny moment that exposes {subject}",
            f"Why {subject} hits harder at night",
            f"What {subject} reveals before you say a word",
        ]
        extras = {
            "bias": [
                f"Why {subject} makes the wrong person look perfect",
                f"The quiet way {subject} changes attraction",
                f"Why {subject} feels logical when it is misleading",
            ],
            "focus": [
                f"Why {subject} steals focus from the person in front of you",
                f"The attention leak behind {subject}",
            ],
            "emotion": [
                f"Why {subject} feels like chemistry when it is stress",
                f"The body cue that reveals {subject}",
            ],
            "habit": [
                f"Why {subject} keeps pulling you back",
                f"The reward loop behind {subject}",
            ],
            "social": [
                f"Why {subject} can feel hot and confusing",
                f"The dating cue behind {subject}",
                f"Why {subject} can feel addictive",
                f"What {subject} says about attraction",
                f"The attractive reset inside {subject}",
                f"Why {subject} can feel like chemistry",
            ],
            "general": [
                f"Why {subject} feels personal so fast",
                f"The hidden loop behind {subject}",
            ],
        }
        ordered = []
        seen = set()
        for headline in extras.get(category, []) + options:
            key = headline.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(headline)
        return ordered

    def _brain_lens_supporting_bullets(self, subject: str, category: str) -> List[str]:
        templates = {
            "bias": [
                f"{subject} shapes judgment before most people realize it is happening.",
                f"In real life, {subject} can affect confidence, relationships and fast decisions.",
                f"Once you spot {subject}, everyday choices and opinions become easier to decode.",
            ],
            "focus": [
                f"{subject} affects attention, memory and mental energy more than people expect.",
                f"In daily life, {subject} can make simple choices feel heavier and focus feel weaker.",
                f"Once you notice {subject}, it becomes easier to protect attention and think clearly.",
            ],
            "emotion": [
                f"{subject} can change how people react to pressure, conflict and uncertainty.",
                f"In everyday situations, {subject} can shape mood, stress and emotional recovery.",
                f"When people understand {subject}, they usually handle feelings with a lot more clarity.",
            ],
            "habit": [
                f"{subject} can quietly shape routines, motivation and self-control day after day.",
                f"In real life, {subject} often decides whether people act now or stay stuck.",
                f"Once you understand {subject}, behavior patterns start to look much less random.",
            ],
            "social": [
                f"A pause, a look, or a shift in tone is often the first clue.",
                f"That tiny cue can make attraction feel intense before compatibility has proved anything.",
                f"If the signal feels addictive, slow down and check whether it brings calm or confusion.",
                f"Real chemistry should not require guessing games to keep your attention hooked.",
                f"The attractive move is simple: notice the spark, but still choose consistency.",
                f"When the pattern is clear, confidence feels calmer and boundaries get easier.",
                f"The hotter move is not chasing harder; it is staying calm enough to read the pattern.",
                f"If one tiny signal changes your whole mood, your nervous system is asking for certainty.",
                f"Attraction gets stronger when confidence and restraint show up at the same time.",
            ],
            "general": [
                f"{subject} can shape how people think, feel and react in everyday situations.",
                f"In daily life, {subject} can affect stress, choices, memory and communication more than people realize.",
                f"Once you notice {subject}, patterns in behavior become easier to understand and manage.",
            ],
        }
        bullets = templates.get(category, templates["general"])
        if category == "social" and len(bullets) > 3:
            rng = random.Random(subject.lower())
            bullets = bullets[:1] + rng.sample(bullets[1:], k=2)
        return bullets

    def _brain_lens_visual_queries(self, title: str, subject: str, category: str) -> List[str]:
        base = [
            f"{subject} emotional close up person",
            f"{subject} real person psychology portrait",
            f"{subject} human behavior",
            title,
        ]
        by_category = {
            "bias": ["person making decision", "judgment conflict", "social interaction close up", "editorial portrait"],
            "focus": ["overwhelmed at desk", "focus problem close up", "work stress person", "editorial portrait"],
            "emotion": ["stressed person close up", "emotional overwhelm", "mental health portrait", "editorial portrait"],
            "habit": ["daily habits person", "phone scrolling at night", "routine close up", "editorial portrait"],
            "social": [
                "stylish couple eye contact close up",
                "flirty texting phone close up",
                "romantic tension couple portrait",
                "body language attraction close up",
                "confident attractive woman studio portrait",
                "stylish relationship portrait",
            ],
            "general": ["human behavior close up", "social interaction", "person thinking", "editorial portrait"],
        }
        return base + by_category.get(category, by_category["general"])

    def _normalize_subject(self, text: str) -> str:
        low = text.lower().strip()
        if low.startswith("the "):
            low = low[4:]
        if low.startswith("a "):
            low = low[2:]
        if low.startswith("an "):
            low = low[3:]
        return low.strip()

    def _memory_prefixes(self) -> tuple[str, ...]:
        return (
            "the bizarre true story of ",
            "the real story behind ",
            "the real story of ",
            "the true story of ",
            "the psychology behind ",
            "why your brain does this: ",
            "the behavior pattern behind ",
            "why people act this way: ",
            "what really happened in ",
            "the lesson inside ",
            "the artifact trail behind ",
            "the artifact that changed how historians see ",
            "3 pieces of evidence that reframe ",
            "3 artifacts that reframe ",
            "3 physical clues that explain ",
            "what the surviving record reveals about ",
            "the facts behind ",
            "what ",
        )

    def _memory_subject(self, text: str) -> str:
        cleaned = self._normalize_subject(text)
        for prefix in self._memory_prefixes():
            normalized_prefix = self._normalize_subject(prefix)
            if cleaned.startswith(normalized_prefix):
                cleaned = cleaned[len(normalized_prefix):].strip()
                break
        return cleaned

    @staticmethod
    def _run_counts_as_published(item: dict) -> bool:
        """Dry-run holds and render failures must not exhaust topic rotation.

        Successful uploads and quality-passed ready-to-upload builds do count.
        """
        if item.get("uploaded") is True:
            return True
        if (
            str(item.get("youtube_id") or "").strip()
            or str(item.get("youtube_video_id") or "").strip()
            or str(item.get("facebook_id") or "").strip()
        ):
            return True
        decision = str(item.get("quality_decision") or "").strip().lower()
        if decision == "pass":
            skipped = str(item.get("upload_skipped") or "").strip().lower()
            return skipped != "quality_gate"
        return False

    def _load_requeued_subjects(self, channel_id: str | None = None) -> set[str]:
        """Subjects currently waiting for retry should be preferred, not burned."""
        values: set[str] = set()
        try:
            path = Path("data/state/failed_topics.jsonl")
            if not path.exists():
                return values
            import json

            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if channel_id and item.get("channel") != channel_id:
                    continue
                status = str(item.get("status") or "").strip().lower()
                if status in {"consumed", "resolved", "uploaded"}:
                    continue
                for field in ("subject", "title"):
                    value = self._memory_subject(str(item.get(field, "")))
                    if value:
                        values.add(value)
        except Exception:
            pass
        return values

    def _load_used_topics(self, channel_id: str | None = None) -> set[str]:
        values: set[str] = set()
        try:
            runs_path = Path("data/state/runs.jsonl")
            if runs_path.exists():
                parsed_runs = []
                for line in runs_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        import json
                        item = json.loads(line)
                    except Exception:
                        continue
                    if channel_id and item.get("channel") != channel_id:
                        continue
                    if channel_id and not self._run_counts_as_published(item):
                        continue
                    parsed_runs.append(item)
                recent_limit = 42 if channel_id == "brain_lens" else 80
                for item in parsed_runs[-recent_limit:]:
                    for field in ("subject", "title"):
                        value = self._memory_subject(str(item.get(field, "")))
                        if value:
                            values.add(value)
        except Exception:
            pass
        if not channel_id:
            try:
                path = Path("data/state/used_topics.txt")
                if path.exists():
                    lines = [self._memory_subject(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                    for line in lines:
                        if line:
                            values.add(line)
            except Exception:
                pass
        # Requeued failures stay available for retry; strip them from used set.
        requeued = self._load_requeued_subjects(channel_id)
        if requeued:
            values -= requeued
        return values
        
    def _is_duplicate_subject(self, subject: str, used: set[str]) -> bool:
        s_clean = self._memory_subject(subject)
        if not s_clean:
            return False
            
        ignore = {"bizarre", "story", "true", "real", "behind", "history", "ancient", "mystery", "shocking", "territory", "angle", "unseen", "facts", "psychology", "brain", "behavior", "mind", "people", "pattern"}
        words_in_subject = {w for w in re.findall(r"\w{4,}", s_clean) if w not in ignore}

        for u in used:
            u_clean = self._memory_subject(u)
            if not u_clean:
                continue
            # Direct match
            if u_clean == s_clean or u_clean in s_clean or s_clean in u_clean:
                return True
            # High word overlap (3+ words)
            words_in_u = {w for w in re.findall(r"\w{4,}", u_clean) if w not in ignore}
            overlap = words_in_u & words_in_subject
            if len(overlap) >= 3:
                return True
        return False

    def _curated_story_pack(self, keywords: List[str], subject_pool: List[str]) -> dict | None:
        used = self._load_used_topics("brain_lens")
        pool = [p for p in subject_pool if not self._is_duplicate_subject(p, used)]
        if not pool:
            return None # Exhausted curated list, let it fallback to infinite Wikipedia dynamic search
            
        random.shuffle(pool)
        
        # Check up to 15 random subjects until we find one with enough assets
        for subject in pool[:15]:
            item = self.wikipedia_summary(subject)
            if not item:
                continue
            bullets = item.get("bullets", [])
            image_urls = item.get("image_urls", [])
            
            # Must have at least a few facts and enough B-roll images
            if len(bullets) >= 2 and len(image_urls) >= 3:
                headline = f"The bizarre true story of {item['title']}"
                return {
                    "headline": headline,
                    "bullets": self._clip(bullets, limit=5),
                    "sources": self._validated_sources([item.get("url", "")], limit=1),
                    "visual_queries": [item["title"], "historical event", "archival photo"],
                    "image_urls": image_urls[:8],
                    "subject": item["title"],
                }

        return None

    def _yt_dlp_path(self) -> str:
        """Find the yt-dlp executable, favoring the venv/Scripts folder if on Windows."""
        bin_dir = Path(sys.executable).parent
        ext = ".exe" if os.name == "nt" else ""
        local_path = bin_dir / f"yt-dlp{ext}"
        if local_path.exists():
            return str(local_path)
        return "yt-dlp"

    def _get_yt_transcript(self, url: str) -> str:
        """Download auto-generated subtitles for one video, return clean text."""
        import tempfile
        ytdlp = self._yt_dlp_path()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                ytdlp,
                "--write-auto-subs",
                "--sub-lang", "en",
                "--sub-format", "vtt",
                "--skip-download",
                "--output", os.path.join(tmp, "%(id)s"),
                "--no-warnings",
                "--quiet",
                url,
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=30)
                for f in Path(tmp).glob("*.vtt"):
                    vtt = f.read_text(encoding="utf-8", errors="ignore")
                    lines = []
                    seen: set[str] = set()
                    for line in vtt.splitlines():
                        line = line.strip()
                        if not line or "-->" in line or line.startswith("WEBVTT") or line.isdigit():
                            continue
                        clean = re.sub(r"<[^>]+>", "", line).strip()
                        clean = re.sub(r"\s+", " ", clean)
                        if clean and clean not in seen:
                            seen.add(clean)
                            lines.append(clean)
                    return " ".join(lines)
            except Exception:
                pass
        return ""

    def search_youtube_transcripts(self, query: str, max_videos: int = 2) -> list[str]:
        """Search YouTube for a topic and return the top transcripts."""
        transcripts = []
        ytdlp = self._yt_dlp_path()
        try:
            cmd = [
                ytdlp,
                "--get-id",
                "--playlist-end", str(max_videos),
                "--no-warnings",
                "--quiet",
                f"ytsearch{max_videos}:{query}",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            ids = [i.strip() for i in result.stdout.splitlines() if i.strip()]
            for vid_id in ids:
                url = f"https://www.youtube.com/watch?v={vid_id}"
                text = self._get_yt_transcript(url)
                if text:
                    transcripts.append(text[:2000]) # Cap per transcript
        except Exception:
            pass
        return transcripts

    def brain_lens_pack(self, terms: List[str], avoid_subjects: Optional[set[str]] = None) -> dict:
        used = self._load_used_topics("brain_lens")
        ranked = []
        for subject in self.brain_lens_subjects:
            lowered = subject.lower()
            category = self._brain_lens_category(subject)
            score = {
                "social": 6,
                "emotion": 5,
                "habit": 4,
                "focus": 2,
                "bias": 1,
                "general": 0,
            }.get(category, 0)
            if lowered in self.brain_lens_priority_subjects:
                score += 18
            for keyword in terms[:8]:
                key = keyword.lower().strip()
                if not key:
                    continue
                if key in lowered or lowered in key:
                    score += 4
                for token in re.findall(r"\w{4,}", key):
                    if token in lowered:
                        score += 2
                if category == "habit" and any(token in key for token in ("habit", "dopamine", "overthinking", "procrastination", "motivation")):
                    score += 3
                if category == "social" and any(token in key for token in ("people", "relationship", "behavior", "social", "body language")):
                    score += 3
                if category == "bias" and any(token in key for token in ("bias", "decision", "judgment", "mind")):
                    score += 3
                if category == "focus" and any(token in key for token in ("memory", "focus", "attention", "brain fog")):
                    score += 3
                if category == "emotion" and any(token in key for token in ("stress", "anxiety", "emotion", "mental health")):
                    score += 3
            ranked.append((score, subject))

        ranked.sort(key=lambda item: (item[0], random.random()), reverse=True)
        avoid_subjects = {self._normalize_subject(item) for item in (avoid_subjects or set()) if item}
        candidate_pool = [
            subject
            for _, subject in ranked
            if self._normalize_subject(subject) in self.brain_lens_priority_subjects
            and not self._is_duplicate_subject(subject, used)
            and not self._is_duplicate_subject(subject, avoid_subjects)
        ]
        if not candidate_pool:
            candidate_pool = [
                subject
                for _, subject in ranked
                if self._normalize_subject(subject) in self.brain_lens_priority_subjects
                and not self._is_duplicate_subject(subject, avoid_subjects)
            ]
        if not candidate_pool:
            candidate_pool = [
                subject
                for subject in self.brain_lens_subjects
                if self._normalize_subject(subject) in self.brain_lens_priority_subjects
                and not self._is_duplicate_subject(subject, avoid_subjects)
            ]
        if not candidate_pool:
            candidate_pool = [
                subject
                for subject in self.brain_lens_subjects
                if self._normalize_subject(subject) in self.brain_lens_priority_subjects
            ]

        shortlist = candidate_pool[:8] if len(candidate_pool) >= 8 else candidate_pool
        subject = random.choice(shortlist)
        # Priority relationship topics use curated, cautious behavior profiles
        # and the verified context registry below. Exact encyclopedia lookups for
        # phrases such as "reply time anxiety" are usually missing and used to
        # stall every rejected candidate for tens of seconds.
        item = (
            None
            if self._normalize_subject(subject) in self.brain_lens_priority_subjects
            else self.wikipedia_summary(subject)
        )
        title = self._display_subject_title(subject)
        spoken_title = title.lower()
        category = self._brain_lens_category(subject)

        bullets: List[str] = []
        first_fact = self._override_fact_for_title(subject) or self._override_fact_for_title(title) or self._first_clean_fact(item)
        if first_fact:
            bullets.append(first_fact)

        bullets.extend(self._brain_lens_supporting_bullets(spoken_title, category))
        bullets = self._dedupe_bullets(bullets, limit=5)

        headline_options = self._brain_lens_headline_options(title, category)
        headline = random.choice(headline_options[: min(4, len(headline_options))])
        return {
            "headline": headline,
            "bullets": bullets,
            "sources": self.enrich_source_urls(
                title,
                [item.get("url") or self._default_source_url(title)] if item else [self._default_source_url(title)],
                limit=4,
            ),
            "visual_queries": self._brain_lens_visual_queries(title, spoken_title, category),
            "image_urls": [],
            "subject": title,
        }

    def story_pack(self, keywords: List[str]) -> dict:
        curated = self._curated_story_pack(keywords, self.true_story_subjects)
        if curated:
            return curated

        seed_queries = keywords[:4] if keywords else ["bizarre true story", "odd real event"]
        query = " OR ".join([f'"{q}"' for q in seed_queries])
        rss_url = (
            "https://news.google.com/rss/search?q="
            f"{quote(query)}&hl=en-US&gl=US&ceid=US:en"
        )

        try:
            feed = feedparser.parse(rss_url)
            entries = list(feed.entries[:10])
        except Exception:
            entries = []

        picked = []
        for entry in entries:
            title = self._clean_text(str(getattr(entry, "title", "")))
            link = self._clean_text(str(getattr(entry, "link", "")))
            if not title:
                continue
            if self._story_score(title) <= 0:
                continue
            picked.append((title, link))

        bullets = [title for title, _ in picked[:5]]
        sources = [link for _, link in picked[:5] if link]
        headline = bullets[0] if bullets else "A bizarre true story people could not ignore"

        if not bullets:
            core = seed_queries[0].title() if seed_queries else "True Story"
            bullets = [
                f"A bizarre real incident around {core} got attention fast.",
                "One twist made people keep sharing the story.",
                "The ending felt so strange that it did not seem real.",
            ]

        return {
            "headline": headline,
            "bullets": self._clip(bullets, limit=5),
            "sources": self._validated_sources(sources, limit=5),
            "visual_queries": self._clip(seed_queries, limit=4, max_chars=80, sentence_mode=False),
            "image_urls": [],
        }

