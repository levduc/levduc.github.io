#!/usr/bin/env python3
"""Browser tests for the inline-suggest front end, in Playwright Firefox.

Serves the built site, drives real selections, and checks the generated issue
URL, the annotation rendering, the XSS path and the phone layout.

Usage:  python3 browser_test.py <path-to-public> [--shots <dir>] [--inject]

--inject copies the public dir to a temp dir and runs inject_suggest.inject on
the copy (repo levduc/suggest-lab, base "/"), so the static pages carry the layer.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import json
import socket
import shutil
import socketserver
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent.parent
sys.path.insert(0, str(SCRIPTS))
import apply_suggestion  # noqa: E402  (its clean() is what a stored note went through)
POST = "/posts/suggest-an-edit/"
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> bool:
    RESULTS.append((bool(ok), label))
    print(("PASS  " if ok else "FAIL  ") + label)
    return bool(ok)


@contextlib.contextmanager
def serve(root: Path):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))

    class Quiet(socketserver.TCPServer):
        allow_reuse_address = True

        def handle_error(self, request, client_address):  # keep output clean
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = Quiet(("127.0.0.1", port), handler)
    httpd.RequestHandlerClass.log_message = lambda *a, **k: None
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


SELECT_JS = """
(needle) => {
  const root = document.querySelector('[data-suggest-source]');
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n, acc = '', nodes = [], offs = [];
  while ((n = walk.nextNode())) {
    for (let i = 0; i < n.nodeValue.length; i++) { nodes.push(n); offs.push(i); }
    acc += n.nodeValue;
  }
  const flat = s => s.replace(/\\s+/g, ' ');
  // Build a whitespace-collapsed projection with a map back, so a needle typed
  // with single spaces finds text the source wrapped across lines.
  let proj = '', back = [], pending = false;
  for (let i = 0; i < acc.length; i++) {
    const c = acc[i];
    if (/\\s/.test(c)) { pending = true; continue; }
    if (pending && proj.length) { proj += ' '; back.push(i); }
    pending = false;
    proj += c; back.push(i);
  }
  back.push(acc.length);
  const k = proj.indexOf(flat(needle).trim());
  if (k === -1) return null;
  const start = back[k], end = back[k + flat(needle).trim().length];
  const r = document.createRange();
  r.setStart(nodes[start], offs[start]);
  r.setEnd(nodes[end - 1], offs[end - 1] + 1);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  document.dispatchEvent(new Event('selectionchange'));
  return sel.toString();
}
"""


# Selecting on the body-rooted static pages. The text root is document.body,
# minus what textIndex skips: our own UI, script, style, KaTeX's hidden TeX copy
# and its zero-width struts (.vlist-s), and SVG.
SELECT_BODY_JS = """
(needle) => {
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode: n => (n.nodeValue && n.parentElement &&
      !n.parentElement.closest('.isg-ui, script, style, .katex-mathml, .vlist-s, svg'))
      ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT });
  let n, acc = '', nodes = [], offs = [];
  while ((n = walk.nextNode())) {
    for (let i = 0; i < n.nodeValue.length; i++) { nodes.push(n); offs.push(i); }
    acc += n.nodeValue;
  }
  const k = acc.indexOf(needle);
  if (k === -1) return null;
  const end = k + needle.length;
  const r = document.createRange();
  r.setStart(nodes[k], offs[k]);
  r.setEnd(nodes[end - 1], offs[end - 1] + 1);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  document.dispatchEvent(new Event('selectionchange'));
  return needle;
}
"""

# Select from the start of the first element matching `host` (that contains
# `host_text`) up to the end of its `nth` (default first) `upto` descendant.
# Returns the visible text of that span (hidden .katex-mathml excluded) so a
# test can compare.
SELECT_UPTO_JS = """
(a) => {
  const host = Array.from(document.querySelectorAll(a.host))
    .find(e => e.textContent.includes(a.hostText));
  if (!host) return null;
  const last = host.querySelectorAll(a.upto)[a.nth || 0];
  if (!last) return null;
  const r = document.createRange();
  r.setStart(host, 0);
  r.setEndAfter(last);
  const w = document.createTreeWalker(host, NodeFilter.SHOW_TEXT, {
    acceptNode: n => n.parentElement.closest('.katex-mathml')
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  let n, vis = '';
  while ((n = w.nextNode())) {
    if (!r.intersectsNode(n)) continue;
    vis += n.nodeValue.slice(n === r.startContainer ? r.startOffset : 0,
                             n === r.endContainer ? r.endOffset : undefined);
  }
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  document.dispatchEvent(new Event('selectionchange'));
  return vis.trim();
}
"""

# Select the whole content of the first element matching a CSS selector.
SELECT_EL_JS = """
(css) => {
  const e = document.querySelector(css);
  if (!e) return null;
  const r = document.createRange();
  r.selectNodeContents(e);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  document.dispatchEvent(new Event('selectionchange'));
  return sel.toString();
}
"""


def open_dialog(page, js, arg, wait_ms=1500):
    """Run a selection script, click the bar, return True if the dialog opened."""
    got = page.evaluate(js, arg)
    if got is None:
        return False
    try:
        page.locator(".isg-bar .isg-btn").wait_for(state="visible", timeout=wait_ms)
        page.locator(".isg-bar .isg-btn").click()
        page.locator("dialog.isg-dialog").wait_for(state="visible", timeout=wait_ms)
    except Exception:
        return False
    return True


# Contrast of each element's text against the nearest opaque background behind
# it, as WCAG defines it. Returns {selector: ratio or null if absent}.
CONTRAST_JS = """
(sels) => {
  const rgb = s => (s.match(/[\\d.]+/g) || []).map(Number);
  const lum = c => {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  };
  const bg = e => {
    for (; e; e = e.parentElement) {
      const c = rgb(getComputedStyle(e).backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] > 0)) return c;
    }
    return [255, 255, 255];
  };
  const out = {};
  for (const s of sels) {
    const e = document.querySelector(s);
    if (!e) { out[s] = null; continue; }
    const a = lum(rgb(getComputedStyle(e).color)), b = lum(bg(e));
    out[s] = Math.round(100 * (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) / 100;
  }
  return out;
}
"""

# The suggest UI's main text, which must stay readable on any host theme.
UI_TEXT = ["dialog.isg-dialog", ".isg-form textarea", ".isg-quote", "a.isg-primary"]
DRAWER_TEXT = [".isg-drawer", ".isg-drawer-head strong", ".isg-drawer .isg-panel-comment",
               ".isg-drawer .isg-panel-meta a", ".isg-pill"]


def issue_query(page) -> dict:
    href = page.locator("a.isg-primary").get_attribute("href") or ""
    return urllib.parse.parse_qs(urllib.parse.urlparse(href).query)


def run(public: Path, shots: Path | None, plain: Path | None = None) -> int:
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    # A notes file with a hostile payload, to prove rendering is inert.
    xss = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>'
    notes = [
        {
            "id": "s1",
            "exact": "That is the whole interface, and this page explains where a suggestion goes\nafter you press it.",
            "prefix": "",
            "suffix": "",
            "comment": "A real reader note. " + xss,
            "author": "octocat",
            "issue": 1,
            "created_at": "2026-11-03T00:00:00Z",
        },
        {
            "id": "s2",
            "exact": "this sentence is not in the post any more",
            "prefix": "",
            "suffix": "",
            "comment": "An orphaned note.",
            "author": "octocat",
            "issue": 2,
            "created_at": "2026-11-03T00:00:00Z",
        },
    ]
    ann_dir = public / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    (ann_dir / "posts--suggest-an-edit.json").write_text(
        json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def note(i, exact, comment, **kw):
        d = {"id": f"n{i}", "exact": exact, "prefix": "", "suffix": "", "comment": comment,
             "author": "octocat", "about": "", "source": "", "issue": i,
             "created_at": "2026-11-04T00:00:00Z"}
        d.update(kw)
        return d

    NOTE_A = "Each transaction below hides the coin it really spent among decoys."
    NOTE_B = "A privacy coin tells you your payment is hidden among 16."
    DECK_NOTE = "Authenticated encryption, every step"
    fixtures = {
        "pdag.json": [
            note(11, NOTE_A, "Plain note with credentials.",
                 about="Jane Doe, ETH", source="https://doi.org/10.1/x"),
            note(12, NOTE_B, "Note with a hostile source.", about="Anon",
                 source="javascript:alert(1)"),
        ],
        "learning--lattice.json": [note(13, "this sentence is not on the lattice page", "Orphan.")],
        "learning--aes-gcm--talk.json": [note(14, DECK_NOTE, "A deck note.")],
    }
    for name, data in fixtures.items():
        (ann_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with serve(public) as base, sync_playwright() as p:
        browser = p.firefox.launch()

        # ---------------------------------------------------- desktop viewport
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + POST, wait_until="load")
        page.wait_for_timeout(600)

        # 1. selection offers the button
        sentence = "A small suggest an edit button appears."
        got = page.evaluate(SELECT_JS, sentence)
        check(got is not None, "test 1a: the target sentence exists in the rendered post")
        page.wait_for_timeout(300)
        bar = page.locator(".isg-bar")
        check(bar.count() == 1 and bar.is_visible(), "test 1b: selecting text shows the suggest button")
        if shots:
            page.screenshot(path=str(shots / "01-button.png"))

        # 2. the dialog builds a correct prefilled issue URL
        page.locator(".isg-bar .isg-btn").click()
        page.wait_for_timeout(200)
        check(page.locator("dialog.isg-dialog").count() == 1, "test 1c: the dialog opens")
        page.locator(".isg-form textarea").first.fill("The button label is wrong.")
        page.locator(".isg-form textarea").nth(1).fill("A small **suggest** button appears.")
        page.wait_for_timeout(200)
        href = page.locator("a.isg-primary").get_attribute("href")
        if shots:
            page.screenshot(path=str(shots / "02-dialog.png"))

        q = urllib.parse.parse_qs(urllib.parse.urlparse(href or "").query)
        check((href or "").startswith("https://github.com/levduc/"),
              "test 1d: the link points at the configured repo's new-issue form")
        check(q.get("template") == ["suggest.yml"], "test 1e: issue form template is selected")
        check(q.get("file") == ["content/posts/suggest-an-edit.md"],
              "test 1f: file field carries the Markdown source path")
        check(q.get("exact", [""])[0].strip() == sentence.strip(),
              f"test 1g: exact field is the selected sentence (got {q.get('exact', [''])[0]!r})")
        check(len(q.get("prefix", [""])[0]) > 0 and len(q.get("suffix", [""])[0]) > 0,
              "test 1h: prefix and suffix context were captured")
        check(q.get("comment") == ["The button label is wrong."], "test 1i: comment is carried")
        check(q.get("replacement") == ["A small **suggest** button appears."],
              "test 1j: replacement is carried")
        check(q.get("labels") == ["suggestion"], "test 1k: the suggestion label is requested")
        page.keyboard.press("Escape")

        # 3. merged notes render, and are inert
        marks = page.locator("mark.isg-mark")
        check(marks.count() >= 1, f"test 3a: a merged note is highlighted (marks={marks.count()})")
        marks.first.click()
        page.wait_for_timeout(200)
        panel = page.locator(".isg-panel")
        check(panel.count() == 1, "test 3b: clicking a highlight opens the note panel")
        shown = panel.locator(".isg-panel-comment").inner_text()
        check(xss in shown, "test 3c: the note's text is shown verbatim as text")
        check(page.evaluate("() => window.__pwned === undefined"),
              "test 5a: script and onerror payloads in a note do NOT execute")
        check(page.locator(".isg-panel-comment script, .isg-panel-comment img").count() == 0,
              "test 5b: no element was created from the payload")
        check(panel.locator("a[href*='/issues/1']").count() == 1,
              "test 3d: the panel links back to the issue")
        check(panel.locator("a[href*='#:~:text=']").count() == 1,
              "test 3e: the panel offers a text-fragment deep link")
        if shots:
            page.screenshot(path=str(shots / "03-note.png"))

        # orphaned note is disclosed rather than dropped
        check(page.locator(".isg-orphan").count() == 1,
              "test 3f: a note that no longer matches is disclosed on the page")

        # 4. an over-long selection is refused with an explanation
        page.reload(wait_until="load")
        page.wait_for_timeout(500)
        long_sentence = (
            "The page captures three things: the sentence you selected, about forty "
            "characters either side of it, and the path of the source file this page was "
            "built from. Together they are a text-quote selector — a way of pointing at "
            "a passage that survives the text moving around, because it describes the passage "
            "by its content rather than by its position."
        )
        got = page.evaluate(SELECT_JS, long_sentence)
        page.wait_for_timeout(300)
        if got and page.locator(".isg-bar .isg-btn").count():
            page.locator(".isg-bar .isg-btn").click()
            page.wait_for_timeout(200)
            warn = page.locator(".isg-dialog .isg-warn").first
            check(warn.count() >= 1 and "300" in warn.inner_text(),
                  "test 4a: an over-long selection is refused with the limit explained")
            check(page.locator("a.isg-primary[aria-disabled='true']").count() == 1,
                  "test 4b: the GitHub link is disabled for an over-long selection")
            if shots:
                page.screenshot(path=str(shots / "04-toolong.png"))
            page.keyboard.press("Escape")
        else:
            check(False, f"test 4: could not select the long passage (len={len(long_sentence)})")

        check(not errors, f"test 7a: no console or page errors on desktop ({errors[:3]})")
        ctx.close()

        # ------------------------------------------------------ phone viewport
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            has_touch=True, is_mobile=True,
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
        )
        page = ctx.new_page()
        merrors: list[str] = []
        page.on("console", lambda m: merrors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: merrors.append(str(e)))
        page.goto(base + POST, wait_until="load")
        page.wait_for_timeout(600)

        page.evaluate(SELECT_JS, sentence)
        page.wait_for_timeout(500)
        bar = page.locator(".isg-bar")
        check(bar.count() == 1, "test 6a: the suggest affordance appears at phone width")
        klass = bar.get_attribute("class") or ""
        check("isg-bar-fixed" in klass,
              "test 6b: on touch it is a viewport-pinned bar, not a floating button next to the selection")
        box = bar.bounding_box() or {}
        check(abs((box.get("y", 0) + box.get("height", 0)) - 844) < 2,
              f"test 6c: the bar sits flush with the bottom edge, clear of the native menu (box={box})")
        sel_box = page.evaluate(
            "() => { const r = window.getSelection().getRangeAt(0).getBoundingClientRect();"
            "return {top: r.top, bottom: r.bottom}; }"
        )
        check(sel_box["bottom"] < box.get("y", 0),
              f"test 6d: the bar does not overlap the selection ({sel_box} vs {box})")
        scroll = page.evaluate(
            "() => ({doc: document.documentElement.scrollWidth, win: window.innerWidth})"
        )
        check(scroll["doc"] <= scroll["win"] + 1,
              f"test 6e: no horizontal page scroll at 390px ({scroll})")
        if shots:
            page.screenshot(path=str(shots / "05-phone.png"), full_page=False)

        page.locator(".isg-bar .isg-btn").click()
        page.wait_for_timeout(300)
        dlg = page.locator("dialog.isg-dialog").bounding_box() or {}
        check(dlg.get("width", 999) <= 390,
              f"test 6f: the dialog fits the phone viewport ({dlg})")
        if shots:
            page.screenshot(path=str(shots / "06-phone-dialog.png"))
        check(not merrors, f"test 7b: no console or page errors at phone width ({merrors[:3]})")
        ctx.close()

        # ------------------------------------- the rest of the site still works
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        herrors: list[str] = []
        page.on("pageerror", lambda e: herrors.append(str(e)))
        page.goto(base + "/", wait_until="load")
        page.wait_for_timeout(400)
        check(page.locator(".isg-bar, .isg-dialog, mark.isg-mark").count() == 0,
              "test 7c: the suggestion layer does not load on the homepage")
        page.evaluate("() => { const r=document.createRange(); r.selectNodeContents(document.body);"
                      "const s=window.getSelection(); s.removeAllRanges(); s.addRange(r); }")
        page.wait_for_timeout(300)
        check(page.locator(".isg-bar").count() == 0,
              "test 7d: selecting text on the homepage offers nothing")
        check(page.locator("a[href='/posts/']").count() == 1,
              "test 7e: the nav links to the notes index")
        page.goto(base + "/learning/aes-gcm/talk/", wait_until="load")
        page.wait_for_timeout(600)
        check(page.locator("section.slide").count() > 30,
              "test 7f: the AES-GCM deck still renders more than 30 slides with the layer loaded")
        check(page.locator("script[src$='js/inline-suggest.js']").count() == 1,
              "test 7f2: the suggestion layer is loaded on the deck")
        check(not herrors, f"test 7g: no page errors on homepage or deck ({herrors[:3]})")
        ctx.close()

        # ------------------------------------------- static pages (Task 8)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        serrors: list[str] = []
        page.on("pageerror", lambda e: serrors.append(str(e)))
        page.goto(base + "/pdag/", wait_until="load")
        try:
            page.locator(".katex").first.wait_for(state="attached", timeout=8000)
        except Exception:
            print("NOTE  KaTeX did not render on /pdag/ (CDN unreachable?)")
        page.wait_for_timeout(500)

        plain = "Expressions are copied as printed in the paper, including Lemma 5's denominator."
        opened = open_dialog(page, SELECT_BODY_JS, plain)
        check(opened, "test 8a1: selecting a plain sentence on /pdag/ shows the bar and opens the dialog")
        if opened:
            page.locator(".isg-form textarea").nth(1).fill("A replacement.")
            page.wait_for_timeout(100)
            q = issue_query(page)
            check(q.get("file") == ["static/pdag/index.html"] and q.get("template") == ["suggest.yml"],
                  f"test 8a: issue URL carries file=static/pdag/index.html and template=suggest.yml ({q.get('file')})")
            href = page.locator("a.isg-primary").get_attribute("href") or ""
            check(href.startswith("https://github.com/levduc/suggest-lab/issues/new?"),
                  "test 8b: empty comment plus a replacement still gives an open-on-GitHub link")
            check(q.get("exact", [""])[0] == plain,
                  f"test 8a2: exact is the selected sentence ({q.get('exact', [''])[0]!r})")
            page.keyboard.press("Escape")
        else:
            check(False, "test 8a: no dialog, so no URL")
            check(False, "test 8b: no dialog, so no link")

        page.reload(wait_until="load")
        try:
            page.locator(".katex").first.wait_for(state="attached", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(500)
        page.evaluate("() => window.getSelection().removeAllRanges()")
        spec = {"host": "p", "hostText": "Address reuse has no equation number", "upto": ".katex"}
        have_katex = page.locator(".katex").count() > 0
        got = page.evaluate(SELECT_UPTO_JS, spec) if have_katex else None
        ok8c = False
        detail = "no .katex element rendered (KaTeX CDN unreachable?)" if not have_katex else "selection failed"
        if got is not None:
            try:
                page.locator(".isg-bar .isg-btn").wait_for(state="visible", timeout=1500)
                page.locator(".isg-bar .isg-btn").click()
                page.locator("dialog.isg-dialog").wait_for(state="visible", timeout=1500)
                page.locator(".isg-form textarea").first.fill("x")
                exact = issue_query(page).get("exact", [""])[0]
                ok8c = (not any(t in exact for t in ("\\", "^{", "_{"))
                        and 0 < len(exact) <= len(got) + 2)
                detail = f"exact={exact!r} visible={got!r}"
                page.keyboard.press("Escape")
            except Exception as e:
                detail = f"dialog did not open: {e.__class__.__name__}"
        check(ok8c, f"test 8c: a selection over a .katex element has no TeX source in exact ({detail})")

        page.reload(wait_until="load")
        page.wait_for_timeout(500)
        spec = {"host": "p", "hostText": "Graph reproduced from", "upto": "code"}
        got = page.evaluate(SELECT_UPTO_JS, spec)
        ok8f = False
        detail = "selection failed"
        if got is not None:
            try:
                page.locator(".isg-bar .isg-btn").wait_for(state="visible", timeout=1500)
                page.locator(".isg-bar .isg-btn").click()
                page.locator("dialog.isg-dialog").wait_for(state="visible", timeout=1500)
                page.locator(".isg-form textarea").first.fill("x")
                exact = issue_query(page).get("exact", [""])[0]
                ok8f = "figures/pdag_example.tex" in exact
                detail = f"exact={exact!r}"
                page.keyboard.press("Escape")
            except Exception as e:
                detail = f"dialog did not open: {e.__class__.__name__}"
        check(ok8f, f"test 8f: a selection spanning inline <code> keeps the code text ({detail})")

        # 8g: length limits and counters
        page.reload(wait_until="load")
        page.wait_for_timeout(500)
        if open_dialog(page, SELECT_BODY_JS, plain):
            ta = page.locator(".isg-form textarea")
            ta.first.press_sequentially("a" * 900)
            ta.nth(1).press_sequentially("b" * 500)
            c_len = ta.first.evaluate("e => e.value.length")
            r_len = ta.nth(1).evaluate("e => e.value.length")
            counts = page.locator(".isg-count").all_inner_texts()
            check(c_len == 800 and (counts or [""])[0] == "800 / 800",
                  f"test 8g: comment stops at 800 and its counter reads 800 / 800 ({c_len}, {counts})")
            check(r_len == 400, f"test 8g2: replacement stops at 400 ({r_len}, {counts})")
            page.keyboard.press("Escape")
        else:
            check(False, "test 8g: dialog did not open")
            check(False, "test 8g2: dialog did not open")
        check(not serrors, f"test 8h: no page errors on /pdag/ ({serrors[:3]})")
        ctx.close()

        # 8d: a deck keeps its keys to itself while the dialog is open
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(base + "/learning/aes-gcm/talk/", wait_until="load")
        page.wait_for_timeout(800)
        which = "() => Array.from(document.querySelectorAll('section.slide')).findIndex(s => s.classList.contains('on'))"
        s0 = page.evaluate(which)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(300)
        s1 = page.evaluate(which)
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(300)
        s2 = page.evaluate(which)
        opened = open_dialog(page, SELECT_EL_JS, "section.slide.on p")
        leaked = -1
        if opened:
            # The deck ignores textareas itself, so also prove no page listener
            # on document or window sees keys typed inside the dialog.
            page.evaluate("() => { window.__keys = 0; "
                          "document.addEventListener('keydown', () => window.__keys++); "
                          "window.addEventListener('keydown', () => window.__keys++); }")
            page.locator(".isg-form textarea").first.focus()
            page.keyboard.press(" ")
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(300)
        s3 = page.evaluate(which)
        if opened:
            leaked = page.evaluate("() => window.__keys")
        check(leaked == 0, f"test 8d2: no document or window keydown listener sees keys typed in the dialog (saw {leaked})")
        check(opened and s1 != s0 and s2 == s0 and s3 == s0,
              f"test 8d: typing space and ArrowRight in the dialog does not move the deck "
              f"(opened={opened}, slides {s0}->{s1}->{s2}->{s3})")
        ctx.close()

        # 8e: the biggest page
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        lerrors: list[str] = []
        page.on("pageerror", lambda e: lerrors.append(str(e)))
        page.goto(base + "/learning/lattice/", wait_until="load")
        page.wait_for_timeout(1500)
        # Time inside the page, from the selection to the bar being inserted.
        # Playwright's own visibility wait also counts paint time of this
        # 367 KB page, which is not our code's latency.
        ms = page.evaluate("""() => new Promise(res => {
          const e = document.querySelector('p.sub');
          if (!e) return res(null);
          const t0 = performance.now();
          const done = () => { mo.disconnect(); res(performance.now() - t0); };
          const mo = new MutationObserver(() => { if (document.querySelector('.isg-bar')) done(); });
          mo.observe(document.body, {childList: true});
          setTimeout(() => { mo.disconnect(); res(-1); }, 3000);
          const r = document.createRange();
          r.selectNodeContents(e);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(r);
          document.dispatchEvent(new Event('selectionchange'));
        })""")
        check(ms is not None and 0 <= ms <= 500 and not lerrors,
              f"test 8e: on /learning/lattice/ the bar appears within 500 ms ({ms} ms), no page error ({lerrors[:3]})")
        ctx.close()

        # ------------------------------------------ drawer and pill (Task 9)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        perr: list[str] = []
        page.on("pageerror", lambda e: perr.append(str(e)))
        page.goto(base + "/pdag/", wait_until="load")
        page.wait_for_timeout(1500)
        check(page.locator("mark.isg-mark").count() >= 1, "test 9a1: a note on a plain sentence is highlighted on /pdag/")
        if page.locator("mark.isg-mark").count():
            page.locator("mark.isg-mark").first.click()
        page.wait_for_timeout(300)
        check(page.locator(".isg-drawer").count() == 1 and page.locator(".isg-drawer").is_visible(), "test 9a2: clicking the highlight opens .isg-drawer")
        stray = page.evaluate("() => Array.from(document.querySelectorAll('.isg-panel'))"
                              ".filter(p => !p.closest('.isg-drawer')).length")
        inmain = page.evaluate("() => document.querySelectorAll('main .isg-panel, body > *:not(.isg-ui) .isg-panel').length")
        check(stray == 0 and inmain == 0,
              f"test 9a3: no .isg-panel is inserted into page content (stray={stray}, in content={inmain})")

        drawer = page.locator(".isg-drawer")
        shown = drawer.inner_text() if drawer.count() else ""
        check("Jane Doe, ETH" in shown and "self-described" in shown,
              "test 9b1: about text and the self-described label are shown")
        check(drawer.locator("a[href='https://doi.org/10.1/x'][rel~='ugc']").count() == 1
              and "nofollow" in (drawer.locator("a[href='https://doi.org/10.1/x']").get_attribute("rel") or ""),
              "test 9b2: an https source is a link with rel ugc nofollow")
        bad = page.evaluate("() => Array.from(document.querySelectorAll('a[href]'))"
                            ".filter(a => /^javascript:/i.test(a.getAttribute('href'))).length")
        check(bad == 0 and "javascript:alert(1)" in shown,
              f"test 9b3: a javascript: source is plain text, not a link ({bad} such links)")
        check(page.evaluate("() => window.__pwned === undefined"), "test 9b4: window.__pwned stays undefined")
        check(not perr, f"test 9a4: no page errors on /pdag/ ({perr[:3]})")
        ctx.close()

        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(base + "/learning/aes-gcm/talk/", wait_until="load")
        page.wait_for_timeout(1200)
        which = "() => Array.from(document.querySelectorAll('section.slide')).findIndex(s => s.classList.contains('on'))"
        n0, c0 = page.locator("section.slide").count(), page.evaluate(which)
        check(page.locator("mark.isg-mark").count() >= 1, "test 9c1: the deck note is highlighted")
        if page.locator("mark.isg-mark").count():
            page.locator("mark.isg-mark").first.click()
        page.wait_for_timeout(300)
        n1, c1 = page.locator("section.slide").count(), page.evaluate(which)
        check(page.locator(".isg-drawer").count() == 1 and page.locator(".isg-drawer").is_visible() and n0 == n1 and c0 == c1,
              f"test 9c: on the deck a highlight opens the drawer; slides {n0}->{n1}, current {c0}->{c1}")
        top = (page.locator(".isg-pill").bounding_box() if page.locator(".isg-pill").count() else None) or {}
        check(top.get("y", 999) < 100, f"test 9c2: on a deck the pill sits at the top ({top})")
        ctx.close()

        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        derr: list[str] = []
        page.on("pageerror", lambda e: derr.append(str(e)))
        page.goto(base + "/learning/lattice/", wait_until="load")
        page.wait_for_timeout(1500)
        if page.locator(".isg-pill").count():
            page.locator(".isg-pill").click()
        page.wait_for_timeout(300)
        dtext = page.locator(".isg-drawer").inner_text() if page.locator(".isg-drawer").count() else ""
        check("1 reader note could not be placed" in dtext and not derr,
              f"test 9d: an unplaceable note is counted in the drawer, no pageerror ({dtext[:80]!r}, {derr[:2]})")
        ctx.close()

        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(base + "/pdag/", wait_until="load")
        page.wait_for_timeout(800)
        pill = page.locator(".isg-pill")
        pbox = pill.bounding_box() or {}
        check(pill.count() == 1 and pill.is_visible() and "note" in pill.inner_text()
              and pbox.get("y", 0) > 450,
              f"test 9e1: the pill is visible, bottom-right, on /pdag/ ({pill.all_inner_texts()})")
        page.goto(base + "/", wait_until="load")
        page.wait_for_timeout(400)
        n_home = page.locator(".isg-pill").count()
        page.goto(base + POST, wait_until="load")
        page.wait_for_timeout(600)
        n_post = page.locator(".isg-pill").count()
        check(n_home == 0 and n_post == 0, f"test 9e2: no pill on / or the post ({n_home}, {n_post})")
        page.goto(base + "/pdag/", wait_until="load")
        page.wait_for_timeout(800)
        page.evaluate("() => window.getSelection().removeAllRanges()")
        if page.locator(".isg-pill").count():
            page.locator(".isg-pill").click()
        page.wait_for_timeout(300)
        dt = page.locator(".isg-drawer").inner_text() if page.locator(".isg-drawer").count() else ""
        check("Select any text on this page to comment or suggest a rewrite." in dt
              and page.locator(".isg-drawer a:has-text('How this works')").count() == 1,
              "test 9e3: the pill opens the drawer with the hint and the help link")
        ctx.close()

        ctx = browser.new_context(
            viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True,
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"))
        page = ctx.new_page()
        page.goto(base + "/pdag/", wait_until="load")
        page.wait_for_timeout(1500)
        page.evaluate(SELECT_BODY_JS, "Expressions are copied as printed in the paper")
        page.wait_for_timeout(600)
        barb = page.locator(".isg-bar-fixed").bounding_box()
        pillb = page.locator(".isg-pill").bounding_box()
        clear = bool(barb and pillb and (pillb["y"] + pillb["height"] <= barb["y"]
                     or pillb["y"] >= barb["y"] + barb["height"]
                     or pillb["x"] + pillb["width"] <= barb["x"]
                     or pillb["x"] >= barb["x"] + barb["width"]))
        sw = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
        check(clear and sw[0] <= sw[1] + 1,
              f"test 9f: at 390px the pill clears the selection bar and nothing scrolls sideways ({pillb} vs {barb}, {sw})")
        ctx.close()

        # ------------------------------------------- final-review fixes
        pdag_notes = ann_dir / "pdag.json"
        pdag_fixture = pdag_notes.read_text(encoding="utf-8")

        def fresh_notes(c):
            # These checks rewrite pdag.json mid-run. Routing turns off
            # Playwright's HTTP cache, so a reload fetches the new file rather
            # than a heuristically fresh copy of the fixture.
            c.route("**/annotations/*.json", lambda route: route.continue_())

        def katex_ready(pg):
            try:
                pg.locator(".katex .vlist-s").first.wait_for(state="attached", timeout=8000)
                return True
            except Exception:
                return False

        # I1: a note over KaTeX, stored as the Action stores it, re-highlights.
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        fresh_notes(ctx)
        page = ctx.new_page()
        page.goto(base + "/pdag/", wait_until="load")
        host = "between them they used up"
        ok, detail = False, "no .katex .vlist-s rendered on /pdag/ (KaTeX CDN unreachable?)"
        if katex_ready(page):
            page.wait_for_timeout(500)
            page.evaluate("() => window.getSelection().removeAllRanges()")
            spec = {"host": "p", "hostText": host, "upto": ".katex", "nth": 1}
            got = page.evaluate(SELECT_UPTO_JS, spec)
            detail = "selection failed"
            if got is not None:
                try:
                    page.locator(".isg-bar .isg-btn").wait_for(state="visible", timeout=1500)
                    page.locator(".isg-bar .isg-btn").click()
                    page.locator("dialog.isg-dialog").wait_for(state="visible", timeout=1500)
                    page.locator(".isg-form textarea").first.fill("A note on the maths.")
                    q = issue_query(page)
                    sel = {k: q.get(k, [""])[0] for k in ("exact", "prefix", "suffix")}
                    stored = {k: apply_suggestion.clean(v, allow_newlines=True) for k, v in sel.items()}
                    data = json.loads(pdag_fixture) + [note(21, stored["exact"], "A note on the maths.",
                                                            prefix=stored["prefix"], suffix=stored["suffix"])]
                    pdag_notes.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    page.reload(wait_until="load")
                    katex_ready(page)
                    page.wait_for_timeout(1200)
                    n = page.evaluate("(h) => { const p = [...document.querySelectorAll('p')]"
                                      ".find(e => e.textContent.includes(h)); "
                                      "return p ? p.querySelectorAll('mark.isg-mark').length : -1; }", host)
                    inside = page.evaluate("(h) => { const p = [...document.querySelectorAll('p')]"
                                           ".find(e => e.textContent.includes(h)); "
                                           "return p ? p.querySelectorAll('.katex mark.isg-mark').length : -1; }", host)
                    pill_text = page.locator(".isg-pill").inner_text()
                    ok = n > 0 and inside > 0 and pill_text.startswith("3 notes")
                    detail = (f"exact={stored['exact']!r}, marks in the paragraph={n}, "
                              f"inside .katex={inside}, pill={pill_text!r}")
                except Exception as e:
                    detail = f"dialog did not open: {e.__class__.__name__}"
                finally:
                    pdag_notes.write_text(pdag_fixture, encoding="utf-8")
        check(ok, f"test 10a: a note over KaTeX, cleaned as the Action stores it, re-highlights after reload ({detail})")
        ctx.close()

        # I2: a note on an SVG chart label leaves the label drawn.
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        fresh_notes(ctx)
        page = ctx.new_page()
        page.goto(base + "/pdag/", wait_until="load")
        page.wait_for_timeout(1200)
        label = "(B) 16/21"
        width_js = ("(s) => { const t = [...document.querySelectorAll('svg text')]"
                    ".find(t => t.textContent.trim() === s); return t ? t.getBBox().width : null; }")
        w0 = page.evaluate(width_js, label)
        try:
            data = json.loads(pdag_fixture) + [note(22, label, "This label is wrong.")]
            pdag_notes.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            page.reload(wait_until="load")
            page.wait_for_timeout(1200)
            w1 = page.evaluate(width_js, label)
            in_svg = page.evaluate("() => document.querySelectorAll('svg mark').length")
            loaded = page.locator(".isg-pill").inner_text()
        finally:
            pdag_notes.write_text(pdag_fixture, encoding="utf-8")
        check(bool(w0) and w1 is not None and abs(w1 - w0) < 0.5 and in_svg == 0
              and loaded.startswith("3 notes"),
              f"test 10b: a note on an SVG label leaves it drawn (width {w0} -> {w1}), no mark inside "
              f"any svg ({in_svg}); the note was loaded (pill {loaded!r})")
        ctx.close()

        # I3: the suggest UI stays readable on dark pages and dark decks.
        def open_ui(pg, css):
            ok = open_dialog(pg, SELECT_EL_JS, css)
            dlg = pg.evaluate(CONTRAST_JS, UI_TEXT) if ok else {}
            if ok:
                pg.keyboard.press("Escape")
                pg.wait_for_timeout(200)
            pg.evaluate("() => window.getSelection().removeAllRanges()")
            if pg.locator(".isg-pill").count():
                pg.locator(".isg-pill").click()
                pg.wait_for_timeout(300)
            drw = pg.evaluate(CONTRAST_JS, DRAWER_TEXT)
            return ok, dlg, drw

        def readable(d, required):
            return all(d.get(k) is not None and d[k] >= 4.5 for k in required) and all(
                v is None or v >= 4.5 for v in d.values())

        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark")
        page = ctx.new_page()
        page.goto(base + "/fastlane/", wait_until="load")
        page.wait_for_timeout(1200)
        dark = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        opened, dlg, drw = open_ui(page, "main p")
        check(opened and readable(dlg, UI_TEXT),
              f"test 10c: on /fastlane/ in dark mode (body {dark}) the dialog's text has contrast >= 4.5 ({dlg})")
        check(readable(drw, [".isg-drawer", ".isg-drawer-head strong", ".isg-pill"]),
              f"test 10c2: ... and so do the drawer and the pill ({drw})")
        ctx.close()

        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(base + "/learning/aes-gcm/talk/", wait_until="load")
        page.wait_for_timeout(1200)
        toggled = False
        if page.locator("#b-theme").count():
            page.locator("#b-theme").click()
            page.wait_for_timeout(300)
            toggled = page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "dark"
        opened, dlg, drw = open_ui(page, "section.slide.on p")
        check(toggled and opened and readable(dlg, UI_TEXT),
              f"test 10d: on the AES-GCM deck after its theme toggle the dialog's text has contrast >= 4.5 "
              f"(dark={toggled}, {dlg})")
        check(readable(drw, DRAWER_TEXT),
              f"test 10d2: ... and so do the drawer, a note in it and the pill ({drw})")
        ctx.close()

        # Keys on a focused highlight, and Escape in the drawer, on a deck.
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(base + "/learning/aes-gcm/talk/", wait_until="load")
        page.wait_for_timeout(1200)
        which = "() => Array.from(document.querySelectorAll('section.slide')).findIndex(s => s.classList.contains('on'))"
        mark = page.locator("section.slide.on mark.isg-mark").first
        s0, s1, s2, drawn, esc, back = None, None, None, False, False, False
        if mark.count():
            mark.focus()
            s0 = page.evaluate(which)
            page.keyboard.press(" ")
            page.wait_for_timeout(300)
            s1 = page.evaluate(which)
            drawn = page.locator(".isg-drawer").count() == 1
            inside = page.evaluate("() => !!document.activeElement.closest('.isg-drawer')")
            if drawn and not inside:
                page.locator(".isg-drawer .isg-btn").first.focus()
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            esc = page.locator(".isg-drawer").count() == 0
            back = page.evaluate("() => document.activeElement && document.activeElement.matches('mark.isg-mark')")
            s2 = page.evaluate(which)
        check(mark.count() > 0 and drawn and s0 == s1,
              f"test 10e: Space on a focused highlight on a deck opens the drawer and leaves the slide alone "
              f"(slide {s0}->{s1}, drawer={drawn})")
        check(esc and back and s1 == s2 if mark.count() else False,
              f"test 10f: Escape inside the drawer closes it and returns focus to the highlight "
              f"(closed={esc}, focus back={back})")
        ctx.close()

        # 9g: injecting adds no page error on any of the pages
        if plain is None:
            print("NOTE  9g skipped: needs --inject (compares against the plain build)")
        else:
            pages = sorted(f.parent.relative_to(public).as_posix() for f in public.rglob("index.html")
                           if "data-suggest-root=\"body\"" in f.read_text(encoding="utf-8", errors="replace"))

            def errs_for(root: Path) -> dict[str, set[str]]:
                out: dict[str, set[str]] = {}
                with serve(root) as b2:
                    ctx2 = browser.new_context(viewport={"width": 1280, "height": 900})
                    for rel in pages:
                        pg = ctx2.new_page()
                        seen: set[str] = set()
                        pg.on("pageerror", lambda e, seen=seen: seen.add(str(e)))
                        try:
                            pg.goto(b2 + "/" + rel + "/", wait_until="load", timeout=30000)
                            pg.wait_for_timeout(700)
                        except Exception as e:
                            seen.add("NAV " + e.__class__.__name__)
                        out[rel] = seen
                        pg.close()
                    ctx2.close()
                return out

            before, after = errs_for(plain), errs_for(public)
            added = {r: sorted(after[r] - before[r]) for r in pages if after[r] - before[r]}
            check(len(pages) == 24 and not added,
                  f"test 9g: {len(pages)} pages, injection adds no pageerror ({added})")
        browser.close()

    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 70)
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} browser checks passed")
    for label in failed:
        print("  FAILED: " + label)
    return 1 if failed else 0


if __name__ == "__main__":
    pub = Path(sys.argv[1]).resolve()
    shots_dir = None
    if "--shots" in sys.argv:
        shots_dir = Path(sys.argv[sys.argv.index("--shots") + 1]).resolve()
    if "--inject" in sys.argv:
        sys.path.insert(0, str(SCRIPTS))
        import inject_suggest  # noqa: E402

        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "public"
            shutil.copytree(pub, copy)
            inject_suggest.inject(copy, REPO_ROOT / "static", "levduc/suggest-lab", "http://localhost/")  # base path "/"
            raise SystemExit(run(copy, shots_dir, pub))
    raise SystemExit(run(pub, shots_dir))
