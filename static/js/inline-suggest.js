/* inline-suggest — highlight a sentence, end up with a pull request.
 *
 * No framework, no build step, no network calls except one static JSON fetch
 * for the notes already published on this post.
 *
 * Two halves:
 *   1. Selecting text offers "suggest an edit". That captures a W3C-style
 *      text-quote selector (exact text plus a little context either side) and
 *      hands off to a prefilled GitHub issue form. A GitHub Action turns the
 *      issue into a PR; see .github/workflows/suggestion-to-pr.yml.
 *   2. Notes already merged are fetched from static/annotations/<slug>.json and
 *      drawn as highlights with a panel.
 *
 * Reader-supplied text is only ever written with textContent. There is no
 * innerHTML path for anything that came from the network.
 */
(function () {
  'use strict';

  var config = document.querySelector('[data-suggest-source]');
  if (!config) return;

  var SOURCE = config.getAttribute('data-suggest-source') || '';
  var REPO = config.getAttribute('data-suggest-repo') || '';
  var NOTES = config.getAttribute('data-suggest-notes') || '';
  if (!SOURCE || !REPO) return;

  /* Posts put the config on the article element and select inside it. The
   * hand-built static pages carry a hidden config div and select anywhere in
   * the body. */
  var TEXT_ROOT = config.getAttribute('data-suggest-root') === 'body' ? document.body : config;
  var LAYOUT = config.getAttribute('data-suggest-layout') || 'post';   /* post | page | deck */
  var HELP = config.getAttribute('data-suggest-help') || '';

  var MAX_EXACT = 300;    // must match apply_suggestion.py
  var MAX_COMMENT = 800;
  var MAX_REPLACEMENT = 400;
  var CONTEXT = 40;       // chars of prefix/suffix captured for disambiguation
  var MAX_URL = 6000;     // conservative: GitHub rejects request lines near 8k
  var coarse = window.matchMedia('(hover: none), (max-width: 640px)').matches;

  /* ------------------------------------------------------------------ utils */

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;   /* never innerHTML */
    return n;
  }

  function flat(s) { return s.replace(/\s+/g, ' '); }

  /* The post's text as one string, with a map from each character back to the
   * text node and offset it came from. This is what lets us match a sentence
   * that straddles <strong> or <a> boundaries and still build a live Range. */
  function textIndex(container) {
    /* A plain walker with the test inline: a JS acceptNode callback per node
     * is the slow part on a large page. Parents repeat, so the (slow)
     * closest() result is cached per parent element. */
    var skipped = new Map();
    var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    var parts = [], segs = [], first = new Map(), len = 0, n;
    while ((n = walker.nextNode())) {
      var v = n.nodeValue;
      if (!v) continue;
      var p = n.parentElement;
      if (!p) continue;
      var skip = skipped.get(p);
      if (skip === undefined) {
        /* Skip our own UI, scripts, styles and KaTeX's hidden TeX copy. Skip
         * KaTeX's .vlist-s struts too: each is a lone U+200B, which the Action
         * strips from a stored note, so a note spanning one would never match
         * again. Skip svg: a <mark> inside SVG <text> is not drawn, so the
         * label would vanish. Whole nodes are skipped, so offsets stay exact.
         * Not [hidden] (it cannot be selected anyway, and a deck hides the
         * slides that are not current) and not pre/code (a selection across
         * inline code must keep that text). */
        skip = !!p.closest('.isg-ui, script, style, .katex-mathml, .vlist-s, svg');
        skipped.set(p, skip);
      }
      if (skip) continue;
      first.set(n, len);
      parts.push(v);
      segs.push(n);
      len += v.length;
    }
    var idx = { text: parts.join(''), first: first };
    /* nodes[i] / offsets[i] map a character back to its text node and offset.
     * Selecting only needs text and first, so build these per-character arrays
     * on first use: on a large page that keeps the suggest bar quick. */
    var table = null;
    function build() {
      if (table) return table;
      var nodes = [], offsets = [];
      segs.forEach(function (t) {
        for (var i = 0; i < t.nodeValue.length; i++) { nodes.push(t); offsets.push(i); }
      });
      table = { nodes: nodes, offsets: offsets };
      return table;
    }
    Object.defineProperty(idx, 'nodes', { get: function () { return build().nodes; } });
    Object.defineProperty(idx, 'offsets', { get: function () { return build().offsets; } });
    return idx;
  }

  /* Index position of a Range boundary. A text node in the index is a lookup.
   * Anything else (an element container, or a text node the index skips) counts
   * the index characters of its element that lie before the boundary. Returns
   * -1 when that element holds no index characters. */
  function boundaryIndex(idx, node, offset) {
    if (idx.first.has(node)) return idx.first.get(node) + offset;
    var scope = node.nodeType === 1 ? node : node.parentElement;
    if (!scope) return -1;
    var at = document.createRange();
    at.setStart(node, offset);
    at.collapse(true);
    var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    var base = -1, count = 0, t;
    while ((t = walker.nextNode())) {
      if (!idx.first.has(t)) continue;
      if (base < 0) base = idx.first.get(t);
      /* comparePoint: -1 means that point is before the boundary. */
      if (at.comparePoint(t, t.nodeValue.length) < 0) { count += t.nodeValue.length; continue; }
      for (var i = 0; i < t.nodeValue.length && at.comparePoint(t, i) < 0; i++) count++;
      break;
    }
    return base < 0 ? -1 : base + count;
  }

  /* Whitespace-insensitive search over the index. Returns {start, end} in index
   * space, or null. Mirrors the ladder in apply_suggestion.py: try verbatim,
   * then collapse whitespace. Context only disambiguates; it never decides the
   * span. */
  function locate(idx, exact, prefix, suffix) {
    if (!exact) return null;

    function hits(hay, needle) {
      var out = [], k = 0;
      while ((k = hay.indexOf(needle, k)) !== -1) { out.push(k); k += 1; }
      return out;
    }

    function pick(hay, map, needle, pre, suf) {
      var found = hits(hay, needle);
      if (!found.length) return null;
      if (found.length > 1 && (pre || suf)) {
        var narrowed = found.filter(function (s) {
          var before = flat(hay.slice(0, s)).replace(/\s+$/, '');
          var after = flat(hay.slice(s + needle.length)).replace(/^\s+/, '');
          return (!pre || before.endsWith(pre)) && (!suf || after.startsWith(suf));
        });
        if (narrowed.length === 1) found = narrowed;
      }
      if (found.length !== 1) return null;   /* ambiguous: decline */
      return { start: map(found[0]), end: map(found[0] + needle.length) };
    }

    /* Pass 1: verbatim against the live text. */
    var direct = pick(idx.text, function (i) { return i; }, exact,
                      flat(prefix).replace(/\s+$/, ''), flat(suffix).replace(/^\s+/, ''));
    if (direct) return direct;

    /* Pass 2: whitespace-collapsed, keeping a map back to index space. */
    var buf = [], back = [], pending = false;
    for (var i = 0; i < idx.text.length; i++) {
      var c = idx.text[i];
      if (/\s/.test(c)) { pending = true; continue; }
      if (pending && buf.length) { buf.push(' '); back.push(i); }
      pending = false;
      buf.push(c); back.push(i);
    }
    back.push(idx.text.length);
    return pick(buf.join(''), function (i) { return back[i]; },
                flat(exact).trim(), flat(prefix).replace(/\s+$/, ''),
                flat(suffix).replace(/^\s+/, ''));
  }

  function rangeFrom(idx, span) {
    var r = document.createRange();
    r.setStart(idx.nodes[span.start], idx.offsets[span.start]);
    var last = span.end - 1;
    r.setEnd(idx.nodes[last], idx.offsets[last] + 1);
    return r;
  }

  /* ------------------------------------------------- capture a text selector */

  function selector() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    var r = sel.getRangeAt(0);
    if (!TEXT_ROOT.contains(r.commonAncestorContainer)) return null;
    var host = r.commonAncestorContainer.nodeType === 1
      ? r.commonAncestorContainer : r.commonAncestorContainer.parentElement;
    if (host && host.closest('.isg-ui')) return null;

    /* exact is sliced from the index, not taken from sel.toString(): the
     * index leaves out KaTeX's hidden TeX copy, which toString() would include
     * and which would never match the source. Context comes from the same
     * index, so it matches what the source will look like. */
    var idx = textIndex(TEXT_ROOT);
    var a = boundaryIndex(idx, r.startContainer, r.startOffset);
    var b = boundaryIndex(idx, r.endContainer, r.endOffset);
    var exact = '', prefix = '', suffix = '';
    if (a >= 0 && b > a) {
      /* Firefox includes a trailing space when a Range ends at a text-node
       * boundary, so trim before measuring context. */
      while (a < b && /\s/.test(idx.text[a])) a++;
      while (b > a && /\s/.test(idx.text[b - 1])) b--;
      exact = idx.text.slice(a, b);
      prefix = idx.text.slice(Math.max(0, a - CONTEXT), a);
      suffix = idx.text.slice(b, b + CONTEXT);
    }
    if (!exact) {
      /* No index character inside the Range. */
      exact = sel.toString().replace(/^\s+|\s+$/g, '');
      prefix = ''; suffix = '';
    }
    if (!exact) return null;
    return { exact: exact, prefix: prefix, suffix: suffix, rect: r.getBoundingClientRect() };
  }

  /* ------------------------------------------------------------- issue URL */

  function issueURL(sel, comment, replacement) {
    var params = new URLSearchParams();
    params.set('template', 'suggest.yml');
    params.set('labels', 'suggestion');
    params.set('title', 'suggestion: ' + flat(sel.exact).slice(0, 60));
    params.set('comment', comment);
    if (replacement) params.set('replacement', replacement);
    params.set('file', SOURCE);
    params.set('exact', sel.exact);
    params.set('prefix', sel.prefix);
    params.set('suffix', sel.suffix);

    var base = 'https://github.com/' + REPO + '/issues/new?';
    var url = base + params.toString();
    if (url.length <= MAX_URL) return { url: url, trimmed: false };

    /* Too long. Context is only a disambiguator, so drop it before anything
     * the reader actually wrote. */
    params.set('prefix', sel.prefix.slice(-15));
    params.set('suffix', sel.suffix.slice(0, 15));
    url = base + params.toString();
    return { url: url, trimmed: true, tooLong: url.length > MAX_URL };
  }

  /* ----------------------------------------------------------------- the UI */

  var bar = null, dialog = null, current = null;

  function hideBar() { if (bar) { bar.remove(); bar = null; } }

  function showBar(sel) {
    hideBar();
    current = sel;
    bar = el('div', 'isg-ui isg-bar');
    var btn = el('button', 'isg-btn', 'suggest an edit');
    btn.type = 'button';
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      openDialog(sel);
    });
    bar.appendChild(btn);

    if (coarse) {
      /* Phones: a bar pinned to the bottom of the viewport. The native
       * selection menu appears next to the selection, so it cannot collide. */
      bar.classList.add('isg-bar-fixed');
      document.body.appendChild(bar);
    } else {
      /* Pointer devices: float just below the selection, clamped to the
       * viewport. Below, not above, so it stays clear of the browser's own
       * menu on the platforms that show one. */
      document.body.appendChild(bar);
      var w = bar.offsetWidth, h = bar.offsetHeight;
      var x = window.scrollX + sel.rect.left + (sel.rect.width - w) / 2;
      var y = window.scrollY + sel.rect.bottom + 8;
      if (sel.rect.bottom + h + 16 > window.innerHeight) {
        y = window.scrollY + sel.rect.top - h - 8;
      }
      bar.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
      bar.style.top = Math.max(8, y) + 'px';
    }
  }

  function openDialog(sel) {
    hideBar();
    if (dialog) dialog.remove();

    dialog = el('dialog', 'isg-ui isg-dialog');

    var head = el('p', 'isg-quotelabel', 'You selected');
    var quote = el('blockquote', 'isg-quote', sel.exact);
    dialog.appendChild(head);
    dialog.appendChild(quote);

    var warn = null;
    if (sel.exact.length > MAX_EXACT) {
      warn = el('p', 'isg-warn',
        'That is ' + sel.exact.length + ' characters. Please select at most ' +
        MAX_EXACT + ' — roughly one sentence — so the suggestion can be ' +
        'anchored to the source.');
      dialog.appendChild(warn);
    }

    var form = el('form', 'isg-form');
    form.method = 'dialog';

    var cl = el('label', null, 'Your comment (optional if you suggest a rewrite)');
    var comment = el('textarea');
    comment.rows = 4;
    comment.maxLength = MAX_COMMENT;
    comment.placeholder = 'What is wrong, unclear, or worth saying?';
    cl.appendChild(comment);
    var commentCount = el('span', 'isg-count', '0 / ' + MAX_COMMENT);
    cl.appendChild(commentCount);

    var rl = el('label', null, 'Suggested replacement (optional)');
    var repl = el('textarea');
    repl.rows = 3;
    repl.maxLength = MAX_REPLACEMENT;
    repl.placeholder = 'Leave empty to publish your comment as a margin note.';
    rl.appendChild(repl);
    var replCount = el('span', 'isg-count', '0 / ' + MAX_REPLACEMENT);
    rl.appendChild(replCount);

    form.appendChild(cl);
    form.appendChild(rl);

    form.appendChild(el('p', 'isg-note',
      'On the next screen GitHub asks who you are and for a source. Neither ' +
      'is required, but suggestions that say who they\'re from get read first.'));

    var note = el('p', 'isg-note',
      'Opens a prefilled issue on GitHub — you review it there before ' +
      'submitting, and you will need a GitHub account. Nothing is sent from ' +
      'this page.');
    form.appendChild(note);

    var row = el('div', 'isg-row');
    var cancel = el('button', 'isg-btn isg-ghost', 'cancel');
    cancel.type = 'button';
    cancel.addEventListener('click', function () { dialog.close(); });

    var go = el('a', 'isg-btn isg-primary', 'open on GitHub');
    go.href = '#';
    go.rel = 'noopener noreferrer';
    go.target = '_blank';
    if (sel.exact.length > MAX_EXACT) go.setAttribute('aria-disabled', 'true');

    var urlWarn = el('p', 'isg-warn');
    urlWarn.hidden = true;

    function refresh() {
      commentCount.textContent = comment.value.length + ' / ' + MAX_COMMENT;
      replCount.textContent = repl.value.length + ' / ' + MAX_REPLACEMENT;
      if (sel.exact.length > MAX_EXACT) { go.href = '#'; return; }
      var built = issueURL(sel, comment.value, repl.value);
      go.href = built.url;
      if (built.tooLong) {
        urlWarn.textContent =
          'This is too long to pass through a link (' + built.url.length +
          ' characters). Shorten your comment, or open a blank issue and paste ' +
          'it in.';
        urlWarn.hidden = false;
      } else if (built.trimmed) {
        urlWarn.textContent =
          'The surrounding context was shortened to fit the link. The ' +
          'suggestion will still anchor, but may need a second look if the ' +
          'sentence appears more than once.';
        urlWarn.hidden = false;
      } else {
        urlWarn.hidden = true;
      }
    }

    go.addEventListener('click', function (e) {
      if (sel.exact.length > MAX_EXACT) { e.preventDefault(); return; }
      if (!comment.value.trim() && !repl.value.trim()) {
        e.preventDefault();
        comment.setCustomValidity('Add a comment or a suggested rewrite.');
        comment.reportValidity();
        return;
      }
      refresh();
      dialog.close();
    });
    comment.addEventListener('input', function () {
      comment.setCustomValidity('');
      refresh();
    });
    repl.addEventListener('input', function () {
      comment.setCustomValidity('');
      refresh();
    });

    row.appendChild(cancel);
    row.appendChild(go);
    form.appendChild(urlWarn);
    form.appendChild(row);
    dialog.appendChild(form);
    document.body.appendChild(dialog);
    refresh();
    dialog.addEventListener('close', function () { dialog.remove(); dialog = null; });
    dialog.showModal();
    comment.focus();
  }

  /* ------------------------------------------------------ published notes */

  function textFragment(entry) {
    var exact = flat(entry.exact).trim();
    var parts = [encodeURIComponent(exact.slice(0, 90))];
    var pre = flat(entry.prefix || '').trim();
    if (pre) parts.unshift(encodeURIComponent(pre.split(' ').slice(-3).join(' ')) + '-');
    return location.pathname + '#:~:text=' + parts.join(',');
  }

  /* A published note. Every string is reader-supplied, so each goes in with
   * textContent. A source becomes a link only when it is plain https; anything
   * else (javascript:, data:, relative) is shown as text. */
  var HTTPS = /^https:\/\/[^\s"'<>]+$/;
  var UGC = 'noopener nofollow ugc';

  function str(v) { return typeof v === 'string' ? v : ''; }

  function panelFor(entry, inDrawer) {
    var panel = el('aside', 'isg-ui isg-panel');
    panel.appendChild(el('p', 'isg-panel-comment', str(entry.comment)));

    var about = str(entry.about), source = str(entry.source);
    if (about) {
      var ab = el('p', 'isg-panel-about', about);
      ab.appendChild(document.createTextNode(' '));
      ab.appendChild(el('span', 'isg-tag', 'self-described'));
      panel.appendChild(ab);
    }

    var bits = [];
    if (entry.author) {
      var who = el('a', null, '@' + entry.author);
      who.href = 'https://github.com/' + encodeURIComponent(entry.author);
      who.rel = UGC;
      bits.push(who);
    }
    if (source) {
      if (HTTPS.test(source)) {
        var src = el('a', null, source);
        src.href = source;
        src.rel = UGC;
        src.target = '_blank';
        bits.push(src);
      } else {
        bits.push(el('span', null, source));
      }
    }
    if (entry.issue) {
      var link = el('a', null, '#' + entry.issue);
      link.href = 'https://github.com/' + REPO + '/issues/' + encodeURIComponent(entry.issue);
      link.rel = UGC;
      bits.push(link);
    }
    var perma = el('a', null, 'link to this');
    perma.href = textFragment(entry);
    bits.push(perma);

    var meta = el('p', 'isg-panel-meta');
    bits.forEach(function (b, i) {
      if (i) meta.appendChild(document.createTextNode(' · '));
      meta.appendChild(b);
    });
    panel.appendChild(meta);

    if (!inDrawer) {
      var close = el('button', 'isg-btn isg-ghost', 'close');
      close.type = 'button';
      close.addEventListener('click', function () { panel.remove(); });
      panel.appendChild(close);
    }
    return panel;
  }

  /* What Enter or Space on each highlight does. The window capture listener
   * below dispatches it, so a deck's own key handler never sees those keys. */
  var opener = new WeakMap();

  /* Wrap a Range in <mark>, one text node at a time so the highlight can cross
   * element boundaries (surroundContents cannot). */
  function highlight(idx, span, entry) {
    var marks = [];
    for (var i = span.start; i < span.end; ) {
      var node = idx.nodes[i], from = idx.offsets[i], to = from;
      while (i < span.end && idx.nodes[i] === node) { to = idx.offsets[i]; i++; }
      var r = document.createRange();
      r.setStart(node, from);
      r.setEnd(node, to + 1);
      var mark = el('mark', 'isg-mark');
      mark.setAttribute('tabindex', '0');
      mark.setAttribute('role', 'button');
      mark.setAttribute('aria-label', 'reader note on this sentence');
      try { r.surroundContents(mark); } catch (e) { continue; }
      marks.push(mark);
    }
    marks.forEach(function (mark) {
      function open() {
        if (LAYOUT !== 'post') { openDrawer(entry); return; }
        var existing = document.querySelector('.isg-panel');
        if (existing) existing.remove();
        var panel = panelFor(entry);
        /* Place the panel after the enclosing block, not next to the mark: an
         * <aside> inside a <p> is invalid nesting, and a mark can sit inside
         * <strong> or <a> where a block child would be worse still. */
        var block = mark.closest('p, li, blockquote, h1, h2, h3, h4, td, figcaption') || mark;
        if (block.parentNode) {
          block.parentNode.insertBefore(panel, block.nextSibling);
        } else {
          mark.parentNode.insertBefore(panel, mark.nextSibling);
        }
      }
      mark.addEventListener('click', open);
      opener.set(mark, open);
    });
    return marks.length > 0;
  }

  /* Pages and decks never take a note into their own layout: notes live in a
   * fixed side drawer and a pill invites a suggestion. */
  var items = [];          /* {entry, placed} for every valid note */
  var drawer = null, pillEl = null;

  function place(item) {
    var idx = textIndex(TEXT_ROOT);   /* re-index: each highlight changes the DOM */
    var span = locate(idx, item.entry.exact, str(item.entry.prefix), str(item.entry.suffix));
    item.placed = !!(span && highlight(idx, span, item.entry));
    return item.placed;
  }

  function pillLabel() {
    var n = items.length;
    return n ? n + (n === 1 ? ' note' : ' notes') + ' \u00b7 suggest an edit' : 'Suggest an edit';
  }

  function pill() {
    if (pillEl) return pillEl;
    pillEl = el('button', 'isg-ui isg-pill isg-pill-' + LAYOUT, pillLabel());
    pillEl.type = 'button';
    pillEl.addEventListener('click', function () {
      var sel = selector();
      if (sel) openDialog(sel); else openDrawer();
    });
    document.body.appendChild(pillEl);
    return pillEl;
  }

  var returnFocus = null;   /* what had focus before the drawer took it */

  function closeDrawer() {
    if (!drawer) return;
    var had = drawer.contains(document.activeElement);
    drawer.remove();
    drawer = null;
    if (had && returnFocus && document.contains(returnFocus)) {
      returnFocus.focus({ preventScroll: true });
    }
    returnFocus = null;
  }

  function openDrawer(entry) {
    items.forEach(function (it) { if (!it.placed) place(it); });   /* retry once */
    var from = document.activeElement;
    if (drawer && drawer.contains(from)) from = returnFocus;   /* reopening in place */
    closeDrawer();
    returnFocus = from && from !== document.body ? from : null;
    drawer = el('aside', 'isg-ui isg-drawer');
    drawer.setAttribute('aria-label', 'Reader notes');
    drawer.tabIndex = -1;

    var head = el('div', 'isg-drawer-head');
    head.appendChild(el('strong', null, 'Reader notes'));
    var x = el('button', 'isg-btn isg-ghost', 'close');
    x.type = 'button';
    x.addEventListener('click', closeDrawer);
    head.appendChild(x);
    drawer.appendChild(head);

    var body = el('div', 'isg-drawer-body');
    var hint = el('p', 'isg-note', 'Select any text on this page to comment or suggest a rewrite.');
    if (HELP) {
      hint.appendChild(document.createTextNode(' '));
      var how = el('a', null, 'How this works');
      how.href = HELP;
      hint.appendChild(how);
    }
    body.appendChild(hint);

    var target = null;
    items.forEach(function (it) {
      var pnl = panelFor(it.entry, true);
      if (it.entry === entry) { pnl.classList.add('isg-current'); target = pnl; }
      body.appendChild(pnl);
    });

    var missed = items.filter(function (it) { return !it.placed; }).length;
    if (missed) {
      body.appendChild(el('p', 'isg-orphan',
        missed + ' reader ' + (missed === 1 ? 'note' : 'notes') +
        ' could not be placed on this page, because the text they refer to ' +
        'has since changed.'));
    }
    drawer.appendChild(body);
    document.body.appendChild(drawer);
    if (target) body.scrollTop = Math.max(0, target.offsetTop - body.offsetTop - 8);
    /* Escape closes it from inside (see the key listener), so keyboard focus
     * can move in. */
    drawer.focus({ preventScroll: true });
  }

  function loadNotes() {
    var paged = LAYOUT !== 'post';
    if (paged) pill();
    if (!NOTES) return;
    fetch(NOTES, { credentials: 'omit' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!Array.isArray(data) || !data.length) return;
        var missed = 0;
        data.forEach(function (entry) {
          if (!entry || typeof entry.exact !== 'string' ||
              typeof entry.comment !== 'string') return;
          var item = { entry: entry, placed: false };
          if (paged) items.push(item);
          if (!place(item)) missed++;
        });
        if (paged) { pill().textContent = pillLabel(); return; }
        if (missed) {
          /* Say so rather than silently dropping a reader's note. */
          var n = el('p', 'isg-ui isg-orphan',
            missed + ' reader ' + (missed === 1 ? 'note' : 'notes') +
            ' could not be placed on this page, because the text they refer to ' +
            'has since changed.');
          TEXT_ROOT.appendChild(n);
        }
      })
      .catch(function () { /* no notes published yet, or offline: silent */ });
  }

  /* --------------------------------------------------------------- wiring */

  var timer = null;
  function onSelect() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var sel = selector();
      if (sel) showBar(sel); else hideBar();
    }, coarse ? 250 : 80);
  }

  document.addEventListener('selectionchange', onSelect);
  document.addEventListener('mousedown', function (e) {
    if (bar && !bar.contains(e.target)) hideBar();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { hideBar(); closeDrawer(); }
  });
  window.addEventListener('resize', hideBar);

  /* Key isolation. A deck (or any page) listens for keys on document or window;
   * typing in our dialog or drawer must not reach it. One capture listener per
   * event on window runs before any of them and covers every .isg-ui element.
   * Default actions still happen (typing, input events, native Escape), so our
   * own UI must not rely on keydown listeners inside .isg-ui: the keys our UI
   * acts on are handled here instead.
   *   - Enter or Space on a highlight opens its note. A highlight sits in page
   *     content, so without this a deck would also advance on Space.
   *   - Escape with focus in the drawer closes it. */
  ['keydown', 'keyup', 'keypress'].forEach(function (type) {
    window.addEventListener(type, function (e) {
      var t = e.target;
      if ((e.key === 'Enter' || e.key === ' ') && t && opener.has(t)) {
        e.stopPropagation();
        if (type === 'keydown') { e.preventDefault(); opener.get(t)(); }
        return;
      }
      if (e.key === 'Escape' && drawer && t && t.nodeType === 1 && drawer.contains(t)) {
        e.stopPropagation();
        if (type === 'keydown') { hideBar(); closeDrawer(); }
        return;
      }
      if (t && t.closest && t.closest('.isg-ui')) e.stopPropagation();
    }, true);
  });

  /* Pages and decks size and draw themselves on window load (KaTeX, a deck's
   * fit-to-slide), so place notes after that. Posts need only the parsed DOM. */
  if (LAYOUT !== 'post') {
    if (document.readyState === 'complete') loadNotes();
    else window.addEventListener('load', loadNotes);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadNotes);
  } else {
    loadNotes();
  }
})();
