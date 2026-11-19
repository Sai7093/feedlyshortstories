"""RSS-to-Blogger and Webhook Autoblogging Engine with SQLite Storage & Deduplication."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import feedparser
import requests

import db

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
BLOGGER_POSTS_URL = "https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts/"

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")


def clean_text(raw: str | None) -> str:
    """Strip HTML tags, unescape entities, and collapse whitespace."""
    if not raw:
        return ""
    no_scripts = SCRIPT_STYLE_RE.sub(" ", raw)
    no_tags = TAG_RE.sub(" ", no_scripts)
    unescaped = html.unescape(no_tags)
    return WHITESPACE_RE.sub(" ", unescaped).strip()


def clean_title(raw: str | None, prefix: str = "") -> str:
    text = clean_text(raw)
    if prefix and text and not text.lower().startswith(prefix.lower()):
        text = f"{prefix}{text}"
    return text[:250].strip()


def parse_entry_date(entry: Any) -> str:
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            try:
                dt = parsedate_to_datetime(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                pass
    for key in ("published_parsed", "updated_parsed"):
        parsed_tuple = entry.get(key)
        if parsed_tuple:
            try:
                dt = datetime(*parsed_tuple[:6], tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                pass
    return db.utc_now_iso()


def is_tracker_image(url: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered
        for token in (
            "feedburner.com/~r/",
            "feedproxy.google.com",
            "/pixel",
            "1x1",
            "doubleclick.net",
            "scorecardresearch.com",
        )
    )


def extract_image_url(entry: Any) -> str:
    media_content = entry.get("media_content") or []
    best_url = ""
    best_width = -1
    for item in media_content:
        url = (item.get("url") or "").strip()
        if not url or is_tracker_image(url):
            continue
        medium = (item.get("medium") or "").lower()
        mime = (item.get("type") or "").lower()
        if medium and medium != "image" and not mime.startswith("image/"):
            continue
        try:
            width = int(item.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        if width >= best_width:
            best_width = width
            best_url = url
    if best_url:
        return best_url

    for item in entry.get("media_thumbnail") or []:
        url = (item.get("url") or "").strip()
        if url and not is_tracker_image(url):
            return url

    for item in entry.get("enclosures") or []:
        url = (item.get("href") or item.get("url") or "").strip()
        mime = (item.get("type") or "").lower()
        if url and mime.startswith("image/") and not is_tracker_image(url):
            return url

    raw_blocks = []
    for block in entry.get("content") or []:
        if isinstance(block, dict) and block.get("value"):
            raw_blocks.append(block["value"])
    if entry.get("summary"):
        raw_blocks.append(entry["summary"])

    for raw in raw_blocks:
        for match in IMG_SRC_RE.findall(raw):
            if match.startswith(("http://", "https://")) and not is_tracker_image(match):
                return match

    return ""


def get_fallback_image(seed: str) -> str:
    """Return a deterministic high-resolution stock image URL from the 250 verified links."""
    links_file = Path(__file__).resolve().parent / "unsplash_250_links.txt"
    if not links_file.exists():
        return ""
    try:
        lines = [line.strip() for line in links_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return ""
        idx = abs(hash(seed)) % len(lines)
        return lines[idx]
    except Exception:
        return ""


def normalize_labels(
    base_labels: list[str],
    extra_tags: list[str],
    label_map: dict[str, str],
) -> list[str]:
    """Normalize labels via label_map, deduplicate, and enforce Blogger's 200-char limit."""
    normalized_map = {k.strip().lower(): v.strip() for k, v in label_map.items()}
    result: list[str] = []
    seen_lower: set[str] = set()
    total_chars = 0

    for raw in [*base_labels, *extra_tags]:
        cleaned = clean_text(raw).replace(",", " ").strip()
        if not cleaned:
            continue
        mapped = normalized_map.get(cleaned.lower(), cleaned)
        mapped = mapped.replace(",", " ").strip()
        if not mapped:
            continue
        key = mapped.lower()
        if key in seen_lower:
            continue
        # Blogger counts comma-separated labels up to 200 chars total
        added_len = len(mapped) + (1 if result else 0)
        if total_chars + added_len > 190:
            break
        seen_lower.add(key)
        result.append(mapped)
        total_chars += added_len

    return result


def build_post_html(
    *,
    summary: str,
    source_url: str,
    source_label: str,
    image_url: str = "",
    read_more_text: str = "Read full story",
) -> str:
    parts: list[str] = []

    if image_url:
        safe_img = html.escape(image_url, quote=True)
        parts.append(
            f'<div class="autoblog-cover" style="margin-bottom:14px;">'
            f'<img src="{safe_img}" alt="" loading="lazy" '
            f'style="max-width:100%;height:auto;border-radius:8px;display:block;"/>'
            f"</div>"
        )

    if summary:
        safe_summary = html.escape(summary)
        parts.append(f"<p>{safe_summary}</p>")

    if source_url:
        safe_url = html.escape(source_url, quote=True)
        safe_source = html.escape(source_label or "Source")
        safe_cta = html.escape(read_more_text)
        parts.append(
            f'<p style="margin-top:14px;">'
            f'<strong>{safe_cta}:</strong> '
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_source}</a>'
            f"</p>"
        )

    return "\n".join(parts)


