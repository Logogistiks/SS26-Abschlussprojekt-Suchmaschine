import sqlite3
import sys
from pathlib import Path
from typing import TextIO

import numpy as np
from bs4 import BeautifulSoup
from scipy.sparse import csr_array

__version__ = "1.1"
__all__ = ["Preprocessor"]


class Preprocessor:
    """Prepare crawled data for search engine use."""

    def __init__(
        self,
        db_path: str="metadata.db",
        save_dir: str="site_storage",
        logging: bool=False,
        logging_output: TextIO|list[TextIO]=sys.stdout
        ):
        """Initialize the Preprocessor.
        
        Args:
            db_path: Path to the SQLite database file.
            save_dir: Directory where crawled HTML files are stored.
            logging: Whether logging output should be enabled.
            logging_output: The output stream or streams used for logs. The default is normal terminal output, but an open file object or list of file objects can also be passed.
        """
        self.dp_path = db_path
        self.save_dir = save_dir
        self.logging = logging
        self.logging_output = logging_output
        self.conn = None


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


    def __enter__(self):
        self.conn = sqlite3.connect(self.dp_path)
        return self


    def __exit__(self, exc_type, exc_value, traceback):
        if self.conn:
            self.conn.close()
            self.conn = None


    def prepare_fts(self, batch_size: int=500, clear_directory: bool=True):
        """Populate the db with title and content of crawled pages and build fts table."""
        if not self.conn:
            self._log("Database connection is not established. Use Preprocessor in a context manager")
            return

        def store():
            self.conn.executemany(
                "UPDATE pages SET title = ?, content = ? WHERE id = ?",
                data # nonlocal read only
            )

        self._log("Parsing title & content and populating db")
        data: list[tuple[str|None, str|None, int]] = [] # (title, content, id)
        for file in Path(self.save_dir).iterdir():
            try:
                soup = BeautifulSoup(file.read_text(encoding="utf-8"), "html.parser")
                title_tags = soup.select("head title")
                data.append((
                    title_tags[0].get_text(strip=True) if len(title_tags) == 1 else None, # if title ambiguous or missing, later show url or untitled
                    soup.get_text(separator=" ", strip=True) or None,
                    int(file.stem)
                ))
                if clear_directory:
                    file.unlink(missing_ok=True)
            except Exception as e:
                self._log(f"Error processing file {file}: {e}")

            if len(data) % 100 == 0:
                self._log(f"Processed {len(data)} files")

            if len(data) >= batch_size: # batch to avoid having content of all pages in memory at once
                store()
                data.clear()

        if data: # update remaining data
            store()

        if clear_directory:
            try: Path(self.save_dir).rmdir()
            except OSError: pass

        self._log("Title & content population completed")

        self.conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('rebuild');")
        self.conn.commit()
        self._log("FTS table synced")


    def calculate_pagerank(self, tolerance: float=1e-6, max_iterations: int=100, m: float=0.15):
        """Calculate PageRank scores and update db."""
        if not self.conn:
            self._log("Database connection is not established. Use Preprocessor in a context manager.")
            return

        self._log("Building 'adjacency' matrix A")

        N = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        out_degree = dict(self.conn.execute(
            "SELECT source_id, COUNT(*) FROM links GROUP BY source_id").fetchall())

        rows, cols, data = [], [], []
        for source_id, target_id in self.conn.execute("SELECT source_id, target_id FROM links"):
            rows.append(target_id)
            cols.append(source_id)
            data.append(1 / out_degree[source_id])

        A = csr_array((data, (rows, cols)), shape=(N, N))
        ms = np.full(N, m / N) # = mSx for probability vector x

        self._log("Calculating PageRank scores")

        x = np.full(N, 1 / N)
        for _ in range(max_iterations):
            x_new = (1 - m) * (A @ x) + ms # as per equation (3.2) in paper
            if np.linalg.norm(x_new - x, 1) < tolerance:
                break
            x = x_new
        else:
            self._log(f"Warning: PageRank did not converge within {max_iterations} (configured max) iterations.")

        self.conn.executemany(
            "UPDATE pages SET score = ? WHERE id = ?",
            [(float(f"{score:.6f}"), i) for i, score in enumerate(x_new)]
        )
        self.conn.commit()
        self._log("PageRank scores calculated and updated in db")


def main():
    with open("preprocessing.log", "w", encoding="utf-8") as f:
        with Preprocessor(logging=True, logging_output=[sys.stdout, f]) as preprocessor:
            preprocessor.prepare_fts()
            preprocessor.calculate_pagerank()


if __name__ == "__main__":
    main()