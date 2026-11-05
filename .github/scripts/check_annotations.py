#!/usr/bin/env python3
"""Validate every file under static/annotations/.

Run in CI so a hand-edited or badly merged annotation file fails the build
rather than breaking the suggestion layer silently in readers' browsers.

Usage:  python3 .github/scripts/check_annotations.py [dir]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REQUIRED = ("id", "exact", "comment", "issue")
MAX_BYTES = 512 * 1024
MAX_ABOUT = 200
MAX_SOURCE = 300
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.json$")
# Script-capable schemes only; "arXiv:..." and "Jane: ..." are ordinary text.
# Shared with apply_suggestion.py so the Action never writes what CI rejects.
SCHEME_RE = re.compile(r"^[\s\x00-\x1f\x7f]*(?:javascript|data|vbscript):", re.IGNORECASE)


def check(path: Path) -> list[str]:
    problems: list[str] = []
    if not FILENAME_RE.match(path.name):
        problems.append(f"{path}: file name must match {FILENAME_RE.pattern}")
    size = path.stat().st_size
    if size > MAX_BYTES:
        problems.append(f"{path}: {size} bytes, over the {MAX_BYTES}-byte limit")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return problems + [f"{path}: not valid UTF-8 JSON ({exc})"]

    if not isinstance(data, list):
        return problems + [f"{path}: top level is {type(data).__name__}, expected a list"]

    seen: set[str] = set()
    for i, entry in enumerate(data):
        where = f"{path}[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: entry is {type(entry).__name__}, expected an object")
            continue
        for key in REQUIRED:
            if key not in entry:
                problems.append(f"{where}: missing required key {key!r}")
        ident = entry.get("id")
        if isinstance(ident, str):
            if ident in seen:
                problems.append(f"{where}: duplicate id {ident!r}")
            seen.add(ident)
        if not isinstance(entry.get("exact", ""), str) or not entry.get("exact"):
            problems.append(f"{where}: 'exact' must be a non-empty string")
        if not isinstance(entry.get("comment", ""), str) or not entry.get("comment"):
            problems.append(f"{where}: 'comment' must be a non-empty string")
        if not isinstance(entry.get("issue", 0), int):
            problems.append(f"{where}: 'issue' must be an integer")
        for key, limit in (("about", MAX_ABOUT), ("source", MAX_SOURCE)):
            if key not in entry:
                continue
            val = entry[key]
            if not isinstance(val, str):
                problems.append(f"{where}: {key!r} must be a string")
            elif len(val) > limit:
                problems.append(f"{where}: {key!r} is {len(val)} characters, over {limit}")
            elif SCHEME_RE.match(val):
                problems.append(f"{where}: {key!r} starts with a script-capable URL scheme")

    if not problems:
        print(f"ok  {path}  ({len(data)} annotation{'s' if len(data) != 1 else ''})")
    return problems


def self_test() -> int:
    """Prove the checker rejects what it should, with no annotations committed.

    Until the first reader note is merged, static/annotations/ holds nothing, so
    a plain CI run would exit "nothing to check" and never exercise this code.
    """
    import contextlib
    import io
    import json as _json
    import tempfile

    @contextlib.contextmanager
    def quiet():
        """Swallow the inner runs' own output: their 'FAIL' lines are the
        expected result here, and would read as real failures in a CI log."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield

    good = [{"id": "s1", "exact": "a sentence", "comment": "a note", "issue": 1}]
    bad_cases = {
        "not-a-list.json": '{"id": "s1"}',
        "not-json.json": "{oops",
        "missing-keys.json": '[{"id": "s1"}]',
        "duplicate-ids.json": _json.dumps(good + good),
        "empty-comment.json": '[{"id":"s1","exact":"x","comment":"","issue":1}]',
        "issue-not-int.json": '[{"id":"s1","exact":"x","comment":"c","issue":"1"}]',
        "javascript-source.json": _json.dumps(
            [dict(good[0], source="javascript:alert(1)")]),
        "about-too-long.json": _json.dumps([dict(good[0], about="a" * 201)]),
        "about-not-string.json": _json.dumps([dict(good[0], about=5)]),
    }

    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        ok_dir = Path(tmp) / "ok"
        ok_dir.mkdir()
        (ok_dir / "post.json").write_text(_json.dumps(good), encoding="utf-8")
        with quiet():
            ok_rc = main(["", str(ok_dir)])
        if ok_rc != 0:
            failures.append("a valid annotation file was rejected")

        for name, content in bad_cases.items():
            bad_dir = Path(tmp) / name
            bad_dir.mkdir()
            (bad_dir / name).write_text(content, encoding="utf-8")
            with quiet():
                rc = main(["", str(bad_dir)])
            if rc == 0:
                failures.append(f"{name} was accepted but should have been rejected")

    for f in failures:
        print(f"SELF-TEST FAIL  {f}", file=sys.stderr)
    if failures:
        return 1
    print(f"ok  self-test: 1 valid file accepted, {len(bad_cases)} invalid files rejected")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--self-test":
        return self_test()
    root = Path(argv[1] if len(argv) > 1 else "static/annotations")
    if not root.is_dir():
        print(f"ok  no {root}/ directory yet, nothing to check")
        return 0

    files = sorted(root.glob("*.json"))
    if not files:
        print(f"ok  {root}/ contains no annotation files yet")
        return 0

    problems = [p for f in files for p in check(f)]
    for p in problems:
        print(f"FAIL  {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
