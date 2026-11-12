"""Narrow Windows bundle policy for native libraries and provenance."""

import hashlib
import json
import ntpath
import os
from pathlib import Path
import subprocess


OPENSSL_DLLS = {"libcrypto-3-x64.dll", "libssl-3-x64.dll"}
QT_OPENSSL_PLUGIN = "qopensslbackend.dll"
QT_SCHANNEL_PLUGIN = "qschannelbackend.dll"
OS_PROVIDED_DLLS = ("api-ms-win-", "ext-ms-win-")
NATIVE_TYPES = {"BINARY", "EXTENSION", "EXECUTABLE"}
MICROSOFT_RUNTIME_DLLS = {
    "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
    "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "concrt140.dll",
}


def canonical_windows_path(path):
    """Canonicalize Windows paths, resolving existing junctions on Windows."""
    value = str(path)
    if os.name == "nt":
        value = os.path.realpath(value)
    value = ntpath.normpath(ntpath.abspath(value))
    return value.casefold()


def _within(path, root):
    path, root = canonical_windows_path(path), canonical_windows_path(root)
    return path == root or path.startswith(root.rstrip("\\") + "\\")


def _pe_imports(filename):
    import pefile

    try:
        pe = pefile.PE(str(filename), fast_load=True)
    except (OSError, pefile.PEFormatError) as error:
        raise RuntimeError(f"Could not parse retained native PE imports: {filename}: {error}") from error
    try:
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
        ])
        names = set()
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", ()):
            names.add(entry.dll.decode("ascii", "ignore").casefold())
        for entry in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", ()):
            names.add(entry.dll.decode("ascii", "ignore").casefold())
    except (AttributeError, pefile.PEFormatError, OSError) as error:
        raise RuntimeError(f"Could not parse retained native PE imports: {filename}: {error}") from error
    finally:
        pe.close()
    return names


def _native(row):
    return len(row) > 2 and str(row[2]).upper() in NATIVE_TYPES


def _basename(row):
    return ntpath.basename(str(row[0])).casefold()


def apply_windows_native_policy(analysis, project_root, venv_root, python_base):
    """Filter optional/global native payload and validate remaining origins."""
    binaries = list(analysis.binaries)
    qopenssl_present = any(_basename(row) == QT_OPENSSL_PLUGIN for row in binaries)
    schannel_present = any(_basename(row) == QT_SCHANNEL_PLUGIN for row in binaries)
    drop_names = OPENSSL_DLLS | {"ucrtbase.dll"}
    retained = [
        row for row in binaries
        if _basename(row) != QT_OPENSSL_PLUGIN
        and _basename(row) not in drop_names
        and not _basename(row).startswith(OS_PROVIDED_DLLS)
    ]
    if qopenssl_present and not schannel_present:
        raise RuntimeError("Qt TLS backend is present but qschannelbackend.dll is missing")

    retained_paths = [str(row[1]) for row in retained if _native(row)]
    imported = set()
    for filename in retained_paths:
        if not os.path.isfile(filename):
            raise RuntimeError(f"Retained native payload is missing: {filename}")
        imported.update(_pe_imports(filename))
    required = imported.intersection(OPENSSL_DLLS)
    if required:
        raise RuntimeError("Retained Windows native dependency requires external OpenSSL DLL(s): " + ", ".join(sorted(required)))
    analysis.binaries = retained
    analysis.datas = [
        row for row in analysis.datas
        if _basename(row) != QT_OPENSSL_PLUGIN
        and _basename(row) not in drop_names
        and not _basename(row).startswith(OS_PROVIDED_DLLS)
    ]

    roots = [project_root, venv_root, python_base]
    for row in retained:
        if not _native(row):
            continue
        source = str(row[1])
        resolved_source = canonical_windows_path(source)
        if not any(_within(resolved_source, root) for root in roots):
            raise RuntimeError(f"Unapproved native payload origin: destination={row[0]!s}, origin={source}")
        name = _basename(row)
        if name.startswith(OS_PROVIDED_DLLS) or name == "ucrtbase.dll":
            raise RuntimeError(f"OS-provided Windows DLL was bundled: destination={row[0]!s}, origin={source}")

    _write_evidence(retained, project_root)


def _write_evidence(rows, project_root):
    selected = {
        "libcrypto-3.dll", "libcrypto-3-x64.dll", "libssl-3.dll", "libssl-3-x64.dll",
        "libffi-8.dll", "opengl32sw.dll", "mesa.dll", *MICROSOFT_RUNTIME_DLLS,
    }
    evidence = []
    for row in rows:
        source = str(row[1])
        name = _basename(row)
        if name not in selected and not (name.startswith("opengl32sw") or name.startswith("mesa")):
            continue
        if not os.path.isfile(source):
            raise RuntimeError(f"Selected native evidence file is missing: {source}")
        import pefile

        try:
            pe = pefile.PE(source, fast_load=True)
            try:
                machine = pe.FILE_HEADER.Machine
            finally:
                pe.close()
        except (OSError, pefile.PEFormatError) as error:
            raise RuntimeError(f"Could not inspect native evidence PE: {source}: {error}") from error
        try:
            powershell = os.path.join(os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            child_env = os.environ.copy()
            child_env["NOVA_DLL_EVIDENCE_PATH"] = os.path.abspath(source)
            metadata_command = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "(Get-Item -LiteralPath $env:NOVA_DLL_EVIDENCE_PATH).VersionInfo | ConvertTo-Json -Compress"
            )
            command = [powershell, "-NoProfile", "-NonInteractive", "-Command", metadata_command]
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="strict",
                check=True, timeout=10, env=child_env,
            )
            metadata = json.loads(result.stdout)
            if not isinstance(metadata, dict):
                raise ValueError("PowerShell version metadata was not an object")
        except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            raise RuntimeError(f"Could not read native DLL version metadata: {source}: {error}") from error
        item = {
            "destination": str(row[0]), "source": os.path.abspath(source),
            "sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            "machine": f"0x{machine:04x}",
            "company": metadata.get("CompanyName"), "product": metadata.get("ProductName"),
            "file_version": metadata.get("FileVersion"),
            "legal_copyright": metadata.get("LegalCopyright"),
            "is_debug": metadata.get("IsDebug"),
            "is_pre_release": metadata.get("IsPreRelease"),
        }
        evidence.append(item)
        print(json.dumps(item, sort_keys=True, ensure_ascii=False), flush=True)
    output = Path(project_root) / "build" / "native-evidence-windows.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
