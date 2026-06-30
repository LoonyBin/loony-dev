"""Guard: every static dashboard JS file must parse as an ES module (issue #297).

The browser loads `loony_dev/web/static/js/*.js` as ES modules (`<script
type="module">` / `import`), which are always strict mode. A strict-mode-only
SyntaxError (e.g. redeclaring a parameter with `const`) aborts the whole module
load and takes the entire dashboard down — yet `node --check file.js` *passes*,
because the `.js` extension makes node parse it as sloppy CommonJS. So we copy
each file to a `.mjs` path (node keys module-vs-script off the extension) and run
`node --check` there, which is the same strict-module parse the browser does.

pytest does not lint JS, so without this the class of bug that #297 fixed could
recur unnoticed.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import loony_dev.web

STATIC_JS_DIR = Path(loony_dev.web.__file__).parent / "static" / "js"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestStaticJsParsesAsModule(unittest.TestCase):
    def test_every_static_js_parses_as_es_module(self) -> None:
        js_files = sorted(STATIC_JS_DIR.glob("*.js"))
        # A path mistake must not let the test pass with zero cases.
        self.assertTrue(
            js_files, f"no static JS files discovered under {STATIC_JS_DIR}"
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for js in js_files:
                with self.subTest(js=js.name):
                    # node --check parses `.js` as sloppy CommonJS; force the
                    # strict-module parse the browser does via a `.mjs` copy.
                    mjs = tmp_dir / f"{js.stem}.mjs"
                    mjs.write_bytes(js.read_bytes())
                    result = subprocess.run(
                        ["node", "--check", str(mjs)],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        f"{js.name} failed to parse as an ES module:\n"
                        f"{result.stderr}",
                    )


if __name__ == "__main__":
    unittest.main()
