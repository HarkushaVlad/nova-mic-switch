import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
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
        self.assertIn("needs: [validate-inputs, linux-x86_64, windows-x64, corresponding-sources]", workflow)
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
        self.assertIn("  corresponding-sources:\n", workflow)
        self.assertIn("needs: [validate-inputs, linux-x86_64, windows-x64]", workflow)
        self.assertIn("scripts/build_source_assets.py --cache-dir build/source-cache --output-dir dist/sources --fetch", workflow)
        self.assertIn("scripts/build_ubuntu_sources.py --report-json build/native-evidence-linux/report.json", workflow)
        self.assertIn("scripts/fetch_runtime_sources.py --output-dir build/runtime-materials/sources --fetch", workflow)
        self.assertIn("--require-complete", workflow)
        self.assertIn("name: native-evidence-linux", workflow)
        self.assertIn("name: nova-mic-sources", workflow)
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
        self.assertEqual(len(assets), 9)
        self.assertTrue(all(asset.startswith("release-assets/final/") for asset in assets))
        self.assertIn('sha256sum "${basenames[@]}" > SHA256SUMS', workflow)
        self.assertIn('"release-assets/corresponding-sources/$source.tar"', workflow)

    def test_binary_uploads_require_review_and_linux_installs_only_runner_qt_libraries(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        for platform, next_job in (("Linux", "  windows-x64:"), ("Windows", "  publish-draft:")):
            start = workflow.index(f"      - name: Upload {platform} release artifact")
            end = workflow.index(next_job, start)
            upload_step = workflow[start:end]
            self.assertIn("if: ${{ inputs.license_reviewed }}", upload_step)
            self.assertLess(upload_step.index("if: ${{ inputs.license_reviewed }}"), upload_step.index("uses:"))
        self.assertEqual(workflow.count("if: ${{ inputs.license_reviewed }}"), 3)
        linux_job = workflow[workflow.index("  linux-x86_64:"):workflow.index("  windows-x64:")]
        install_step = linux_job.index("Install Linux build and Qt runtime libraries")
        unit_step = linux_job.index("Run unit tests")
        self.assertLess(install_step, unit_step)
        sync_step = linux_job.index("Sync locked Python 3.13 environment")
        self.assertLess(install_step, sync_step)
        self.assertIn(
            "sudo apt-get install --no-install-recommends -y build-essential pkg-config "
            "libusb-1.0-0-dev libudev-dev libegl1 libopengl0 libpulse0",
            linux_job,
        )
        self.assertIn("Frozen Linux license manifest SHA-256", linux_job)
        self.assertIn("Frozen Linux native library inventory", linux_job)
        windows_job = workflow[workflow.index("  windows-x64:"):workflow.index("  publish-draft:")]
        self.assertIn("Frozen Windows license manifest SHA-256", windows_job)
        self.assertIn("Frozen Windows native payload inventory", windows_job)

    def test_linux_system_package_notices_are_frozen_and_verified(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        linux_start = workflow.index("  linux-x86_64:")
        windows_start = workflow.index("  windows-x64:")
        linux_job = workflow[linux_start:windows_start]
        build_start = linux_job.index("      - name: Build Linux onedir bundle and AppImage")
        build_end = linux_job.index("      - name:", build_start + 1)
        build_step = linux_job[build_start:build_end]
        for required in (
            'LINUX_NOTICES="$LICENSES/linux-system"',
            'test -s "$LINUX_NOTICES/manifest.json"',
            'test -s "$LINUX_NOTICES/THIRD-PARTY-NOTICES.md"',
            'manifest.get("source_packages")',
            'manifest.get("texts")',
            'hashlib.sha256(data).hexdigest()',
            'resolved.relative_to(resolved_root)',
            'entry.get("size")',
            'source packages, {len(texts)} notice texts',
        ):
            self.assertIn(required, build_step)
        self.assertLess(build_step.index("Frozen Linux license payload missing"),
                        build_step.index("Verified Linux system-package notices"))
        windows_job = workflow[windows_start:workflow.index("  publish-draft:", windows_start)]
        self.assertNotIn("linux-system", windows_job)
        self.assertNotIn("collect_linux_notices", windows_job)

        spec = (ROOT / "packaging/nova-mic-switch.spec").read_text(encoding="utf-8")
        filter_position = spec.index("a.binaries = [row for row in a.binaries if not _excluded_unused_qt_plugin(row)]")
        helper_position = spec.index("from collect_linux_notices import collect_linux_notices")
        append_position = spec.index("a.datas.extend(notice_data_rows)")
        policy_position = spec.index("if IS_WINDOWS and os.name == \"nt\":")
        pyz_position = spec.index("pyz = PYZ(a.pure)")
        self.assertLess(filter_position, helper_position)
        self.assertLess(helper_position, append_position)
        self.assertLess(append_position, policy_position)
        self.assertLess(append_position, pyz_position)
        self.assertIn("toc_rows=a.binaries + a.datas", spec)
        self.assertIn('"DATA"', spec)

    def test_windows_installer_refusal_is_negative_only_and_safe_metadata_is_uploaded(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        windows_start = workflow.index("  windows-x64:")
        windows_end = workflow.index("  corresponding-sources:", windows_start)
        windows_job = workflow[windows_start:windows_end]
        build_pos = windows_job.index("Build Windows onedir bundle and installer")
        refusal_pos = windows_job.index("Verify installer refuses silent runtime terms acceptance")
        report_pos = windows_job.index("Generate Windows native dependency evidence")
        upload_pos = windows_job.index("Upload Windows native evidence metadata")
        self.assertLess(build_pos, refusal_pos)
        self.assertLess(refusal_pos, report_pos)
        self.assertLess(report_pos, upload_pos)
        refusal_step = windows_job[refusal_pos:windows_job.index("      - name:", refusal_pos + 1)]
        for required in (
            "'/VERYSILENT'", "'/SUPPRESSMSGBOXES'", "'/NORESTART'", "'/SP-'", "'/NOICONS'",
            "'/TASKS=!startup'", "WaitForExit(30000)", "Stop-Process -Id $Process.Id -Force",
            "Silent installation requires explicit Microsoft runtime terms acceptance",
            "Get-ItemPropertyValue -LiteralPath $RunKeyPath -Name 'NovaMicSwitch' -ErrorAction SilentlyContinue",
            "Get-NovaMicSwitchRunValue", "$InstallDirectoryCreated = Test-Path -LiteralPath $InstallDirectory",
            "windows-runtime", "TERMS.txt", "manifest.json", "installer-refusal.json",
            "explicit_acceptance_supplied = $false", "interactive_acceptance_tested = $false",
            "successful_installation_claimed = $false", "application_or_audio_tested = $false",
            "redistribution_grant_claimed = $false",
        ):
            self.assertIn(required, refusal_step)
        self.assertNotIn("/ACCEPTMICROSOFTRUNTIMETERMS", refusal_step)
        self.assertNotIn("$Process.WaitForExit()", refusal_step)
        self.assertEqual(refusal_step.count("Get-NovaMicSwitchRunValue"), 3)
        for mutator in ("Set-ItemProperty", "New-ItemProperty", "Remove-ItemProperty", "reg add", "reg delete"):
            self.assertNotIn(mutator, refusal_step)
        self.assertGreaterEqual(refusal_step.count("Test-Path -LiteralPath $InstallDirectory"), 2)
        self.assertIn("if (Test-Path -LiteralPath $RefusalRoot)", refusal_step)
        self.assertIn("Refusing to overwrite installer test log", refusal_step)

        upload_start = windows_job.index("Upload Windows native evidence metadata")
        upload_end = windows_job.index("      - name:", upload_start + 1)
        upload_step = windows_job[upload_start:upload_end]
        path_block = upload_step.split("          path: |\n", 1)[1].split("          if-no-files-found:", 1)[0]
        uploaded_paths = [line.strip() for line in path_block.splitlines() if line.strip()]
        self.assertEqual(uploaded_paths, [
            "build/native-evidence/report.json",
            "build/native-evidence/windows/media-probe.json",
            "build/native-evidence/notices/**/*.txt",
            "build/native-evidence/windows-runtime/TERMS.txt",
            "build/native-evidence/windows-runtime/manifest.json",
            "build/native-evidence/windows-runtime/installer-refusal.json",
        ])

    def test_linux_hidapi_source_build_policy_and_read_only_runtime_proof(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        linux_job = workflow[workflow.index("  linux-x86_64:"):workflow.index("  windows-x64:")]
        windows_job = workflow[workflow.index("  windows-x64:"):workflow.index("  publish-draft:")]
        self.assertIn("    env:\n      UV_NO_BINARY_PACKAGE: hidapi", linux_job)
        self.assertEqual(linux_job.count("UV_NO_BINARY_PACKAGE: hidapi"), 1)
        self.assertNotIn("UV_NO_BINARY_PACKAGE", windows_job)
        self.assertNotIn("HIDAPI_WITH_HIDRAW", workflow)
        self.assertNotIn("HIDAPI_WITH_LIBUSB", workflow)

        apt_index = linux_job.index("Install Linux build and Qt runtime libraries")
        sync_index = linux_job.index("Sync locked Python 3.13 environment")
        proof_index = linux_job.index("Verify Linux hidapi source build and host dependencies")
        self.assertLess(apt_index, sync_index)
        self.assertLess(sync_index, proof_index)
        proof_step = linux_job[proof_index:linux_job.index("      - name:", proof_index + 1)]
        self.assertIn('for name in ("hidraw", "hid")', proof_step)
        self.assertIn('getattr(module, "enumerate", None)', proof_step)
        self.assertIn('getattr(module, "device", None)', proof_step)
        self.assertIn('site_packages.rglob("*")', proof_step)
        self.assertIn('"hidapi.libs"', proof_step)
        self.assertIn('readelf -d "$extension"', proof_step)
        self.assertIn('ldd "$extension"', proof_step)
        self.assertIn("'libusb-1\\.0\\.so\\.0' 'libudev\\.so\\.1'", proof_step)
        self.assertIn('grep -Eq "$dependency => /"', proof_step)
        self.assertIn("build/hidapi-workflow-check.log", proof_step)
        self.assertNotIn(".enumerate(", proof_step)
        self.assertNotIn(".device(", proof_step)

    def test_native_evidence_is_generated_and_uploaded_as_metadata_only(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        for platform, bundle, workpath in (
            ("Linux", "dist/linux/NovaMicSwitch", "build/linux"),
            ("Windows", "dist\\windows\\NovaMicSwitch", "build\\windows"),
        ):
            report_name = f"Generate {platform} native dependency evidence"
            report_start = workflow.index(f"      - name: {report_name}")
            report_end = workflow.index("      - name:", report_start + 1)
            report_step = workflow[report_start:report_end]
            self.assertIn(f"--bundle {bundle}", report_step)
            self.assertIn(f"--workpath {workpath}", report_step)
            self.assertIn("--output-dir build", report_step)

            upload_name = f"Upload {platform} native evidence metadata"
            upload_start = workflow.index(f"      - name: {upload_name}")
            upload_end = workflow.index("      - name:", upload_start + 1)
            upload_step = workflow[upload_start:upload_end]
            self.assertIn("!cancelled() && success() && hashFiles('build/native-evidence/report.json') != ''", upload_step)
            self.assertNotIn("inputs.license_reviewed", upload_step)
            self.assertIn(f"name: native-evidence-{platform.lower()}", upload_step)
            self.assertIn("build/native-evidence/report.json", upload_step)
            self.assertIn("build/native-evidence/notices/**/*.txt", upload_step)
            self.assertNotIn("dist/", upload_step)
            self.assertNotIn("dist\\", upload_step)

        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("packaging/licenses/** -text", attributes)
        docs = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        self.assertIn("With `license_reviewed` false", docs)
        self.assertIn("no downloadable binaries", docs)

    def test_source_workflow_has_separate_metadata_and_review_gated_archive_uploads(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        start = workflow.index("  corresponding-sources:")
        end = workflow.index("  publish-draft:", start)
        job = workflow[start:end]
        self.assertIn("needs: [validate-inputs, linux-x86_64, windows-x64]", job)
        self.assertIn("name: native-evidence-linux", job)
        self.assertIn("if: ${{ inputs.license_reviewed }}", job)
        source_upload = job[job.index("Upload corresponding-source release assets"):]
        self.assertIn("NovaMicSwitch-${{ needs.validate-inputs.outputs.version }}-Corresponding-Sources.tar", source_upload)
        metadata_upload = job[job.index("Upload source evidence metadata"):job.index("Upload corresponding-source release assets")]
        self.assertIn("SOURCE-INDEX.md", metadata_upload)
        self.assertIn("source-bundle-index.json", metadata_upload)
        self.assertNotIn(".tar", metadata_upload)
        self.assertNotIn("dist/release-sources/*", job)
        self.assertIn("sha256sum -c SHA256SUMS", job)

    def test_runtime_fetch_helper_has_no_unpinned_action_or_release_tool_dependency(self):
        helper = (ROOT / "scripts" / "fetch_runtime_sources.py").read_text(encoding="utf-8")
        self.assertIn("MAX_ARCHIVE_BYTES = 100 * 1024 * 1024", helper)
        self.assertIn("TIMEOUT_SECONDS = 45", helper)
        self.assertIn("archive_sha256", helper)
        self.assertIn("archive_bytes", helper)
        self.assertIn("if not fetch:", helper)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("sha256sum"),
                         "requires POSIX bash and sha256sum")
    def test_publisher_verification_stages_flat_assets_and_rejects_bad_inputs(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        step_start = workflow.index("      - name: Verify artifact sets and checksums")
        run_start = workflow.index("        run: |\n", step_start) + len("        run: |\n")
        step_end = workflow.index("      - name:", run_start)
        script = textwrap.dedent(workflow[run_start:step_end])
        self.assertNotIn("gh release", script)
        version = "0.1.0"
        linux = f"NovaMicSwitch-{version}-x86_64.AppImage"
        windows = f"NovaMicSwitch-Setup-{version}.exe"
        source = f"NovaMicSwitch-{version}-Corresponding-Sources"
        names = [linux, f"{linux}.sha256", windows, f"{windows}.sha256",
                 f"{source}.tar", f"{source}.tar.sha256", "SOURCE-INDEX.md", "source-bundle-index.json"]

        def create_fixture(root, *, missing=None, tamper_source_sum=False, corrupt_source=False):
            groups = {
                "linux-x86_64": [linux, f"{linux}.sha256"],
                "windows-x64": [windows, f"{windows}.sha256"],
                "corresponding-sources": [f"{source}.tar", f"{source}.tar.sha256", "SOURCE-INDEX.md", "source-bundle-index.json"],
            }
            for group, files in groups.items():
                directory = root / "release-assets" / group
                directory.mkdir(parents=True)
                for name in files:
                    (directory / name).write_bytes(f"fixture payload for {name}\n".encode())
            for group, binary in (("linux-x86_64", linux), ("windows-x64", windows)):
                directory = root / "release-assets" / group
                sidecar = f"{hashlib.sha256((directory / binary).read_bytes()).hexdigest()}  {binary}\n"
                (directory / f"{binary}.sha256").write_text(sidecar, encoding="ascii")
                (directory / "SHA256SUMS").write_text(sidecar, encoding="ascii")
            directory = root / "release-assets" / "corresponding-sources"
            source_tar = f"{source}.tar"
            sidecar = f"{hashlib.sha256((directory / source_tar).read_bytes()).hexdigest()}  {source_tar}\n"
            (directory / f"{source_tar}.sha256").write_text(sidecar, encoding="ascii")
            records = []
            for name in (source_tar, "SOURCE-INDEX.md", "source-bundle-index.json"):
                digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
                if tamper_source_sum and name == "SOURCE-INDEX.md":
                    digest = "0" * 64
                records.append(f"{digest}  {name}\n")
            (directory / "SHA256SUMS").write_text("".join(records), encoding="ascii")
            if corrupt_source:
                (directory / source_tar).write_bytes(b"tampered archive\n")
            if missing:
                for group, files in groups.items():
                    if missing in files:
                        (root / "release-assets" / group / missing).unlink()

        def run_script(root):
            return subprocess.run(["bash", "-c", script], cwd=root,
                                  env={**os.environ, "RELEASE_VERSION": version},
                                  text=True, capture_output=True, timeout=10)

        temp_dir = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=temp_dir) as temp:
            root = Path(temp)
            create_fixture(root)
            result = run_script(root)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            final = root / "release-assets" / "final"
            self.assertEqual({path.name for path in final.iterdir()}, set(names) | {"SHA256SUMS"})
            lines = (final / "SHA256SUMS").read_text(encoding="ascii").splitlines()
            self.assertEqual(len(lines), 8)
            self.assertEqual({line.split(maxsplit=1)[1] for line in lines}, set(names))
            self.assertTrue(all("/" not in line.split(maxsplit=1)[1] for line in lines))
            verify = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=final,
                                    text=True, capture_output=True, check=False)
            self.assertEqual(verify.returncode, 0, verify.stderr + verify.stdout)

        for options in ({"missing": linux}, {"tamper_source_sum": True}, {"corrupt_source": True}):
            with self.subTest(**options), tempfile.TemporaryDirectory(dir=temp_dir) as temp:
                root = Path(temp)
                create_fixture(root, **options)
                result = run_script(root)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((root / "release-assets" / "final").exists())

    def test_frozen_media_probe_runs_with_bounded_timeouts_and_is_metadata_only(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        linux_job = workflow[workflow.index("  linux-x86_64:"):workflow.index("  windows-x64:")]
        windows_job = workflow[workflow.index("  windows-x64:"):workflow.index("  publish-draft:")]
        self.assertIn('timeout 60s env QT_QPA_PLATFORM=offscreen APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGE" --media-probe --output-json', linux_job)
        self.assertIn("build/native-evidence/linux/media-probe.json", linux_job)
        self.assertIn("--media-probe", windows_job)
        self.assertIn("build/native-evidence/windows/media-probe.json", windows_job)
        self.assertIn("WaitForExit(60000)", windows_job)
        self.assertIn("Stop-Process -Id $Process.Id", windows_job)
        self.assertIn("$env:PATH = $System32", windows_job)
        self.assertIn("build/native-evidence/linux/media-probe.json", linux_job)
        self.assertIn("build/native-evidence/windows/media-probe.json", windows_job)
        self.assertIn("license_reviewed:", workflow)
        self.assertIn("default: false", workflow)
        self.assertEqual(workflow.count("if: ${{ inputs.license_reviewed }}"), 3)
        for upload in (linux_job, windows_job):
            self.assertIn("build/native-evidence/report.json", upload)
            self.assertIn("build/native-evidence/notices/**/*.txt", upload)
            self.assertNotIn("build/native-evidence/**/silent.wav", upload)


if __name__ == "__main__":
    unittest.main()
