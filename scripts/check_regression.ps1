<#
.SYNOPSIS
    Regression gate for the ppsspp-dfx-mcp package.

.DESCRIPTION
    Runs the project's canonical full-suite scope (the default `testpaths`,
    i.e. `tests` + `evals`) and asserts the passed / skipped counts.

    TWO MODES -- the numbers are NOT interchangeable:

      static (default, CI-safe)   -> 2475 passed, 0 failed, 45 skipped
          The real-device environment variables are NOT set, so the
          device-gated integration tests skip deterministically. This mirrors
          CI, which never sets those variables.
          Authoritative measurement (this gate's OWN interpreter, so the
          asserted numbers and the execution path share one source):
              mcp_test_report/derived/gate-static-measure.json
          Cross-check, same scope but the other interpreter:
              mcp_test_report/derived/regression-010-post.json (schema
              `regression-run/2`) -- 2407 / 42 / 0 is the T004-era snapshot;
              specs/010 added guard tests since (rebases: 2026-10-04a
              -> 2442/42 after US4 guards; 2026-10-04b -> 2447/45 after
              T037/T038 added three device-gated skips).

      real (-Real)                -> REPORTED, NOT ASSERTED
          Sets PPSSPP_DFX_TEST_EXE_PATH / _ISO_PATH so the device tests
          actually run. Measured 2026-10-02 at 2155 passed / 4 skipped, but
          that same run produced 2 failures + 20 errors which PASS in
          isolation -- a load-dependent flake whose root cause is not yet
          captured. Until it is captured and the counts are re-measured,
          -Real reports the numbers and does NOT fail on them
          (specs/010 FR-038: never assert a count the mode cannot back).

    DEFECTS FIXED HERE (specs/010 N-8):
      1. The script used to unconditionally set the real-device environment
         variables while asserting the *static* numbers, so it could never
         pass whenever a PPSSPP binary and an ISO were present. Verified on
         the same 12 tests:  env unset -> "12 skipped", env set -> "12 errors".
         The skip count flips with the mode.
      2. It also ran pytest against the host temp root. Once that tree grows
         past the host's bulk-delete guard threshold, pytest is killed
         mid-run and the summary line is swallowed -- which this gate would
         have reported as a failure. The temp root is now redirected into the
         repo, same as mcp_test_report/tools/run_full_suite.py (that script's
         docstring documents the failure mode in full).

.PARAMETER Real
    Run in real-machine mode (sets the device environment variables).
    Counts are reported but not asserted -- see above.

.PARAMETER ExpectedPassed
    Minimum passing tests in the asserted mode. The authoritative value lives
    in `param(...)` below; this sentence mirrors it. When they diverge, the
    `param(...)` value wins and this line is the bug (specs/008 T003 rule:
    one number, one source).

.PARAMETER ExpectedSkipped
    Exact skipped count in the asserted mode. Mirrors `param(...)`.

.PARAMETER ExpectedDeselected
    Exact deselected count. Default 0 -- the gate asserts that nothing is
    being quietly excluded.

.EXAMPLE
    pwsh -File scripts/check_regression.ps1
    pwsh -File scripts/check_regression.ps1 -Real
#>
[CmdletBinding()]
param(
    [switch]$Real,
    [int]$ExpectedPassed = 2475,
    [int]$ExpectedSkipped = 45,
    [int]$ExpectedDeselected = 0
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..' '..' '..')
$pkgRoot  = Join-Path $repoRoot 'mcps\ppsspp-dfx-mcp'
$python   = Join-Path $pkgRoot '.venv-test\Scripts\python.exe'

# Keep pytest's temp tree inside the repo: a temp root that outgrows the host's
# bulk-delete guard gets the run killed and the summary swallowed.
$tempRoot = Join-Path $repoRoot '.tmp\sys-temp\gate-run'
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

function Resolve-FirstExisting {
    param([string[]]$Candidates)
    foreach ($c in $Candidates) {
        if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path }
    }
    return $null
}

if (-not (Test-Path $python)) { throw "test python not found: $python" }

if ($Real) {
    $ppsspp = Resolve-FirstExisting @(
        $(if ($env:PPSSPP_DFX_TEST_EXE_PATH) { $env:PPSSPP_DFX_TEST_EXE_PATH }),
        (Join-Path $repoRoot 'tools\ppsspp_dev\PPSSPPWindows64.exe'),
        (Join-Path $repoRoot 'open_source\ppsspp\PPSSPPWindows64.exe')
    )
    $iso = Resolve-FirstExisting @(
        $(if ($env:PPSSPP_DFX_TEST_ISO_PATH) { $env:PPSSPP_DFX_TEST_ISO_PATH }),
        (Join-Path $repoRoot 'open_source\ppsspp\pspautotests\demos\cube.iso')
    )
    if (-not $ppsspp) { throw 'PPSSPP binary not found; set PPSSPP_DFX_TEST_EXE_PATH' }
    if (-not $iso)    { throw 'smoke ISO not found; set PPSSPP_DFX_TEST_ISO_PATH' }
    $env:PPSSPP_DFX_EXE_PATH      = $ppsspp
    $env:PPSSPP_DFX_TEST_EXE_PATH = $ppsspp
    $env:PPSSPP_DFX_TEST_ISO_PATH = $iso
    Write-Host "mode   : REAL (counts reported, not asserted)"
    Write-Host "ppsspp : $ppsspp"
    Write-Host "iso    : $iso"
} else {
    # Static mode MUST actively clear the variables: a value inherited from the
    # calling shell would silently switch the skip profile underneath us.
    Remove-Item Env:PPSSPP_DFX_TEST_EXE_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:PPSSPP_DFX_TEST_EXE -ErrorAction SilentlyContinue
    Remove-Item Env:PPSSPP_DFX_TEST_ISO_PATH -ErrorAction SilentlyContinue
    $env:PPSSPP_DFX_EXE_PATH = $null
    Write-Host "mode   : STATIC (device-gated tests skip; mirrors CI)"
}

