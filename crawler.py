import re
import shutil
import sqlite3
import sys
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import TextIO, overload
from urllib.parse import urljoin, urlparse

import requests as rq
import urllib3
from bs4 import BeautifulSoup
from colorama import Fore, Style

__version__ = "1.1"
__all__ = ["AdjacencyList", "BiMapStr2Int", "Crawler", "EXTENSIONS_TO_IGNORE"]

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

text_red = lambda s: f"{Fore.LIGHTRED_EX}{s}{Style.RESET_ALL}"
text_yellow = lambda s: f"{Fore.LIGHTYELLOW_EX}{s}{Style.RESET_ALL}"
text_green = lambda s: f"{Fore.LIGHTGREEN_EX}{s}{Style.RESET_ALL}"
text_cyan = lambda s: f"{Fore.LIGHTCYAN_EX}{s}{Style.RESET_ALL}"
text_grey = lambda s: f"{Style.DIM}{s}{Style.RESET_ALL}"


EXTENSIONS_TO_IGNORE = (
    ".txt", ".md", ".rtf", ".tex", ".text", ".pdf", ".epub", "rss",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".wps", ".odt", ".ods", ".odp",
    ".pages", ".numbers", ".key", ".csv", ".tsv",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif", ".heic", ".heif", ".tif", ".tiff", ".ico", ".cur", ".jxl", ".svg", ".eps", ".ai", ".cdr", ".wmf", ".emf",
    ".flac", ".wav", ".aiff", ".alac", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".amr",
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".tgz", ".txz", ".iso", ".img", ".dmg", ".vhd",
    ".css", ".js", ".json", ".jsonc", ".xml", ".yaml", ".yml", ".toml",
    ".ttf", ".otf", ".woff", ".woff2", ".eot"
)


class BiMapStr2Int:
    """Bidirectional string-to-integer mapping with consecutive increasing ints starting at 0."""

    def __init__(self):
        """Initialize the mapping containers."""
        self._str_to_int = {}
        self._int_to_str = []


    @overload
    def __getitem__(self, key: str) -> int|None: ...
    @overload
    def __getitem__(self, key: int) -> str|None: ...
    def __getitem__(self, key):
        """Return the mapped value for a string or an integer."""
        if isinstance(key, str):
            return self._str_to_int.get(key)
        elif isinstance(key, int):
            return self._int_to_str[key] if 0 <= key < len(self._int_to_str) else None
        else:
            raise TypeError("Key must be either str or int")


    def add(self, item: str) -> int:
        """Add a string and return its assigned id. If the string is already present, return its existing id."""
        if not isinstance(item, str):
            raise TypeError("Item must be a string")
        if item in self._str_to_int:
            return self._str_to_int[item]
        index = len(self._int_to_str)
        self._str_to_int[item] = index
        self._int_to_str.append(item)
        return index


    def __contains__(self, item: str|int) -> bool:
        """Check whether a string or id exists in the mapping."""
        if isinstance(item, str):
            return item in self._str_to_int
        elif isinstance(item, int):
            return 0 <= item < len(self._int_to_str)
        else:
            raise TypeError("Item must be either str or int")


    def __str__(self) -> str:
        """Return a readable representation of the mapping."""
        width = len(str(len(self._int_to_str) - 1))
        return "BiMapStr2Int(\n" + "\n".join(f"  [{i:>{width}d}] -> {repr(s)}" for i, s in enumerate(self._int_to_str)) + "\n)"


class AdjacencyList:
    """Simple adjacency list storing a set of child ids for each id."""

    def __init__(self):
        """Initialize the adjacency container."""
        self._adj_list: dict[int, set[int]] = {}


    def update(self, parent: int, child: int):
        """Add a directed edge from a parent node to a child node."""
        if parent not in self._adj_list:
            self._adj_list[parent] = set()
        self._adj_list[parent].add(child)


    def __str__(self) -> str:
        """Return a readable representation of the adjacency list."""
        width = len(str(len(self._adj_list) - 1))
        return "AdjacencyList(\n" + "\n".join(f"  [{parent:>{width}d}] -> {children}" for parent, children in self._adj_list.items()) + "\n)"


