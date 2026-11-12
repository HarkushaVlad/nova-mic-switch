import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "packaging" / "windows" / "runtime_terms.py"
SPEC = importlib.util.spec_from_file_location("nova_windows_runtime_terms_test", HELPER_PATH)
RUNTIME_TERMS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME_TERMS)


class RuntimeTermsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.python_base = self.root / "Python"
        self.venv = self.root / ".venv"
        self.site_packages = self.venv / "Lib" / "site-packages"
        self.python_base.mkdir()
        self.site_packages.mkdir(parents=True)
        self.source = self.python_base / "vcruntime140.dll"
        self.source_bytes = b"synthetic runtime fixture bytes"
        self.source.write_bytes(self.source_bytes)
        self.template = self.root / "MICROSOFT-RUNTIME-TERMS.txt"
        self.template_bytes = b"Fixture terms supplied by parent template fixture.\n"
        self.template.write_bytes(self.template_bytes)
        self.evidence = self.root / "evidence.json"
        self.output = self.root / "output"
        self.destination = "vcruntime140.dll"
        self.toc_rows = [(self.destination, str(self.source), "BINARY")]
        self.evidence.write_text(json.dumps([self.record()]), encoding="utf-8")

    def record(self, **overrides):
        record = {
            "destination": self.destination,
            "source": str(self.source),
            "sha256": hashlib.sha256(self.source_bytes).hexdigest(),
            "company": "Microsoft Corporation",
            "file_version": "14.40.33810.0",
            "legal_copyright": "Copyright (c) Microsoft Corporation.\nFixture copyright line.",
            "is_debug": False,
            "is_pre_release": False,
        }
        record.update(overrides)
        return record

    def collect(self, **overrides):
        values = {
            "toc_rows": self.toc_rows,
            "evidence_path": self.evidence,
            "template_path": self.template,
            "output_dir": self.output,
            "python_base": self.python_base,
            "venv_root": self.venv,
        }
        values.update(overrides)
        return RUNTIME_TERMS.collect_microsoft_runtime_terms(**values)

    def write_evidence(self, records):
        self.evidence.write_text(json.dumps(records), encoding="utf-8")

    def test_collects_verbatim_template_and_runtime_provenance_facts(self):
        result = self.collect()
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        terms_path = self.output / "TERMS.txt"
        terms_bytes = terms_path.read_bytes()
        text = terms_bytes.decode("utf-8")
        self.assertTrue(terms_bytes.startswith(self.template_bytes))
        self.assertEqual(manifest["template"]["sha256"], hashlib.sha256(self.template_bytes).hexdigest())
        self.assertEqual(manifest["terms"]["path"], "licenses/windows-runtime/TERMS.txt")
        self.assertEqual(manifest["runtime_files"][0]["source"], "CPython/vcruntime140.dll")
        self.assertIs(manifest["runtime_files"][0]["is_debug"], False)
        self.assertIs(manifest["runtime_files"][0]["is_pre_release"], False)
        self.assertIn("Fixture terms supplied by parent template fixture.", text)
        self.assertIn("14.40.33810.0", text)
        self.assertIn("Copyright (c) Microsoft Corporation.\nFixture copyright line.", text)
        self.assertIn("not a separate grant", text)
        self.assertNotIn(str(self.root), text)

    def test_missing_mismatched_and_invalid_evidence_fail_closed(self):
        self.write_evidence([])
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "Missing exact native evidence"):
            self.collect()
        self.write_evidence([self.record(sha256="0" * 64)])
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "hash does not match"):
            self.collect()
        self.write_evidence([self.record(destination="other.dll")])
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "Missing exact native evidence"):
            self.collect()

    def test_unknown_debug_prerelease_and_missing_metadata_fail_closed(self):
        self.toc_rows.append(("msvcp140_custom.dll", str(self.source), "BINARY"))
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "Unrecognized retained"):
            self.collect()
        self.toc_rows.pop()
        for overrides, message in (
            ({"is_debug": True}, "debug/pre-release"),
            ({"is_pre_release": True}, "debug/pre-release"),
            ({"is_debug": None}, "debug/pre-release"),
            ({"company": None}, "company"),
            ({"legal_copyright": ""}, "copyright"),
            ({"file_version": None}, "version"),
        ):
            self.write_evidence([self.record(**overrides)])
            with self.subTest(overrides=overrides), self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, message):
                self.collect()

    def test_runtime_sources_must_be_under_python_or_venv_site_packages(self):
        foreign = self.root / "foreign" / "vcruntime140.dll"
        foreign.parent.mkdir()
        foreign.write_bytes(self.source_bytes)
        self.toc_rows = [(self.destination, str(foreign), "BINARY")]
        self.write_evidence([self.record(source=str(foreign))])
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "outside the approved Python roots"):
            self.collect()

    def test_unsafe_destination_and_source_symlink_are_rejected(self):
        self.toc_rows = [("../vcruntime140.dll", str(self.source), "BINARY")]
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "Unsafe runtime bundle destination"):
            self.collect()
        self.toc_rows = [(self.destination, str(self.source), "BINARY")]
        if hasattr(Path, "symlink_to"):
            linked = self.python_base / "linked.dll"
            try:
                linked.symlink_to(self.source)
            except OSError:
                self.skipTest("File symlinks are unavailable")
            self.toc_rows = [(self.destination, str(linked), "BINARY")]
            with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "symlink"):
                self.collect()

    def test_template_missing_placeholder_and_unowned_output_fail(self):
        self.template.unlink()
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "Could not read"):
            self.collect()
        self.template.write_bytes(self.template_bytes)
        self.template.write_text("TODO final terms", encoding="utf-8")
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "placeholder"):
            self.collect()
        self.template.write_bytes(self.template_bytes)
        self.output.mkdir()
        (self.output / "TERMS.txt").write_text("foreign", encoding="utf-8")
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "unowned"):
            self.collect()

    def test_output_symlink_is_rejected_without_touching_target(self):
        target = self.root / "unrelated-output"
        target.mkdir()
        output_link = self.root / "output-link"
        try:
            output_link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable")
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "symlink"):
            self.collect(output_dir=output_link)
        self.assertEqual(list(target.iterdir()), [])

    def test_repeat_generation_requires_owned_untampered_output(self):
        first = self.collect()
        first_bytes = (self.output / "TERMS.txt").read_bytes()
        second = self.collect()
        self.assertEqual(first_bytes, (self.output / "TERMS.txt").read_bytes())
        self.assertEqual(first["manifest"], second["manifest"])
        (self.output / "TERMS.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "tampered"):
            self.collect()

    def test_verifier_accepts_matching_bundle_and_rejects_missing_or_tampered_dll(self):
        self.collect()
        manifest = self.output / "manifest.json"
        bundle = self.root / "bundle"
        internal = bundle / "_internal" / "licenses" / "windows-runtime"
        internal.mkdir(parents=True)
        (internal / "manifest.json").write_bytes(manifest.read_bytes())
        (internal / "TERMS.txt").write_bytes((self.output / "TERMS.txt").read_bytes())
        (bundle / "_internal" / self.destination).write_bytes(self.source_bytes)
        result = RUNTIME_TERMS.verify_runtime_terms(bundle=bundle, manifest_path=manifest)
        self.assertEqual(result, {"verified_files": 3, "runtime_files": 1})
        (internal / "manifest.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "differs"):
            RUNTIME_TERMS.verify_runtime_terms(bundle=bundle, manifest_path=manifest)
        (internal / "manifest.json").write_bytes(manifest.read_bytes())
        (bundle / "_internal" / self.destination).write_bytes(b"modified");
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "modified"):
            RUNTIME_TERMS.verify_runtime_terms(bundle=bundle, manifest_path=manifest)
        (bundle / "_internal" / self.destination).unlink()
        with self.assertRaisesRegex(RUNTIME_TERMS.RuntimeTermsError, "missing|Could not read"):
            RUNTIME_TERMS.verify_runtime_terms(bundle=bundle, manifest_path=manifest)


if __name__ == "__main__":
    unittest.main()
