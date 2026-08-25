import sqlite3
import sys
from dataclasses import dataclass
from typing import TextIO

from colorama import Fore, Back, Style

__version__ = "1.1"
__all__ = ["FTSBase", "SearchResultItem"]


@dataclass
class SearchResultItem:
    """Represents a single page in search results."""
    id_: int
    url: str
    title: str
    score: float
    snippet: str


class FTSBase:
    """Base class for full-text search functionality."""

    def __init__(
        self,
        db_path: str="metadata.db",
        match_before: str=Back.WHITE + Fore.BLACK,
        match_after: str=Style.RESET_ALL,
        snippet_border: str=" … ",
        snippet_tokens: int=100, #depends on tokenizer
        logging: bool=False,
        logging_output: TextIO|list[TextIO]=sys.stdout
        ):
        """Initializes an FTSBase instance.
        
        Args:
            db_path: Path to the SQLite database file.
            match_before: String to prepend to matched terms in the snippet.
            match_after: String to append to matched terms in the snippet.
            snippet_border: String to use at the cutoff points of the snippet.
            snippet_tokens: Number of tokens to include in the snippet.
            logging: Whether logging output should be enabled.
            logging_output: The output stream or streams used for logs. The default is normal terminal output, but an open file object or list of file objects can also be passed.
        """
        self.db_path = db_path
        self.match_before = match_before
        self.match_after = match_after
        self.snippet_border = snippet_border
        self.snippet_tokens = snippet_tokens
        self.logging = logging
        self.logging_output = logging_output
        self.fts_stmt = """
        SELECT
            p.id,
            p.url,
            p.title,
            p.score,
            snippet(pages_fts, 1, ?, ?, ?, ?)
        FROM pages_fts
        JOIN pages AS p ON p.id = pages_fts.rowid
        WHERE pages_fts MATCH ?
        ORDER BY p.score DESC;
        """


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


    def search(self, search_string: str) -> list[SearchResultItem]:
        """Do a full-text search on title and content across all crawled pages in the db.
        
        Returns a list of matched pages, sorted descending by pageRank score. Each page has attributes id_, url, title, score, and snippet.
        """
        if not search_string:
            self._log("No search string provided for search")
            return []

        self._log(f"Searching for: {search_string}")

        try:
            with sqlite3.connect(self.db_path) as conn:
                fts_result = conn.execute(
                    self.fts_stmt,
                    (self.match_before, self.match_after, self.snippet_border, self.snippet_tokens, search_string)
                ).fetchall()
        except sqlite3.Error as e:
            self._log(f"Error executing search query: {e}")
            fts_result = []

        search_results = []
        result_ids = set()
        for id_, url, title, score, snippet in fts_result:
            result_ids.add(id_)
            search_results.append(SearchResultItem(id_, url, title, score, snippet))

        self._log(f"Found {len(result_ids)} matching pages. Ids: {repr(result_ids)}")
        return search_results


class FTSCLI(FTSBase):
    """Command-line interface for full-text search."""

    def _get_search_string(self) -> str:
        """Queries the user and builds an fts5 search string."""
        search_string = input("Enter search keywords (separated by a space): ")
        self._log(f"User entered search string: {search_string}")

        # spaces are implicit AND
        if " " in search_string and input("Should matches contain any or all of the keywords? [any/all]: ").lower() != "all":
            search_string = search_string.replace(" ", " OR ")

        return search_string


    def _repr_single_page(self, page: SearchResultItem) -> str:
        """Return a string representation of a single page in search results."""
        return (
            f"{Fore.LIGHTCYAN_EX}{page.title or 'Untitled'}{Style.RESET_ALL} {Fore.CYAN+Style.DIM}{page.score}{Style.RESET_ALL}\n"
            f"  ╠═ {Style.DIM}{page.url}{Style.RESET_ALL}\n"
            f"  ╚═ {page.snippet.strip()}"
        )


    def _print_results(self, search_results: list[SearchResultItem]):
        """Prints the search results nicely formatted."""
        print(f"\nFound {len(search_results)} matching pages.")
        print("\n" + "="*60)
        for index, page in enumerate(search_results, start=1):
            print(f"\n[{index}] {self._repr_single_page(page)}")
        print("\n" + "="*60 + "\n")


    def handle_search(self):
        """Handles the search process."""
        try:
            self._print_results(self.search(self._get_search_string()))
        except Exception as e:
            errmsg = f"Error occurred: {e}"
            self._log(errmsg)
            print(errmsg)


if __name__ == "__main__":
    with open("search_cli.log", "w", encoding="utf-8") as f:
        fts = FTSCLI(logging=True, logging_output=f)

        try:
            while True:
                fts.handle_search()
        except KeyboardInterrupt:
            pass