def get_blogger_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    timeout: int = 20,
) -> str:
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to refresh Blogger OAuth token ({resp.status_code}): {resp.text}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("OAuth response did not contain an access_token.")
    return token


def publish_to_blogger(
    *,
    blog_id: str,
    access_token: str,
    title: str,
    content_html: str,
    labels: list[str],
    is_draft: bool = False,
    timeout: int = 25,
) -> dict[str, Any]:
    url = BLOGGER_POSTS_URL.format(blog_id=blog_id)
    payload: dict[str, Any] = {
        "kind": "blogger#post",
        "blog": {"id": blog_id},
        "title": title,
        "content": content_html,
    }
    if labels:
        payload["labels"] = labels

    resp = requests.post(
        url,
        params={"isDraft": "true" if is_draft else "false"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Blogger API error ({resp.status_code}): {resp.text}")
    return resp.json()


def require_blogger_creds() -> tuple[str, str, str, str]:
    client_id = os.environ.get("BLOGGER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("BLOGGER_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("BLOGGER_REFRESH_TOKEN", "").strip()
    blog_id = os.environ.get("BLOGGER_BLOG_ID", "").strip()

    missing = [
        name
        for name, val in (
            ("BLOGGER_CLIENT_ID", client_id),
            ("BLOGGER_CLIENT_SECRET", client_secret),
            ("BLOGGER_REFRESH_TOKEN", refresh_token),
            ("BLOGGER_BLOG_ID", blog_id),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            "Missing required Blogger environment variables: " + ", ".join(missing)
        )
    return client_id, client_secret, refresh_token, blog_id


def send_notify_webhook(message: str) -> None:
    webhook_url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    try:
        requests.post(
            webhook_url,
            json={"content": message, "text": message},
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"[WARN] Notification webhook failed: {exc}", file=sys.stderr)


def load_config(config_path: str | Path) -> dict[str, Any]:
    with open(config_path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def run_rss_pipeline(
    config: dict[str, Any],
    db_path: str | Path,
    *,
    dry_run: bool = False,
    only_feed: str | None = None,
    trigger_type: str = "manual",
) -> int:
    settings = config.get("settings", {})
    label_map = config.get("label_map", {})
    feeds = config.get("feeds", [])

    max_per_run = int(settings.get("max_posts_per_run", 3))
    max_per_day = int(settings.get("max_posts_per_day", 25))
    timeout = int(settings.get("request_timeout_sec", 20))
    min_summary_chars = int(settings.get("min_summary_chars", 30))
    post_delay = float(settings.get("post_delay_sec", 2))
    user_agent = settings.get("user_agent", "FeedlyShortStoriesBot/1.0")
    default_status = str(settings.get("default_status", "LIVE")).upper()
    read_more_text = settings.get("read_more_text", "Read full story")

    conn = db.get_connection(db_path)
    run_id = db.start_run(conn, "dry-run" if dry_run else trigger_type)

    published_today = db.count_published_today(conn)
    remaining_today = max(0, max_per_day - published_today)
    run_budget = min(max_per_run, remaining_today)

    print(
        f"Starting RSS run (dry_run={dry_run}, published_today={published_today}/{max_per_day}, run_budget={run_budget})"
    )

    feeds_checked = 0
    items_found = 0
    posts_published = 0
    posts_skipped = 0
    errors: list[str] = []
    published_titles: list[str] = []

    access_token = ""
    blog_id = ""
    if not dry_run and run_budget > 0:
        client_id, client_secret, refresh_token, blog_id = require_blogger_creds()
        access_token = get_blogger_access_token(
            client_id, client_secret, refresh_token, timeout=timeout
        )

    if run_budget <= 0:
        print("Daily posting quota reached; skipping RSS publishing for today.")
        db.finish_run(
            conn,
            run_id,
            feeds_checked=0,
            items_found=0,
            posts_published=0,
            posts_skipped=0,
            errors_count=0,
            notes="Daily quota reached",
        )
        conn.close()
        return 0

    for feed_cfg in feeds:
        if posts_published >= run_budget:
            break
        if not feed_cfg.get("enabled", False):
            continue
        feed_name = feed_cfg.get("name", feed_cfg.get("url", "Unnamed Feed"))
        if only_feed and feed_name.lower() != only_feed.lower():
            continue

        feed_url = feed_cfg.get("url", "").strip()
        if not feed_url:
            continue

        feeds_checked += 1
        print(f"\nFetching feed: {feed_name} ({feed_url})")
        try:
            resp = requests.get(
                feed_url,
                headers={"User-Agent": user_agent},
                timeout=timeout,
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            msg = f"Feed fetch failed [{feed_name}]: {exc}"
            print(f"[ERROR] {msg}", file=sys.stderr)
            errors.append(msg)
            continue

        entries = parsed.get("entries") or []
        items_found += len(entries)
        feed_limit = min(int(feed_cfg.get("max_per_run", 2)), run_budget - posts_published)
        feed_published = 0

        for entry in entries:
            if feed_published >= feed_limit or posts_published >= run_budget:
                break

            title = clean_title(entry.get("title"), feed_cfg.get("title_prefix", ""))
            source_url = (entry.get("link") or "").strip()
            guid = (entry.get("id") or source_url).strip()

            if not title or not source_url:
                posts_skipped += 1
                continue

            if db.is_duplicate(conn, source_url, guid, title):
                posts_skipped += 1
                continue

            raw_summary = ""
            if entry.get("summary"):
                raw_summary = entry["summary"]
            elif entry.get("content"):
                raw_summary = entry["content"][0].get("value", "")

            summary = clean_text(raw_summary)
            if len(summary) < min_summary_chars:
                posts_skipped += 1
                continue

            extra_tags: list[str] = []
            if feed_cfg.get("include_feed_tags", False):
                extra_tags = [
                    t.get("term", "") for t in (entry.get("tags") or []) if t.get("term")
                ]

            labels = normalize_labels(
                feed_cfg.get("labels", []),
                extra_tags,
                label_map,
            )
            image_url = extract_image_url(entry) if feed_cfg.get("include_image", True) else ""
            if not image_url and feed_cfg.get("include_image", True):
                image_url = get_fallback_image(title)
            source_label = feed_cfg.get("source_label") or feed_name
            content_html = build_post_html(
                summary=summary,
                source_url=source_url,
                source_label=source_label,
                image_url=image_url,
                read_more_text=read_more_text,
            )

            status = str(feed_cfg.get("status", default_status)).upper()
            is_draft = status == "DRAFT"
            published_at = parse_entry_date(entry)

            if dry_run:
                print(
                    f"  [DRY-RUN] Would publish ({status}): {title}\n"
                    f"            Labels: {labels}\n"
                    f"            Source: {source_url}\n"
                    f"            Image : {image_url or '(none)'}"
                )
                posts_published += 1
                feed_published += 1
                continue

            try:
                created = publish_to_blogger(
                    blog_id=blog_id,
                    access_token=access_token,
                    title=title,
                    content_html=content_html,
                    labels=labels,
                    is_draft=is_draft,
                    timeout=timeout,
                )
                blogger_post_id = str(created.get("id", ""))
                blogger_url = str(created.get("url", ""))
                db.record_article(
                    conn,
                    title=title,
                    source_url=source_url,
                    guid=guid,
                    feed_name=feed_name,
                    labels=labels,
                    summary=summary,
                    blogger_post_id=blogger_post_id,
                    blogger_url=blogger_url,
                    status="DRAFT" if is_draft else "PUBLISHED",
                    published_at=published_at,
                )
                posts_published += 1
                feed_published += 1
                published_titles.append(f"- {title} ({blogger_url or 'draft'})")
                print(f"  [PUBLISHED] {title} -> {blogger_url or blogger_post_id}")
                if post_delay > 0 and posts_published < run_budget:
                    time.sleep(post_delay)
            except Exception as exc:
                msg = f"Publish failed [{title[:60]}]: {exc}"
                print(f"[ERROR] {msg}", file=sys.stderr)
                errors.append(msg)

    db.finish_run(
        conn,
        run_id,
        feeds_checked=feeds_checked,
        items_found=items_found,
        posts_published=posts_published,
        posts_skipped=posts_skipped,
        errors_count=len(errors),
        notes="\n".join(errors),
    )
    conn.close()

    print(
        f"\nDone: feeds_checked={feeds_checked}, items_found={items_found}, "
        f"published={posts_published}, skipped={posts_skipped}, errors={len(errors)}"
    )

    if not dry_run and (posts_published > 0 or errors):
        summary_lines = [
            f"FeedlyShortStories Autoblog ({trigger_type}): "
            f"{posts_published} published, {len(errors)} errors."
        ]
        summary_lines.extend(published_titles[:5])
        if errors:
            summary_lines.append("Errors: " + "; ".join(errors[:3]))
        send_notify_webhook("\n".join(summary_lines))

    return 1 if errors and posts_published == 0 else 0


def run_custom_publish(
    payload: dict[str, Any],
    config: dict[str, Any],
    db_path: str | Path,
    *,
    dry_run: bool = False,
    trigger_type: str = "webhook",
) -> int:
    label_map = config.get("label_map", {})
    title = clean_title(payload.get("title", ""))
    raw_content = str(payload.get("content", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    raw_labels = payload.get("labels") or []
    if isinstance(raw_labels, str):
        raw_labels = [x.strip() for x in raw_labels.split(",") if x.strip()]
    is_draft = bool(payload.get("is_draft", False))

    if not title or not raw_content:
        raise ValueError("Custom publish payload must include non-empty 'title' and 'content'.")

    labels = normalize_labels(list(raw_labels), [], label_map)
    content_html = raw_content if "<" in raw_content else f"<p>{html.escape(raw_content)}</p>"
    if source_url and source_url not in content_html:
        safe_url = html.escape(source_url, quote=True)
        content_html += (
            f'\n<p><strong>Source:</strong> '
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_url}</a></p>'
        )

    conn = db.get_connection(db_path)
    if db.is_duplicate(conn, source_url, source_url, title):
        print(f"[SKIPPED] Duplicate post detected in SQLite: {title}")
        conn.close()
        return 0

    if dry_run:
        print(f"[DRY-RUN] Would publish custom post: {title}\nLabels: {labels}")
        conn.close()
        return 0

    client_id, client_secret, refresh_token, blog_id = require_blogger_creds()
    access_token = get_blogger_access_token(client_id, client_secret, refresh_token)
    created = publish_to_blogger(
        blog_id=blog_id,
        access_token=access_token,
        title=title,
        content_html=content_html,
        labels=labels,
        is_draft=is_draft,
    )
    blogger_post_id = str(created.get("id", ""))
    blogger_url = str(created.get("url", ""))
    db.record_article(
        conn,
        title=title,
        source_url=source_url,
        guid=source_url,
        feed_name=f"{trigger_type}:custom",
        labels=labels,
        summary=clean_text(content_html),
        blogger_post_id=blogger_post_id,
        blogger_url=blogger_url,
        status="DRAFT" if is_draft else "PUBLISHED",
    )
    conn.close()
    print(f"[PUBLISHED] Custom post '{title}' -> {blogger_url or blogger_post_id}")
    send_notify_webhook(f"Custom post published ({trigger_type}): {title} - {blogger_url}")
    return 0


def print_stats(db_path: str | Path) -> None:
    conn = db.get_connection(db_path)
    stats = db.get_stats(conn)
    labels = db.get_label_counts(conn)
    conn.close()

    print("=== SQLite Archive Stats ===")
    print(f"Database       : {db_path}")
    print(f"Total articles : {stats['total_articles']}")
    print(f"Published today: {stats['published_today']}")
    print(f"By status      : {stats['by_status']}")
    print("\nTop 15 Labels:")
    for name, count in labels[:15]:
        print(f"  - {name}: {count}")
    if stats["recent_runs"]:
        print("\nRecent Runs:")
        for run in stats["recent_runs"]:
            print(
                f"  #{run['id']} [{run['trigger_type']}] {run['started_at']} "
                f"published={run['posts_published']} skipped={run['posts_skipped']} errors={run['errors_count']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="FeedlyShortStories Blogger Autoblogger")
    parser.add_argument("--config", default="feeds.json", help="Path to feeds.json")
    parser.add_argument("--db", default=None, help="Override SQLite DB path")
    parser.add_argument("--dry-run", action="store_true", help="Preview without publishing")
    parser.add_argument("--feed", default=None, help="Run only one feed by name")
    parser.add_argument("--trigger-type", default="manual", help="Run trigger label")
    parser.add_argument("--publish-json", default=None, help="JSON payload for custom post")
    parser.add_argument("--publish-file", default=None, help="Path to JSON payload file")
    parser.add_argument("--stats", action="store_true", help="Print DB stats and exit")
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = args.db or config.get("settings", {}).get("db_path", "data/blog.db")

    if args.stats:
        print_stats(db_path)
        return 0

    if args.publish_file:
        with open(args.publish_file, "r", encoding="utf-8-sig") as fh:
            payload = json.load(fh)
        return run_custom_publish(
            payload, config, db_path, dry_run=args.dry_run, trigger_type=args.trigger_type
        )

    if args.publish_json:
        payload = json.loads(args.publish_json)
        return run_custom_publish(
            payload, config, db_path, dry_run=args.dry_run, trigger_type=args.trigger_type
        )

    return run_rss_pipeline(
        config,
        db_path,
        dry_run=args.dry_run,
        only_feed=args.feed,
        trigger_type=args.trigger_type,
    )


if __name__ == "__main__":
    raise SystemExit(main())
