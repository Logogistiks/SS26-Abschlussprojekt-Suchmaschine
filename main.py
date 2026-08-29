import crawler
import preprocessing
import search
import search_gui

__version__ = "1.0"


if __name__ == "__main__":
    crawler.main()
    preprocessing.main()

    if input("\nDo you want to search via cli or gui? [cli/gui]: ").lower() == "gui":
        search_gui.main()
    else:
        search.main()