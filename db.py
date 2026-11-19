"""SQLite storage and deduplication helper for FeedlyShortStories autoblogging."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def canonicalize_url(url: str | None) -> str:
    """Normalize a URL by removing fragments and common tracking query parameters."""
    if not url:
        return ""
    raw = url.strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme and not parsed.netloc:
        return raw

    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = re.sub(r"/+$", "", parsed.path or "") or "/"

    cleaned_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    cleaned_query.sort(key=lambda pair: (pair[0], pair[1]))
    query_str = urlencode(cleaned_query)

    return urlunparse((scheme, netloc, path, "", query_str, ""))


def normalize_title(title: str | None) -> str:
    """Normalize a post title so minor prefix/punctuation changes still match."""
    if not title:
        return ""
    cleaned = title.strip()
    cleaned = re.sub(r"^(news\s*[:~-]+\s*)+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned.lower(), flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_url_hash(source_url: str | None, guid: str | None, title: str | None) -> str:
    canon = canonicalize_url(source_url)
    if canon:
        return sha256_text(canon)
    if guid and guid.strip():
        return sha256_text(f"guid:{guid.strip()}")
    return sha256_text(f"title:{normalize_title(title)}")


def compute_title_hash(title: str | None) -> str:
    normalized = normalize_title(title)
    return sha256_text(normalized) if normalized else ""


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # DELETE journal mode keeps data/blog.db as a single self-contained file for Git commits.
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash TEXT UNIQUE NOT NULL,
            title_hash TEXT NOT NULL,
            source_url TEXT DEFAULT '',
            guid TEXT DEFAULT '',
            title TEXT NOT NULL,
            feed_name TEXT DEFAULT '',
            labels_json TEXT DEFAULT '[]',
            summary TEXT DEFAULT '',
            body_html TEXT,
            blogger_post_id TEXT DEFAULT '',
            blogger_url TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PUBLISHED',
            published_at TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_articles_title_hash ON articles(title_hash);
        CREATE INDEX IF NOT EXISTS idx_articles_guid ON articles(guid);
        CREATE INDEX IF NOT EXISTS idx_articles_status_created ON articles(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_articles_feed_name ON articles(feed_name);

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_type TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT DEFAULT '',
            feeds_checked INTEGER DEFAULT 0,
            items_found INTEGER DEFAULT 0,
            posts_published INTEGER DEFAULT 0,
            posts_skipped INTEGER DEFAULT 0,
            errors_count INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        );
        """
    )
    conn.commit()


def is_duplicate(
    conn: sqlite3.Connection,
    source_url: str | None,
    guid: str | None,
    title: str | None,
) -> bool:
    url_hash = compute_url_hash(source_url, guid, title)
    title_hash = compute_title_hash(title)
    clean_guid = (guid or "").strip()

    clauses = ["url_hash = ?"]
    params: list[str] = [url_hash]
    if clean_guid:
        clauses.append("guid = ?")
        params.append(clean_guid)
    if title_hash:
        clauses.append("title_hash = ?")
        params.append(title_hash)

    sql = f"SELECT 1 FROM articles WHERE {' OR '.join(clauses)} LIMIT 1"
    row = conn.execute(sql, tuple(params)).fetchone()
    return row is not None


def count_published_today(conn: sqlite3.Connection) -> int:
    today_prefix = utc_today_prefix()
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM articles
        WHERE status IN ('PUBLISHED', 'DRAFT')
          AND created_at LIKE ?
        """,
        (f"{today_prefix}%",),
    ).fetchone()
    return int(row["cnt"]) if row else 0


def record_article(
    conn: sqlite3.Connection,
    *,
    title: str,
    source_url: str = "",
    guid: str = "",
    feed_name: str = "",
    labels: list[str] | None = None,
    summary: str = "",
    body_html: str | None = None,
    blogger_post_id: str = "",
    blogger_url: str = "",
    status: str = "PUBLISHED",
    published_at: str = "",
    created_at: str | None = None,
) -> bool:
    url_hash = compute_url_hash(source_url, guid, title)
    title_hash = compute_title_hash(title)
    labels_json = json.dumps(labels or [], ensure_ascii=False)
    created = created_at or utc_now_iso()

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO articles (
            url_hash, title_hash, source_url, guid, title, feed_name,
            labels_json, summary, body_html, blogger_post_id, blogger_url,
            status, published_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            url_hash,
            title_hash,
            canonicalize_url(source_url) or source_url.strip(),
            (guid or "").strip(),
            title.strip(),
            feed_name.strip(),
            labels_json,
            summary[:500].strip() if summary else "",
            body_html,
            (blogger_post_id or "").strip(),
            (blogger_url or "").strip(),
            status,
            published_at or created,
            created,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def start_run(conn: sqlite3.Connection, trigger_type: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (trigger_type, started_at) VALUES (?, ?)",
        (trigger_type, utc_now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    feeds_checked: int,
    items_found: int,
    posts_published: int,
    posts_skipped: int,
    errors_count: int,
    notes: str = "",
) -> None:
    conn.execute(
        """
        UPDATE runs
        SET finished_at = ?,
            feeds_checked = ?,
            items_found = ?,
            posts_published = ?,
            posts_skipped = ?,
            errors_count = ?,
            notes = ?
        WHERE id = ?
        """,
        (
            utc_now_iso(),
            feeds_checked,
            items_found,
            posts_published,
            posts_skipped,
            errors_count,
            notes[:1000],
            run_id,
        ),
    )
    conn.commit()


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS cnt FROM articles").fetchone()["cnt"]
    by_status = {
        row["status"]: row["cnt"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM articles GROUP BY status ORDER BY cnt DESC"
        ).fetchall()
    }
    today = count_published_today(conn)
    recent_runs = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 5"
        ).fetchall()
    ]
    return {
        "total_articles": total,
        "published_today": today,
        "by_status": by_status,
        "recent_runs": recent_runs,
    }


def get_label_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT labels_json FROM articles").fetchall():
        try:
            labels = json.loads(row["labels_json"] or "[]")
        except json.JSONDecodeError:
            labels = []
        for lbl in labels:
            counts[lbl] = counts.get(lbl, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))


def search_articles(
    conn: sqlite3.Connection, query: str, limit: int = 20
) -> list[dict[str, Any]]:
    pattern = f"%{query.strip()}%"
    rows = conn.execute(
        """
        SELECT id, title, source_url, blogger_url, labels_json, status, published_at
        FROM articles
        WHERE title LIKE ? OR summary LIKE ? OR labels_json LIKE ?
        ORDER BY published_at DESC, id DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    ).fetchall()
    return [dict(r) for r in rows]
