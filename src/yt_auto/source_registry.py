from __future__ import annotations


# Stable context pages verified by the project. Psychology pages are deliberately
# broad context, never proof of a specific person's motive or a diagnosis. History
# pages are subject anchors; individual claims must still stay inside the supplied
# research facts.
VERIFIED_CONTEXT_URLS = {
    "Attachment theory": "https://en.wikipedia.org/wiki/Attachment_theory",
    "Breadcrumbing": "https://en.wikipedia.org/wiki/Breadcrumbing",
    "Boudican revolt": "https://en.wikipedia.org/wiki/Boudican_revolt",
    "Computer-mediated communication": "https://en.wikipedia.org/wiki/Computer-mediated_communication",
    "Consent": "https://en.wikipedia.org/wiki/Consent",
    "Ghosting (behavior)": "https://en.wikipedia.org/wiki/Ghosting_(behavior)",
    "Interpersonal attraction": "https://en.wikipedia.org/wiki/Interpersonal_attraction",
    "Interpersonal relationship": "https://en.wikipedia.org/wiki/Interpersonal_relationship",
    "Jealousy": "https://en.wikipedia.org/wiki/Jealousy",
    "Limerence": "https://en.wikipedia.org/wiki/Limerence",
    "Love bombing": "https://en.wikipedia.org/wiki/Love_bombing",
    "Operant conditioning": "https://en.wikipedia.org/wiki/Operant_conditioning",
    "Overchoice": "https://en.wikipedia.org/wiki/Overchoice",
    "Plague of Justinian": "https://en.wikipedia.org/wiki/Plague_of_Justinian",
    "Cadaver Synod": "https://en.wikipedia.org/wiki/Cadaver_Synod",
    "Göbekli Tepe": "https://en.wikipedia.org/wiki/G%C3%B6bekli_Tepe",
    "Kingdom of Kush": "https://en.wikipedia.org/wiki/Kingdom_of_Kush",
    "Late Bronze Age collapse": "https://en.wikipedia.org/wiki/Late_Bronze_Age_collapse",
    "Machu Picchu": "https://en.wikipedia.org/wiki/Machu_Picchu",
    "Mausoleum of the First Qin Emperor": "https://en.wikipedia.org/wiki/Mausoleum_of_the_First_Qin_Emperor",
    "Minoan eruption": "https://en.wikipedia.org/wiki/Minoan_eruption",
    "Nubian pyramids": "https://en.wikipedia.org/wiki/Nubian_pyramids",
    "Petra": "https://en.wikipedia.org/wiki/Petra",
    "Pompeii": "https://en.wikipedia.org/wiki/Pompeii",
    "Rosetta Stone": "https://en.wikipedia.org/wiki/Rosetta_Stone",
    "Silk Road": "https://en.wikipedia.org/wiki/Silk_Road",
    "Sogdia": "https://en.wikipedia.org/wiki/Sogdia",
    "Terracotta Army": "https://en.wikipedia.org/wiki/Terracotta_Army",
    "Angkor Wat": "https://en.wikipedia.org/wiki/Angkor_Wat",
    "Tikal": "https://en.wikipedia.org/wiki/Tikal",
    "Chichen Itza": "https://en.wikipedia.org/wiki/Chichen_Itza",
    "Great Zimbabwe": "https://en.wikipedia.org/wiki/Great_Zimbabwe",
    "Mohenjo-daro": "https://en.wikipedia.org/wiki/Mohenjo-daro",
    "Obelisk of Axum": "https://en.wikipedia.org/wiki/Obelisk_of_Axum",
    "Lascaux": "https://en.wikipedia.org/wiki/Lascaux",
    "Cyrus Cylinder": "https://en.wikipedia.org/wiki/Cyrus_Cylinder",
    "Olmec colossal heads": "https://en.wikipedia.org/wiki/Olmec_colossal_heads",
    "Roman concrete": "https://en.wikipedia.org/wiki/Roman_concrete",
    "Lachish gallery": "https://www.britishmuseum.org/collection/galleries/assyria-lion-hunts",
    "Lachish throne relief": "https://www.britishmuseum.org/collection/object/W_1856-0909-14_7",
    "Sennacherib and Jerusalem": "https://www.metmuseum.org/exhibitions/listings/2014/assyria-to-iberia/blog/posts/sennacherib-and-jerusalem",
    "Tel Lachish archaeomagnetism": "https://pmc.ncbi.nlm.nih.gov/articles/PMC11090153/",
    # Curated peer-reviewed DOI. Wiley blocks the project's non-browser source
    # checker with HTTP 403, so this exact URL is trusted from the registry; the
    # validator must continue rejecting arbitrary 403 responses.
    "Lachish siege-ramp construction": "https://onlinelibrary.wiley.com/doi/10.1111/ojoa.12231",
    "Great Zimbabwe UNESCO": "https://whc.unesco.org/en/list/364/",
    "Great Zimbabwe Met": "https://www.metmuseum.org/essays/great-zimbabwe-11th-15th-century",
    "Reply-time attachment study": "https://pubmed.ncbi.nlm.nih.gov/35085449/",
    "APA relationship texting research": "https://www.apa.org/news/press/releases/2018/08/relationship-texting",
    "Friends-with-benefits rules study": "https://pubmed.ncbi.nlm.nih.gov/34779977/",
    "RAINN consent guidance": "https://rainn.org/share-the-facts/consent-101-respect-boundaries-and-building-trust/",
    "Friends-with-benefits communication review": "https://pmc.ncbi.nlm.nih.gov/articles/PMC12124867/",
}

VERIFIED_CONTEXT_URL_SET = frozenset(VERIFIED_CONTEXT_URLS.values())


# Topic-specific packs supplement whatever source was already discovered. They
# never bypass ``ContentResearcher._validated_sources``; the exact URLs above
# merely make stable institutional pages resilient to transient bot blocking.
CURATED_TOPIC_SOURCE_PACKS = {
    "great zimbabwe": (
        VERIFIED_CONTEXT_URLS["Great Zimbabwe UNESCO"],
        VERIFIED_CONTEXT_URLS["Great Zimbabwe Met"],
    ),
    "reply time anxiety": (
        VERIFIED_CONTEXT_URLS["Reply-time attachment study"],
        VERIFIED_CONTEXT_URLS["APA relationship texting research"],
    ),
    "friends with benefits boundaries": (
        VERIFIED_CONTEXT_URLS["Friends-with-benefits rules study"],
        VERIFIED_CONTEXT_URLS["RAINN consent guidance"],
        VERIFIED_CONTEXT_URLS["Friends-with-benefits communication review"],
    ),
}
