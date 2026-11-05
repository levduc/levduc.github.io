#!/usr/bin/env python3
"""Turn a `suggestion` issue into a one-sentence source edit or a published annotation.

Security model
--------------
Every value taken from the issue is untrusted input. This script does pure
string processing: it never builds a shell command, never emits YAML, and never
hands issue text to anything executable. The workflow passes the issue body as a
*file path*, not as an argument or an interpolated expression, so GitHub never
expands it into a `run:` line.

Text that ends up in the pull-request body is wrapped in a code fence, which
makes it inert: no @mentions fire, no `Closes #N` cross-links resolve, no
Markdown renders. Text that ends up in the commit message is restricted to a
conservative character whitelist before it is written, so it cannot break out of
the YAML scalar that consumes it.

Writes are confined to an allow-list: one Markdown file under content/posts/, or
one JSON file under static/annotations/. Anything else is refused.

Outputs
-------
Always exits 0 (except on an internal error) and reports via GITHUB_OUTPUT:

    status   rejected | not_anchored | manual | replace | annotate
    reason   a short message written by *this script*, never by the reporter
    ...plus branch / pr_title / commit_message / paths when applicable

The workflow branches on `status`. Refusals are a normal outcome, not a failure,
so a bad issue leaves a comment instead of a red run.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_annotations import SCHEME_RE  # noqa: E402
from html_locate import project_html, same_markup  # noqa: E402
from suggest_paths import POST_RE, PageRef, classify, notes_filename, url_path  # noqa: E402,F401
from typing import NamedTuple  # noqa: E402

# --------------------------------------------------------------------- limits

MAX_BODY = 64 * 1024        # whole issue body
MAX_EXACT = 300             # the selected sentence
MAX_CONTEXT = 100           # prefix / suffix
MAX_COMMENT = 800
MAX_REPLACEMENT = 400
MAX_ABOUT = 200
MAX_SOURCE = 300
MAX_FILE_FIELD = 120
MAX_ANNOTATIONS = 500       # per post, to bound the JSON the browser fetches
MAX_ANNOTATION_BYTES = 512 * 1024

# Only these paths may ever be written.
ANNOTATION_DIR = "static/annotations"

# GitHub logins: alphanumeric with single internal hyphens, <= 39 chars.
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

# What a commit message is allowed to contain once our own text is added. Any
# character outside this set means we drop the untrusted part rather than guess.
COMMIT_SAFE_RE = re.compile(r"^[A-Za-z0-9 ._,:;#@+/()<>'\-\n]*$")

# Field id -> the label GitHub renders as an `### ` heading in the issue body.
FIELD_LABELS = {
    "comment": "Comment",
    "replacement": "Suggested replacement",
    "about": "About you (recommended)",
    "source": "Source (recommended if you're correcting a claim)",
    "file": "Source file",
    "exact": "Selected text",
    "prefix": "Preceding context",
    "suffix": "Following context",
}

NO_RESPONSE = "_No response_"


class Refused(Exception):
    """A validation refusal. The message is ours, and is safe to publish."""


class NeedsHand(Exception):
    """A located rewrite that must be applied by hand. str(exc) is our own wording."""


# ---------------------------------------------------------------- parse input


def parse_issue_form(body: str) -> dict[str, str]:
    """Split a rendered GitHub issue-form body into its fields.

    The rendered body looks like::

        ### Comment

        the text

        ### Source file

        content/posts/x.md

    Unknown headings are ignored, so adding a field to the form later cannot
    make this throw. Missing fields come back as the empty string.
    """
    label_to_id = {v: k for k, v in FIELD_LABELS.items()}
    out = {k: "" for k in FIELD_LABELS}

    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            out[current] = "\n".join(buf).strip()

    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            flush()
            current = label_to_id.get(m.group(1))
            buf = []
        elif current is not None:
            buf.append(line)
    flush()

    # GitHub writes a literal placeholder for an empty optional field.
    return {k: ("" if v == NO_RESPONSE else v) for k, v in out.items()}


def clean(value: str, *, allow_newlines: bool) -> str:
    """Strip control characters and normalise newlines.

    Keeps the text human-readable while removing anything that could confuse a
    downstream consumer: NULs, bidirectional overrides, zero-width characters
    and (where not allowed) line breaks.
    """
    value = unicodedata.normalize("NFC", value)
    kept = []
    for ch in value:
        if ch == "\n":
            kept.append(ch if allow_newlines else " ")
            continue
        if ch == "\t":
            kept.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue  # control, format (incl. RTL overrides), surrogate, private
        kept.append(ch)
    return "".join(kept).strip()


def validate(fields: dict[str, str], repo_root: Path) -> tuple[dict[str, str], PageRef]:
    """Apply every length, charset and path check. Raises Refused."""
    f = {
        "file": clean(fields["file"], allow_newlines=False),
        "exact": clean(fields["exact"], allow_newlines=True),
        "prefix": clean(fields["prefix"], allow_newlines=True),
        "suffix": clean(fields["suffix"], allow_newlines=True),
        "comment": clean(fields["comment"], allow_newlines=True),
        "replacement": clean(fields["replacement"], allow_newlines=True),
        "about": clean(fields.get("about", ""), allow_newlines=True),
        "source": clean(fields.get("source", ""), allow_newlines=False),
    }

    if not f["comment"] and not f["replacement"]:
        raise Refused(
            "Neither a comment nor a suggested rewrite was given, so there is "
            "nothing to record."
        )
    if not f["exact"]:
        raise Refused(
            "No selected text was supplied, so there is nothing to anchor the "
            "suggestion to. Paste the sentence you mean into **Selected text**."
        )
    if not f["file"]:
        raise Refused("No source file was supplied.")

    for key, cap in (
        ("exact", MAX_EXACT),
        ("prefix", MAX_CONTEXT),
        ("suffix", MAX_CONTEXT),
        ("comment", MAX_COMMENT),
        ("replacement", MAX_REPLACEMENT),
        ("about", MAX_ABOUT),
        ("source", MAX_SOURCE),
        ("file", MAX_FILE_FIELD),
    ):
        if len(f[key]) > cap:
            raise Refused(
                f"The **{FIELD_LABELS[key]}** field is {len(f[key])} characters, "
                f"over the {cap}-character limit."
            )

    for key in ("about", "source"):
        if SCHEME_RE.match(f[key]):
            raise Refused(
                f"The **{FIELD_LABELS[key]}** field starts with a script-style link "
                "scheme. Links must use https."
            )

    # --- the file path: allow-list first, then resolve and re-check.
    ref = classify(f["file"])
    if ref is None:
        raise Refused(
            f"`{redact(f['file'])}` is not an editable source path. Suggestions are "
            "accepted only for posts under `content/posts/` and the site's pages."
        )

    root = repo_root.resolve()
    target = (root / f["file"]).resolve()
    allowed = ((root / "content" / "posts").resolve(), (root / "static").resolve())
    if not any(d in target.parents for d in allowed):
        raise Refused("The source path resolves outside the editable directories.")
    # No component from the target up to the repo root may be a symlink.
    probe = root / f["file"]
    while probe != root:
        if probe.is_symlink():
            raise Refused("The source path is a symbolic link, which is not allowed.")
        probe = probe.parent
    if not target.is_file():
        raise Refused(
            f"`{redact(f['file'])}` does not exist in the repository. The page may "
            "have been renamed since it was loaded."
        )

    if f["replacement"] and f["replacement"] == f["exact"]:
        raise Refused(
            "The suggested replacement is identical to the selected text, so "
            "there is nothing to change."
        )

    return f, ref


def site_base_url(repo_root: Path, override: str) -> str:
    """The deployed site's base URL, for building a link back to the sentence.

    Read from config.toml so it follows the site rather than being duplicated
    here. Only used to build a convenience link in the PR body, so a wrong
    value is cosmetic.
    """
    if override:
        return override.rstrip("/") + "/"
    try:
        import tomllib

        with open(repo_root / "config.toml", "rb") as fh:
            base = tomllib.load(fh).get("baseURL", "")
        if isinstance(base, str) and base.startswith("http"):
            return base.rstrip("/") + "/"
    except Exception:
        pass
    return ""


def live_link(base: str, path: str, exact: str) -> str:
    """A text-fragment deep link to the sentence as it appears on the page.

    Lets a reviewer jump from the pull request to the sentence in context, with
    the browser scrolling to and highlighting it.
    """
    if not base:
        return ""
    snippet = re.sub(r"\s+", " ", exact).strip()[:90]
    return f"{base}{path}#:~:text={urllib.parse.quote(snippet)}"


def redact(value: str, limit: int = 80) -> str:
    """Make an untrusted string safe to drop into a Markdown sentence.

    Backticks and newlines are removed so the value cannot escape the inline
    code span we always wrap it in.
    """
    flat = re.sub(r"\s+", " ", value.replace("`", "")).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


# ------------------------------------------------------------------ anchoring


def project(text: str, *, collapse_ws: bool, strip_md: bool) -> tuple[str, list[int]]:
    """Reduce `text` to a comparable form, keeping a map back to the original.

    Returns `(projected, index_map)` where `index_map[i]` is the offset in
    `text` of the character that produced `projected[i]`. `index_map` has one
    extra trailing entry so a match end can always be mapped.

    This is what lets a fuzzy match report an *exact* span in the real file: we
    match in the reduced space, then translate the span back.
    """
    out: list[str] = []
    imap: list[int] = []
    i, n = 0, len(text)
    pending_ws = False

    while i < n:
        ch = text[i]

        if strip_md:
            # A backslash escape contributes its second character literally.
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                imap.append(i + 1)
                i += 2
                pending_ws = False
                continue
            # Inline code: drop the fences, keep the contents.
            if ch == "`":
                run = len(text[i:]) - len(text[i:].lstrip("`"))
                i += run
                continue
            # Images: drop the alt text wholesale, it is not prose.
            if ch == "!" and text.startswith("![", i):
                close = text.find("]", i)
                if close != -1 and text.startswith("(", close + 1):
                    end = text.find(")", close)
                    if end != -1:
                        i = end + 1
                        continue
            # Links: keep the label, drop the target.
            if ch == "[":
                close = text.find("]", i)
                if close != -1 and text.startswith("(", close + 1):
                    end = text.find(")", close + 1)
                    if end != -1:
                        inner, inner_map = project(
                            text[i + 1 : close], collapse_ws=collapse_ws, strip_md=True
                        )
                        # Flush whitespace pending from before the "[", or the
                        # link label fuses onto the preceding word.
                        if pending_ws and out and inner:
                            out.append(" ")
                            imap.append(i)
                        out.extend(inner)
                        imap.extend(i + 1 + k for k in inner_map[: len(inner)])
                        i = end + 1
                        pending_ws = False
                        continue
            # Emphasis / strong markers carry no text.
            if ch in "*_":
                run = len(text[i:]) - len(text[i:].lstrip(ch))
                i += run
                continue

        if collapse_ws and ch.isspace():
            pending_ws = True
            i += 1
            continue

        if pending_ws and out:
            out.append(" ")
            imap.append(i)
        pending_ws = False

        out.append(ch)
        imap.append(i)
        i += 1

    imap.append(n)
    return "".join(out), imap


def _all_spans(haystack: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    spans, start = [], 0
    while True:
        k = haystack.find(needle, start)
        if k == -1:
            return spans
        spans.append((k, k + len(needle)))
        start = k + 1  # overlapping occurrences count as distinct


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _locate(hay: str, exact: str, prefix: str, suffix: str) -> list[tuple[int, int]]:
    """Find `exact` in `hay`, using prefix/suffix only to disambiguate.

    The context comparison is deliberately whitespace-insensitive even in the
    verbatim pass. Context never decides *what* gets written — only *which* of
    several identical candidates is meant — so tolerance here costs nothing,
    and without it a selection whose boundary whitespace was trimmed in transit
    would fail to disambiguate at all.
    """
    spans = _all_spans(hay, exact)
    if len(spans) <= 1:
        return spans

    flat_prefix = _flat(prefix).rstrip()
    flat_suffix = _flat(suffix).lstrip()

    def before_ok(s: int) -> bool:
        return not flat_prefix or _flat(hay[:s]).rstrip().endswith(flat_prefix)

    def after_ok(e: int) -> bool:
        return not flat_suffix or _flat(hay[e:]).lstrip().startswith(flat_suffix)

    narrowed = [(s, e) for s, e in spans if before_ok(s) and after_ok(e)]
    if len(narrowed) == 1:
        return narrowed

    # Still ambiguous: score by how much context agrees, which handles a
    # selection whose surrounding text was lightly reflowed.
    def context_score(span: tuple[int, int]) -> int:
        s, e = span
        before = _flat(hay[:s]).rstrip()
        after = _flat(hay[e:]).lstrip()
        p = next(
            (k for k in range(min(len(flat_prefix), len(before)), 0, -1)
             if before.endswith(flat_prefix[-k:])),
            0,
        )
        q = next(
            (k for k in range(min(len(flat_suffix), len(after)), 0, -1)
             if after.startswith(flat_suffix[:k])),
            0,
        )
        return p + q

    if flat_prefix or flat_suffix:
        scored = sorted(spans, key=context_score, reverse=True)
        best = context_score(scored[0])
        if best > 0 and (len(scored) == 1 or context_score(scored[1]) < best):
            return [scored[0]]

    return narrowed or spans


class Anchor(NamedTuple):
    start: int      # source offsets
    end: int
    how: str        # "verbatim" | "whitespace-normalised" | "markdown-tolerant"
    line: int       # 1-based line of `start` in the source
    blocker: str    # "" if the located span may be rewritten, else a reason (our text)


class NotLocated(Exception):
    """The text is absent or ambiguous. str(exc) is our own wording."""


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _ambiguous(how: str) -> NotLocated:
    return NotLocated(
        f"The selected text appears more than once in the source and the "
        f"surrounding context was not enough to tell the occurrences apart "
        f"(matched {how}). Selecting a longer passage usually fixes this."
    )


def find_md_anchor(source: str, exact: str, prefix: str, suffix: str) -> Anchor:
    """Locate `exact` in Markdown `source`.

    Three passes, strictest first. Each must identify exactly one span; an
    ambiguous or absent match raises rather than guessing, because writing to
    the wrong sentence is far worse than declining.
    """
    ladder = (
        ("verbatim", dict(collapse_ws=False, strip_md=False)),
        ("whitespace-normalised", dict(collapse_ws=True, strip_md=False)),
        ("markdown-tolerant", dict(collapse_ws=True, strip_md=True)),
    )

    ambiguous_at: str | None = None
    for how, opts in ladder:
        hay, imap = project(source, **opts)
        ned, _ = project(exact, **opts)
        pre, _ = project(prefix, **opts)
        suf, _ = project(suffix, **opts)
        if not ned:
            continue

        spans = _locate(hay, ned, pre, suf)
        if len(spans) == 1:
            s, e = spans[0]
            return Anchor(imap[s], imap[e], how, _line_of(source, imap[s]), "")
        if len(spans) > 1 and ambiguous_at is None:
            ambiguous_at = how

    if ambiguous_at:
        raise _ambiguous(ambiguous_at)
    raise NotLocated(
        "The selected text could not be found in the Markdown source. This "
        "usually means the sentence contains formatting that does not survive "
        "rendering, or the post has been edited since the page was loaded."
    )


def find_html_anchor(source: str, exact: str, prefix: str, suffix: str) -> Anchor:
    """Locate `exact` in the visible text of HTML `source`.

    The needle and its context are reader text, never HTML, so they go through
    `project`, not `project_html`: a selection containing `<` must stay text.
    """
    ladder = (("verbatim", False), ("whitespace-normalised", True))

    ambiguous_at: str | None = None
    for how, collapse in ladder:
        proj = project_html(source, collapse_ws=collapse)
        ned, _ = project(exact, collapse_ws=collapse, strip_md=False)
        pre, _ = project(prefix, collapse_ws=collapse, strip_md=False)
        suf, _ = project(suffix, collapse_ws=collapse, strip_md=False)
        if not ned:
            continue

        spans = _locate(proj.text, ned, pre, suf)
        if len(spans) > 1 and ambiguous_at is None:
            ambiguous_at = how
        if len(spans) != 1:
            continue

        s, e = spans[0]
        # Virtual spaces (block tags) have no source extent; never start or end on one.
        while s < e and proj.starts[s] >= proj.ends[s]:
            s += 1
        while e > s and proj.starts[e - 1] >= proj.ends[e - 1]:
            e -= 1
        start, end = proj.starts[s], proj.ends[e - 1] if e > s else proj.starts[s]
        if start >= end:
            continue  # a zero-width span is never a rewrite target

        blocker = ""
        if proj.script_comment:
            blocker = ("This page has script content the automation can't parse "
                       "safely, so it has to be edited by hand.")
        elif source[start - 1:start] == "<":
            # "n<5": a rewrite starting with a letter would complete a tag.
            blocker = ("The selected text starts right after a `<`, where a rewrite "
                       "could become markup, so it has to be edited by hand.")
        elif len(set(proj.run[s:e])) > 1:
            blocker = ("The selected text crosses formatting (a link, emphasis or "
                       "code), so it has to be edited by hand.")
        elif any(proj.math[s:e]):
            blocker = ("The selected text contains math, which the automation "
                       "never rewrites.")
        return Anchor(start, end, how, _line_of(source, start), blocker)

    if ambiguous_at:
        raise _ambiguous(ambiguous_at)
    raise NotLocated(
        "The selected text could not be found in the page's visible text. It may "
        "sit in a script or an attribute, or the page has been edited since it "
        "was loaded."
    )


def rewrite_blocker(replacement: str) -> str:
    # "{{" covers Hugo's "{{<" and "{{%" shortcodes, which run at build time
    # even though Goldmark omits raw HTML.
    if "$" in replacement or "\\" in replacement or "{{" in replacement:
        return ("The suggested rewrite contains `$`, a backslash or `{{`, which this "
                "site's math rendering or Hugo's shortcodes would misread, so it has "
                "to be applied by hand.")
    return ""


# Every "{{" with the character after it, overlapping, so "{{{<" yields both.
_BRACES = re.compile(r"(?=(\{\{.?))", re.S)


def _braces(text: str) -> list[str]:
    return [m.group(1) for m in _BRACES.finditer(text)]


# -------------------------------------------------------------------- outputs


def fence(text: str) -> str:
    """Wrap untrusted text in a code fence long enough to contain it.

    Inside a fence, Markdown does not render: @mentions do not notify anyone and
    `Closes #12` does not link an issue. That is the point.
    """
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    bars = "`" * max(3, longest + 1)
    return f"{bars}\n{text}\n{bars}"


def co_author_trailer(login: str, user_id: str) -> str:
    """`Co-authored-by` using GitHub's public noreply address, or "" if unusable."""
    if not LOGIN_RE.match(login or "") or not (user_id or "").isdigit():
        return ""
    return f"Co-authored-by: {login} <{user_id}+{login}@users.noreply.github.com>"


