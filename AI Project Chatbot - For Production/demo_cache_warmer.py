"""
DEMO CACHE WARMER
=================
Run this script 30-60 minutes BEFORE your presentation.
It sends all your planned demo questions to the server,
which triggers the AI pipeline and stores the responses in Redis.

During the presentation, every question will be answered instantly
from Redis cache — ZERO Gemini API calls needed.

Usage:
    python demo_cache_warmer.py
"""

import requests
import time
import json

# ── Server URL ──────────────────────────────────────────────────────────────
SERVER_URL = "http://localhost:8000/api/chat"

# ── ADD ALL YOUR PLANNED PRESENTATION QUESTIONS HERE ────────────────────────
# These will be pre-cached in Redis before your demo.
# Add as many as you want — the more you add now, the smoother your demo.

DEMO_QUESTIONS = [
    # General / Greeting
    "hi",
    "hello",
    "what can you do?",

    # Courses (STATIC — uses RAG Store 2)
    "show all courses",
    "show all MTech courses",
    "show all BTech courses",
    "show all BCA courses",
    "show all MBA courses",
    "show all engineering courses",
    "show all medical courses",
    "show all 4 year courses",
    "how many total courses are there?",
    "list all postgraduate courses",
    "list all undergraduate courses",

    # Roles (STATIC — uses RAG Store 2)
    "list all university roles",
    "show all roles",

    # Academic Years (STATIC — uses RAG Store 2)
    "show all academic years",
    "show current academic year",

    # Analysis questions
    "which course has the highest capacity?",
    "show only 2 year courses",
    "show only 3 year courses",

    # ADD YOUR OWN SPECIFIC DEMO QUESTIONS BELOW:
    # "your custom question here",
]

# ── WARMER LOGIC ─────────────────────────────────────────────────────────────

def warm_cache():
    print("=" * 60)
    print("  UNIVERSITY CHATBOT — DEMO CACHE WARMER")
    print("=" * 60)
    print(f"\nTarget server : {SERVER_URL}")
    print(f"Questions to warm: {len(DEMO_QUESTIONS)}\n")

    ok_count     = 0
    cached_count = 0
    fail_count   = 0

    for i, question in enumerate(DEMO_QUESTIONS, 1):
        print(f"[{i:2}/{len(DEMO_QUESTIONS)}] Warming: \"{question}\"")
        try:
            t0  = time.time()
            res = requests.post(
                SERVER_URL,
                json={"message": question, "history": [], "is_warmer": True},
                timeout=60
            )
            elapsed = time.time() - t0

            if res.status_code == 200:
                data = res.json()
                if data.get("cached"):
                    cached_count += 1
                    print(f"         Already cached ({elapsed:.2f}s)")
                else:
                    ok_count += 1
                    print(f"         [OK] Cached now  ({elapsed:.2f}s)")
            else:
                fail_count += 1
                print(f"         [FAIL] HTTP {res.status_code}")

        except requests.exceptions.ConnectionError:
            fail_count += 1
            print(f"         [ERROR] Server not reachable. Is it running on port 8000?")
            print("         Stopping — please start the server first.\n")
            break
        except Exception as e:
            fail_count += 1
            print(f"         [ERROR] {e}")

        # Small delay between requests to protect rate limits
        time.sleep(1.5)

    print("\n" + "=" * 60)
    print("  WARM-UP COMPLETE")
    print("=" * 60)
    print(f"  Freshly cached : {ok_count}")
    print(f"  Already cached : {cached_count}")
    print(f"  Failed         : {fail_count}")
    print(f"  Total done     : {ok_count + cached_count} / {len(DEMO_QUESTIONS)}")
    print()
    if fail_count == 0:
        print("  All questions are cached in Redis.")
        print("  During your presentation, all responses will be")
        print("  INSTANT with ZERO Gemini API calls.")
    else:
        print(f"  {fail_count} question(s) failed. Re-run this script to retry.")
    print("=" * 60)


if __name__ == "__main__":
    warm_cache()
