import os
import re
from pathlib import Path

def prune_topics():
    used_topics_file = Path("data/state/used_topics.txt")
    if not used_topics_file.exists():
        print("No used_topics.txt found.")
        return

    lines = used_topics_file.read_text(encoding="utf-8").splitlines()
    original_count = len(lines)
    
    junk_starts = [
        "and ", "but ", "or ", "as ", "at ", "to ", "of ", "with ", "for ", "in ", "on ", 
        "due to", "according to", "also known as", "by ", "from ", "is a ", "was a ",
        "which ", "that ", "who ", "where ", "when ", "why ", "a key ", "one ", "then ",
        "finally ", "next ", "here is ", "it is ", "they are ", "there is "
    ]
    
    pruned = []
    for line in lines:
        clean = line.strip().lower()
        if not clean:
            continue
            
        words = clean.split()
        
        # Rule 1: Too short (likely not a topic)
        if len(clean) < 6:
            continue
            
        # Rule 2: Starts with junk transition/fragment
        if any(clean.startswith(j) for j in junk_starts):
            continue
            
        # Rule 3: Ends with transition or looks like a sentence fragment
        if clean.endswith((" and", " of", " the", " to", " with", " in", " on", " at")):
            continue
            
        # Rule 4: Model specific prefix - keep these if reasonable length
        is_title_prefix = any(clean.startswith(p) for p in [
            "the real story of", "the psychology behind", "why your brain", 
            "the behavior pattern", "the true story of", "what really happened"
        ])
        
        if is_title_prefix:
            if 15 < len(clean) < 90:
                pruned.append(line)
            continue

        # Rule 5: Length and word count for "normal" subjects
        if 6 <= len(clean) <= 45 and 1 <= len(words) <= 5:
            # Check for too many numbers (likely a fact, not a subject)
            digit_count = sum(c.isdigit() for c in clean)
            if digit_count > 3:
                continue
            pruned.append(line)
            
    # Dedup and sort
    pruned = sorted(list(set(pruned)))
    
    used_topics_file.write_text("\n".join(pruned) + "\n", encoding="utf-8")
    print(f"Pruned used_topics.txt: {original_count} -> {len(pruned)} entries.")

if __name__ == "__main__":
    prune_topics()
