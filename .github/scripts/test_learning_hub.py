"""The /learning/ hub: topics, one card per paper, and an Explainer / Companion / Talk row.

Pins the hub's structure so a new learning page (added by hand or by the /learn skill) can't
leave a paper without a card, a card outside a topic, or a row that links to a missing page.
Standard library only.
"""
from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEARNING = ROOT / "static" / "learning"
HUB = LEARNING / "index.html"
KINDS = ("explainer", "companion", "talk")


class Hub(HTMLParser):
    """Collects topic sections, cards and the entries of each card's kinds row."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict]] = []
        self.topics: dict[str, dict] = {}       # id -> {"h2": int, "cards_div": int}
        self.cards: dict[str, dict] = {}        # id -> {"topic": str|None, "kind": str, "row": [...], "count": int}
        self._entry: dict | None = None
        self.index_links: list[str] = []          # hrefs in nav.topics-index

    def _inside(self, pred) -> dict | None:
        for tag, attrs in reversed(self.stack):
            if pred(tag, attrs):
                return attrs
        return None

    def handle_starttag(self, tag, attrs_list):
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())
        topic = self._inside(lambda t, a: t == "section" and "topic" in (a.get("class") or "").split())
        card = self._inside(lambda t, a: t == "article" and "card" in (a.get("class") or "").split())
        if tag == "section" and "topic" in classes:
            self.topics.setdefault(attrs.get("id", ""), {"h2": 0, "cards_div": 0})
        if tag == "h2" and topic is not None:
            self.topics[topic.get("id", "")]["h2"] += 1
        if tag == "div" and "cards" in classes and topic is not None:
            self.topics[topic.get("id", "")]["cards_div"] += 1
        if tag == "article" and "card" in classes:
            cid = attrs.get("id", "")
            c = self.cards.setdefault(cid, {"topic": None, "kind": "explainer", "row": [], "count": 0})
            c["count"] += 1
            c["topic"] = topic.get("id") if topic is not None else None
            c["kind"] = attrs.get("data-kind", "explainer")
        in_row = self._inside(lambda t, a: t == "div" and {"links", "kinds"} <= set((a.get("class") or "").split()))
        if in_row is not None and card is not None and tag in ("a", "span") and "btn" in classes:
            self._entry = {"tag": tag, "classes": classes, "href": attrs.get("href"), "text": ""}
            self.cards[card.get("id", "")]["row"].append(self._entry)
        if tag == "a" and self._inside(lambda t, a: t == "nav" and "topics-index" in (a.get("class") or "").split()):
            self.index_links.append(attrs.get("href", ""))
        if tag not in ("br", "img", "meta", "link", "input", "hr"):
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        if self._entry is not None and tag == self._entry["tag"]:
            self._entry = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._entry is not None:
            self._entry["text"] += data


def parse() -> Hub:
    h = Hub()
    h.feed(HUB.read_text(encoding="utf-8"))
    return h


def papers() -> dict[str, set[str]]:
    """slug -> which files exist: {"root", "companion", "talk"}."""
    out: dict[str, set[str]] = {}
    for d in sorted(p for p in LEARNING.iterdir() if p.is_dir()):
        have = {name for name, rel in (("root", "index.html"), ("companion", "companion/index.html"),
                                       ("talk", "talk/index.html")) if (d / rel).is_file()}
        if have:
            out[d.name] = have
    return out


class HubTests(unittest.TestCase):
    def setUp(self):
        self.hub = parse()

    def test_topics_are_well_formed(self):
        self.assertTrue(self.hub.topics, "no <section class=\"topic\"> on the hub")
        for tid, t in self.hub.topics.items():
            with self.subTest(topic=tid):
                self.assertTrue(tid.startswith("topic-"), tid)
                self.assertEqual(t["h2"], 1)
                self.assertEqual(t["cards_div"], 1)

    def test_topic_index_lists_every_topic(self):
        self.assertEqual(sorted(self.hub.index_links), sorted("#" + t for t in self.hub.topics))

    def test_every_paper_has_one_card_in_a_topic(self):
        for slug in papers():
            with self.subTest(slug=slug):
                self.assertIn(slug, self.hub.cards, f"no card for learning/{slug}/")
                self.assertEqual(self.hub.cards[slug]["count"], 1)
                self.assertIsNotNone(self.hub.cards[slug]["topic"], f"card {slug} is outside a topic")

    def test_no_card_without_a_paper(self):
        for cid in self.hub.cards:
            with self.subTest(card=cid):
                self.assertIn(cid, papers(), f"card {cid} has no page under learning/{cid}/")

    def test_kinds_row(self):
        for slug, have in papers().items():
            card = self.hub.cards.get(slug)
            if card is None:
                continue  # reported by test_every_paper_has_one_card_in_a_topic
            root_kind = card["kind"]
            with self.subTest(slug=slug):
                self.assertIn(root_kind, ("explainer", "companion"))
                row = card["row"][:3]
                self.assertEqual([e["text"].strip() for e in row], [f"[{k}]" for k in KINDS])
                for kind, entry in zip(KINDS, row):
                    if kind == root_kind and "root" in have:
                        href = f"{slug}/"
                    elif kind == "companion" and "companion" in have:
                        href = f"{slug}/companion/"
                    elif kind == "talk" and "talk" in have:
                        href = f"{slug}/talk/"
                    else:
                        href = None
                    if href:
                        self.assertEqual((entry["tag"], entry["href"]), ("a", href), kind)
                    else:
                        self.assertEqual(entry["tag"], "span", kind)
                        self.assertIn("off", entry["classes"], kind)


if __name__ == "__main__":
    unittest.main()