def emit(out_path: str | None, pairs: dict[str, str]) -> None:
    """Append key/value pairs to GITHUB_OUTPUT using the heredoc form.

    The random-ish delimiter and the explicit newline handling are what keep a
    multi-line value from being read as further key/value pairs.
    """
    rendered = []
    for key, value in pairs.items():
        if "\n" in value:
            delim = f"__eof_{key}_{os.urandom(8).hex()}__"
            rendered.append(f"{key}<<{delim}\n{value}\n{delim}")
        else:
            rendered.append(f"{key}={value}")
    blob = "\n".join(rendered) + "\n"

    if out_path:
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(blob)
    else:
        sys.stdout.write(blob)


# ----------------------------------------------------------------------- main


def do_replace(
    repo_root: Path, f: dict[str, str], ref: PageRef, issue: int, anchor: Anchor,
    base: str,
) -> tuple[str, str]:
    """Rewrite the located span. Returns (pr_body, summary_line)."""
    target = repo_root / f["file"]
    source = target.read_text(encoding="utf-8")
    # Pages are executed by browsers, so reader text is escaped; posts are
    # Markdown, where Hugo omits raw HTML by default.
    new_text = f["replacement"] if ref.kind == "post" else html.escape(f["replacement"], quote=True)
    updated = source[:anchor.start] + new_text + source[anchor.end:]
    if updated == source:
        raise Refused("Applying the replacement produced no change to the file.")
    # Check the file as it would be written, before writing it. A rewrite
    # changes text only: it must never change markup, and in a post it must
    # never form a shortcode with the text beside it ("{" + "{< youtube …").
    if ref.kind == "post":
        if _braces(source) != _braces(updated):
            raise NeedsHand("The rewrite would form or change a Hugo shortcode (`{{`) "
                            "together with the text beside it, so it has to be applied "
                            "by hand.")
    elif not same_markup(source, updated):
        raise NeedsHand("The rewrite would change the page's markup, not just its text, "
                        "so it has to be edited by hand.")
    target.write_text(updated, encoding="utf-8")

    before = source[anchor.start:anchor.end]
    link = live_link(base, url_path(ref), f["exact"])
    creds = credentials_block(f)
    parts = [
        f"Closes #{issue}.",
        "",
        f"A reader suggested a change at line {anchor.line} of `{f['file']}`. "
        f"The text was located {anchor.how}."
        + (f" [See it in context]({link})." if link else ""),
        "",
    ]
    if creds:
        parts += [creds, ""]
    parts += [
        "### The text as it stands",
        "",
        fence(before),
        "",
        "### Suggested rewrite",
        "",
        fence(f["replacement"]),
        "",
    ]
    if f["comment"]:
        parts += ["### Why", "", fence(f["comment"]), ""]
    parts += [
        "---",
        "",
        "Opened automatically from the linked issue. The quoted text above is "
        "reader-supplied and is shown in code fences so it cannot render, "
        "mention anyone or cross-link other issues. Read the diff, not the "
        "quote, before merging.",
    ]
    return "\n".join(parts), f"replaces one sentence in {f['file']}"


