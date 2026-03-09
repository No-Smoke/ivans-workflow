#!/usr/bin/env python3
"""Migrate ebatt_pattern_library from 384-dim to 1024-dim.

Reads all points from ebatt_pattern_library (384-dim),
re-embeds text via Ollama mxbai-embed-large (1024-dim),
writes to ebatt_patterns_v2 (1024-dim).

Old collection is preserved as backup.

Usage:
    python3 scripts/migrate_patterns_384_to_1024.py [--dry-run]
"""

import argparse
import json
import sys
import time
import httpx

QDRANT_URL = "http://74.50.49.35:6333"
QDRANT_API_KEY = "qdrant-ethospower-2025-secure-key"
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "mxbai-embed-large"

SOURCE_COLLECTION = "ebatt_pattern_library"
TARGET_COLLECTION = "ebatt_patterns_v2"
TARGET_DIM = 1024
SCROLL_BATCH = 20
# mxbai-embed-large has a 512-token context window (~1200 chars safe limit)
MAX_EMBED_CHARS = 1200


def qdrant_headers():
    return {"api-key": QDRANT_API_KEY, "Content-Type": "application/json"}


def scroll_all_points(client: httpx.Client) -> list[dict]:
    """Scroll all points from source collection (no vectors needed)."""
    points = []
    offset = None
    while True:
        body = {"limit": SCROLL_BATCH, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        resp = client.post(
            f"{QDRANT_URL}/collections/{SOURCE_COLLECTION}/points/scroll",
            headers=qdrant_headers(),
            json=body,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        batch = result.get("points", [])
        points.extend(batch)
        offset = result.get("next_page_offset")
        if offset is None or not batch:
            break
    return points


def embed_text(client: httpx.Client, text: str) -> list[float] | None:
    """Get 1024-dim embedding from Ollama."""
    try:
        resp = client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception as e:
        print(f"  ⚠ Embedding failed: {e}", file=sys.stderr)
        return None


def create_target_collection(client: httpx.Client):
    """Create target collection with 1024-dim Cosine vectors."""
    resp = client.put(
        f"{QDRANT_URL}/collections/{TARGET_COLLECTION}",
        headers=qdrant_headers(),
        json={
            "vectors": {"size": TARGET_DIM, "distance": "Cosine"},
        },
    )
    if resp.status_code == 409:
        print(f"Collection {TARGET_COLLECTION} already exists — will upsert into it.")
        return
    resp.raise_for_status()
    print(f"Created collection: {TARGET_COLLECTION} ({TARGET_DIM}-dim Cosine)")


def upsert_points(client: httpx.Client, points: list[dict]):
    """Upsert points to target collection in batches of 20."""
    for i in range(0, len(points), SCROLL_BATCH):
        batch = points[i:i + SCROLL_BATCH]
        resp = client.put(
            f"{QDRANT_URL}/collections/{TARGET_COLLECTION}/points",
            headers=qdrant_headers(),
            json={"points": batch},
        )
        resp.raise_for_status()
        print(f"  Upserted batch {i // SCROLL_BATCH + 1}: {len(batch)} points")


def main():
    parser = argparse.ArgumentParser(description="Migrate patterns 384→1024 dim")
    parser.add_argument("--dry-run", action="store_true", help="Read and embed but don't write")
    args = parser.parse_args()

    with httpx.Client(timeout=30) as client:
        # Step 1: Read all source points
        print(f"Reading all points from {SOURCE_COLLECTION}...")
        source_points = scroll_all_points(client)
        print(f"  Found {len(source_points)} points")

        if not source_points:
            print("No points to migrate. Exiting.")
            return

        # Step 2: Create target collection
        if not args.dry_run:
            create_target_collection(client)

        # Step 3: Re-embed and prepare target points
        print(f"\nRe-embedding {len(source_points)} points via Ollama {EMBED_MODEL}...")
        target_points = []
        failed = 0
        for i, point in enumerate(source_points):
            text = point["payload"].get("text") or point["payload"].get("_document") or ""
            if not text:
                print(f"  ⚠ Point {point['id']}: no text field, skipping")
                failed += 1
                continue

            vector = embed_text(client, text[:MAX_EMBED_CHARS])
            if vector is None:
                failed += 1
                continue

            if len(vector) != TARGET_DIM:
                print(f"  ⚠ Point {point['id']}: got {len(vector)}-dim, expected {TARGET_DIM}")
                failed += 1
                continue

            # Preserve original payload, add migration metadata
            # Normalize: ensure 'text' is the canonical text field
            payload = dict(point["payload"])
            if "text" not in payload and "_document" in payload:
                payload["text"] = payload["_document"]
            payload["migrated_from"] = SOURCE_COLLECTION
            payload["migrated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            payload["original_id"] = point["id"]
            payload["embedding_truncated"] = len(text) > MAX_EMBED_CHARS

            target_points.append({
                "id": point["id"],
                "vector": vector,
                "payload": payload,
            })

            if (i + 1) % 10 == 0:
                print(f"  Embedded {i + 1}/{len(source_points)}...")

        print(f"\nEmbedding complete: {len(target_points)} success, {failed} failed")

        if args.dry_run:
            print(f"\n[DRY RUN] Would write {len(target_points)} points to {TARGET_COLLECTION}")
            return

        # Step 4: Write to target collection
        if target_points:
            print(f"\nUpserting {len(target_points)} points to {TARGET_COLLECTION}...")
            upsert_points(client, target_points)

        # Step 5: Verify
        resp = client.get(
            f"{QDRANT_URL}/collections/{TARGET_COLLECTION}",
            headers=qdrant_headers(),
        )
        resp.raise_for_status()
        info = resp.json()["result"]
        final_count = info["points_count"]
        print(f"\n✅ Migration complete!")
        print(f"   Source: {SOURCE_COLLECTION} ({len(source_points)} points, 384-dim)")
        print(f"   Target: {TARGET_COLLECTION} ({final_count} points, {TARGET_DIM}-dim)")
        if failed:
            print(f"   ⚠ {failed} points failed to migrate")
        print(f"\n   Old collection preserved as backup.")
        print(f"   Update tos-bridge default collection to '{TARGET_COLLECTION}'.")


if __name__ == "__main__":
    main()
