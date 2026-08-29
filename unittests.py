"""Small unit test suite by copilot."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler import Crawler
from preprocessing import Preprocessor
from search import FTSBase


class FakeResponse:
    def __init__(self, url, body="", content_type="text/html; charset=utf-8", ok=True):
        self.url = url
        self.text = body
        self.headers = {"Content-Type": content_type}
        self.ok = ok

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class CrawlerTests(unittest.TestCase):
    def test_normalizes_relative_links_and_rejects_non_html_links(self):
        with tempfile.TemporaryDirectory() as directory:
            crawler = Crawler(
                "https://www.math.kit.edu/start",
                domain_restriction="math.kit.edu",
                save_dir=directory,
            )

            html = """
                <a href="../about">about</a>
                <a href="https://math.kit.edu/file.pdf">pdf</a>
                <a href="https://example.org/outside">outside</a>
                <a href="mailto:test@example.org">mail</a>
            """

            self.assertEqual(
                crawler._extract_links(html, "https://math.kit.edu/section/start"),
                {"https://math.kit.edu/about"},
            )

    @patch("crawler.rq.get")
    def test_crawls_each_discovered_page_once_and_respects_depth(self, get):
        pages = {
            "https://math.kit.edu": '<a href="/a">A</a><a href="/b">B</a>',
            "https://math.kit.edu/a": '<a href="/c">C</a>',
            "https://math.kit.edu/b": '<a href="/c">C</a>',
            "https://math.kit.edu/c": "<p>leaf</p>",
        }
        calls = []

        def get_page(url, **kwargs):
            calls.append(url)
            return FakeResponse(url, pages[url])

        get.side_effect = get_page

        with tempfile.TemporaryDirectory() as directory:
            crawler = Crawler(
                "https://math.kit.edu",
                domain_restriction="math.kit.edu",
                max_depth=2,
                save_dir=directory,
            )

            self.assertTrue(crawler.crawl())

            self.assertEqual(calls.count("https://math.kit.edu"), 1)
            self.assertEqual(calls.count("https://math.kit.edu/a"), 1)
            self.assertEqual(calls.count("https://math.kit.edu/b"), 1)
            self.assertEqual(calls.count("https://math.kit.edu/c"), 1)
            self.assertEqual(set(crawler.bimap), set(pages))
            self.assertEqual(crawler.adj_list[0], {1, 2})
            self.assertEqual(crawler.adj_list[1], {3})
            self.assertEqual(crawler.adj_list[2], {3})

    @patch("crawler.rq.get")
    def test_skips_non_html_responses(self, get):
        def get_page(url, **kwargs):
            if url == "https://math.kit.edu":
                return FakeResponse(url, '<a href="/document.pdf">PDF</a>')
            return FakeResponse(url, content_type="application/pdf")

        get.side_effect = get_page

        with tempfile.TemporaryDirectory() as directory:
            crawler = Crawler(
                "https://math.kit.edu",
                domain_restriction="math.kit.edu",
                save_dir=directory,
            )

            self.assertTrue(crawler.crawl())
            self.assertEqual(len(crawler.bimap), 1)
            self.assertEqual(crawler._crawl_stats.pages_skipped_non_html, 0)

    @patch("crawler.rq.get")
    def test_exports_pages_and_links_to_sqlite(self, get):
        pages = {
            "https://math.kit.edu": '<a href="/child">child</a>',
            "https://math.kit.edu/child": "<p>child</p>",
        }
        get.side_effect = lambda url, **kwargs: FakeResponse(url, pages[url])

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "metadata.db"
            crawler = Crawler(
                "https://math.kit.edu",
                domain_restriction="math.kit.edu",
                save_dir=str(Path(directory) / "pages"),
            )
            crawler.crawl()
            crawler.export_to_sqlite(str(db_path))

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM links").fetchone()[0], 1)
            finally:
                connection.close()


class PreprocessingAndSearchTests(unittest.TestCase):
    def _create_database(self, directory):
        db_path = Path(directory) / "metadata.db"
        pages_directory = Path(directory) / "site_storage"
        pages_directory.mkdir()

        with sqlite3.connect(db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE pages (
                    id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT,
                    content TEXT,
                    score REAL DEFAULT 0.0
                );
                CREATE TABLE links (source_id INTEGER NOT NULL, target_id INTEGER NOT NULL);
                CREATE VIRTUAL TABLE pages_fts USING fts5(
                    title, content, content='pages', content_rowid='id', tokenize='trigram'
                );
                INSERT INTO pages (id, url) VALUES
                    (0, 'https://math.kit.edu/low'),
                    (1, 'https://math.kit.edu/high');
                INSERT INTO links VALUES (0, 1);
                INSERT INTO links VALUES (1, 1);
                """
            )

        (pages_directory / "0").write_text(
            "<html><head><title>Low</title></head><body>alpha</body></html>",
            encoding="utf-8",
        )
        (pages_directory / "1").write_text(
            "<html><head><title>High</title></head><body>alpha beta</body></html>",
            encoding="utf-8",
        )
        return db_path, pages_directory

    def test_prepares_text_and_calculates_normalized_pagerank(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, pages_directory = self._create_database(directory)

            with Preprocessor(str(db_path), str(pages_directory)) as preprocessor:
                preprocessor.prepare_fts()
                preprocessor.calculate_pagerank()

            connection = sqlite3.connect(db_path)
            try:
                rows = connection.execute(
                    "SELECT title, content, score FROM pages ORDER BY id"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(rows[0][:2], ("Low", "Low alpha"))
            self.assertEqual(rows[1][:2], ("High", "High alpha beta"))
            self.assertAlmostEqual(sum(row[2] for row in rows), 1.0, places=5)
            self.assertGreater(rows[1][2], rows[0][2])

    def test_search_returns_ranked_results_with_snippets(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, pages_directory = self._create_database(directory)

            with Preprocessor(str(db_path), str(pages_directory)) as preprocessor:
                preprocessor.prepare_fts()
                preprocessor.calculate_pagerank()

            results = FTSBase(str(db_path), snippet_tokens=10).search("alpha")

            self.assertEqual([result.id_ for result in results], [1, 0])
            self.assertTrue(all(result.snippet for result in results))
            self.assertEqual(FTSBase(str(db_path)).search(""), [])


if __name__ == "__main__":
    unittest.main()