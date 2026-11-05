"""Visible-text projection of an HTML source with exact maps back to source offsets.

One linear, hand-written scan.  Standard library only.
"""
from __future__ import annotations

import html
import re
from typing import NamedTuple

SKIP_ELEMENTS = frozenset({"script", "style", "title", "textarea", "noscript", "template", "svg"})
BLOCK_TAGS = frozenset({"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "br",
                        "section", "article", "aside", "header", "footer", "nav", "blockquote",
                        "figure", "figcaption", "table", "tr", "td", "th", "dt", "dd", "hr", "pre"})


class Projection(NamedTuple):
    text: str          # visible text, entities decoded
    starts: list[int]  # source offset where text[i] begins; len == len(text) + 1, last == len(source)
    ends: list[int]    # source offset just after text[i]; a virtual space has ends[i] == starts[i]
    run: list[int]     # id of the contiguous text run text[i] came from; every tag starts a new run
    math: list[bool]   # text[i] lies inside (or is) a KaTeX delimiter pair within its run
    # Some script or style body contains "<!--". Browsers then enter the
    # escaped (and double-escaped) script states, where "</script>" may not end
    # the element. This scan does not model those states, so callers must not
    # trust the projection of such a page for rewrites.
    script_comment: bool = False


_SPECIAL = re.compile(r"[<&]")
_ENTITY = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
_TAG_NAME = re.compile(r"[A-Za-z][^\s/>]*")
_CLOSERS: dict[str, re.Pattern] = {}


def _skip_closer(name: str) -> re.Pattern:
    """The end tag of a skipped element, as a browser recognises it.

    A browser ends script (and style, title, ...) data only at "</name" followed
    by tab, LF, FF, CR, space, "/" or ">", and matches the name ASCII
    case-insensitively. Python's \\s and Unicode re.I are both wider (U+00A0,
    U+2028; U+017F folds to "s"), so they would end the skip early: text the
    browser still runs as script would look like prose.
    """
    pat = _CLOSERS.get(name)
    if pat is None:
        pat = _CLOSERS[name] = re.compile(
            "</" + re.escape(name) + r"(?=[\t\n\f\r />])", re.IGNORECASE | re.ASCII)
    return pat


def _tag_end(src: str, i: int) -> tuple[int, bool]:
    """Given src[i] is the first char after the tag name, return (index after '>', self_closing).

    A quote opens a value only right after '='; the matching quote is skipped.
    An unterminated tag runs to the end of the source.
    """
    n = len(src)
    last = ""  # last non-space char seen
    while i < n:
        c = src[i]
        if c == ">":
            return i + 1, last == "/"
        if c in "\"'" and last == "=":
            j = src.find(c, i + 1)
            if j < 0:
                return n, False
            i = j + 1
            last = c
            continue
        if not c.isspace():
            last = c
        i += 1
    return n, False


def _mask_math(t: str, lo: int, hi: int, math: list[bool]) -> None:
    """Mark KaTeX delimiter pairs (and unclosed delimiters) of the run text[lo:hi]."""
    s = t[lo:hi]
    if "$" not in s and "\\" not in s:
        return
    n = len(s)
    dead: set[str] = set()  # delimiters known to have no closer after the current point

    def close_dollar(j: int, double: bool) -> int:
        while j < n:
            c = s[j]
            if c == "\\":
                j += 2
            elif c == "$" and (not double or s.startswith("$$", j)):
                return j
            else:
                j += 1
        return -1

    def put(a: int, b: int) -> None:
        for k in range(lo + a, lo + b):
            math[k] = True

    i = 0
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            d = s[i + 1]
            if d in "([":
                closer = "\\)" if d == "(" else "\\]"
                j = -1 if closer in dead else s.find(closer, i + 2)
                if j < 0:
                    dead.add(closer)
                    put(i, i + 2)
                    i += 2
                else:
                    put(i, j + 2)
                    i = j + 2
            else:
                i += 2  # \$ and any other escape: literal
        elif c == "$":
            double = s.startswith("$$", i)
            width = 2 if double else 1
            key = "$$" if double else "$"
            j = -1 if key in dead else close_dollar(i + width, double)
            if j < 0:
                dead.add(key)
                put(i, i + width)
                i += width
            else:
                put(i, j + width)
                i = j + width
        else:
            i += 1


