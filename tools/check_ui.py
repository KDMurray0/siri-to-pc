"""Syntax-check the JavaScript in the templates.

A duplicate `const` once shipped and broke every button on the page — the whole
script fails to parse, so nothing gets wired up and the app looks bricked.
Nothing catches that at build time unless you look, so: look.

    python tools/check_ui.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "mrs" / "web" / "templates"


def extract(html: str) -> str:
    """Everything inside <script> tags, with jinja placeholders neutralised."""
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    js = "\n;\n".join(blocks)
    # these sit inside quotes already, so swap in a bare word
    return re.sub(r"\{\{.*?\}\}", "JINJA", js)


def ids_used(js: str) -> set[str]:
    return set(re.findall(r'\$\("([^"]+)"\)', js))


def ids_present(html: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', html))


def main() -> int:
    node = shutil.which("node")
    problems = 0

    for page in sorted(TEMPLATES.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        js = extract(html)
        if not js.strip():
            continue

        # 1. does it parse at all
        if node:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as f:
                f.write(js)
                tmp = f.name
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            Path(tmp).unlink(missing_ok=True)
            if r.returncode != 0:
                problems += 1
                print(f"[{page.name}] SYNTAX ERROR")
                for line in (r.stderr or "").strip().splitlines():
                    if "SyntaxError" in line or "^" in line or ".js:" in line:
                        print("   " + line.strip())
        else:
            print("(node not found — skipping the parse check)")

        # 2. does it reach for elements that aren't there
        missing = sorted(ids_used(js) - ids_present(html))
        if missing:
            problems += 1
            print(f"[{page.name}] uses ids that don't exist: {', '.join(missing)}")

        if not problems:
            print(f"[{page.name}] ok")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
