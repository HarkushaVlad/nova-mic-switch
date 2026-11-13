import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def extract_powershell_function(source: str, function_name: str) -> str:
    match = re.search(rf"(?m)^\s*function\s+{re.escape(function_name)}\s*\{{", source)
    if match is None:
        raise AssertionError(f"Workflow helper function not found: {function_name}")
    opening = source.index("{", match.start())
    depth = 0
    for position in range(opening, len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return textwrap.dedent(source[match.start():position + 1])
    raise AssertionError(f"Unterminated workflow helper function: {function_name}")


class InstallerRefusalPowerShellTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is not installed")
    def test_run_value_snapshot_handles_absence_and_propagates_access_denied(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        helper = extract_powershell_function(workflow, "Get-NovaMicSwitchRunValue")
        self.assertNotIn("Get-ItemPropertyValue", helper)
        self.assertNotIn("SilentlyContinue", helper)

        script = f"""
$RunKeyPath = 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run'
$script:FixtureMode = ''
$script:Calls = 0
$script:SeenPath = $null
$script:SeenNameBound = $false
$script:SeenErrorAction = $null
function Get-ItemProperty {{
  param([string]$LiteralPath, [string]$Name, [string]$ErrorAction)
  $script:Calls++
  $script:SeenPath = $LiteralPath
  $script:SeenNameBound = $PSBoundParameters.ContainsKey('Name')
  $script:SeenErrorAction = $ErrorAction
  if ($script:FixtureMode -eq 'missing-key') {{
    throw [System.Management.Automation.ItemNotFoundException]::new('fixture missing key')
  }}
  if ($script:FixtureMode -eq 'access-denied') {{
    throw [System.UnauthorizedAccessException]::new('fixture access denied')
  }}
  if ($script:FixtureMode -eq 'present') {{
    return [pscustomobject]@{{ NovaMicSwitch = '"C:\\Apps\\NovaMicSwitch.exe" --tray'; OtherValue = 'untouched' }}
  }}
  return [pscustomobject]@{{ OtherValue = 'present key, absent value' }}
}}
function Get-ItemPropertyValue {{ throw 'Unexpected Get-ItemPropertyValue call' }}
function Set-ItemProperty {{ throw 'Registry writes are forbidden in this test' }}
function New-ItemProperty {{ throw 'Registry writes are forbidden in this test' }}
function Remove-ItemProperty {{ throw 'Registry writes are forbidden in this test' }}
{helper}
function Assert-ReadContract {{
  if ($script:Calls -ne 1) {{ throw "Expected one mocked property read, got $script:Calls" }}
  if ($script:SeenPath -cne $RunKeyPath) {{ throw "Unexpected registry path: $script:SeenPath" }}
  if ($script:SeenNameBound) {{ throw 'The helper must query all properties without -Name.' }}
  if ($script:SeenErrorAction -cne 'Stop') {{ throw "Unexpected ErrorAction: $script:SeenErrorAction" }}
}}

$script:FixtureMode = 'present'
$script:Calls = 0
$value = Get-NovaMicSwitchRunValue
Assert-ReadContract
if (-not $value.Present -or $value.Value -cne '"C:\\Apps\\NovaMicSwitch.exe" --tray') {{ throw 'Existing REG_SZ value was not returned exactly.' }}

$script:FixtureMode = 'missing-value'
$script:Calls = 0
$value = Get-NovaMicSwitchRunValue
Assert-ReadContract
if ($value.Present -or $null -ne $value.Value) {{ throw 'A present key without this value must report absence.' }}

$script:FixtureMode = 'missing-key'
$script:Calls = 0
$value = Get-NovaMicSwitchRunValue
Assert-ReadContract
if ($value.Present -or $null -ne $value.Value) {{ throw 'A missing Run key must report absence.' }}

$script:FixtureMode = 'access-denied'
$script:Calls = 0
$deniedPropagated = $false
try {{
  Get-NovaMicSwitchRunValue | Out-Null
}} catch [System.UnauthorizedAccessException] {{
  $deniedPropagated = $true
}}
Assert-ReadContract
if (-not $deniedPropagated) {{ throw 'Access denied must propagate rather than be treated as an absent value.' }}
Write-Output 'Installer refusal registry snapshot helper tests passed.'
"""
        with tempfile.TemporaryDirectory(prefix="nova-installer-refusal-pwsh-") as directory:
            script_path = Path(directory) / "test-run-value.ps1"
            script_path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Installer refusal registry snapshot helper tests passed.", result.stdout)


if __name__ == "__main__":
    unittest.main()
