# Runs the Codex command-line program with the arguments given.
#
# Codex is installed here as the desktop app, which keeps its command-line
# program in a folder whose name changes with each update and is not on PATH.
# This finds the newest copy. A `codex` on PATH (the npm install) wins if present.
#
#   scripts/codex.ps1 review --base main
#   "the task" | scripts/codex.ps1 exec -s read-only -C <folder> -
#
# Pass a prompt on stdin (the trailing `-`) rather than as an argument, so that
# quotes in it survive.

$ErrorActionPreference = 'Stop'

$onPath = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($onPath) {
    $codex = $onPath.Source
} else {
    $bin = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $found = Get-ChildItem -Path $bin -Filter codex.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $found) {
        Write-Error "Codex was not found on PATH or under $bin. Install the Codex app or run: npm install -g @openai/codex"
        exit 127
    }
    $codex = $found.FullName
}

if ($MyInvocation.ExpectingInput) {
    $input | & $codex @args
} else {
    & $codex @args
}
exit $LASTEXITCODE
