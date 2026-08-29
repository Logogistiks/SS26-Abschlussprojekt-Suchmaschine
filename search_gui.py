import sys

from PySide6.QtCore import *
from PySide6.QtGui import *
from PySide6.QtWidgets import *

from search import FTSBase, SearchResultItem

__version__ = "1.1"
__all__ = ["FTSGUIWindow", "SearchResultWidget", "Worker", "WorkerSignals"]


class WorkerSignals(QObject):
    """Signals for the worker thread."""
    finished = Signal(list)
    error = Signal(Exception)


class Worker(QRunnable):
    """Worker thread."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()


    @Slot()
    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.signals.error.emit(e)
        else:
            self.signals.finished.emit(result)


class SearchResultWidget(QFrame):
    """Simple result card displaying title, score, URL, and snippet."""

    def __init__(self, item: SearchResultItem, parent=None):
        super().__init__(parent)
        self.setStyleSheet("QFrame { background: #303030; border-radius: 5px;}")

        style = { # cant outsource to qss file because qss doesnt allow styling html dom elements >:(
            "title": "font-size: 18px; font-weight: bold; color: #33CCCC; text-decoration: none;",
            "score": "font-size: 14px;",
            "url": "font-size: 12px; color: #00cc00;",
            "snippet": "font-size: 12px; color: #cccccc;",
        }

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        # Header: Title + Score
        header_layout = QHBoxLayout()

        title_label = QLabel(f"<a href='{item.url}' style='{style['title']}'>{item.title or item.url}</a>")
        title_label.setOpenExternalLinks(True)

        score_label = QLabel(f"<span style='{style['score']}'>PageRank: {item.score:.6f}</span>")

        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(score_label)

        # URL & Snippet
        url_label = QLabel(f"<span style='{style['url']}'>{item.url}</span>")
        url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        url_label.setCursor(QCursor(Qt.CursorShape.IBeamCursor))

        snippet_label = QLabel(f"<span style='{style['snippet']}'>{item.snippet}</span>")
        snippet_label.setWordWrap(True)
        snippet_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        snippet_label.setCursor(QCursor(Qt.CursorShape.IBeamCursor))

        layout.addLayout(header_layout)
        layout.addWidget(url_label)
        layout.addWidget(snippet_label)


class FTSGUIWindow(QWidget):
    """Graphical user interface for full-text search."""

    def __init__(self, fts: FTSBase):
        super().__init__()
        self.fts = fts
        self.search_results = []
        self.search_results_index = 0
        self.is_loading_results = False

        self.setWindowTitle("Search Engine GUI")
        self.resize(800, 600)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        self.threadpool = QThreadPool()

        # Top Bar
        top_layout = QHBoxLayout()
        top_layout.setSpacing(6)

        self.option_combo = QComboBox()
        self.option_combo.addItems(["Any", "All"])
        self.option_combo.setCurrentIndex(0)
        self.option_combo.setEnabled(False)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search...")
        self.search_input.returnPressed.connect(self._handle_search)
        self.search_input.textChanged.connect(lambda text: self.option_combo.setEnabled(" " in text.strip()))

        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self._handle_search)

        top_layout.addWidget(self.search_input, stretch=1)
        top_layout.addWidget(self.option_combo)
        top_layout.addWidget(self.search_button)

        main_layout.addLayout(top_layout)

        # Scroll Area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self.results_container = QWidget()
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.results_layout.setSpacing(8)
        self.results_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area.setWidget(self.results_container)
        main_layout.addWidget(self.scroll_area)

        # Status Bar
        self.status_bar = QStatusBar()
        main_layout.addWidget(self.status_bar)


    def _on_scroll(self, value: int):
        """Load more results when user scrolls near bottom of scroll area."""
        scrollbar = self.scroll_area.verticalScrollBar()
        if value >= scrollbar.maximum() - scrollbar.pageStep(): # load when one page left to scroll
            self._append_next_results()


    def _clear_results(self):
        """Clear all stored and displayed search results."""
        self.search_results = []
        self.search_results_index = 0
        while self.results_layout.count():
            child = self.results_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        self.status_bar.clearMessage()


    def _append_next_results(self, batch_size: int=30):
        """Append the next batch of search results to the GUI."""
        if self.is_loading_results:
            return

        if self.search_results_index >= len(self.search_results):
            return

        self.is_loading_results = True
        self.scroll_area.setUpdatesEnabled(False) # prevent flickering
        try:
            end_index = min(self.search_results_index + batch_size, len(self.search_results))
            for item in self.search_results[self.search_results_index:end_index]:
                self.results_layout.addWidget(SearchResultWidget(item))

            self.search_results_index = end_index
        finally:
            self.is_loading_results = False
            self.scroll_area.setUpdatesEnabled(True)


    def _handle_search(self):
        search_string = self.search_input.text().strip()

        # spaces are implicit AND
        if " " in search_string and self.option_combo.currentText().lower() != "all":
            search_string = search_string.replace(" ", " OR ")

        self._clear_results()

        worker = Worker(self.fts.search, search_string)
        worker.signals.finished.connect(self._handle_search_success)
        worker.signals.error.connect(self._handle_search_error)
        self.threadpool.start(worker)


    def _handle_search_success(self, results: list[SearchResultItem]):
        """Update the GUI according to the search results."""
        self.search_results = results
        if results:
            self._append_next_results()
        else:
            self.results_layout.addWidget(QLabel("--- No results found ---", alignment=Qt.AlignmentFlag.AlignCenter))

        self.status_bar.showMessage(f"Found {len(results)} result(s)")


    def _handle_search_error(self, e: Exception):
        """Handle errors that occur during the search process."""
        errmsg = f"Error occurred: {e}"
        self.fts._log(errmsg)
        self.status_bar.showMessage(errmsg)


def main():
    app = QApplication()
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)

    with open("search_gui.log", "w", encoding="utf-8") as f:
        fts = FTSBase(match_before="<b style='color: #ffbb94;'>", match_after="</b>", snippet_tokens=250, logging=True, logging_output=[sys.stdout, f])
        window = FTSGUIWindow(fts)
        window.showMaximized()

        app.exec()


if __name__ == "__main__":
    main()