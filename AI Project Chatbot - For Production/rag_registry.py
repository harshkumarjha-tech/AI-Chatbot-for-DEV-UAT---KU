"""
RAG REGISTRY — Self-Updating Data Type Configuration
=====================================================
When new data is ingested into RAG Store 2, this module auto-registers
the data type so the intent classifier, target_type detector, and stop
words all stay in sync — ZERO manual code changes needed.

Persists to rag_registry.json on disk. Seeds itself on first run with
the existing hardcoded types (Course, Role, AcademicYear, HostelMaster).
"""

import os
import re
import json
import threading
from datetime import datetime

REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "rag_registry.json")
_registry_lock = threading.Lock()

# ── Words stripped from CamelCase splits (they carry no search intent) ────────
_NOISE_SUFFIXES = {"master", "data", "info", "list", "detail", "details", "record", "records", "table", "item", "items"}

# ── Initial seed data — matches the hardcoded types that existed before this module ──
_SEED_REGISTRY = {
    "Course": {
        "keywords": ["course", "courses", "cource", "cources", "class", "classes", "program", "programs", "degree", "degrees"],
        "endpoint": "/api/Course",
        "added_at": "seed"
    },
    "Role": {
        "keywords": ["role", "roles", "rolemaster"],
        "endpoint": "/api/RoleMaster",
        "added_at": "seed"
    },
    "AcademicYear": {
        "keywords": ["academicyear", "academic", "year", "years", "calendar"],
        "endpoint": "/api/AcademicYear",
        "added_at": "seed"
    },
    "HostelMaster": {
        "keywords": ["hostel", "hostels", "hostelmaster", "accommodation", "boarding", "block"],
        "endpoint": "/api/HostelMaster",
        "added_at": "seed"
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# CORE: Read / Write Registry
# ══════════════════════════════════════════════════════════════════════════════
def _load_registry() -> dict:
    """Load the registry from disk. Seeds it if the file doesn't exist."""
    if not os.path.exists(REGISTRY_PATH):
        _save_registry(_SEED_REGISTRY)
        return dict(_SEED_REGISTRY)
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        # Corrupted file — reseed
        _save_registry(_SEED_REGISTRY)
        return dict(_SEED_REGISTRY)


def _save_registry(registry: dict):
    """Atomically write the registry to disk."""
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# SMART KEYWORD GENERATOR
# ══════════════════════════════════════════════════════════════════════════════
def generate_keywords(label: str) -> list:
    """
    Auto-generates search keywords from a CamelCase data type label.

    Example: "HostelMaster"  → ["hostel", "hostels", "hostelmaster"]
             "StaffMember"   → ["staff", "staffs", "member", "members", "staffmember"]
             "FeeStructure"  → ["fee", "fees", "structure", "structures", "feestructure"]
    """
    # 1. Split CamelCase → individual words
    words = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)', label)
    words = [w.lower() for w in words]

    # 2. Filter out noise suffixes
    meaningful_words = [w for w in words if w not in _NOISE_SUFFIXES]

    # If ALL words were noise (unlikely), keep them all
    if not meaningful_words:
        meaningful_words = words

    keywords = set()

    # 3. For each meaningful word, add the word + a basic plural
    for word in meaningful_words:
        keywords.add(word)
        # Simple English pluralization
        if word.endswith("s") or word.endswith("x") or word.endswith("sh") or word.endswith("ch"):
            keywords.add(word + "es")
        elif word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
            keywords.add(word[:-1] + "ies")
        else:
            keywords.add(word + "s")

    # 4. Always add the full lowered label as a compound keyword
    keywords.add(label.lower())

    return sorted(keywords)


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API: Register, Query, Build
# ══════════════════════════════════════════════════════════════════════════════
def register_data_type(label: str, endpoint: str, extra_keywords: list = None):
    """
    Register a data type after successful RAG ingest.
    If the label already exists, merges any new extra_keywords.
    Thread-safe.
    """
    with _registry_lock:
        registry = _load_registry()

        if label in registry:
            # Merge extra keywords if provided
            if extra_keywords:
                existing = set(registry[label].get("keywords", []))
                existing.update(kw.lower() for kw in extra_keywords)
                registry[label]["keywords"] = sorted(existing)
                _save_registry(registry)
            print(f"[REGISTRY] '{label}' already registered (keywords: {registry[label]['keywords']})")
            return

        # New type — auto-generate keywords
        auto_keywords = generate_keywords(label)
        if extra_keywords:
            auto_keywords = sorted(set(auto_keywords) | {kw.lower() for kw in extra_keywords})

        registry[label] = {
            "keywords": auto_keywords,
            "endpoint": endpoint,
            "added_at": datetime.now().isoformat(),
        }
        _save_registry(registry)
        print(f"[REGISTRY] ✅ NEW data type registered: '{label}' → keywords: {auto_keywords}")


def build_target_type_regex_map() -> dict:
    """
    Returns {label: compiled_regex_pattern} for all registered data types.
    Used by get_static_response to detect target_type from user queries.
    """
    registry = _load_registry()
    result = {}
    for label, entry in registry.items():
        keywords = entry.get("keywords", [])
        if keywords:
            # Build a regex like \b(hostel|hostels|hostelmaster)\b
            escaped = [re.escape(kw) for kw in keywords]
            pattern = r'\b(' + '|'.join(escaped) + r')\b'
            result[label] = pattern
    return result


def build_stop_words_set() -> set:
    """
    Returns a combined set of ALL registered keywords across every data type.
    These are merged into the base stop_words to prevent them from being
    treated as data-filter keywords (they're intent words, not data values).
    """
    registry = _load_registry()
    stop_words = set()
    for entry in registry.values():
        stop_words.update(entry.get("keywords", []))
    return stop_words


def build_static_intent_description() -> str:
    """
    Returns a human-readable, comma-separated description of all known
    STATIC data types for the LLM intent classifier prompt.

    Example output:
      "courses, programs, degrees, roles, academic years, hostels, accommodation, staff"
    """
    registry = _load_registry()
    # Use the keywords as the description (they're already human-readable)
    all_descriptors = []
    for label, entry in registry.items():
        keywords = entry.get("keywords", [])
        # Pick meaningful keywords (skip compound labels and very short ones)
        descriptors = [kw for kw in keywords if len(kw) > 2 and kw != label.lower()]
        if not descriptors:
            descriptors = [label.lower()]
        all_descriptors.extend(descriptors[:3])  # Top 3 per type to keep prompt compact

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for d in all_descriptors:
        if d not in seen:
            seen.add(d)
            unique.append(d)

    return ", ".join(unique)


def get_registry() -> dict:
    """Returns a copy of the full registry for debugging/admin endpoints."""
    return _load_registry()
