"""The package declares Python >=3.11, so every source file must parse as 3.11.

Regression: the webhook embeds used quote reuse inside f-strings (3.12+ only), so
`coldsnake` failed to import at all on 3.11 from 0.1.6 to 0.1.8 even though
requires-python said >=3.11. ast.parse with feature_version is the only check that
works without a 3.11 interpreter present.
"""
from __future__ import annotations

import ast
import os
import unittest


class GrammarCompatibilityTests(unittest.TestCase):
    def test_sources_parse_under_the_oldest_supported_python(self):
        package = os.path.join(os.path.dirname(__file__), "..", "src", "coldsnake")
        for name in sorted(os.listdir(package)):
            if not name.endswith(".py"):
                continue
            with self.subTest(module=name), open(os.path.join(package, name)) as handle:
                ast.parse(handle.read(), filename=name, feature_version=(3, 11))


if __name__ == "__main__":
    unittest.main()
