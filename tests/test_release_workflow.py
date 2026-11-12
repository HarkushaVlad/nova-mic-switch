import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_tools


class FakeResponse:
    def __init__(self, payload, url="https://release-assets.githubusercontent.com/object"):
        self.payload = payload
        self.offset = 0
        self.headers = {"Content-Length": str(len(payload))}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload)
        result = self.payload[self.offset:self.offset + size]
        self.offset += len(result)
        return result


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp_root = "/tmp/opencode" if os.name != "nt" and Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def config_file(self, name, payload=b"verified tool", *, url=None, digest=None):
        config = self.root / f"{name}.json"
        entry = {
            "url": url or release_tools.KNOWN_TOOLS[name]["url"],
            "sha256": digest or hashlib.sha256(payload).hexdigest(),
        }
        if name == "appimage-runtime":
            entry["size_bytes"] = release_tools.KNOWN_TOOLS[name]["size_bytes"]
            entry["source_commit"] = release_tools.KNOWN_TOOLS[name]["source_commit"]
        config.write_text(json.dumps({"schema_version": 1, "tools": {name: entry}}), encoding="utf-8")
        return config

    def test_download_hash_verification_and_verified_cache_reuse(self):
        payload = b"verified appimagetool bytes"
        config = self.config_file("appimagetool", payload)
        output = self.root / "cache"
        opener = patch.object(release_tools.urllib.request, "urlopen", return_value=FakeResponse(payload))
        with opener as mocked:
            target = release_tools.fetch_tool("appimagetool", output, config_path=config)
            self.assertEqual(target.read_bytes(), payload)
            self.assertTrue(target.name.endswith("appimagetool-x86_64.AppImage"))
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(release_tools.fetch_tool("appimagetool", output, config_path=config), target)
            self.assertEqual(mocked.call_count, 1)

    def test_reject_bad_download_and_corrupt_cached_file_without_executing(self):
        payload = b"expected"
        config = self.config_file("inno-setup", payload)
        output = self.root / "cache"
        with patch.object(release_tools.urllib.request, "urlopen", return_value=FakeResponse(b"tampered")):
            with self.assertRaisesRegex(release_tools.ReleaseToolError, "does not match"):
                release_tools.fetch_tool("inno-setup", output, config_path=config)
        self.assertEqual(list(output.iterdir()), [])
        with patch.object(release_tools.urllib.request, "urlopen", return_value=FakeResponse(payload)):
            target = release_tools.fetch_tool("inno-setup", output, config_path=config)
        target.write_bytes(b"changed after verification")
        with patch.object(release_tools.urllib.request, "urlopen") as mocked:
            with self.assertRaisesRegex(release_tools.ReleaseToolError, "Cached.*failed"):
                release_tools.fetch_tool("inno-setup", output, config_path=config)
            mocked.assert_not_called()

    def test_runtime_cached_size_rechecked_and_download_errors_cleanup(self):
        output = self.root / "runtime-cache"
        payload = b"too short"
        config = self.config_file("appimage-runtime", payload)
        target = output / release_tools.KNOWN_TOOLS["appimage-runtime"]["filename"]
        output.mkdir()
        target.write_bytes(payload)
        with patch.object(release_tools.urllib.request, "urlopen") as mocked:
            with self.assertRaisesRegex(release_tools.ReleaseToolError, "size or SHA-256"):
                release_tools.fetch_tool("appimage-runtime", output, config_path=config)
            mocked.assert_not_called()
        target.unlink()
        response = FakeResponse(b"short")
        response.headers["Content-Length"] = str(release_tools.MAX_DOWNLOAD_BYTES + 1)
        with patch.object(release_tools.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(release_tools.ReleaseToolError, "size limit"):
                release_tools.fetch_tool("appimage-runtime", output, config_path=config)
        self.assertEqual(list(output.iterdir()), [])
        with patch.object(release_tools.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(release_tools.ReleaseToolError, "offline"):
                release_tools.fetch_tool("appimage-runtime", output, config_path=config)
        self.assertEqual(list(output.iterdir()), [])

    def test_missing_pin_fails_clearly_without_network_access(self):
        config = self.root / "missing.json"
        config.write_text(json.dumps({"schema_version": 1, "tools": {}}), encoding="utf-8")
        with patch.object(release_tools.urllib.request, "urlopen") as mocked:
            with self.assertRaisesRegex(release_tools.ReleaseToolError, "No verified pin is configured"):
                release_tools.fetch_tool("appimage-runtime", self.root / "missing-cache", config_path=config)
            mocked.assert_not_called()

    def test_reject_unsafe_urls_digests_redirects_and_validate_runtime_pin(self):
        payload = b"x"
        config = self.config_file("appimagetool", payload, url="https://example.com/tool")
        with self.assertRaisesRegex(release_tools.ReleaseToolError, "approved official"):
            release_tools.fetch_tool("appimagetool", self.root / "cache", config_path=config)
        config = self.config_file("appimagetool", payload, digest="a" * 63)
        with self.assertRaisesRegex(release_tools.ReleaseToolError, "64 lowercase"):
            release_tools.fetch_tool("appimagetool", self.root / "cache", config_path=config)
        config = self.config_file("appimagetool", payload)
        with patch.object(release_tools.urllib.request, "urlopen", return_value=FakeResponse(payload, "http://bad.invalid/tool")):
            with self.assertRaisesRegex(release_tools.ReleaseToolError, "redirected outside"):
                release_tools.fetch_tool("appimagetool", self.root / "redirect-cache", config_path=config)
        self.assertEqual(list((self.root / "redirect-cache").iterdir()), [])
        runtime_data = b"R" * 944632
        runtime_config = self.config_file("appimage-runtime", runtime_data)
        manifest_data = json.loads((ROOT / "packaging" / "release-tools.json").read_text(encoding="utf-8"))
        locked_pins = release_tools._read_config(release_tools.DEFAULT_CONFIG)
        self.assertEqual(manifest_data["tools"]["appimage-runtime"]["sha256"],
                         "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d")
        self.assertEqual(manifest_data["tools"]["appimage-runtime"]["size_bytes"], 944632)
        self.assertEqual(manifest_data["tools"]["appimage-runtime"]["source_commit"],
                         "dd6cebedcbddde9c82f89b011e8e1d40b6e43868")
        self.assertEqual(locked_pins["appimage-runtime"]["sha256"],
                         "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d")
        with patch.object(release_tools.urllib.request, "urlopen", return_value=FakeResponse(b"short")):
            with self.assertRaisesRegex(release_tools.ReleaseToolError, "Content-Length"):
                release_tools.fetch_tool("appimage-runtime", self.root / "runtime-cache", config_path=runtime_config)
        with patch.object(release_tools.urllib.request, "urlopen", return_value=FakeResponse(runtime_data)):
            runtime_path = release_tools.fetch_tool("appimage-runtime", self.root / "runtime-cache", config_path=runtime_config)
        self.assertEqual(runtime_path.name, "runtime-x86_64")
        self.assertEqual(runtime_path.stat().st_size, 944632)

    def test_workflow_is_manual_gated_and_minimally_privileged(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("  push:", workflow)
        self.assertIn("      publish:\n", workflow)
        self.assertIn("      license_reviewed:\n", workflow)
        self.assertIn("needs: [validate-inputs, linux-x86_64, windows-x64]", workflow)
        self.assertIn("needs: [validate-inputs]", workflow)
        self.assertIn("if: ${{ inputs.publish }}", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertEqual(workflow.count("contents: write"), 1)
        self.assertIn("--verify-tag --draft", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertIn("--safe-mode", workflow)
        self.assertIn("--smoke-test", workflow)
        self.assertIn("QT_QPA_PLATFORM=offscreen", workflow)
        self.assertIn("appimage-runtime", workflow)
        self.assertIn("6.7.4", (ROOT / "packaging" / "release-tools.json").read_text(encoding="utf-8"))
        self.assertIn("0.13.7", workflow)
        self.assertIn("ubuntu-22.04", workflow)
        self.assertIn("windows-latest", workflow)
        self.assertIn("Python 3.13", workflow)
        self.assertIn("Build Linux onedir bundle and AppImage", workflow)
        self.assertIn("Build Windows onedir bundle and installer", workflow)
        self.assertEqual(workflow.count("scripts/collect_licenses.py --fetch-python-license"), 1)
        self.assertEqual(workflow.count("scripts\\collect_licenses.py --fetch-python-license"), 1)
        self.assertLess(workflow.index("Collect bundled third-party license notices"),
                        workflow.index("Build Linux onedir bundle and AppImage"))
        self.assertLess(workflow.index("Collect bundled third-party license notices", workflow.index("windows-x64:")),
                        workflow.index("Build Windows onedir bundle and installer"))
        preflight = workflow[workflow.index("  validate-inputs:"):workflow.index("  linux-x86_64:")]
        self.assertIn("fetch-depth: 0", preflight)
        self.assertIn("python-version: '3.13'", preflight)
        self.assertIn("refs/tags/{tag}^{{commit}}", preflight)
        self.assertIn('PUBLISH_INPUT: ${{ inputs.publish }}', preflight)
        self.assertIn('LICENSE_REVIEWED_INPUT: ${{ inputs.license_reviewed }}', preflight)
        self.assertIn('tag != expected', preflight)
        self.assertIn('result.stdout.strip() != os.environ["WORKFLOW_COMMIT"]', preflight)
        self.assertNotIn('"git", "fetch"', preflight)
        self.assertEqual(workflow.count("GH_TOKEN: ${{ github.token }}"), 1)
        self.assertIn("GH_REPO: ${{ github.repository }}", workflow)
        self.assertIn("timeout 60s env QT_QPA_PLATFORM=offscreen APPIMAGE_EXTRACT_AND_RUN=1", workflow)
        self.assertIn("--safe-mode --config-dir", workflow)
        self.assertIn("linux-appimage-smoke", workflow)
        self.assertIn("WaitForExit(60000)", workflow)
        self.assertNotIn("-Wait -PassThru -WindowStyle Hidden", workflow)
        self.assertIn("QuotedSmokeConfig", workflow)
        self.assertIn("qt-6.12.0/LGPL-3.0-only.txt", workflow)
        self.assertIn("ffmpeg-9.0.1/qt_attribution.json", workflow)
        self.assertIn("ffmpeg-9.0.1/LICENSE.LGPL-2.1-or-later.txt", workflow)
        self.assertIn("virtualkeyboard", workflow.lower())
        self.assertIn("_internal\\licenses", workflow)
        windows_script = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("WriteAllText", windows_script)
        self.assertIn("function Get-InnoEngineVersion", windows_script)
        self.assertIn("$CompilerExitCode = $LASTEXITCODE", windows_script)
        self.assertNotIn("VersionInfo.ProductVersion", windows_script)
        self.assertNotIn("VersionInfo.FileVersion", windows_script)
        create = workflow[workflow.index("gh release create"):workflow.index("--verify-tag --draft")]
        assets = re.findall(r'"(release-assets/[^"\n]+)"', create)
        basenames = [Path(asset).name for asset in assets]
        self.assertEqual(len(basenames), len(set(basenames)))
        self.assertEqual(basenames.count("SHA256SUMS"), 1)
        self.assertIn("cat release-assets/linux-x86_64/SHA256SUMS release-assets/windows-x64/SHA256SUMS > release-assets/SHA256SUMS", workflow)

    def test_binary_uploads_require_review_and_linux_installs_only_runner_qt_libraries(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        for platform, next_job in (("Linux", "  windows-x64:"), ("Windows", "  publish-draft:")):
            start = workflow.index(f"      - name: Upload {platform} release artifact")
            end = workflow.index(next_job, start)
            upload_step = workflow[start:end]
            self.assertIn("if: ${{ inputs.license_reviewed }}", upload_step)
            self.assertLess(upload_step.index("if: ${{ inputs.license_reviewed }}"), upload_step.index("uses:"))
        self.assertEqual(workflow.count("if: ${{ inputs.license_reviewed }}"), 2)
        linux_job = workflow[workflow.index("  linux-x86_64:"):workflow.index("  windows-x64:")]
        install_step = linux_job.index("Install Qt headless runtime libraries")
        unit_step = linux_job.index("Run unit tests")
        self.assertLess(install_step, unit_step)
        self.assertIn("sudo apt-get install --no-install-recommends -y libegl1 libopengl0 libpulse0", linux_job)
        self.assertIn("Frozen Linux license manifest SHA-256", linux_job)
        self.assertIn("Frozen Linux native library inventory", linux_job)
        windows_job = workflow[workflow.index("  windows-x64:"):workflow.index("  publish-draft:")]
        self.assertIn("Frozen Windows license manifest SHA-256", windows_job)
        self.assertIn("Frozen Windows native payload inventory", windows_job)

        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("packaging/licenses/** -text", attributes)
        docs = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        self.assertIn("With `license_reviewed` false", docs)
        self.assertIn("no downloadable binaries", docs)


if __name__ == "__main__":
    unittest.main()
