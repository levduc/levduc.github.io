# levduc.github.io

Hugo source for [levduc.github.io](https://levduc.github.io/). GitHub Actions
builds it and deploys it to Pages on every push to `main`; there is no local
publish step.

```
content/posts/      notes, in Markdown
layouts/            templates; layouts/index.html is the whole homepage
static/             copied verbatim: paper landing pages, the learning pages and decks, PDFs
static/annotations/ published reader notes, one JSON file per page
.github/            the suggestion workflow, its script, and the deploy workflow
```

Build locally with `hugo server -D`. CI pins Hugo **0.157.0 extended**; if you
upgrade locally, bump `HUGO_VERSION` in both workflows to match.

---

## Inline suggestions

A reader selects a sentence on a page, types a comment or a rewrite, and ends up
with a pull request anchored to that sentence. There is no server, no database and no
third-party service — only GitHub.

### Which pages take suggestions

Every page except the homepage:

- the Markdown posts under `content/posts/`;
- the 20 paper pages (`static/<slug>/index.html`);
- `learning/` and `learning/lattice/`;
- the two decks (`static/<slug>/talk/` and `static/learning/<slug>/talk/`).

The allow-list is `.github/scripts/suggest_paths.py`; `classify()` is the only
place that decides. The homepage is out because Hugo renders it from
`layouts/index.html` and `config.toml`, so there is no single source file for a
suggestion to edit. Asset directories under `static/` (`js`, `css`,
`annotations`, `papers`, `fonts`, `img`) are never pages.

### How it works

1. **In the browser.** `static/js/inline-suggest.js` loads on every allow-listed
   page. Selecting text captures a [W3C text-quote
   selector](https://www.w3.org/TR/annotation-model/#text-quote-selector): the
   exact sentence, ~40 characters of context either side, and the source path.
   On posts, `layouts/_default/single.html` renders the path into a
   `data-suggest-source` attribute at build time. On the hand-built pages, the
   injector (below) adds it.

   Posts keep the inline panel. On pages and decks, notes open in a side drawer,
   and a "Suggest an edit" pill opens it: bottom-right on pages, top-right on
   decks.

2. **The hand-off.** Pressing *open on GitHub* opens a prefilled
   [issue form](.github/ISSUE_TEMPLATE/suggest.yml) with those fields filled in.
   Nothing is transmitted from the page itself. Creating a PR needs the reader's
   GitHub identity, and the alternatives — OAuth with a server-held secret, or
   asking readers to paste an access token — are both worse than a hand-off.

3. **The Action.** `suggestion-to-pr.yml` fires on issues labelled `suggestion`
   and runs `.github/scripts/apply_suggestion.py`, which locates the sentence in
   the Markdown source and then either:
   - rewrites that one span, if a replacement was given;
   - appends the comment to `static/annotations/<slug>.json` (for a page,
     `<page>.json` with `/` written as `--`, e.g. `learning--lattice.json`); or
   - takes the manual route, below.

   The first two open a pull request. **Nothing merges automatically.**

4. **Publication.** Merging the PR pushes to `main`, which triggers
   `pages.yml`, which deploys. Review before merge is review before publish.

### Injection and local preview

The built pages under `static/` are not edited by hand to carry the suggestion
layer. `.github/scripts/inject_suggest.py` runs after `hugo` in `pages.yml` and
`tests.yml` and adds the config and script tag to the built copies in `public/`.
It fails the build if an allow-listed page has no built copy.

`scripts/preview.sh` builds, injects and serves the result locally. `PORT`
(default 1313) and `SUGGEST_REPO` (default `levduc/suggest-lab`) override its
defaults.

```sh
PORT=1320 SUGGEST_REPO=you/your-fork scripts/preview.sh
```

### Short suggestions and credentials

The Action enforces these limits, and the form states them. The on-page dialog
also stops typing at the comment and rewrite limits and shows a counter.

| Field | Limit |
|-------|-------|
| Comment | 800 characters |
| Suggested replacement | 400 |
| About you | 200 |
| Source | 300 |

A comment is optional when a rewrite is given. With neither, the suggestion is
refused with a comment on the issue.

"About you (recommended)" and "Source (recommended if you're correcting a
claim)" are optional. On a published note they are shown labelled
`self-described`: the site does not verify them. Only `https` links become
links. A value starting with `javascript:`, `data:` or `vbscript:` is refused.

### The manual route

Some rewrites cannot be applied safely by script:

- the text crosses formatting;
- the text contains math;
- the text is not found, or is found more than once and context cannot choose;
- the rewrite contains `$`, `\` or `{{` (a Hugo shortcode), or in a post would
  form `{{` with the text beside it;
- on a page or deck: the text starts right after a literal `<`, the rewrite
  would change the page's markup rather than its text, or the page has a
  `<script>` or `<style>` containing `<!--`, which the automation does not
  parse.

These stay as the issue. The workflow adds the `needs-manual-edit` label and
posts a comment saying why and where the passage is; no pull request is opened.
A hand edit then applies it. A comment-only suggestion never takes this route:
it is published as a note even when its passage cannot be located.

### Anchoring

The script looks for the sentence three times, strictest first:

| Pass | Tolerates |
|------|-----------|
| verbatim | nothing |
| whitespace-normalised | the source wrapping a sentence across lines |
| markdown-tolerant | `**emphasis**`, `` `code` ``, `[links](...)`, escapes |

Each pass matches over a *projection* of the source that keeps a map back to
real byte offsets, so even a fuzzy match rewrites an exact span. Context
(prefix/suffix) only ever chooses between identical candidates — it never
decides the span itself.

If the sentence is absent, or present more than once and the context cannot
separate the occurrences, the Action writes nothing to the source. A rewrite
goes to the manual route; a comment is still published as a note.
Writing to the wrong sentence is a worse failure than declining.

### Adding it to a new post or page

A post at `content/posts/<slug>.md` gets it with nothing to do. A new page at
`static/<slug>/index.html` (or `static/learning/<slug>/index.html`, or a
`talk/index.html` under either) is accepted by the allow-list, and the injector
wires it in on the next build.

The allow-list covers only those shapes, so the Action cannot touch
`config.toml`, a layout, a workflow or any other file under `static/` except
`static/annotations/*.json`. The Action's `add-paths` repeats the same shapes
as `:(glob)` pathspecs, so each `*` matches within one directory, as it does in
the allow-list. It cannot express the excluded asset directories (`static/js/`
and the like), so the allow-list is the gate and `add-paths` the backstop.

### Larger edits

Suggestions are for sentence-sized changes. For anything larger, fork the
repository, edit the file, and open a pull request in the usual way. Set
`params.suggestrepo` (below) in your fork so its suggestion links point to it.

### Labels

Only `suggestion` must exist before the first suggestion, because the issue form
applies it. `needs-review` is created by `create-pull-request`, and
`needs-manual-edit` by the workflow.

To point suggestions at a different repository, set `params.suggestrepo` in
`config.toml`. CI overrides it with `HUGO_PARAMS_SUGGESTREPO=<owner/repo>`, so
a fork or a staging copy is automatically correct. (The param name has no
underscore on purpose: Hugo's environment overrides do not reach param names
containing one.)

### Security

Reader input is hostile input, and is handled accordingly:

- The issue body reaches the script as a **file**, never as a `${{ }}`
  expression inside a `run:` line, so it is never expanded into source.
- The trigger is `issues`, never `pull_request_target`. No fork code is ever
  checked out or executed.
- Writes are confined to the allow-list above, enforced twice: in the script,
  and again by `add-paths` on the PR action.
- Reader text in a PR body is wrapped in a code fence sized to contain it, so
  `@mentions` do not notify anyone and `Closes #12` does not cross-link.
- The commit message is checked against a character whitelist before it reaches
  YAML.
- On the page, reader text is only ever written with `textContent`. There is no
  `innerHTML` path for anything fetched from the network.
- Permissions are granted per job. Third-party actions are pinned to commit
  SHAs.

Optional spam gate: set the repository variable `MIN_ACCOUNT_AGE_DAYS` (Settings
→ Secrets and variables → Actions → Variables) to ignore accounts newer than
that. Unset, it does nothing.

### Limits

- **Readers need a GitHub account.** Unavoidable without a server.
- **Anchoring can fail** on sentences inside tables, footnotes, or heavy inline
  markup. The Action does not guess: an unplaceable rewrite takes the manual
  route, and a comment is still published as a note.
- **Selections are capped at 300 characters**, about one sentence.
- **The homepage takes no suggestions** (see above).
- **Suggestion PRs get no CI check.** GitHub does not fire workflows for events
  created by `GITHUB_TOKEN`, so `tests.yml` does not run on them. Your review is
  the gate. To get a check on one:
  `gh workflow run tests.yml --ref suggest/<issue-number>`.
- **A note whose sentence later changes** stops matching. The page says how many
  notes could not be placed instead of dropping them silently.

### Tests

```sh
python3 -m unittest discover -s .github/scripts -p 'test_*.py'   # unit tests
python3 -I .github/scripts/check_annotations.py --self-test       # then without --self-test
out=$(mktemp -d) && hugo --quiet -d "$out" && python3 .github/scripts/browser_test.py "$out" --inject
```

`browser_test.py` drives a headless browser against the built and injected site.
