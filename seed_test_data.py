"""
seed_test_data.py
=================
One-time script to populate Qdrant with synthetic user interaction data
so Collaborative Filtering has enough signal to return recommendations
before any real users have used the system.

Run this ONCE before testing your agent:
    python seed_test_data.py

Run with --clear to wipe and re-seed:
    python seed_test_data.py --clear

What it does
------------
- Creates 8 synthetic users with distinct but realistic purchase patterns
- Each user has 4-8 interactions (search + add_to_cart + order) so CF has
  genuine co-occurrence signal to work with
- Patterns are designed so testing as "test_user" will produce visible recs:
    - Search "off road tyres" → should surface wheels, mud flaps, alignment
    - Search "camera lens"   → should surface camera accessories, tripods
    - Search "engine oil"    → should surface oil filters, engine parts
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("seed")

# ─────────────────────────────────────────────────────────────────────────────
# Synthetic dataset
# Each entry: (username, action, query, product_code, category, price_range)
# ─────────────────────────────────────────────────────────────────────────────

SYNTHETIC_INTERACTIONS: list[dict] = [

    # ── user_alice: off-road enthusiast ──────────────────────────────────────
    {"username": "user_alice", "action": "search",      "query": "off road tyres",        "product_code": "",          "category": "tyres",       "price_range": "high"},
    {"username": "user_alice", "action": "add_to_cart", "query": "off road tyres",        "product_code": "TYRE-4X4",  "category": "tyres",       "price_range": "high"},
    {"username": "user_alice", "action": "search",      "query": "steel wheels 4x4",      "product_code": "",          "category": "wheels",      "price_range": "medium"},
    {"username": "user_alice", "action": "add_to_cart", "query": "steel wheels 4x4",      "product_code": "WHEEL-ST",  "category": "wheels",      "price_range": "medium"},
    {"username": "user_alice", "action": "order",       "query": "mud flaps heavy duty",  "product_code": "MUD-HD",    "category": "accessories", "price_range": "low"},
    {"username": "user_alice", "action": "search",      "query": "wheel alignment tool",  "product_code": "",          "category": "alignment",   "price_range": "medium"},
    {"username": "user_alice", "action": "order",       "query": "off road tyres",        "product_code": "TYRE-4X4",  "category": "tyres",       "price_range": "high"},

    # ── user_bob: city driver, tyre/alignment focused ─────────────────────────
    {"username": "user_bob",   "action": "search",      "query": "summer tyres 205 55 r16","product_code": "",         "category": "tyres",       "price_range": "medium"},
    {"username": "user_bob",   "action": "add_to_cart", "query": "summer tyres 205 55 r16","product_code": "TYRE-SUM", "category": "tyres",       "price_range": "medium"},
    {"username": "user_bob",   "action": "search",      "query": "wheel alignment service","product_code": "",         "category": "alignment",   "price_range": "low"},
    {"username": "user_bob",   "action": "order",       "query": "summer tyres",           "product_code": "TYRE-SUM", "category": "tyres",       "price_range": "medium"},
    {"username": "user_bob",   "action": "search",      "query": "alloy wheels 16 inch",   "product_code": "",         "category": "wheels",      "price_range": "medium"},
    {"username": "user_bob",   "action": "add_to_cart", "query": "alloy wheels 16 inch",   "product_code": "WHEEL-AL", "category": "wheels",      "price_range": "medium"},

    # ── user_carol: camera / photography ──────────────────────────────────────
    {"username": "user_carol", "action": "search",      "query": "telephoto camera lens",  "product_code": "",         "category": "lenses",      "price_range": "high"},
    {"username": "user_carol", "action": "add_to_cart", "query": "telephoto camera lens",  "product_code": "LENS-200", "category": "lenses",      "price_range": "high"},
    {"username": "user_carol", "action": "search",      "query": "mirrorless camera body", "product_code": "",         "category": "cameras",     "price_range": "high"},
    {"username": "user_carol", "action": "order",       "query": "camera lens 50mm",       "product_code": "LENS-50",  "category": "lenses",      "price_range": "medium"},
    {"username": "user_carol", "action": "add_to_cart", "query": "camera tripod carbon",   "product_code": "TRIP-CF",  "category": "accessories", "price_range": "medium"},
    {"username": "user_carol", "action": "order",       "query": "camera lens filter UV",  "product_code": "FILT-UV",  "category": "accessories", "price_range": "low"},

    # ── user_dave: engine / oil maintenance ───────────────────────────────────
    {"username": "user_dave",  "action": "search",      "query": "synthetic engine oil 5w40","product_code": "",       "category": "engine_oil",  "price_range": "medium"},
    {"username": "user_dave",  "action": "add_to_cart", "query": "synthetic engine oil 5w40","product_code": "OIL-5W4","category": "engine_oil",  "price_range": "medium"},
    {"username": "user_dave",  "action": "search",      "query": "oil filter replacement",   "product_code": "",       "category": "filters",     "price_range": "low"},
    {"username": "user_dave",  "action": "order",       "query": "engine oil 5w40",          "product_code": "OIL-5W4","category": "engine_oil",  "price_range": "medium"},
    {"username": "user_dave",  "action": "add_to_cart", "query": "air filter engine",        "product_code": "FILT-AIR","category": "filters",    "price_range": "low"},
    {"username": "user_dave",  "action": "search",      "query": "spark plugs iridium",      "product_code": "",       "category": "engine",      "price_range": "medium"},
    {"username": "user_dave",  "action": "order",       "query": "oil filter for 2.0 diesel","product_code": "FILT-OIL","category": "filters",    "price_range": "low"},

    # ── user_eve: brakes and service ──────────────────────────────────────────
    {"username": "user_eve",   "action": "search",      "query": "front brake pads",         "product_code": "",       "category": "brakes",      "price_range": "medium"},
    {"username": "user_eve",   "action": "add_to_cart", "query": "front brake pads",         "product_code": "BRAKE-F","category": "brakes",      "price_range": "medium"},
    {"username": "user_eve",   "action": "search",      "query": "brake disc rotor replacement","product_code": "",     "category": "brakes",      "price_range": "medium"},
    {"username": "user_eve",   "action": "order",       "query": "front brake pads ceramic", "product_code": "BRAKE-F","category": "brakes",      "price_range": "medium"},
    {"username": "user_eve",   "action": "search",      "query": "brake fluid DOT4",         "product_code": "",       "category": "service",     "price_range": "low"},
    {"username": "user_eve",   "action": "add_to_cart", "query": "brake fluid DOT4",         "product_code": "BRK-FLD","category": "service",     "price_range": "low"},

    # ── user_frank: off-road (overlaps with alice for CF signal) ─────────────
    {"username": "user_frank", "action": "search",      "query": "all terrain tyres",        "product_code": "",       "category": "tyres",       "price_range": "high"},
    {"username": "user_frank", "action": "add_to_cart", "query": "all terrain tyres",        "product_code": "TYRE-AT","category": "tyres",       "price_range": "high"},
    {"username": "user_frank", "action": "order",       "query": "all terrain tyres 4x4",    "product_code": "TYRE-AT","category": "tyres",       "price_range": "high"},
    {"username": "user_frank", "action": "search",      "query": "steel off road wheels",    "product_code": "",       "category": "wheels",      "price_range": "medium"},
    {"username": "user_frank", "action": "add_to_cart", "query": "steel off road wheels",    "product_code": "WHEEL-ST","category": "wheels",     "price_range": "medium"},
    {"username": "user_frank", "action": "order",       "query": "mud tyres off road",       "product_code": "TYRE-MUD","category": "tyres",      "price_range": "high"},
    {"username": "user_frank", "action": "search",      "query": "wheel spacers 4x4",        "product_code": "",       "category": "accessories", "price_range": "low"},

    # ── user_grace: camera + accessories (overlaps carol) ────────────────────
    {"username": "user_grace", "action": "search",      "query": "wide angle lens camera",   "product_code": "",       "category": "lenses",      "price_range": "high"},
    {"username": "user_grace", "action": "add_to_cart", "query": "wide angle lens camera",   "product_code": "LENS-WA","category": "lenses",      "price_range": "high"},
    {"username": "user_grace", "action": "search",      "query": "camera bag waterproof",    "product_code": "",       "category": "accessories", "price_range": "medium"},
    {"username": "user_grace", "action": "order",       "query": "wide angle lens",          "product_code": "LENS-WA","category": "lenses",      "price_range": "high"},
    {"username": "user_grace", "action": "add_to_cart", "query": "ND filter set camera",     "product_code": "FILT-ND","category": "accessories", "price_range": "medium"},

    # ── user_harry: engine + oil (overlaps dave) ──────────────────────────────
    {"username": "user_harry", "action": "search",      "query": "fully synthetic motor oil","product_code": "",       "category": "engine_oil",  "price_range": "high"},
    {"username": "user_harry", "action": "add_to_cart", "query": "fully synthetic motor oil","product_code": "OIL-FS", "category": "engine_oil",  "price_range": "high"},
    {"username": "user_harry", "action": "order",       "query": "engine oil 0w20 synthetic","product_code": "OIL-FS", "category": "engine_oil",  "price_range": "high"},
    {"username": "user_harry", "action": "search",      "query": "cabin air filter",         "product_code": "",       "category": "filters",     "price_range": "low"},
    {"username": "user_harry", "action": "add_to_cart", "query": "cabin air filter",         "product_code": "FILT-CAB","category": "filters",    "price_range": "low"},
    {"username": "user_harry", "action": "search",      "query": "engine coolant antifreeze","product_code": "",       "category": "engine",      "price_range": "medium"},
]


def clear_collection() -> None:
    """Delete and recreate the user_profiles collection."""
    from memory_history.user_memory import _get_client, USER_COLLECTION, _collection_ready
    import memory_history.user_memory as um

    client = _get_client()
    existing = {c.name for c in client.get_collections().collections}
    if USER_COLLECTION in existing:
        client.delete_collection(USER_COLLECTION)
        logger.info("Deleted existing collection '%s'", USER_COLLECTION)

    # Reset the cached flag so ensure_user_collection() re-creates it
    um._collection_ready = False


def seed(interactions: list[dict], delay_ms: int = 50) -> None:
    """Write all synthetic interactions to Qdrant via save_user_interaction()."""
    from memory_history.user_memory import save_user_interaction, ensure_user_collection

    ensure_user_collection()

    total  = len(interactions)
    errors = 0

    for i, row in enumerate(interactions, 1):
        username = row.pop("username")
        try:
            save_user_interaction(username, row)  # type: ignore[arg-type]
            if i % 10 == 0 or i == total:
                logger.info("  Progress: %d / %d interactions seeded", i, total)
        except Exception as exc:
            logger.error("  Failed interaction %d (user=%s): %s", i, username, exc)
            errors += 1
        finally:
            row["username"] = username  # restore for logging

        if delay_ms:
            time.sleep(delay_ms / 1000)

    logger.info("Seeding complete — %d succeeded, %d failed", total - errors, errors)


def verify() -> None:
    """
    Quick smoke test: run CF for a test_user with three different queries
    and print the results so you can manually verify they make sense.
    """
    from memory_history.user_memory import get_cf_recommendations

    test_cases = [
        ("test_user", "off road tyres",      "expect: wheels, accessories, alignment"),
        ("test_user", "camera lens 50mm",    "expect: cameras, lenses, accessories"),
        ("test_user", "engine oil synthetic","expect: filters, engine, engine_oil"),
    ]

    print("\n" + "=" * 60)
    print("  CF recommendation smoke test")
    print("=" * 60)

    all_ok = True
    for username, query, expectation in test_cases:
        recs = get_cf_recommendations(username, query, top_k=5)
        status = "OK" if recs else "EMPTY — check Qdrant connection / threshold"
        if not recs:
            all_ok = False
        print(f"\n  Query   : '{query}'")
        print(f"  ({expectation})")
        print(f"  Status  : {status}")
        for r in recs:
            print(f"    item={r['item']:<20} cf_score={r['cf_score']}")

    print("\n" + "=" * 60)
    if all_ok:
        print("  All queries returned results — CF is working correctly.")
    else:
        print("  Some queries returned no results.")
        print("  Check: QDRANT_HOST in .env, Qdrant is running, score_threshold.")
    print("=" * 60 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed Qdrant with synthetic CF test data")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete and recreate the user_profiles collection before seeding",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip seeding and only run the smoke test (collection must already be seeded)",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=50,
        help="Milliseconds to wait between upserts (default: 50)",
    )
    args = parser.parse_args()

    if args.verify_only:
        verify()
        sys.exit(0)

    if args.clear:
        logger.info("--clear flag set: wiping existing collection...")
        clear_collection()

    logger.info("Seeding %d interactions for %d synthetic users...",
                len(SYNTHETIC_INTERACTIONS),
                len({r["username"] for r in SYNTHETIC_INTERACTIONS}))

    # Pass a copy so we don't mutate the module-level list
    seed([dict(row) for row in SYNTHETIC_INTERACTIONS], delay_ms=args.delay_ms)

    verify()