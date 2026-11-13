# Gives Google's Antigravity command-line program (agy) one task and prints its answer.
#
#   "the question" | scripts/agy.ps1 -C <worktree>          (asked to read and advise only)
#   "the task"     | scripts/agy.ps1 -C <worktree> -Edit    (may edit files there)
#
# Without -Edit agy is only asked not to change files: in a test on 1.3.3 its plan
# mode still wrote one. So this compares `git status` before and after and exits 3,
# naming the files, if anything changed.
#
# The task goes in on stdin, so quotes in it survive; agy's own -p flag takes the
# prompt as an argument and does not read stdin, so this uses its stream format.
#
# agy reads and writes files in the folder it is started in without asking, and
# everything it reads is sent to Google. So -C is required and a folder holding
# `data` is refused: start it in a git worktree, which has only committed files.
# Shell commands stay refused (agy cannot ask in this mode and nothing here allows
# them), so it cannot run the tests; run them yourself on what it wrote.

param(
    [string]$C,
    [switch]$Edit,
    [string]$Model,
    [string]$Effort
)

$ErrorActionPreference = 'Stop'

$onPath = Get-Command agy -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($onPath) {
    $agy = $onPath.Source
} else {
    $agy = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
    if (-not (Test-Path $agy)) {
        Write-Error "Antigravity CLI was not found on PATH or at $agy. Install it from https://antigravity.google/docs/cli/install/"
        exit 127
    }
}

if (-not $C) {
    Write-Error 'Name the folder to work in with -C.'
    exit 2
}
if (-not (Test-Path $C -PathType Container)) {
    Write-Error "No such folder: $C"
    exit 2
}
$folder = (Resolve-Path $C).Path
if (Test-Path (Join-Path $folder 'data')) {
    Write-Error "$folder holds a data folder, which must not be sent to Google. Use a git worktree."
    exit 2
}

$task = (@($input) -join "`n").Trim()
if (-not $task) {
    Write-Error 'Pass the task on stdin.'
    exit 2
}

# Without being told, agy reaches for a shell command, is refused, and the whole run
# ends with no answer.
$standing = 'Shell commands are refused here and a refused one ends the run with no answer, so run none: use only your file reading and search tools.'
if ($Edit) {
    $standing += ' You may edit files in this folder.'
} else {
    $standing += ' Change no files; give your answer as text.'
}
$task = "$standing`n`n$task"

$mode = if ($Edit) { 'accept-edits' } else { 'plan' }
$arguments = "--mode $mode --input-format stream-json --output-format stream-json"
foreach ($pair in @(@('--model', $Model), @('--effort', $Effort))) {
    if ($pair[1]) {
        if ($pair[1] -notmatch '^[A-Za-z0-9._-]+$') {
            Write-Error "Not a valid value for $($pair[0]): $($pair[1])"
            exit 2
        }
        $arguments += " $($pair[0]) $($pair[1])"
    }
}

# The state of the checkout: git's status, and the contents (by hash) of every file
# that differs from the last commit, so a second edit to such a file shows too.
function Get-Changes {
    $lines = @(& git -C $folder status --porcelain --untracked-files=all 2>$null)
    if ($LASTEXITCODE -ne 0) { return $null }
    $paths = @(& git -C $folder -c core.quotepath=off ls-files --modified --others --exclude-standard 2>$null)
    foreach ($path in ($paths | Sort-Object -Unique)) {
        $full = Join-Path $folder $path
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            $lines += "$((Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash) $path"
        }
    }
    return $lines -join "`n"
}
$before = if ($Edit) { $null } else { Get-Changes }

$utf8 = New-Object System.Text.UTF8Encoding $false
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = $agy
$start.Arguments = $arguments
$start.WorkingDirectory = $folder
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardInput = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.StandardOutputEncoding = $utf8
$start.StandardErrorEncoding = $utf8

# Windows PowerShell opens a program's stdin in the console's encoding and, when that
# is UTF-8, writes a byte-order mark into it first, which agy rejects as bad JSON.
$consoleEncoding = [Console]::InputEncoding
[Console]::InputEncoding = $utf8
try {
    $process = [System.Diagnostics.Process]::Start($start)
} finally {
    [Console]::InputEncoding = $consoleEncoding
}
$stdout = $process.StandardOutput.ReadToEndAsync()
$stderr = $process.StandardError.ReadToEndAsync()

$message = @{ event = 'user'; message = @{ content = $task } } | ConvertTo-Json -Compress -Depth 5
$bytes = $utf8.GetBytes($message + "`n")
$process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
$process.StandardInput.Close()
$process.WaitForExit()

$result = $null
foreach ($line in $stdout.Result -split "`n") {
    if ($line -notmatch '"event":"result"') { continue }
    try { $result = ($line | ConvertFrom-Json).result } catch { }
}

$notices = $stderr.Result.Trim()
if ($notices) { [Console]::Error.WriteLine($notices) }

# Checked whether or not agy answered: a run that failed may still have written.
$changed = $false
if (-not $Edit) {
    if ($null -eq $before) {
        [Console]::Error.WriteLine("$folder is not a git checkout, so it was not checked for changed files.")
    } else {
        $after = Get-Changes
        if ($after -ne $before) {
            $changed = $true
            [Console]::Error.WriteLine("agy changed files although asked not to. Before:`n$before`nNow:`n$after")
        }
    }
}

# agy exits 0 with no answer when a tool it wanted was refused, so check the answer.
if (-not $result -or $result.status -ne 'SUCCESS' -or -not "$($result.response)".Trim()) {
    $why = if ($result -and $result.error) { $result.error } elseif ($result) { "status $($result.status), empty answer" } else { 'no result' }
    [Console]::Error.WriteLine("agy gave no answer: $why")
    if ($changed) { exit 3 }
    if ($process.ExitCode -ne 0) { exit $process.ExitCode }
    exit 1
}

Write-Output "$($result.response)".TrimEnd()
if ($changed) { exit 3 }
exit 0
