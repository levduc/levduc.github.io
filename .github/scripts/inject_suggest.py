"""Wire the inline-suggestion layer into the built static pages.

The 24 hand-built pages under static/ are copied verbatim into the build
output by Hugo, so layouts cannot reach them. This runs after `hugo` and adds
a config element, a stylesheet link and a script tag to each allow-listed page
in the build output. Sources are never edited; only files under --public are
written. Standard library only; run as `python3 -I`.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from suggest_paths import PageRef, classify, notes_filename  # noqa: E402

MARK_BEGIN = "<!-- inline-suggest:begin -->"
MARK_END = "<!-- inline-suggest:end -->"
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")

_BLOCK_RE = re.compile(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END), re.S)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.I)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def snippet(ref: PageRef, repo: str, base_path: str) -> str:
    layout = "deck" if ref.kind == "deck" else "page"
    notes = f"{base_path}annotations/{notes_filename(ref.page_id)}"
    return (
        f"{MARK_BEGIN}\n"
        f'<div class="isg-ui isg-config" hidden data-suggest-root="body" '
        f'data-suggest-layout="{_esc(layout)}"\n'
        f'     data-suggest-source="{_esc(ref.source)}" '
        f'data-suggest-repo="{_esc(repo)}"\n'
        f'     data-suggest-notes="{_esc(notes)}"\n'
        f'     data-suggest-help="{_esc(base_path + "posts/suggest-an-edit/")}"></div>\n'
        f'<link rel="stylesheet" href="{_esc(base_path + "css/inline-suggest.css")}">\n'
        f'<script src="{_esc(base_path + "js/inline-suggest.js")}" defer></script>\n'
        f"{MARK_END}\n"
    )


def _base_path(base_url: str) -> str:
    u = urlparse(base_url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"bad --base-url: {base_url!r}")
    path = u.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path += "/"
    return path


def _apply(text: str, block: str) -> str:
    if MARK_BEGIN in text:
        if not _BLOCK_RE.search(text):
            raise ValueError("begin marker without end marker")
        return _BLOCK_RE.sub(lambda _m: block.rstrip("\n"), text, count=1)
    last = None
    for last in _BODY_CLOSE_RE.finditer(text):
        pass
    if last is None:
        return text + ("" if text.endswith("\n") or not text else "\n") + block
    return text[:last.start()] + block + text[last.start():]


def inject(public: Path, static: Path, repo: str, base_url: str) -> list[str]:
    if not REPO_RE.match(repo):
        raise ValueError(f"bad --repo: {repo!r}")
    base_path = _base_path(base_url)
    public_root = public.resolve()
    plan: list[tuple[PageRef, Path]] = []
    for dirpath, dirnames, filenames in os.walk(static):
        dirnames.sort()
        if "index.html" not in filenames:
            continue
        rel = (Path(dirpath) / "index.html").relative_to(static).as_posix()
        ref = classify(f"static/{rel}")
        if ref is None or ref.kind not in ("page", "deck"):
            continue
        target = public / rel
        if not target.is_file():
            raise ValueError(f"built page missing for {ref.source}: {target}")
        if public_root not in target.resolve().parents:
            raise ValueError(f"refusing to write outside --public: {target}")
        plan.append((ref, target))
    # Compute every result before writing, so a failure leaves public/ untouched.
    results = []
    for ref, target in plan:
        old = target.read_bytes().decode("utf-8")
        results.append((target, old, _apply(old, snippet(ref, repo, base_path))))
    for target, old, new in results:
        if new != old:
            target.write_bytes(new.encode("utf-8"))
    wired = [ref.page_id for ref, _ in plan]
    return wired


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--public", required=True, type=Path)
    ap.add_argument("--static", required=True, type=Path)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base-url", required=True)
    args = ap.parse_args(argv)
    if not REPO_RE.match(args.repo):
        print(f"inject_suggest: invalid --repo {args.repo!r}", file=sys.stderr)
        return 2
    try:
        _base_path(args.base_url)
        wired = inject(args.public, args.static, args.repo, args.base_url)
    except (ValueError, OSError) as e:
        print(f"inject_suggest: {e}", file=sys.stderr)
        return 1
    print(f"inject_suggest: wired {len(wired)} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
