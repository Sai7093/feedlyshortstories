"""Unit and integration tests for FeedlyShortStories autoblogging tools."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autoblog
import db
import import_backup


SAMPLE_ATOM_BACKUP = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom' xmlns:blogger='http://schemas.google.com/blogger/2018'>
  <entry>
    <id>tag:blogger.com,1999:blog-111.post-222</id>
    <published>2024-05-01T10:00:00Z</published>
    <category scheme='http://schemas.google.com/g/2005#kind' term='http://schemas.google.com/blogger/2008/kind#post'/>
    <category scheme='http://www.blogger.com/atom/ns#' term='NDTV News- Special'/>
    <category scheme='http://www.blogger.com/atom/ns#' term='Short Stories'/>
    <title type='text'>NEWS:~Sample Syndicated Story</title>
    <content type='html'>&lt;p&gt;A short news snippet.&lt;/p&gt;&lt;p&gt;To Read more &lt;a href=&quot;https://www.ndtv.com/india-news/sample-story-123?utm_source=rss&quot;&gt;click here&lt;/a&gt;&lt;/p&gt;</content>
    <link rel='alternate' type='text/html' href='https://feedlyshortstories.blogspot.com/2024/05/sample.html'/>
  </entry>
  <entry>
    <id>tag:blogger.com,1999:blog-111.comment-999</id>
    <published>2024-05-02T10:00:00Z</published>
    <category scheme='http://schemas.google.com/g/2005#kind' term='http://schemas.google.com/blogger/2008/kind#comment'/>
    <title type='text'>Nice post!</title>
    <content type='html'>Comment should be ignored.</content>
  </entry>
  <entry>
    <id>tag:blogger.com,1999:blog-111.post-333</id>
    <published>2025-01-10T08:00:00Z</published>
    <blogger:type>POST</blogger:type>
    <blogger:status>LIVE</blogger:status>
    <blogger:filename>/2025/01/original-short-story.html</blogger:filename>
    <category scheme='http://www.blogger.com/atom/ns#' term='SHORT STORY'/>
    <title type='text'>Midnight Train</title>
    <content type='html'>&lt;p&gt;An original 50-word story.&lt;/p&gt;</content>
  </entry>
</feed>
"""


