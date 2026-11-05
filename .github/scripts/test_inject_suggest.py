import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inject_suggest as inj  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HTML = "<html><body><p>hi</p></body></html>\n"


def make_tree(tmp, html=HTML):
    static, public = tmp / "static", tmp / "public"
    for rel in ("pdag", "learning/aes-gcm/talk", "js/x"):
        for base in (static, public):
            (base / rel).mkdir(parents=True)
            (base / rel / "index.html").write_text(html)
    (public / "index.html").write_text("<html><body>home</body></html>\n")
    return static, public


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


class InjectTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.addCleanup(self._t.cleanup)
        self.static, self.public = make_tree(self.tmp)
        self.repo = "levduc/suggest-lab"
        self.base = "https://levduc.github.io/"

    def run_inject(self, base=None):
        return inj.inject(self.public, self.static, self.repo, base or self.base)

    def test_wires_pages_and_decks_not_homepage(self):
        home = (self.public / "index.html").read_bytes()
        ids = self.run_inject()
        self.assertEqual(sorted(ids), ["learning/aes-gcm/talk", "pdag"])
        self.assertEqual((self.public / "index.html").read_bytes(), home)
        self.assertNotIn(inj.MARK_BEGIN,
                         (self.public / "js/x/index.html").read_text())

    def test_deck_layout_attribute(self):
        self.run_inject()
        deck = (self.public / "learning/aes-gcm/talk/index.html").read_text()
        page = (self.public / "pdag/index.html").read_text()
        self.assertIn('data-suggest-layout="deck"', deck)
        self.assertIn('data-suggest-layout="page"', page)
        self.assertIn('data-suggest-source="static/pdag/index.html"', page)
        self.assertIn('data-suggest-repo="levduc/suggest-lab"', page)
        self.assertLess(page.index(inj.MARK_END), page.index("</body>"))

    def test_idempotent(self):
        self.run_inject()
        first = snapshot(self.tmp)
        self.run_inject()
        self.assertEqual(snapshot(self.tmp), first)
        page = (self.public / "pdag/index.html").read_text()
        self.assertEqual(page.count(inj.MARK_BEGIN), 1)

    def test_base_path_prefix(self):
        self.run_inject("https://levduc.github.io/suggest-lab/")
        page = (self.public / "pdag/index.html").read_text()
        self.assertIn('src="/suggest-lab/js/inline-suggest.js"', page)
        self.assertIn('data-suggest-notes="/suggest-lab/annotations/pdag.json"', page)
        self.assertIn('href="/suggest-lab/css/inline-suggest.css"', page)
        deck = (self.public / "learning/aes-gcm/talk/index.html").read_text()
        self.assertIn("/suggest-lab/annotations/learning--aes-gcm--talk.json", deck)

    def test_missing_body_tag_appends(self):
        (self.public / "pdag/index.html").write_text("<p>no body</p>")
        self.run_inject()
        page = (self.public / "pdag/index.html").read_text()
        self.assertTrue(page.startswith("<p>no body</p>"))
        self.assertTrue(page.rstrip().endswith(inj.MARK_END))

    def test_uppercase_body_tag(self):
        (self.public / "pdag/index.html").write_text("<BODY>x</BODY>")
        self.run_inject()
        page = (self.public / "pdag/index.html").read_text()
        self.assertLess(page.index(inj.MARK_END), page.index("</BODY>"))

    def test_bad_repo_is_rejected(self):
        before = snapshot(self.tmp)
        rc = inj.main(["--public", str(self.public), "--static", str(self.static),
                       "--repo", 'x"/><script>', "--base-url", self.base])
        self.assertEqual(rc, 2)
        self.assertEqual(snapshot(self.tmp), before)

    def test_snippet_escapes_attributes(self):
        ref = inj.classify("static/pdag/index.html")
        s = inj.snippet(ref, "a/b", '/x"y/')
        self.assertNotIn('x"y', s)
        self.assertIn("x&quot;y", s)

    def test_main_ok(self):
        rc = inj.main(["--public", str(self.public), "--static", str(self.static),
                       "--repo", self.repo, "--base-url", self.base])
        self.assertEqual(rc, 0)
        self.assertIn(inj.MARK_BEGIN, (self.public / "pdag/index.html").read_text())

    def test_missing_built_page_fails(self):
        (self.public / "pdag/index.html").unlink()
        before = snapshot(self.public)
        rc = inj.main(["--public", str(self.public), "--static", str(self.static),
                       "--repo", self.repo, "--base-url", self.base])
        self.assertEqual(rc, 1)
        self.assertEqual(snapshot(self.public), before)

    @unittest.skipUnless(shutil.which("hugo"), "hugo not installed")
    def test_real_site_wires_24(self):
        with tempfile.TemporaryDirectory() as out:
            subprocess.run(["hugo", "--quiet", "-d", out, "--baseURL",
                            "https://levduc.github.io/"],
                           cwd=ROOT, check=True, capture_output=True)
            ids = inj.inject(Path(out), ROOT / "static", "levduc/suggest-lab",
                             "https://levduc.github.io/")
            self.assertEqual(len(ids), 24, sorted(ids))
            self.assertNotIn(inj.MARK_BEGIN, (Path(out) / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
