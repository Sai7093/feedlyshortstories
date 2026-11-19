"""Stream-import a Blogger XML or Google Takeout Atom backup into SQLite and audit labels."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import db

ATOM_NS = "http://www.w3.org/2005/Atom"
BLOGGER_NS = "http://schemas.google.com/blogger/2018"

ANCHOR_RE = re.compile(
    r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
HREF_RE = re.compile(r"""<a\b[^>]+href=["']([^"']+)["']""", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

INTERNAL_HOST_SUFFIXES = (
    "blogspot.com",
    "blogger.com",
    "googleusercontent.com",
    "ggpht.com",
    "feedburner.com",
    "feedproxy.google.com",
    "ift.tt",
    "bit.ly",
    "t.co",
    "goo.gl",
)

PRIMARY_CTA_TEXTS = {
    "click here",
    "read more",
    "read full story",
    "[link]",
    "link",
    "source",
    "full story",
    "view post",
}

NON_ARTICLE_PATH_PREFIXES = {
    "www.reddit.com": ("/user/", "/u/"),
    "reddit.com": ("/user/", "/u/"),
    "old.reddit.com": ("/user/", "/u/"),
    "crunchbase.com": ("/organization/", "/person/"),
    "www.crunchbase.com": ("/organization/", "/person/"),
}


def local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def element_text(elem: ET.Element) -> str:
    return "".join(elem.itertext())


def clean_plain_text(raw: str | None) -> str:
    if not raw:
        return ""
    stripped = TAG_RE.sub(" ", raw)
    return WS_RE.sub(" ", html.unescape(stripped)).strip()


def _is_ignored_external_url(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return True
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return True
    if any(host == suf or host.endswith("." + suf) for suf in INTERNAL_HOST_SUFFIXES):
        return True
    if host in ("twitter.com", "www.twitter.com", "x.com", "www.x.com"):
        return True
    prefixes = NON_ARTICLE_PATH_PREFIXES.get(host, ())
    if prefixes and parsed.path.lower().startswith(prefixes):
        return True
    return False


def extract_external_source_url(body_html: str) -> str:
    """Find the external article link inside syndicated posts (e.g. 'click here' or '[link]')."""
    if not body_html:
        return ""

    anchors = ANCHOR_RE.findall(body_html)
    # Pass 1: prefer explicit "click here" / "[link]" / "read more" CTA anchors
    for href, inner_html in anchors:
        candidate = html.unescape(href).strip()
        if _is_ignored_external_url(candidate):
            continue
        anchor_text = clean_plain_text(inner_html).lower()
        if anchor_text in PRIMARY_CTA_TEXTS:
            return candidate

    # Pass 2: fallback to first non-profile external anchor
    for href, _ in anchors:
        candidate = html.unescape(href).strip()
        if not _is_ignored_external_url(candidate):
            return candidate

    # Pass 3: unclosed anchor tags fallback
    for href in HREF_RE.findall(body_html):
        candidate = html.unescape(href).strip()
        if not _is_ignored_external_url(candidate):
            return candidate

    return ""


def detect_blog_url(backup_path: Path, explicit_blog_url: str | None = None) -> str:
    """Resolve blog base URL from explicit arg or sibling Google Takeout settings.csv."""
    if explicit_blog_url is not None:
        return explicit_blog_url.strip().rstrip("/")

    settings_csv = backup_path.parent / "settings.csv"
    if settings_csv.exists():
        try:
            with settings_csv.open("r", encoding="utf-8", errors="replace") as fh:
                rows = list(csv.reader(fh))
            if len(rows) >= 2:
                mapping = dict(zip(rows[0], rows[1]))
                custom_domain = (mapping.get("blog_custom_domain") or "").strip()
                subdomain = (mapping.get("blog_subdomain") or "").strip()
                if custom_domain:
                    if not custom_domain.startswith(("http://", "https://")):
                        custom_domain = f"https://{custom_domain}"
                    return custom_domain.rstrip("/")
                if subdomain:
                    return f"https://{subdomain}.blogspot.com"
        except OSError:
            pass
    return ""


def resolve_backup_file(path_or_dir: str | Path) -> Path:
    """Accept an .xml/.atom file or a Google Takeout directory and return the feed file path."""
    path = Path(path_or_dir)
    if not path.exists():
        raise FileNotFoundError(f"Backup path not found: {path}")
    if path.is_file():
        return path

    atom_candidates = sorted(
        path.rglob("feed.atom"),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if atom_candidates:
        return atom_candidates[0]

    xml_candidates = [
        p
        for p in path.rglob("*.xml")
        if p.name.lower() not in {"theme-layouts.xml"}
    ]
    if xml_candidates:
        xml_candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        return xml_candidates[0]

    raise FileNotFoundError(
        f"No feed.atom or Blogger backup .xml found inside directory: {path}"
    )


def parse_blogger_entry(
    entry: ET.Element,
    *,
    blog_url: str = "",
    summary_chars: int = 0,
) -> dict | None:
    entry_type = ""
    blogger_status = "LIVE"
    trashed = ""
    kind_terms: list[str] = []
    labels: list[str] = []
    blogger_url = ""
    filename = ""
    entry_id = ""
    title = ""
    published = ""
    body_html = ""

    for child in entry:
        name = local_tag(child.tag)
        ns = child.tag[1:].split("}", 1)[0] if child.tag.startswith("{") else ""
        text = element_text(child)

        if name == "id" and text.strip():
            entry_id = text.strip()
        elif name == "title" and text.strip():
            title = clean_plain_text(text)
        elif name == "published" and text.strip():
            published = text.strip()
        elif name == "content" and text:
            body_html = text
        elif name == "type" and ns == BLOGGER_NS and text.strip():
            entry_type = text.strip().upper()
        elif name == "status" and ns == BLOGGER_NS and text.strip():
            blogger_status = text.strip().upper()
        elif name == "trashed" and ns == BLOGGER_NS and text.strip():
            trashed = text.strip()
        elif name == "filename" and ns == BLOGGER_NS and text.strip():
            filename = text.strip()
        elif name == "link":
            if child.attrib.get("rel") == "alternate" and child.attrib.get("type") == "text/html":
                blogger_url = (child.attrib.get("href") or "").strip()
        elif name == "category":
            scheme = (child.attrib.get("scheme") or "").strip()
            term = (child.attrib.get("term") or "").strip()
            if not term:
                continue
            if scheme.endswith("#kind") or "#kind" in term:
                kind_terms.append(term.lower())
            else:
                labels.append(term)

    if trashed or blogger_status in ("TRASHED", "SOFT_TRASHED"):
        return None

    # Determine whether this entry is an actual blog post (not comment/page/settings)
    is_post = False
    if entry_type:
        is_post = entry_type == "POST"
    elif kind_terms:
        is_post = any(t.endswith("#post") for t in kind_terms)

    if not is_post:
        return None

    is_untitled = not bool(title)
    if is_untitled:
        title = "(Untitled Post)"

    post_id_match = re.search(r"post-(\d+)", entry_id)
    post_id = post_id_match.group(1) if post_id_match else entry_id

    if not blogger_url and filename:
        if blog_url and filename.startswith("/"):
            blogger_url = f"{blog_url.rstrip('/')}{filename}"
        else:
            blogger_url = filename

    external_source = extract_external_source_url(body_html)
    plain_body = clean_plain_text(body_html)
    summary = plain_body[:summary_chars] if summary_chars > 0 else ""
    archive_summary = plain_body[: max(250, summary_chars)]

    if blogger_status == "DRAFT":
        status = "IMPORTED_DRAFT"
    elif blogger_status == "LIVE":
        status = "IMPORTED"
    else:
        status = f"IMPORTED_{blogger_status}"

    return {
        "post_id": post_id,
        "entry_id": entry_id,
        "title": title,
        "is_untitled": is_untitled,
        "published_at": published,
        "labels": labels,
        "blogger_url": blogger_url,
        "source_url": external_source or blogger_url,
        "summary": summary,
        "archive_summary": archive_summary,
        "body_html": body_html,
        "status": status,
    }


def import_xml_to_db(
    xml_path: str | Path,
    db_path: str | Path,
    *,
    include_body: bool = False,
    archive_db_path: str | Path | None = None,
    blog_url: str | None = None,
    summary_chars: int = 0,
) -> dict[str, int]:
    resolved_path = resolve_backup_file(xml_path)
    resolved_blog_url = detect_blog_url(resolved_path, blog_url)

    conn = db.get_connection(db_path)
    archive_conn: sqlite3.Connection | None = (
        db.get_connection(archive_db_path) if archive_db_path else None
    )

    total_entries = 0
    posts_found = 0
    inserted = 0
    skipped = 0
    import_ts = db.utc_now_iso()

    insert_sql = """
        INSERT OR IGNORE INTO articles (
            url_hash, title_hash, source_url, guid, title, feed_name,
            labels_json, summary, body_html, blogger_post_id, blogger_url,
            status, published_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    batch_main: list[tuple] = []
    batch_archive: list[tuple] = []

    context = ET.iterparse(str(resolved_path), events=("end",))
    for _, elem in context:
        if local_tag(elem.tag) != "entry":
            continue
        total_entries += 1
        post = parse_blogger_entry(
            elem,
            blog_url=resolved_blog_url,
            summary_chars=summary_chars,
        )
        elem.clear()

        if not post:
            continue
        posts_found += 1

        url_hash = db.compute_url_hash(
            post["source_url"], post["entry_id"], "" if post["is_untitled"] else post["title"]
        )
        title_hash = "" if post["is_untitled"] else db.compute_title_hash(post["title"])
        canon_src = db.canonicalize_url(post["source_url"]) or post["source_url"]
        labels_json = json.dumps(post["labels"], ensure_ascii=False)
        pub = post["published_at"] or import_ts

        lite_guid = "" if canon_src else post["entry_id"]

        batch_main.append((
            url_hash,
            title_hash,
            canon_src,
            lite_guid,
            post["title"],
            "blogger-backup",
            labels_json,
            post["summary"],
            post["body_html"] if include_body else None,
            post["post_id"],
            post["blogger_url"],
            post["status"],
            pub,
            pub,
        ))
        if archive_conn is not None:
            batch_archive.append((
                url_hash,
                title_hash,
                canon_src,
                post["entry_id"],
                post["title"],
                "blogger-backup",
                labels_json,
                post["archive_summary"],
                post["body_html"],
                post["post_id"],
                post["blogger_url"],
                post["status"],
                pub,
                pub,
            ))

        if len(batch_main) >= 1000:
            before = conn.total_changes
            conn.executemany(insert_sql, batch_main)
            conn.commit()
            added = conn.total_changes - before
            inserted += added
            skipped += len(batch_main) - added
            batch_main.clear()

            if archive_conn is not None and batch_archive:
                archive_conn.executemany(insert_sql, batch_archive)
                archive_conn.commit()
                batch_archive.clear()

    if batch_main:
        before = conn.total_changes
        conn.executemany(insert_sql, batch_main)
        conn.commit()
        added = conn.total_changes - before
        inserted += added
        skipped += len(batch_main) - added

    if archive_conn is not None and batch_archive:
        archive_conn.executemany(insert_sql, batch_archive)
        archive_conn.commit()

    conn.close()
    if archive_conn is not None:
        archive_conn.close()

    return {
        "total_xml_entries": total_entries,
        "posts_found": posts_found,
        "inserted": inserted,
        "duplicates_skipped": skipped,
    }


def print_label_report(db_path: str | Path) -> None:
    conn = db.get_connection(db_path)
    counts = db.get_label_counts(conn)
    conn.close()

    print(f"Total distinct labels: {len(counts)}\n")
    print("Top 30 Labels:")
    for label, count in counts[:30]:
        print(f"  {label}: {count}")

    # Detect near-duplicates (ignoring case, spaces, hyphens, emojis/punctuation)
    buckets: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for label, count in counts:
        key = re.sub(r"[^a-z0-9]", "", label.lower())
        if key:
            buckets[key].append((label, count))

    near_dupes = [group for group in buckets.values() if len(group) > 1]
    if near_dupes:
        print("\nPotential Duplicate Labels to Normalize:")
        for group in sorted(near_dupes, key=lambda g: -sum(c for _, c in g)):
            formatted = ", ".join(f"'{lbl}' ({cnt})" for lbl, cnt in group)
            print(f"  - {formatted}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import Blogger XML or Google Takeout feed.atom into SQLite"
    )
    parser.add_argument(
        "backup_path",
        nargs="?",
        help="Path to Blogger .xml, Takeout feed.atom, or extracted Takeout directory",
    )
    parser.add_argument("--db", default="data/blog.db", help="Lightweight SQLite DB path")
    parser.add_argument(
        "--archive-db",
        default=None,
        help="Optional full-body SQLite DB path (e.g. data/archive.db or Google Drive path)",
    )
    parser.add_argument(
        "--blog-url",
        default=None,
        help="Base blog URL to turn relative Takeout filenames into full links "
        "(auto-detected from settings.csv or defaults to https://feedlyshortstories.blogspot.com)",
    )
    parser.add_argument(
        "--summary-chars",
        type=int,
        default=0,
        help="Plain-text snippet length kept in --db (default: 0 to keep GitHub DB compact)",
    )
    parser.add_argument(
        "--include-body",
        action="store_true",
        help="Store full HTML post bodies inside --db as well (larger file size)",
    )
    parser.add_argument(
        "--label-report",
        action="store_true",
        help="Print label counts and potential duplicate labels",
    )
    args = parser.parse_args()

    if args.backup_path:
        try:
            resolved = resolve_backup_file(args.backup_path)
        except FileNotFoundError as exc:
            print(f"Error: {exc}")
            return 1

        blog_url = args.blog_url
        if blog_url is None:
            detected = detect_blog_url(resolved, None)
            blog_url = detected or "https://feedlyshortstories.blogspot.com"

        print(f"Importing {resolved} (blog_url={blog_url}) into {args.db} ...")
        result = import_xml_to_db(
            resolved,
            args.db,
            include_body=args.include_body,
            archive_db_path=args.archive_db,
            blog_url=blog_url,
            summary_chars=args.summary_chars,
        )
        print("Import complete:")
        for k, v in result.items():
            print(f"  {k}: {v}")

    if args.label_report or not args.backup_path:
        print()
        print_label_report(args.db)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