class AutoblogTests(unittest.TestCase):
    def test_url_canonicalization_and_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = db.get_connection(db_path)

            db.record_article(
                conn,
                title="NEWS:~ India Wins Match!",
                source_url="https://www.ndtv.com/sports/india-wins-123/?utm_source=feed&fbclid=abc#top",
                labels=["News"],
                status="PUBLISHED",
            )

            self.assertTrue(
                db.is_duplicate(
                    conn,
                    "https://ndtv.com/sports/india-wins-123",
                    "",
                    "Different Headline Variant",
                )
            )
            self.assertTrue(
                db.is_duplicate(
                    conn,
                    "https://other-mirror.example.com/article",
                    "",
                    "India Wins Match",
                )
            )
            self.assertFalse(
                db.is_duplicate(
                    conn,
                    "https://ndtv.com/sports/another-story-456",
                    "",
                    "Completely Different Story",
                )
            )
            self.assertEqual(db.count_published_today(conn), 1)
            conn.close()

    def test_label_normalization_and_length_cap(self) -> None:
        label_map = {
            "ndtv news- special": "NDTV News",
            "short story": "Short Stories",
        }
        many_tags = [
            "NDTV News- Special",
            "short story",
            "Short Stories",
            "Tag, With Comma",
            *[f"ExtraCategoryNumber{i:02d}" for i in range(20)],
        ]
        normalized = autoblog.normalize_labels([], many_tags, label_map)
        self.assertEqual(normalized[0], "NDTV News")
        self.assertEqual(normalized[1], "Short Stories")
        self.assertNotIn(",", "".join(normalized))
        self.assertLessEqual(len(",".join(normalized)), 190)

    def test_title_unescape_and_html_builder(self) -> None:
        title = autoblog.clean_title("J&amp;K News &amp; Updates   today", "NEWS: ")
        self.assertEqual(title, "NEWS: J&K News & Updates today")

        body = autoblog.build_post_html(
            summary="Summary <script>alert(1)</script> text",
            source_url="https://example.com/story?a=1&b=2",
            source_label="Example News",
            image_url="https://example.com/cover.jpg",
        )
        self.assertIn("https://example.com/cover.jpg", body)
        self.assertNotIn("<script>", body)
        self.assertIn("Example News", body)

    def test_import_blogger_xml_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xml_file = Path(tmp) / "backup.xml"
            lite_db = Path(tmp) / "blog.db"
            full_db = Path(tmp) / "archive.db"
            xml_file.write_text(SAMPLE_ATOM_BACKUP, encoding="utf-8")

            res = import_backup.import_xml_to_db(
                xml_file, lite_db, include_body=False, archive_db_path=full_db
            )
            self.assertEqual(res["posts_found"], 2)
            self.assertEqual(res["inserted"], 2)

            conn_lite = db.get_connection(lite_db)
            conn_full = db.get_connection(full_db)
            self.assertTrue(
                db.is_duplicate(
                    conn_lite,
                    "https://ndtv.com/india-news/sample-story-123",
                    "",
                    "Unrelated",
                )
            )
            lite_row = conn_lite.execute(
                "SELECT body_html FROM articles WHERE blogger_post_id = '222'"
            ).fetchone()
            full_row = conn_full.execute(
                "SELECT body_html FROM articles WHERE blogger_post_id = '222'"
            ).fetchone()
            self.assertIsNone(lite_row["body_html"])
            self.assertIn("ndtv.com", full_row["body_html"])
            conn_lite.close()
            conn_full.close()

    def test_takeout_directory_url_reconstruction_cta_and_safety(self) -> None:
        takeout_atom = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom' xmlns:blogger='http://schemas.google.com/blogger/2018'>
  <entry>
    <id>tag:blogger.com,1999:blog-345.post-101</id>
    <published>2021-04-01T10:00:00Z</published>
    <blogger:type>POST</blogger:type>
    <blogger:status>LIVE</blogger:status>
    <blogger:filename>/2021/04/reddit-story.html</blogger:filename>
    <blogger:trashed/>
    <title>Midnight Station</title>
    <content type='html'>submitted by &lt;a href=&quot;https://www.reddit.com/user/author1&quot;&gt;/u/author1&lt;/a&gt; &lt;a href=&quot;https://www.reddit.com/r/shortstories/comments/abc123/midnight_station/&quot;&gt;[link]&lt;/a&gt;</content>
    <link/>
  </entry>
  <entry>
    <id>tag:blogger.com,1999:blog-345.post-102</id>
    <published>2026-11-11T10:00:00Z</published>
    <blogger:type>POST</blogger:type>
    <blogger:status>DRAFT</blogger:status>
    <blogger:trashed/>
    <title></title>
    <content type='html'>&lt;p&gt;Untitled draft.&lt;/p&gt;</content>
    <link/>
  </entry>
  <entry>
    <id>tag:blogger.com,1999:blog-345.post-103</id>
    <published>2022-01-01T10:00:00Z</published>
    <blogger:type>POST</blogger:type>
    <blogger:status>LIVE</blogger:status>
    <blogger:filename>/2022/01/deleted.html</blogger:filename>
    <blogger:trashed>2022-01-02T00:00:00Z</blogger:trashed>
    <title>Should Be Skipped</title>
    <content type='html'>Trashed post.</content>
    <link/>
  </entry>
</feed>
"""
        with tempfile.TemporaryDirectory() as tmp:
            blog_dir = Path(tmp) / "Takeout" / "Blogger" / "Blogs" / "MyBlog"
            blog_dir.mkdir(parents=True)
            (blog_dir / "feed.atom").write_text(takeout_atom, encoding="utf-8")
            (blog_dir / "settings.csv").write_text(
                "blog_subdomain,blog_name\nfeedlyshortstories,FEEDLY\n",
                encoding="utf-8",
            )
            db_path = Path(tmp) / "blog.db"
            res = import_backup.import_xml_to_db(Path(tmp) / "Takeout", db_path)
            self.assertEqual(res["posts_found"], 2)
            self.assertEqual(res["inserted"], 2)

            conn = db.get_connection(db_path)
            row101 = conn.execute(
                "SELECT source_url, blogger_url FROM articles WHERE blogger_post_id = '101'"
            ).fetchone()
            self.assertEqual(
                row101["blogger_url"],
                "https://feedlyshortstories.blogspot.com/2021/04/reddit-story.html",
            )
            self.assertIn("/r/shortstories/comments/abc123/", row101["source_url"])

            # Historical draft and untitled post must not consume today's quota or trigger empty-title dedup
            self.assertEqual(db.count_published_today(conn), 0)
            self.assertFalse(db.is_duplicate(conn, "https://example.com/new-post", "", ""))
            self.assertFalse(db.is_duplicate(conn, "https://example.com/emoji", "", "🌅"))
            conn.close()

    def test_feeds_and_notebook_valid_json(self) -> None:
        feeds = json.loads((ROOT / "feeds.json").read_text(encoding="utf-8"))
        self.assertIn("feeds", feeds)
        self.assertGreaterEqual(len(feeds["feeds"]), 1)

        nb = json.loads((ROOT / "colab_control.ipynb").read_text(encoding="utf-8"))
        self.assertEqual(nb["nbformat"], 4)
        self.assertGreaterEqual(len(nb["cells"]), 5)


if __name__ == "__main__":
    unittest.main()