SAFE_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~%/+\-=&?#:]*)?")
# A bare issue or PR URL in a PR body makes GitHub cross-reference that issue,
# so any reader could make the bot post into arbitrary threads. These stay in
# the fence and are never listed.
GITHUB_XREF_RE = re.compile(
    r"https://(?:www\.)?github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/\d", re.IGNORECASE)


def credentials_block(f: dict[str, str]) -> str:
    """Who the suggester says they are, fenced, plus plain https links to check.

    The self-description and source are reader text, so they sit in fences.
    Only URLs matching SAFE_URL_RE, and not GITHUB_XREF_RE, are repeated
    outside a fence.
    """
    about, source = f.get("about", ""), f.get("source", "")
    if not about and not source:
        return ""
    parts = ["### About the suggester (self-described)", "", fence(about or "(not given)"),
             "", "### Source", "", fence(source or "(not given)"), ""]
    links: list[str] = []
    for text in (about, source):
        for m in SAFE_URL_RE.finditer(text):
            url = m.group(0)
            if url not in links and not GITHUB_XREF_RE.match(url):
                links.append(url)
    if links:
        parts += ["Links:", ""] + [f"- {u}" for u in links] + [""]
    return "\n".join(parts)


def do_annotate(
    repo_root: Path, f: dict[str, str], ref: PageRef, issue: int, login: str, base: str,
) -> tuple[str, str, str]:
    """Append a reader note to the page's notes file. Needs no source anchor.

    Returns (pr_body, summary, rel_path).
    """
    rel = f"{ANNOTATION_DIR}/{notes_filename(ref.page_id)}"
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.json$", Path(rel).name):
        raise Refused("The page name contains characters that cannot be used in a filename.")
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if path.is_file():
        if path.stat().st_size > MAX_ANNOTATION_BYTES:
            raise Refused("This page's annotation file is already at its size limit.")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise Refused(
                f"`{rel}` is not valid JSON, so it cannot be appended to safely."
            )
        if not isinstance(loaded, list):
            raise Refused(f"`{rel}` does not contain a JSON array.")
        existing = loaded

    if len(existing) >= MAX_ANNOTATIONS:
        raise Refused(
            f"This page already has {len(existing)} annotations, which is the limit."
        )
    if any(isinstance(a, dict) and a.get("issue") == issue for a in existing):
        # An edited issue re-runs the workflow; replace rather than duplicate.
        existing = [a for a in existing if not (isinstance(a, dict) and a.get("issue") == issue)]

    # The locator only decides what the PR body says; the note never needs it.
    source = (repo_root / f["file"]).read_text(encoding="utf-8")
    finder = find_md_anchor if ref.kind == "post" else find_html_anchor
    try:
        a = finder(source, f["exact"], f["prefix"], f["suffix"])
        where = f"Found at line {a.line} of `{f['file']}`."
    except NotLocated:
        where = ("Not found in the source (it may be math or text the page's code "
                 "draws); it will be matched on the rendered page.")

    entry = {
        "id": f"s{issue}",
        "exact": f["exact"],
        "prefix": f["prefix"],
        "suffix": f["suffix"],
        "comment": f["comment"],
        "author": login if LOGIN_RE.match(login or "") else "",
        "about": f.get("about", ""),
        "source": f.get("source", ""),
        "issue": issue,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    existing.append(entry)
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    link = live_link(base, url_path(ref), entry["exact"])
    creds = credentials_block(f)
    parts = [
        f"Closes #{issue}.",
        "",
        f"A reader left a comment on a sentence in `{f['file']}`. There is no "
        "suggested rewrite, so this publishes the comment as a margin note. "
        + where
        + (f" [See it in context]({link})." if link else ""),
        "",
    ]
    if creds:
        parts += [creds, ""]
    parts += [
        "### The sentence",
        "",
        fence(entry["exact"]),
        "",
        "### The comment",
        "",
        fence(f["comment"]),
        "",
        "---",
        "",
        f"Merging adds the note to `{rel}`, which the page fetches at load. "
        "Reader-supplied text is quoted in code fences here, and is rendered on "
        "the page with `textContent`, so it is inert in both places.",
    ]
    return "\n".join(parts), f"publishes a reader note on {f['file']}", rel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--body-file", required=True, help="file holding the raw issue body")
    ap.add_argument("--issue", required=True, type=int)
    ap.add_argument("--author", default="", help="issue author login")
    ap.add_argument("--author-id", default="", help="issue author numeric id")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--out-dir", required=True, help="scratch dir for body/comment files")
    ap.add_argument("--site-url", default="",
                    help="deployed base URL; defaults to baseURL from config.toml")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    gh_out = os.environ.get("GITHUB_OUTPUT")

    raw = Path(args.body_file).read_bytes()
    if len(raw) > MAX_BODY:
        write_refusal(
            out_dir, gh_out,
            f"The issue body is {len(raw)} bytes, over the {MAX_BODY}-byte limit.",
        )
        return 0
    body = raw.decode("utf-8", errors="replace")

    try:
        fields, ref = validate(parse_issue_form(body), repo_root)
    except Refused as exc:
        write_refusal(out_dir, gh_out, str(exc))
        return 0

    source = (repo_root / fields["file"]).read_text(encoding="utf-8")
    finder = find_md_anchor if ref.kind == "post" else find_html_anchor
    base = site_base_url(repo_root, args.site_url)
    trailer = co_author_trailer(args.author, args.author_id)

    try:
        if not fields["replacement"]:
            status = "annotate"
            pr_body, summary, changed = do_annotate(
                repo_root, fields, ref, args.issue, args.author, base
            )
            try:
                how = finder(source, fields["exact"], fields["prefix"],
                             fields["suffix"]).how
            except NotLocated:
                how = "not located"
        else:
            try:
                anchor = finder(source, fields["exact"], fields["prefix"], fields["suffix"])
            except NotLocated as exc:
                write_manual(out_dir, gh_out, fields, ref, str(exc), None, base)
                return 0
            how = anchor.how
            reason = anchor.blocker or rewrite_blocker(fields["replacement"])
            if reason:
                write_manual(out_dir, gh_out, fields, ref, reason, anchor, base,
                             located=source[anchor.start:anchor.end])
                return 0
            status = "replace"
            try:
                pr_body, summary = do_replace(
                    repo_root, fields, ref, args.issue, anchor, base
                )
            except NeedsHand as exc:
                write_manual(out_dir, gh_out, fields, ref, str(exc), anchor, base,
                             located=source[anchor.start:anchor.end])
                return 0
            changed = fields["file"]
    except Refused as exc:
        write_refusal(out_dir, gh_out, str(exc))
        return 0

    commit = f"suggestion #{args.issue}: {summary}"
    if trailer:
        commit = f"{commit}\n\n{trailer}"
    if not COMMIT_SAFE_RE.match(commit):
        # Should be unreachable: every component is either ours or regex-checked.
        commit = f"suggestion #{args.issue}: apply reader suggestion"

    body_path = out_dir / "pr_body.md"
    body_path.write_text(pr_body + "\n", encoding="utf-8")

    emit(gh_out, {
        "status": status,
        "reason": "",
        "branch": f"suggest/{args.issue}",
        "pr_title": f"suggestion #{args.issue}: {ref.page_id}",
        "commit_message": commit,
        "body_path": str(body_path),
        "changed_path": changed,
        "anchored_how": how,
    })
    print(f"[apply_suggestion] {status}: {summary} (located {how})")
    return 0


def write_manual(
    out_dir: Path, gh_out: str | None, f: dict[str, str], ref: PageRef,
    reason: str, anchor: Anchor | None, base: str, located: str = "",
) -> None:
    """A rewrite that cannot be applied automatically: comment on the issue, open no PR."""
    before = located if anchor is not None and located else f["exact"]
    parts = [
        "Thanks, this suggestion needs a hand edit, so no pull request was opened.",
        "",
        reason,
        "",
        "### The text as it stands",
        "",
        fence(before),
        "",
        "### Suggested rewrite",
        "",
        fence(f["replacement"]),
        "",
    ]
    if anchor is not None:
        parts += [f"Line {anchor.line} of `{f['file']}`", ""]
        link = live_link(base, url_path(ref), f["exact"])
        if link:
            parts += [f"[See it in context]({link})", ""]
    parts += ["Nothing has been changed. The site owner will apply it by hand, or reply here."]
    comment_path = out_dir / "issue_comment.md"
    comment_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    emit(gh_out, {
        "status": "manual",
        "reason": reason,
        "comment_path": str(comment_path),
    })
    print(f"[apply_suggestion] manual: {reason}", file=sys.stderr)


def write_refusal(out_dir: Path, gh_out: str | None, reason: str) -> None:
    """Record a refusal for the workflow to post as an issue comment."""
    note = "\n".join([
        "Thanks for the suggestion — the automation could not apply it.",
        "",
        reason,
        "",
        "Nothing has been changed. Reply here and it will be handled by hand, or "
        "edit the issue and the check will run again.",
    ])
    comment_path = out_dir / "issue_comment.md"
    comment_path.write_text(note + "\n", encoding="utf-8")
    emit(gh_out, {
        "status": "not_anchored",
        "reason": reason,
        "comment_path": str(comment_path),
    })
    print(f"[apply_suggestion] refused: {reason}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
