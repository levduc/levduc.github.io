---
title: "Suggest an edit"
date: 2026-11-03
description: "How the inline suggestion layer on these notes works, and what happens to a suggestion after you send it."
---

Select any sentence on this page. A small **suggest an edit** button appears.
That is the whole interface, and this page explains where a suggestion goes
after you press it.

It works on every page of the site except the homepage: these notes, the paper
pages, the learning pages and the talk decks. The homepage is built from
templates and configuration rather than from one source file, so there is
nothing for a suggestion to edit. On the paper pages and decks, the notes open
in a side drawer, from a **Suggest an edit** button at the bottom right of a
page or the top right of a deck.

## What happens when you select something

The page captures three things: the sentence you selected, about forty
characters either side of it, and the path of the source file this page was
built from. Together they are a [text-quote
selector](https://www.w3.org/TR/annotation-model/#text-quote-selector) — a way
of pointing at a passage that survives the text moving around, because it
describes the passage by its content rather than by its position.

You then type a comment, a replacement sentence, or both. A comment can be at
most 800 characters and a replacement at most 400; the box stops you there and
shows a count. You need one or the other. Pressing **open on GitHub** carries
all of it into a prefilled issue form. Nothing is sent from this page; you see
the issue before you submit it.

## Saying who you are

The form has two optional fields: **About you** (up to 200 characters) and
**Source** (up to 300). They are recommended, not required. Say who you are and
link something I can check, such as a homepage, Google Scholar, DBLP or ORCID.
If you are correcting a claim, give the paper or DOI that supports the
correction. Suggestions that say who they are from get read first.

I cannot verify what you write there, so a published note shows it labelled
*self-described*. Only `https` links become links. Anything that starts with
`javascript:`, `data:` or `vbscript:` is refused.

## What happens after you submit

A GitHub Action reads the issue and tries to find your sentence in the source
file. It looks three times, from strictest to loosest: first for the sentence
exactly as you selected it, then ignoring differences in whitespace, then
ignoring inline formatting such as emphasis and links. If it finds the sentence
in exactly one place, it acts. If it finds it in several places, or nowhere at
all, it never edits the source: writing to the wrong sentence is a worse
outcome than declining. A rewrite in that case stays as the issue for a hand
edit (see below), and a comment is still published as a note.

What it does depends on what you sent:

- If you suggested a replacement that can be applied, it opens a pull request that changes that one
  sentence and nothing else.
- If you only left a comment, it opens a pull request that adds your comment to
  this page's notes file. Merging it publishes your comment as a margin note,
  highlighted against the sentence it refers to.

Either way I read it and merge it by hand. Nothing reaches this page
automatically.

## When a rewrite can't be applied automatically

Some rewrites are not safe to apply by script, so the Action does not try. That
covers text that crosses formatting such as emphasis or a link, text that
contains maths, text it cannot find or finds in more than one place, a rewrite
that contains `$`, `\` or `{{`, and a rewrite that would change a page's markup
rather than just its text. No pull request is opened. Your suggestion
stays as the issue, it gets the `needs-manual-edit` label, and the Action
comments to say why. I then make the change by hand. Your wording is not lost.

## The limits, stated plainly

You need a GitHub account. There is no way around that: creating a pull request
requires an identity, and the alternatives all involve either a server holding a
secret or you pasting an access token into a web page. Neither is something I
want to run or ask of you.

Anchoring can fail. A sentence broken across a table cell, wrapped in a
footnote, or carrying heavy inline markup may not be locatable in the source
even though it reads cleanly here. When that happens the automation does not
guess. A rewrite stays as the issue, with a comment saying why, and I apply it
by hand. A comment is still published as a note, though the note may not be
highlighted against the sentence.

Long selections are refused. The cap is 300 characters, roughly one sentence,
because the longer the quoted passage the more likely it spans formatting that
does not survive the round trip.

## Why bother

Writing notes like these in public means being wrong in public. The useful
version of that is a correction I can apply in one click, anchored to the exact
sentence that is wrong, with the reasoning attached and the author credited. The
less useful version is an email I read three weeks later.

If you find a mistake in the maths on any page of this site, that is exactly the
case this was built for. Select the sentence and tell me.