def project_html(source: str, *, collapse_ws: bool) -> Projection:
    n = len(source)
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    runs: list[int] = []
    run = 0
    script_comment = False

    # True while the last emitted char is a collapsed space.
    prev_space = False

    def emit(ch: str, s: int, e: int, r: int) -> None:
        nonlocal prev_space
        if collapse_ws and ch.isspace():
            if prev_space:
                # Extend a real collapsed space over directly adjacent whitespace.
                if runs[-1] == r and ends[-1] == s and ends[-1] > starts[-1]:
                    ends[-1] = e
                return
            ch = " "
            prev_space = True
        else:
            prev_space = False
        chars.append(ch)
        starts.append(s)
        ends.append(e)
        runs.append(r)

    i = 0
    while i < n:
        m = _SPECIAL.search(source, i)
        stop = m.start() if m else n
        for k in range(i, stop):
            emit(source[k], k, k + 1, run)
        if m is None:
            break
        i = stop
        if source[i] == "&":
            em = _ENTITY.match(source, i)
            if em:
                raw = em.group()
                dec = html.unescape(raw)
                if dec != raw:
                    for ch in dec:
                        emit(ch, i, em.end(), run)
                    i = em.end()
                    continue
            emit("&", i, i + 1, run)
            i += 1
            continue
        # source[i] == "<"
        if source.startswith("<!--", i):
            j = source.find("-->", i + 4)
            i = n if j < 0 else j + 3
            run += 1
            continue
        if source.startswith("<![CDATA[", i):
            j = source.find("]]>", i + 9)
            i = n if j < 0 else j + 3
            run += 1
            continue
        nxt = source[i + 1:i + 2]
        if nxt in ("!", "?"):
            j = source.find(">", i + 2)
            i = n if j < 0 else j + 1
            run += 1
            continue
        closing = nxt == "/"
        nm = _TAG_NAME.match(source, i + 2 if closing else i + 1)
        if nm is None:
            emit("<", i, i + 1, run)  # stray '<' is literal text
            i += 1
            continue
        name = nm.group().lower()
        tag_start = i
        i, self_closing = _tag_end(source, nm.end())
        run += 1
        if name in BLOCK_TAGS:
            emit(" ", tag_start, tag_start, run)
            run += 1
        # "/>" means nothing on a non-void HTML element: <script/> still opens a
        # script. Only svg (foreign content) honours it.
        if name in SKIP_ELEMENTS and not closing and not (self_closing and name == "svg"):
            cm = _skip_closer(name).search(source, i)
            body_end = n if cm is None else cm.start()
            if name in ("script", "style") and "<!--" in source[i:body_end]:
                script_comment = True
            i = body_end
            run += 1

    text = "".join(chars)
    starts.append(n)
    math = [False] * len(text)
    lo = 0
    for k in range(1, len(text) + 1):
        if k == len(text) or runs[k] != runs[lo]:
            _mask_math(text, lo, k, math)
            lo = k
    return Projection(text, starts, ends, runs, math, script_comment)


# Every "<" a browser may read as the start of markup, from its data state: a
# start or end tag name, "<!" (comment, doctype, CDATA), "<?" and "</" before a
# non-letter (both bogus comments). Deliberately independent of project_html:
# a check that shared its scanner would share its mistakes. It does not parse
# attributes, so attribute values are never compared.
_MARKUP = re.compile(r"<(/?[A-Za-z][^\t\n\f\r />]*|[!?/])")


def markup_tokens(source: str) -> list[str]:
    """The sequence of markup openings in `source`, names lower-cased. Linear."""
    return [m.group(1).lower() for m in _MARKUP.finditer(source)]


def same_markup(old: str, new: str) -> bool:
    """True when `new` opens exactly the same tags, in order, as `old`."""
    return markup_tokens(old) == markup_tokens(new)
