"""extensions/badcat-chrome: a plain MV3 extension, kept out of the wheel, no build step."""
import hashlib
import base64
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extensions" / "badcat-chrome"
SOURCES = ("shared.js", "background.js", "content.js", "popup.js")


def extension_id(key_b64: str) -> str:
    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    return digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((EXT / "manifest.json").read_text())

    def test_is_valid_mv3_and_every_referenced_file_exists(self):
        m = self.manifest
        self.assertEqual(m["manifest_version"], 3)
        files = [m["background"]["service_worker"], m["action"]["default_popup"]]
        files += list(m["icons"].values()) + list(m["action"]["default_icon"].values())
        for script in m["content_scripts"]:
            files += script["js"]
        for name in files:
            self.assertTrue((EXT / name).is_file(), name)
        self.assertEqual(set(m["icons"]), {"16", "32", "48", "128"})
        self.assertNotIn("options_page", m)
        self.assertNotIn("options_ui", m)

    def test_requests_only_the_needed_permissions(self):
        m = self.manifest
        self.assertEqual(sorted(m["permissions"]), ["nativeMessaging", "storage"])
        self.assertEqual(m["host_permissions"], ["https://chatgpt.com/*"])
        self.assertEqual([c["matches"] for c in m["content_scripts"]], [["https://chatgpt.com/*"]])
        self.assertNotIn("optional_permissions", m)
        self.assertNotIn("externally_connectable", m)
        self.assertNotIn("content_security_policy", m)  # keep the strict MV3 default

    def test_fixed_key_gives_the_documented_extension_id(self):
        # The same ID goes to `badcat-host install --extension-id` (see README).
        self.assertEqual(extension_id(self.manifest["key"]), "iiaiioncejjhjmglaiionpmgpjminnkb")


class SourceTests(unittest.TestCase):
    def test_no_build_step_or_dependency_tree(self):
        for name in ("package.json", "package-lock.json", "vite.config.js", "tsconfig.json", "node_modules", "dist"):
            self.assertFalse((EXT / name).exists(), name)
        self.assertFalse(list(EXT.rglob("*.ts")) + list(EXT.rglob("*.vue")) + list(EXT.rglob("*.jsx")))

    def test_self_contained_code_with_no_remote_loading_or_credentials(self):
        for name in SOURCES + ("popup.html",):
            text = (EXT / name).read_text()
            self.assertNotRegex(text, r"\beval\s*\(", name)
            self.assertNotRegex(text, r"new Function|import\(|importScripts\(\s*['\"]https?:", name)
            self.assertNotRegex(text, r"<script[^>]+src=[\"']https?:", name)
            self.assertNotRegex(text, r"(?i)ghp_|github_pat_|authorization|api\.github\.com|\btoken\b", name)

    def test_native_host_name_matches_badcat_host(self):
        from badcat_host import host

        self.assertIn(f"'{host.HOST_NAME}'", (EXT / "shared.js").read_text())

    def test_extension_is_not_in_the_wheel(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('where = ["src"]', pyproject)
        self.assertFalse((ROOT / "src" / "extensions").exists())
        self.assertFalse(re.search(r"extensions", pyproject))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class NodeTests(unittest.TestCase):
    def test_extension_logic_tests_pass(self):
        tests = sorted(str(p) for p in (EXT / "test").glob("*.test.js"))
        self.assertTrue(tests)
        result = subprocess.run(["node", "--test", *tests], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
