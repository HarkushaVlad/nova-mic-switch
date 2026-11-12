import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


@unittest.skipUnless(PWSH, "PowerShell 7 is not installed")
class InnoEngineVersionTests(unittest.TestCase):
    def test_engine_banner_parser_accepts_only_one_exact_pinned_banner(self):
        powershell = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:BUILD_WINDOWS_SCRIPT, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw "PowerShell parse failed: $parseErrors" }
$functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-InnoEngineVersion'
}, $true)
if (-not $functionAst) { throw 'Get-InnoEngineVersion function was not found.' }
Invoke-Expression $functionAst.Extent.Text

if ((Get-InnoEngineVersion -Output @('Compiler engine version: Inno Setup 6.7.4  ')) -cne '6.7.4') {
    throw 'The exact pinned compiler engine banner was not accepted.'
}
function Assert-Rejected([string[]]$Lines) {
    $rejected = $false
    try { Get-InnoEngineVersion -Output $Lines | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw "Unexpectedly accepted compiler output: $Lines" }
}
Assert-Rejected -Lines @()
Assert-Rejected -Lines @('Compiler engine version: Inno Setup 6.7.5')
Assert-Rejected -Lines @('Compiler engine version: Inno Setup 6.7.4.1')
Assert-Rejected -Lines @('Compiler engine version: Inno Setup 6.7.40')
Assert-Rejected -Lines @('Untrusted prefix Compiler engine version: Inno Setup 6.7.4')
Assert-Rejected -Lines @('Compiler engine version: Inno Setup 6.7.4', 'Compiler engine version: Inno Setup 6.7.4')
"""
        environment = os.environ.copy()
        environment["BUILD_WINDOWS_SCRIPT"] = str(ROOT / "scripts" / "build_windows.ps1")
        result = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", powershell],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout}\nstderr={result.stderr}")


if __name__ == "__main__":
    unittest.main()
