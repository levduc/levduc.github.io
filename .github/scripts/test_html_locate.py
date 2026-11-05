"""Tests for html_locate: visible-text projection with exact source maps."""
from __future__ import annotations

import html
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import html_locate  # noqa: E402
from html_locate import project_html  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def proj(source, collapse=True):
    return project_html(source, collapse_ws=collapse)


class ProjectionTests(unittest.TestCase):
    def test_tags_and_attributes_are_not_text(self):
        p = proj('<p class="AES">Hello <em>big</em> world</p>')
        self.assertEqual(p.text.strip(), "Hello big world")

    def test_skipped_elements_and_comments(self):
        src = ("<title>Secret one</title><script>var a = 'Secret two <b>';</script>"
               "<style>p{} /* Secret three */</style><!-- Secret four -->"
               "<svg><text>Secret five</text></svg><p>Visible</p>")
        self.assertEqual(proj(src).text.strip(), "Visible")

    def test_gt_inside_quoted_attribute(self):
        self.assertEqual(proj('<a title="a > b">link</a>').text.strip(), "link")

    def test_entities_decode_and_map_back(self):
        src = "<p>AES &amp; GCM</p>"
        p = proj(src)
        i = p.text.index("&")
        self.assertEqual(src[p.starts[i]:p.ends[i]], "&amp;")

    def test_unknown_entity_stays_literal(self):
        self.assertEqual(proj("<p>a &bogus; b</p>").text.strip(), "a &bogus; b")

    def test_nbsp_entity_and_char_are_whitespace(self):
        self.assertEqual(proj("<p>at&nbsp;most</p>").text.strip(), "at most")
        self.assertEqual(proj("<p>at most</p>").text.strip(), "at most")

    def test_typographic_entities_decode(self):
        self.assertEqual(proj("<p>it&rsquo;s &mdash; ok</p>").text.strip(), "it’s — ok")

    def test_block_boundaries_separate_words(self):
        self.assertEqual(proj("<h2>Setup</h2><p>We fix</p>").text.strip(), "Setup We fix")

    def test_runs_change_at_inline_tags(self):
        p = proj("Hello <em>big</em> world")
        t = p.text
        self.assertEqual(p.run[t.index("H")], p.run[t.index("o")])
        self.assertNotEqual(p.run[t.index("H")], p.run[t.index("b")])

    def test_math_is_masked(self):
        p = proj(r"<p>Let $x^2$ be big and \(y\) small, costs \$5.</p>")
        t = p.text
        for frag in ("$x^2$", r"\(y\)"):
            a = t.index(frag)
            self.assertTrue(all(p.math[a:a + len(frag)]), frag)
        a = t.index("be big")
        self.assertFalse(any(p.math[a:a + 6]))
        self.assertFalse(p.math[t.index("5")])

    def test_display_math_and_bracket_delimiters(self):
        p = proj(r"<p>a $$x+y$$ b \[z\] c</p>")
        t = p.text
        for frag in ("$$x+y$$", r"\[z\]"):
            a = t.index(frag)
            self.assertTrue(all(p.math[a:a + len(frag)]), frag)
        self.assertFalse(p.math[t.index("b")])

    def test_unclosed_dollar_masks_only_itself(self):
        p = proj("<p>costs $5 today</p>")
        self.assertEqual(sum(p.math), 1)
        self.assertTrue(p.math[p.text.index("$")])

    def test_math_does_not_span_runs(self):
        p = proj("<p>a $x <em>y</em> z$ b</p>")
        self.assertEqual(sum(p.math), 2)

    def test_lengths_and_sentinel(self):
        src = "<p>a &amp; b</p>\n<!-- c -->"
        p = proj(src)
        n = len(p.text)
        self.assertEqual(len(p.starts), n + 1)
        self.assertEqual(p.starts[-1], len(src))
        self.assertEqual((len(p.ends), len(p.run), len(p.math)), (n, n, n))

    def test_virtual_space_sits_at_tag_offset(self):
        src = "<h2>Setup</h2><p>We"
        p = proj(src)
        i = p.text.index(" ", p.text.index("Setup"))
        self.assertEqual(p.starts[i], p.ends[i])
        self.assertEqual(p.starts[i], src.index("</h2>"))

    def test_collapse_off_keeps_whitespace(self):
        self.assertIn("a  \n b", proj("<span>a  \n b</span>", collapse=False).text)

    def test_round_trip_offsets(self):
        src = ("<!doctype html><html><head><title>T</title><style>p{}</style></head><body>"
               '<h1 id="x">Head &amp; shoulders</h1>\n  <p class="a>b">Let $x$ be   it&rsquo;s '
               "<em>big</em>&nbsp;or <b>small</b>.</p><!-- c --><script>1<2</script>"
               "<p>&#65;&#x42; &bogus; costs \\$5 &lt;ok&gt;</p></body></html>")
        for collapse in (True, False):
            p = proj(src, collapse)
            for i, ch in enumerate(p.text):
                if p.starts[i] == p.ends[i]:
                    continue
                got = html.unescape(src[p.starts[i]:p.ends[i]])
                self.assertTrue(got == ch or (collapse and ch == " "),
                                (collapse, i, ch, got))

    def test_lattice_page_projects_quickly(self):
        src = (REPO / "static/learning/lattice/index.html").read_text(encoding="utf-8")
        t0 = time.perf_counter()
        p = proj(src)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertGreater(len(p.text), 50_000)


    # --- a skipped element ends only where a browser ends it (C1)

    def test_script_closer_needs_a_delimiter(self):
        # Browsers end script data only at "</script" followed by tab, LF, FF,
        # CR, space, "/" or ">". "</scripts>" is still script.
        src = '<script>var s = "</scripts>"; var n = 1;</script><p>Visible</p>'
        self.assertEqual(proj(src).text.strip(), "Visible")

    def test_script_closer_is_ascii_case_insensitive_only(self):
        # U+017F folds to "s" under Unicode re.I, and U+00A0 is \s to Python,
        # but neither ends a script element in a browser.
        for closer in ("</\u017fcript>", "</script\u00a0>", "</script\u2028>"):
            with self.subTest(closer=closer):
                src = "<script>a = 1;" + closer + " b = 2;</script><p>Visible</p>"
                self.assertEqual(proj(src).text.strip(), "Visible")
        for closer in ("</SCRIPT>", "</script >", "</script\t>", "</script/>", "</Script\n>"):
            with self.subTest(closer=closer):
                src = "<script>a = 1;" + closer + "<p>Visible</p>"
                self.assertEqual(proj(src).text.strip(), "Visible")

    def test_comment_open_inside_script_or_style_is_flagged(self):
        for src in ("<script><!--<script>var a = 1;</script><p>x</p></script>",
                    "<style><!-- p{} --></style><p>x</p>",
                    "<p>a</p><SCRIPT type=module>// <!--\n</SCRIPT>"):
            with self.subTest(src=src):
                self.assertTrue(proj(src).script_comment)
        for src in ("<p>a</p><script>var a = 1;</script>", "<!-- c --><p>a</p>",
                    "<textarea><!-- x --></textarea>", "<svg><!-- x --></svg>"):
            with self.subTest(src=src):
                self.assertFalse(proj(src).script_comment)

    def test_self_closing_script_still_skips(self):
        # Browsers ignore "/>" on non-void HTML elements: <script/> opens a
        # script that runs to the next </script>. Only svg honours it.
        src = '<p>a</p><script src="x.js"/>var n = 1;</script><p>Visible</p>'
        self.assertEqual(proj(src).text.replace(" ", ""), "aVisible")
        self.assertEqual(proj("<svg/><p>Visible</p>").text.strip(), "Visible")


