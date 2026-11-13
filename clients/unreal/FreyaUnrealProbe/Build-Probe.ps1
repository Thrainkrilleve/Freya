param(
    [string]$EngineRoot = 'D:\UE_5.8',
    [ValidateSet('Editor','Game')][string]$Target = 'Editor'
)
$ErrorActionPreference = 'Stop'
$projectFile = Join-Path $PSScriptRoot 'FreyaUnrealProbe.uproject'
$buildScript = Join-Path $EngineRoot 'Engine\Build\BatchFiles\Build.bat'
if (-not (Test-Path -LiteralPath $buildScript)) { throw "Build.bat not found at $buildScript" }
$targetName = if ($Target -eq 'Editor') { 'FreyaUnrealProbeEditor' } else { 'FreyaUnrealProbe' }
$priorUbaRoot = $env:UBA_ROOT
try {
    $env:UBA_ROOT = Join-Path $PSScriptRoot 'Saved\BuildCache'
    & $buildScript $targetName Win64 Development "-Project=$projectFile" -WaitMutex -NoHotReload -MaxParallelActions=4
    if ($LASTEXITCODE -ne 0) { throw "Build failed, exit code $LASTEXITCODE. Keep the build output for diagnosis." }
} finally {
    $env:UBA_ROOT = $priorUbaRoot
}
