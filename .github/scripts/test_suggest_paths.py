"""Tests for the allow-list module suggest_paths."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import suggest_paths  # noqa: E402
from suggest_paths import PageRef, classify, notes_filename, url_path  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


class ClassifyTests(unittest.TestCase):
    def test_paper_page(self):
        self.assertEqual(classify("static/pdag/index.html"),
                         PageRef("page", "static/pdag/index.html", "pdag"))

    def test_learning_pages(self):
        self.assertEqual(classify("static/learning/index.html"),
                         PageRef("page", "static/learning/index.html", "learning"))
        self.assertEqual(classify("static/learning/lattice/index.html"),
                         PageRef("page", "static/learning/lattice/index.html",
                                 "learning/lattice"))

    def test_decks(self):
        self.assertEqual(classify("static/fastlane/talk/index.html"),
                         PageRef("deck", "static/fastlane/talk/index.html",
                                 "fastlane/talk"))
        self.assertEqual(classify("static/learning/aes-gcm/talk/index.html"),
                         PageRef("deck", "static/learning/aes-gcm/talk/index.html",
                                 "learning/aes-gcm/talk"))

    def test_post(self):
        self.assertEqual(classify("content/posts/suggest-an-edit.md"),
                         PageRef("post", "content/posts/suggest-an-edit.md",
                                 "posts/suggest-an-edit"))

    def test_rejects_everything_else(self):
        for p in [
            "static/js/inline-suggest.js", "static/js/index.html",
            "static/css/index.html", "static/annotations/index.html",
            "static/papers/index.html",
            "static/index.html", "static/duc-resume.pdf", "static/papers/t3.pdf",
            "layouts/index.html", "config.toml", "content/_index.md",
            ".github/workflows/pages.yml",
            "static/../layouts/index.html", "static/pdag/../js/index.html",
            "static\\pdag\\index.html", "/static/pdag/index.html",
            "static/PDAG/index.html", "static/a/b/c/index.html",
            # Asset directories are never pages, whatever shape is under them.
            "static/js/talk/index.html", "static/annotations/talk/index.html",
            "static/css/talk/index.html", "static/papers/talk/index.html",
        ]:
            with self.subTest(path=p):
                self.assertIsNone(classify(p))

    def test_denied_first(self):
        self.assertEqual(suggest_paths.DENIED_FIRST,
                         frozenset({"js", "css", "annotations", "papers",
                                    "fonts", "img"}))


class NamingTests(unittest.TestCase):
    def test_notes_filenames(self):
        self.assertEqual(notes_filename("learning/lattice"), "learning--lattice.json")
        self.assertEqual(notes_filename("fastlane/talk"), "fastlane--talk.json")
        self.assertEqual(notes_filename("posts/suggest-an-edit"),
                         "posts--suggest-an-edit.json")

    def test_post_and_page_ids_never_collide(self):
        post = classify("content/posts/pdag.md")
        page = classify("static/pdag/index.html")
        self.assertNotEqual(notes_filename(post.page_id), notes_filename(page.page_id))

    def test_url_path(self):
        self.assertEqual(url_path(classify("content/posts/suggest-an-edit.md")),
                         "posts/suggest-an-edit/")
        self.assertEqual(url_path(classify("static/learning/aes-gcm/talk/index.html")),
                         "learning/aes-gcm/talk/")
        self.assertEqual(url_path(classify("static/pdag/index.html")), "pdag/")


class CompanionTests(unittest.TestCase):
    def test_companion_page(self):
        ref = classify("static/learning/lattice/companion/index.html")
        self.assertEqual(ref, PageRef("page", "static/learning/lattice/companion/index.html",
                                      "learning/lattice/companion"))
        self.assertEqual(url_path(ref), "learning/lattice/companion/")
        self.assertEqual(notes_filename("learning/lattice/companion"),
                         "learning--lattice--companion.json")

    def test_companion_shapes_refused(self):
        for p in ("static/learning/lattice/companion/x/index.html",
                  "static/lattice/companion/index.html",
                  "static/learning/lattice/Companion/index.html",
                  "static/learning/js/../companion/index.html"):
            with self.subTest(path=p):
                self.assertIsNone(classify(p))

    def test_slug_named_companion_is_still_a_page(self):
        self.assertEqual(classify("static/learning/companion/index.html").page_id,
                         "learning/companion")


class RealTreeTests(unittest.TestCase):
    def test_every_real_page_classifies(self):
        found = sorted(p.relative_to(REPO).as_posix()
                       for p in (REPO / "static").rglob("index.html"))
        refs = [classify(p) for p in found]
        for p, r in zip(found, refs):
            with self.subTest(path=p):
                self.assertIsNotNone(r)
        # Counted from the tree, so a new page needs no edit here.
        self.assertGreater(len(found), 0)
        decks = sum(1 for p in found if p.endswith("/talk/index.html"))
        self.assertEqual(sum(1 for r in refs if r and r.kind == "deck"), decks)


if __name__ == "__main__":
    unittest.main()