class MarkupTokenTests(unittest.TestCase):
    """The structural check behind every page rewrite (C1, layer 4)."""

    def test_text_change_keeps_markup(self):
        old = '<p class="a">Plain prose can be rewritten.</p><script>if (a<b) x();</script>'
        new = old.replace("Plain prose can be rewritten.", "New &amp; better prose.")
        self.assertTrue(html_locate.same_markup(old, new))

    def test_attribute_values_are_ignored(self):
        self.assertTrue(html_locate.same_markup('<a href="x">t</a>', '<a href="y" title="z">t</a>'))

    def test_new_tag_is_rejected(self):
        old = "<p>The scheme is secure when n<5 holds for every round.</p>"
        new = "<p>The scheme is secure when n<img src=x onerror=alert(1) title=</p>"
        self.assertFalse(html_locate.same_markup(old, new))

    def test_changed_tag_sequence_is_rejected(self):
        cases = [
            ("<p>a</5 b</p>", "<p>a</p b</p>"),          # bogus comment becomes an end tag
            ("<p>n<5</p>", "<p>n<!-- </p>"),             # a comment opens
            ("<p>n<5</p>", "<p>n<?x </p>"),
            ("<p>a</p>", "<div>a</div>"),
            ("<p>a</p><p>b</p>", "<p>a</p>"),
        ]
        for old, new in cases:
            with self.subTest(new=new):
                self.assertFalse(html_locate.same_markup(old, new))

    def test_tokens_are_linear_on_the_biggest_page(self):
        src = (REPO / "static/learning/lattice/index.html").read_text(encoding="utf-8")
        t0 = time.perf_counter()
        self.assertTrue(html_locate.same_markup(src, src))
        self.assertLess(time.perf_counter() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
