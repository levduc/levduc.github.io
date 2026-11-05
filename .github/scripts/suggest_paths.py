"""The one allow-list of files an inline suggestion may touch.

Pure string checks only; symlinks are the caller's business. Standard library
only. Scripts run under `python3 -I`, so importers must put this directory on
sys.path first.
"""
from __future__ import annotations

import re
from typing import NamedTuple

POST_RE = re.compile(r"^content/posts/[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.md$")

# First path segments under static/ that are assets, never pages.
DENIED_FIRST = frozenset({"js", "css", "annotations", "papers", "fonts", "img"})

SEG = r"[a-z0-9][a-z0-9-]{0,63}"

# Deck shapes come before page shapes.
_DECK_RES = (
    re.compile(rf"static/({SEG}/talk)/index\.html"),
    re.compile(rf"static/(learning/{SEG}/talk)/index\.html"),
)
_PAGE_RES = (
    re.compile(rf"static/({SEG})/index\.html"),
    re.compile(rf"static/(learning/{SEG})/index\.html"),
)


class PageRef(NamedTuple):
    kind: str      # "post" | "page" | "deck"
    source: str    # repo-relative path, as given
    page_id: str   # "pdag", "learning/lattice", "fastlane/talk", "posts/<stem>"


def classify(rel_path: str) -> PageRef | None:
    """Return the PageRef for an allowed path, else None."""
    if (".." in rel_path or "\\" in rel_path or rel_path.startswith("/")
            or "\n" in rel_path):
        return None
    if POST_RE.match(rel_path):
        stem = rel_path[len("content/posts/"):-len(".md")]
        return PageRef("post", rel_path, f"posts/{stem}")
    for kind, regexes in (("deck", _DECK_RES), ("page", _PAGE_RES)):
        for rx in regexes:
            m = rx.fullmatch(rel_path)
            if not m:
                continue
            page_id = m.group(1)
            # Decks too: static/js/talk/index.html is an asset, not a deck.
            if page_id.split("/")[0] in DENIED_FIRST:
                return None
            return PageRef(kind, rel_path, page_id)
    return None


def notes_filename(page_id: str) -> str:
    return page_id.replace("/", "--") + ".json"


def url_path(ref: PageRef) -> str:
    return f"{ref.page_id}/"
