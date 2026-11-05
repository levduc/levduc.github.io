"""Text-level checks on the workflow files. No YAML library: lines only."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF_DIR = ROOT / ".github" / "workflows"
WF = WF_DIR / "suggestion-to-pr.yml"

# ":(glob)" makes "*" stop at "/". In git's default pathspec syntax it does not,
# so "static/*/index.html" would also add static/a/b/index.html.
EXPECTED_PATHS = {
    ":(glob)content/posts/*.md",
    ":(glob)static/*/index.html",
    ":(glob)static/*/talk/index.html",
    ":(glob)static/learning/*/index.html",
    ":(glob)static/learning/*/talk/index.html",
    ":(glob)static/annotations/*.json",
}


def lines():
    return WF.read_text(encoding="utf-8").splitlines()


def step_block(name):
    """Lines of the step whose `- name:` equals `name`."""
    ls = lines()
    start = next(i for i, l in enumerate(ls) if l.strip() == f"- name: {name}")
    indent = len(ls[start]) - len(ls[start].lstrip())
    end = len(ls)
    for j in range(start + 1, len(ls)):
        l = ls[j]
        if l.strip() and len(l) - len(l.lstrip()) <= indent and l.lstrip().startswith("- "):
            end = j
            break
    return ls[start:end]


def add_paths():
    """The `add-paths` lines of the PR step, in order."""
    block = step_block("Open the pull request")
    i = next(k for k, l in enumerate(block) if l.strip() == "add-paths: |")
    indent = len(block[i]) - len(block[i].lstrip())
    got = []
    for l in block[i + 1:]:
        if not l.strip():
            continue
        if len(l) - len(l.lstrip()) <= indent:
            break
        got.append(l.strip())
    return got


class WorkflowTests(unittest.TestCase):
    def test_manual_step_labels_and_comments(self):
        block = step_block("Flag for a hand edit")
        self.assertIn("        if: steps.apply.outputs.status == 'manual'", block)
        text = "\n".join(block)
        a = text.index("gh label create needs-manual-edit --force")
        b = text.index("gh issue edit")
        c = text.index("gh issue comment")
        self.assertLess(a, b)
        self.assertLess(b, c)
        for var in ("GH_TOKEN", "ISSUE_NUMBER", "COMMENT_PATH"):
            self.assertRegex(text, rf"(?m)^\s+{var}: \$\{{\{{ ")

    def test_manual_step_after_apply(self):
        names = [l.strip()[len("- name: "):] for l in lines() if l.strip().startswith("- name: ")]
        self.assertGreater(names.index("Flag for a hand edit"),
                           names.index("Locate the sentence and apply the suggestion"))

    def test_pr_step_never_runs_on_manual(self):
        block = step_block("Open the pull request")
        ifs = [l.strip() for l in block if l.strip().startswith("if:")]
        self.assertEqual(ifs, ["if: steps.apply.outputs.status == 'replace' || "
                               "steps.apply.outputs.status == 'annotate'"])

    def test_add_paths_match_spec(self):
        got = add_paths()
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(set(got), EXPECTED_PATHS)

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_add_paths_admit_only_the_allowed_shapes(self):
        """Run the workflow's own pathspecs through `git add -n` in a scratch repo."""
        allowed = {
            "content/posts/a.md",
            "static/a/index.html", "static/learning/index.html",
            "static/a/talk/index.html",
            "static/learning/b/index.html",
            "static/learning/b/talk/index.html",
            "static/annotations/a.json",
        }
        refused = {
            "content/posts/sub/evil.md", "content/a.md",
            "static/a/b/index.html", "static/js/x/index.html",
            "static/a/b/talk/index.html",
            "static/learning/a/b/index.html", "static/learning/a/b/talk/index.html",
            "static/annotations/deep/a.json", "static/index.html",
            "layouts/index.html", "config.toml", ".github/workflows/x.yml",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for rel in allowed | refused:
                f = Path(tmp) / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, HOME=tmp)
            run = dict(cwd=tmp, env=env, capture_output=True, text=True)
            subprocess.run(["git", "init", "-q"], check=True, **run)
            proc = subprocess.run(["git", "add", "-n", "--", *add_paths()], **run)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        added = set(re.findall(r"^add '(.+)'$", proc.stdout, re.M))
        self.assertEqual(added, allowed)

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_every_add_path_matches_a_tracked_file(self):
        """create-pull-request runs one `git add -- <all paths>` and ignores its
        exit code. If any one pathspec matches nothing, git refuses the whole
        command and nothing is staged, so the PR is silently never opened."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        if inside.stdout.strip() != "true":
            self.skipTest("not a git checkout")
        for spec in add_paths():
            with self.subTest(spec=spec):
                proc = subprocess.run(["git", "ls-files", "--", spec], cwd=ROOT, env=env,
                                      capture_output=True, text=True, check=True)
                self.assertTrue(proc.stdout.strip(), f"{spec} matches no tracked file")

    def test_deploy_and_tests_run_on_main(self):
        """levduc.github.io serves the source branch `main`; its old `master`
        holds the built HTML and is kept only as a rollback, so nothing may
        deploy or gate on it."""
        for name in ("pages.yml", "tests.yml"):
            text = (WF_DIR / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertIn("    branches: [main]\n", text)
                self.assertNotIn("branches: [master]", text)

    def test_no_issue_text_in_run_lines(self):
        bad = ("github.event.issue.body", "github.event.issue.title", "github.event.comment")
        in_run, run_indent = False, 0
        for n, l in enumerate(lines(), 1):
            ind = len(l) - len(l.lstrip())
            if in_run and l.strip() and ind <= run_indent:
                in_run = False
            if not in_run and re.match(r"\s*(- )?run:", l):
                run_indent = ind
                in_run = True
                body = l.split("run:", 1)[1]
            else:
                body = l if in_run else ""
            for b in bad:
                self.assertNotIn(b, body, f"line {n}")
            self.assertNotIn("${{", body, f"line {n}: expression inside run")

    def test_no_pull_request_target(self):
        for f in WF_DIR.iterdir():
            if f.suffix in (".yml", ".yaml"):
                self.assertNotIn("pull_request_target", f.read_text(encoding="utf-8"), f.name)

    def test_actions_pinned_by_sha(self):
        for l in lines():
            m = re.match(r"\s*(?:- )?uses:\s*(\S+)", l)
            if m:
                self.assertRegex(m.group(1), r"@[0-9a-f]{40}$", l)


class FrontEndTextTests(unittest.TestCase):
    CSS = ROOT / "static" / "css" / "inline-suggest.css"
    JS = ROOT / "static" / "js" / "inline-suggest.js"

    def test_css_vars_have_fallbacks(self):
        css = self.CSS.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"var\(--[a-z-]+\)", css), [])

    def test_css_reads_only_its_own_tokens(self):
        # Host pages name their tokens differently and some are dark, so the
        # suggest UI reads no host token at all (final review I3).
        css = self.CSS.read_text(encoding="utf-8")
        names = re.findall(r"var\((--[A-Za-z0-9-]+)", css)
        self.assertTrue(names)
        self.assertEqual([n for n in names if not n.startswith("--isg-")], [])

    def test_every_surface_sets_ink_and_background(self):
        css = re.sub(r"/\*.*?\*/", "", self.CSS.read_text(encoding="utf-8"), flags=re.S)
        for sel in (".isg-bar", ".isg-bar-fixed", ".isg-btn", ".isg-dialog", ".isg-quote",
                    ".isg-form textarea", ".isg-panel", ".isg-drawer", ".isg-pill", ".isg-mark"):
            with self.subTest(sel=sel):
                m = re.search(r"(?m)^" + re.escape(sel) + r"\s*\{([^}]*)\}", css)
                self.assertIsNotNone(m, sel)
                body = m.group(1)
                self.assertRegex(body, r"(?m)^\s*color:\s*var\(--isg-")
                self.assertRegex(body, r"(?m)^\s*background:\s*var\(--isg-")

    def test_js_limits_match_python(self):
        sys.path.insert(0, str(ROOT / ".github" / "scripts"))
        import apply_suggestion
        js = self.JS.read_text(encoding="utf-8")
        for name in ("MAX_COMMENT", "MAX_REPLACEMENT"):
            m = re.search(r"var %s = (\d+);" % name, js)
            self.assertIsNotNone(m, name)
            self.assertEqual(int(m.group(1)), getattr(apply_suggestion, name), name)


if __name__ == "__main__":
    unittest.main()