class Crawler:
    """Crawl a website and save discovered pages locally."""

    @dataclass
    class CrawlStats:
        """Used to aggregate crawl progress and summary data."""
        started_at: float = 0.0
        finished_at: float | None = None
        termination_reason: str | None = None
        pages_known: int = 0
        pages_processed: int = 0
        pages_fetched: int = 0
        pages_failed_to_fetch: int = 0
        pages_skipped_non_html: int = 0
        pages_skipped_depth_limit: int = 0
        pages_saved: int = 0
        pages_failed_to_save: int = 0
        pages_skipped_save: int = 0
        links_examined: int = 0
        links_valid: int = 0
        links_ignored: int = 0
        pages_enqueued: int = 0
        max_queue_size: int = 0
        max_depth_seen: int = 0


    def __init__(
        self,
        start_url: str,
        max_depth: int|None=None,
        domain_restriction: str|None=None,
        timeout: float=10.0,
        logging: bool=False,
        logging_output: TextIO|list[TextIO]=sys.stdout,
        save_dir: str="site_storage"
        ):
        """Initialize the crawler.

        Args:
            start_url: The entry url for the crawl.
            max_depth: The maximum crawl depth, or None for unlimited depth.
            domain_restriction: The host that links must stay within. Subdomains and Sub-Subdomains etc are always allowed. If None, the hostname (subdomains included) of start_url is used.
            timeout: The timeout in seconds for HTTP requests.
            logging: Whether logging output should be enabled.
            logging_output: The output stream or streams used for logs. The default is normal terminal output, but an open file object or list of file objects can also be passed.
            save_dir: The directory where downloaded pages are stored.
        """
        self.start_url = start_url
        self.max_depth = max_depth
        self.domain_restriction = domain_restriction or urlparse(start_url).hostname # hostname property is netloc in lowercase
        self.timeout = timeout
        self.logging = logging
        self.logging_output = logging_output
        self.save_dir = save_dir
        self._crawl_stats: Crawler.CrawlStats | None = None

        self.bimap = BiMapStr2Int()
        self.adj_list = AdjacencyList()

        # put directory in clean slate
        path = Path(self.save_dir)
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)


    def _log(self, *values: object, sep: str|None=" ", end: str|None="\n", file=None, flush: bool=False): # rebuild instead of argument packing to preserve signature of built in print
        """Can be used like built-in print, but respects logging settings."""
        if self.logging and file is not None: # logging destination can be overridden on individual calls
            print(*values, sep=sep, end=end, file=file, flush=flush)
            return
        if self.logging and isinstance(self.logging_output, list):
            for output in self.logging_output:
                print(*values, sep=sep, end=end, file=output, flush=flush)
            return
        if self.logging:
            print(*values, sep=sep, end=end, file=self.logging_output, flush=flush)


    def _reset_crawl_stats(self) -> CrawlStats:
        """Create a fresh stats object for a new crawl run."""
        self._crawl_stats = Crawler.CrawlStats(started_at=perf_counter())
        return self._crawl_stats


    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        """Format a duration for logs and reports."""
        if seconds is None:
            return "n/a"
        return str(timedelta(seconds=max(0, int(round(seconds)))))


    @staticmethod
    def _format_rate(count: int, elapsed: float) -> str:
        """Format a throughput value for logs and reports."""
        if elapsed <= 0:
            return "n/a"
        return f"{count / elapsed:.2f}/s"


    @staticmethod
    def _format_eta(remaining: int, elapsed: float, processed: int) -> str:
        """Estimate the remaining runtime based on current throughput."""
        if remaining <= 0:
            return "0:00:00"
        if elapsed <= 0 or processed <= 0:
            return "n/a"
        return Crawler._format_duration((elapsed / processed) * remaining)


    def _report(self):
        """Print a crawl summary."""
        stats = self._crawl_stats
        if stats is None:
            return

        finished_at = stats.finished_at or perf_counter()
        duration = finished_at - stats.started_at

        self._log("\n" + "="*60)
        self._log("Crawl report")
        self._log(f"  Runtime: {text_cyan(self._format_duration(duration))}")
        self._log(f"  Termination: {stats.termination_reason}") # termination text is expected to already be colored
        self._log(f"  Pages: known {text_cyan(stats.pages_known)}, processed {text_cyan(stats.pages_processed)}")
        self._log(
            f"  Fetches: ok {text_cyan(stats.pages_fetched)}, failed {text_cyan(stats.pages_failed_to_fetch)}, non-html {text_cyan(stats.pages_skipped_non_html)}, depth-limited {text_cyan(stats.pages_skipped_depth_limit)}"
        )
        self._log(
            f"  Saves: ok {text_cyan(stats.pages_saved)}, skipped {text_cyan(stats.pages_skipped_save)}, failed {text_cyan(stats.pages_failed_to_save)}"
        )
        self._log(
            f"  Links: examined {text_cyan(stats.links_examined)}, accepted {text_cyan(stats.links_valid)}, ignored {text_cyan(stats.links_ignored)}, enqueued pages {text_cyan(stats.pages_enqueued)}"
        )
        self._log(
            f"  Crawl shape: max queue {text_cyan(stats.max_queue_size)}, max depth {text_cyan(stats.max_depth_seen)}, avg throughput {text_cyan(self._format_rate(stats.pages_processed, duration))}"
        )
        self._log("="*60 + "\n")


    def __str__(self) -> str:
        """Return a readable representation of the crawler's state."""
        def indent_multiline(value: str, indent: int) -> str:
            lines = value.splitlines()
            if not lines:
                return ""
            return lines[0] + "".join(f"\n{' '*indent}{line}" for line in lines[1:-1]) + f"\n{' '*indent}{lines[-1]}"

        return (
            f"Crawler(\n"
            f"  start_url={repr(self.start_url)},\n"
            f"  max_depth={self.max_depth},\n"
            f"  domain_restriction={repr(self.domain_restriction)},\n"
            f"  logging={self.logging},\n"
            f"  save_dir={repr(self.save_dir)},\n"
            f"  bimap={indent_multiline(str(self.bimap), 2)},\n"
            f"  adj_list={indent_multiline(str(self.adj_list), 2)}\n"
            f")"
        )


    def _download_html(self, url: str) -> tuple[str|None, str] | None:
        """Download and return the HTML body and resolved url for a url if possible. Returns None if request fails or content is not HTML."""
        try:
            if url in self.bimap: # avoid web-request for the case that unresolved url is already known
                return None, url
            with rq.get(url, stream=True, verify=False, timeout=self.timeout) as response: # dont verify ssl certs to avoid unnecessary errors, assume kit homepage is trusted
                if not response.ok:
                    if self._crawl_stats is not None:
                        self._crawl_stats.pages_failed_to_fetch += 1
                    return None
                normalized_url = self._normalize_and_validate_url(response.url, response.url)
                if normalized_url in self.bimap: # avoid downloading the same page twice
                    return None, normalized_url
                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith("text/html"):
                    if self._crawl_stats is not None:
                        self._crawl_stats.pages_skipped_non_html += 1
                    return None

                if self._crawl_stats is not None:
                    self._crawl_stats.pages_fetched += 1
                return response.text, normalized_url
        except Exception:
            if self._crawl_stats is not None:
                self._crawl_stats.pages_failed_to_fetch += 1
            return None


    def _save_content(self, page_id: int, file_content: str|None) -> bool:
        """Save page content to disk for a given page id."""

        if file_content is None:
            if self._crawl_stats is not None:
                self._crawl_stats.pages_skipped_save += 1
            return False

        try:
            Path(self.save_dir, f"{page_id}_").write_text(file_content, encoding="utf-8")
        except Exception:
            if self._crawl_stats is not None:
                self._crawl_stats.pages_failed_to_save += 1
            return False

        if self._crawl_stats is not None:
            self._crawl_stats.pages_saved += 1
        return True


    def _normalize_and_validate_url(self, base_url: str, raw_url: str) -> str | None:
        """Normalize a url and check whether it conforms to the desired filtering rules like file type and domain restriction. Returns normalized url if valid, None otherwise."""
        if not raw_url or raw_url.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            return None

        if re.search(r"\s", raw_url): # ignore urls with whitespace and malformed/conjoined urls
            return None

        # construct absolute if not already, parse
        try:
            parsed = urlparse(urljoin(base_url, raw_url))
        except Exception:
            return None

        host = parsed.hostname
        if not host:
            return None

        # ignore external urls and unwanted subdomains
        if host.startswith(("wwwalt", "www2025")):
            return None
        if self.domain_restriction and not host.endswith(self.domain_restriction):
            return None

        # normalize subdomain
        if host.startswith("www."):
            host = host[4:]

        # remove trailing slash from urls without path
        path = parsed.path
        if path == "/":
            path = ""

        # ignore bad file extensions
        if path.lower().endswith(EXTENSIONS_TO_IGNORE):
            return None

        return parsed._replace(netloc=host, path=path, query="", fragment="", params="").geturl()


    def _extract_links(self, file_content: str|None, current_url: str) -> set[str]:
        """Extract and filter valid internal links from page HTML."""
        if file_content is None:
            return set()

        result = set()
        examined = 0
        valid = 0
        ignored = 0
        soup = BeautifulSoup(file_content, "html.parser")

        for tag in soup.find_all("a", href=True):
            examined += 1
            validated = self._normalize_and_validate_url(current_url, tag["href"].strip())
            if validated:
                result.add(validated)
                valid += 1
            else:
                ignored += 1

        if self._crawl_stats is not None:
            self._crawl_stats.links_examined += examined
            self._crawl_stats.links_valid += valid
            self._crawl_stats.links_ignored += ignored

        return result


    def crawl(self) -> bool:
        """Crawl the site breadth-first from the start url. Returns True if the crawl finished normally, False otherwise."""
        stats = self._reset_crawl_stats()
        self._log(
            f"Starting crawl from {text_grey(repr(self.start_url))} | depth limit: {text_cyan(self.max_depth if self.max_depth is not None else 'unbounded')} | domain: {text_grey(repr(self.domain_restriction))}"
        )

        if (result := self._download_html(self.start_url)) is None:
            self._log("Start url is invalid. Crawl aborted.")
            return False
        start_content, start_url = result

        start_id = self.bimap.add(start_url)
        self._save_content(start_id, start_content)

        queue = deque([(start_id, 0)]) # (page_id, depth)

        stats.pages_known = 1
        stats.max_queue_size = 1

        try:
            while queue:
                current_id, current_depth = queue.popleft()

                if self.max_depth is not None and current_depth >= self.max_depth:
                    stats.pages_skipped_depth_limit += 1
                    continue

                stats.pages_processed += 1
                stats.max_depth_seen = max(stats.max_depth_seen, current_depth)

                current_url = self.bimap[current_id]
                file_content = Path(self.save_dir, f"{current_id}_").read_text(encoding="utf-8")
                links_before = stats.links_valid
                ignored_before = stats.links_ignored
                newly_enqueued = 0

                for link_url in self._extract_links(file_content, current_url):
                    if (result := self._download_html(link_url)) is None:
                        continue
                    link_content, link_url = result

                    link_id = self.bimap.add(link_url) # get id if already present
                    self.adj_list.update(current_id, link_id)

                    if link_content is not None:
                        self._save_content(link_id, link_content)
                        queue.append((link_id, current_depth + 1))
                        newly_enqueued += 1

                stats.pages_known = len(self.bimap._int_to_str)
                stats.pages_enqueued += newly_enqueued
                stats.max_queue_size = max(stats.max_queue_size, len(queue))

                page_links_valid = stats.links_valid - links_before
                page_links_ignored = stats.links_ignored - ignored_before

                elapsed = perf_counter() - stats.started_at
                remaining_known = len(queue)
                self._log(
                    f"Page {text_cyan(stats.pages_processed)}/{text_cyan(stats.pages_known)}"
                    f" | depth {text_cyan(current_depth)}"
                    f" | queue {text_cyan(remaining_known)}"
                    f" | elapsed {text_cyan(self._format_duration(elapsed))}"
                    f" | eta {text_cyan(self._format_eta(remaining_known, elapsed, stats.pages_processed))}"
                    f" | links valid {text_cyan(page_links_valid)}"
                    f" ignored {text_cyan(page_links_ignored) if page_links_ignored <= 1.5*max(page_links_valid, 1) else text_yellow(page_links_ignored)}"
                    f" | {text_grey(repr(current_url))}"
                )

            if self.max_depth is not None and stats.pages_skipped_depth_limit > 0:
                stats.termination_reason = text_yellow("depth limit reached")
            else:
                stats.termination_reason = text_cyan("queue emptied normally")

            return True

        except KeyboardInterrupt:
            self._log(text_yellow("\nCrawl interrupted by user."))
            stats.termination_reason = text_yellow("interrupted by user")
            return True
        except Exception as e:
            self._log(f"\nCrawl aborted due to unexpected/critical error: {text_red(repr(e))}")
            stats.termination_reason = f"unexpected error: {text_red(repr(e.__class__.__name__))}"
            return False
        finally:
            stats.finished_at = perf_counter()
            self._report()


    def cleanup(self):
        """Ignore pages with no outgoing links and reindex remaining pages."""
        if not self.bimap._int_to_str:
            return

        all_nodes = set(range(len(self.bimap._int_to_str)))
        out_degree = {node_id: 0 for node_id in all_nodes}
        parents_of: dict[int, set[int]] = {node_id: set() for node_id in all_nodes}

        for parent, children in self.adj_list._adj_list.items():
            #* V this pre-filtering is needed because nodes that only have a self-link would not be removed in the following iteration,
            #* V but after later filterign would become leaf nodes which cant exist in the final graph.
            children = [child for child in children if child != parent]
            out_degree[parent] = len(children)
            for child in children:
                parents_of[child].add(parent)

        # iteratively find all leaf nodes
        queue = deque(node_id for node_id, degree in out_degree.items() if degree == 0)
        removed: set[int] = set()

        while queue:
            node_id = queue.popleft()
            if node_id in removed:
                continue
            removed.add(node_id)

            for parent in parents_of[node_id]:
                if parent in removed:
                    continue
                out_degree[parent] -= 1
                if out_degree[parent] == 0:
                    queue.append(parent)

        # rebuild graph without leaf nodes by reindexing and remapping ids
        kept_nodes = [node_id for node_id in range(len(self.bimap._int_to_str)) if node_id not in removed]
        if not kept_nodes:
            self.bimap = BiMapStr2Int()
            self.adj_list = AdjacencyList()
            return

        id_map = {old_id: new_id for new_id, old_id in enumerate(kept_nodes)}
        new_bimap = BiMapStr2Int()
        new_adj_list = AdjacencyList()

        # fill new bimap and adj_list
        for old_id in kept_nodes:
            new_id = new_bimap.add(self.bimap._int_to_str[old_id])

            # rename file corresponding to id
            source_path = Path(self.save_dir, f"{old_id}_")
            if source_path.exists():
                source_path.replace(Path(self.save_dir, str(new_id)))

            children = self.adj_list._adj_list.get(old_id)
            if not children:
                continue

            # this second filtering is needed to actually ignore self-links.
            # nodes with only a self-link would become leaf nodes after this, but were already ignored by the pre-filtering
            mapped_children = {id_map[child] for child in children if child in id_map and child != old_id}
            if mapped_children:
                new_adj_list._adj_list[new_id] = mapped_children

        # remove orphaned files
        for old_id in removed:
            orphan_path = Path(self.save_dir, f"{old_id}_")
            if orphan_path.exists():
                orphan_path.unlink(missing_ok=True)

        self.bimap = new_bimap
        self.adj_list = new_adj_list


    def export_to_sqlite(self, db_path: str="metadata.db"):
        """Export the crawl data to an SQLite database."""
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON;")
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA synchronous=NORMAL;")

                # setup tables in clean slate
                cur.executescript("""
                    DROP TABLE IF EXISTS links;
                    DROP TABLE IF EXISTS pages;
                    CREATE TABLE pages (
                        id INTEGER PRIMARY KEY,
                        url TEXT NOT NULL UNIQUE,
                        title TEXT,
                        content TEXT,
                        score REAL DEFAULT 0.0
                    );
                    CREATE TABLE links (
                        source_id INTEGER NOT NULL,
                        target_id INTEGER NOT NULL,
                        FOREIGN KEY (source_id) REFERENCES pages(id),
                        FOREIGN KEY (target_id) REFERENCES pages(id)
                    );
                """)

                # write pages
                cur.executemany("INSERT INTO pages (id, url) VALUES (?, ?)", enumerate(self.bimap._int_to_str))

                # write links
                link_rows = [
                    (source_id, target_id)
                    for source_id, links in self.adj_list._adj_list.items()
                    for target_id in links
                ]
                cur.executemany("INSERT INTO links (source_id, target_id) VALUES (?, ?)", link_rows)
        except Exception as e:
            self._log(f"Error exporting to SQLite: {text_red(repr(e))}")


if __name__ == "__main__":
    url = "https://www.math.kit.edu/"
    domain = "math.kit.edu"
    limit = None

    with open("crawl.log", "w", encoding="utf-8") as f:
        crawler = Crawler(start_url=url, max_depth=limit, domain_restriction=domain, logging=True, logging_output=[sys.stdout, f])
        success = crawler.crawl()
        if success:
            crawler.cleanup()
        crawler.export_to_sqlite()