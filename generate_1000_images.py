"""Pixabay 1000+ HD Landscape Image Fetcher & Generator.

Uses Pixabay API to collect 1,000+ unique high-definition widescreen photos
across multiple curated themes and exports them as JSON and JS arrays.
"""

from __future__ import annotations

import json
import time
import requests

API_KEY = "7211070-2c62ab0836499a0ace2218a13"

CATEGORIES = [
    "nature landscape",
    "mountains lake",
    "night sky galaxy stars",
    "ocean sunset beach",
    "forest trees misty",
    "city architecture skyline",
    "travel world adventure",
    "books library aesthetic",
    "autumn road foliage",
    "winter snow scenic"
]


def fetch_1000_images(api_key: str = API_KEY, target_count: int = 1200) -> list[dict]:
    all_photos = []
    seen_ids = set()

    print(f"🚀 Starting Pixabay fetch for {target_count}+ HD photos...")

    for query in CATEGORIES:
        url = "https://pixabay.com/api/"
        params = {
            "key": api_key,
            "q": query,
            "image_type": "photo",
            "orientation": "horizontal",
            "safesearch": "true",
            "min_width": 1280,
            "per_page": 200,
            "page": 1,
        }

        try:
            res = requests.get(url, params=params, timeout=12)
            data = res.json()
            hits = data.get("hits", [])

            added = 0
            for h in hits:
                img_id = h.get("id")
                if img_id in seen_ids:
                    continue
                seen_ids.add(img_id)

                tag_words = (h.get("tags") or "Landscape").split(",")
                primary_tag = tag_words[0].strip().title()

                all_photos.append({
                    "id": img_id,
                    "url": h.get("largeImageURL") or h.get("webformatURL"),
                    "title": f"{primary_tag} Horizons",
                    "author": h.get("user") or "Pixabay",
                    "user_id": h.get("user_id"),
                    "page_url": h.get("pageURL"),
                    "tags": h.get("tags")
                })
                added += 1

            print(f"  ✓ '{query}': added {added} photos (Total: {len(all_photos)})")
            time.sleep(0.3)

            if len(all_photos) >= target_count:
                break

        except Exception as err:
            print(f"  ✗ Error fetching '{query}': {err}")

    print(f"\n🎉 Done! Collected {len(all_photos)} unique HD photos.")
    return all_photos


if __name__ == "__main__":
    photos = fetch_1000_images()

    # Save JSON
    with open("images_1000.json", "w", encoding="utf-8") as f:
        json.dump(photos, f, indent=2)

    # Save JS for direct browser embedding
    js_content = "var photoPool = " + json.dumps(photos, indent=2) + ";\n"
    with open("photo_pool.js", "w", encoding="utf-8") as f:
        f.write(js_content)

    print("Saved 'images_1000.json' and 'photo_pool.js'")