Write-Host "python : $python"
Write-Host "temp   : $tempRoot"
Write-Host ''

# Scope: no path argument -> the default `testpaths` from pyproject.toml
# (`tests` + `evals`), which is the project's canonical full-suite scope.
$saved = @{}
foreach ($k in @('TEMP', 'TMP', 'TMPDIR')) { $saved[$k] = [Environment]::GetEnvironmentVariable($k) }
$env:TEMP = $tempRoot
$env:TMP  = $tempRoot
$env:TMPDIR = $tempRoot
Push-Location $pkgRoot
try {
    Write-Host 'scope  : default testpaths (tests + evals); nothing deselected'
    $out = & $python -m pytest -q --no-header -p no:cacheprovider 2>&1
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    foreach ($k in @('TEMP', 'TMP', 'TMPDIR')) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
}

$text = ($out | Out-String)
$summary = ($text -split "`n" | Where-Object { $_ -match '\d+ (passed|failed).*in ' } | Select-Object -Last 1)
Write-Host $summary
Write-Host ''

if ($text -notmatch '(\d+)\s+passed|\d+\s+failed') {
    Write-Host 'FAIL: no summary line captured -- the run is not evidence.'
    ($text -split "`n" | Select-Object -Last 40) | ForEach-Object { Write-Host $_ }
    exit 1
}

if ($text -match '(\d+)\s+passed')  { $passed  = [int]$Matches[1] } else { $passed  = 0 }
if ($text -match '(\d+)\s+skipped') { $skipped = [int]$Matches[1] } else { $skipped = 0 }
if ($text -match '(\d+)\s+failed')  { $failed  = [int]$Matches[1] } else { $failed  = 0 }
if ($text -match '(\d+)\s+error')   { $errors  = [int]$Matches[1] } else { $errors  = 0 }
if ($text -match '(\d+)\s+deselected') { $desel = [int]$Matches[1] } else { $desel = 0 }

$fail = $false

# Mode-independent assertions: a green run must be green in EITHER mode.
if ($code -ne 0)   { Write-Host "FAIL: pytest exit code $code"; $fail = $true }
if ($errors -gt 0) { Write-Host "FAIL: $errors error(s)";       $fail = $true }
if ($failed -gt 0) { Write-Host "FAIL: $failed test(s) failed"; $fail = $true }
if ($desel -ne $ExpectedDeselected) {
    Write-Host "FAIL: deselected $desel != expected $ExpectedDeselected"
    Write-Host '      Deselection must stay visible so the suite cannot silently shrink.'
    $fail = $true
}

if ($Real) {
    # FR-038: do not assert counts this mode cannot back yet.
    Write-Host ("REPORTED (not asserted): passed={0} skipped={1} failed={2} errors={3} (real-machine mode)" -f $passed, $skipped, $failed, $errors)
    if ($passed -lt $ExpectedPassed -or $skipped -ne $ExpectedSkipped) {
        Write-Host ("      note   : differs from the STATIC baseline {0}/{1} -- expected; the two modes are not interchangeable" -f $ExpectedPassed, $ExpectedSkipped)
    }
    Write-Host '      status : real-machine counts are NOT asserted until the load-dependent'
    Write-Host '               flake is root-caused and the counts re-measured (specs/010 FR-038).'
} else {
    if ($passed -lt $ExpectedPassed) {
        Write-Host "FAIL: passed $passed < expected $ExpectedPassed (zero-regression gate, SC-015)"
        Write-Host '      baseline source: mcp_test_report/derived/gate-static-measure.json'
        $fail = $true
    }
    if ($skipped -ne $ExpectedSkipped) {
        Write-Host "FAIL: skipped $skipped != expected $ExpectedSkipped"
        Write-Host '      In STATIC mode the device-gated tests skip deterministically.'
        Write-Host '      A different count means either the mode leaked (an inherited env'
        Write-Host '      var switched it on) or the gated set changed. Both need review.'
        $fail = $true
    }
}

Write-Host ''
Write-Host ("passed={0} skipped={1} failed={2} errors={3} mode={4}" -f $passed, $skipped, $failed, $errors, $(if ($Real) { 'real' } else { 'static' }))

if ($fail) {
    Write-Host ''
    Write-Host '--- last 40 lines ---'
    ($text -split "`n" | Select-Object -Last 40) | ForEach-Object { Write-Host $_ }
    exit 1
}

Write-Host 'OK: regression gate passed'
exit 0
