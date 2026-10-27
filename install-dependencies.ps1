param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'

function Get-ProjectChildPath([string]$Root, [string]$Name) {
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\', '/')
    if ($Name -notmatch '^[a-zA-Z0-9._-]+$' -or $Name -in @('.', '..')) {
        throw 'Invalid project child name.'
    }
    $result = [IO.Path]::GetFullPath((Join-Path $resolvedRoot $Name))
    if (-not [IO.Path]::GetDirectoryName($result).Equals($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The target must be a direct child of this project.'
    }
    if (Test-Path -LiteralPath $result) {
        $item = Get-Item -LiteralPath $result -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing to operate on a symbolic link or junction: $Name"
        }
    }
    return $result
}

function Invoke-PythonProbe([string]$Executable, [string[]]$Arguments = @()) {
    # Single quotes inside Python avoid Windows PowerShell 5 native argument
    # conversion stripping embedded double quotes from the -c expression.
    $code = 'import json,sys; print(json.dumps(dict(version=list(sys.version_info[:3]),executable=sys.executable,base_executable=getattr(sys,''_base_executable'',sys.executable),prefix=sys.prefix,base_prefix=sys.base_prefix)))'
    try {
        $probeArguments = @($Arguments) + @('-I', '-c', $code)
        $output = & $Executable @probeArguments 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $result = ($output -join "`n") | ConvertFrom-Json
        if (-not $result.version -or -not $result.executable) { return $null }
        return $result
    } catch { return $null }
}

function Test-PythonVersion($Probe) {
    return ($null -ne $Probe -and [version]($Probe.version -join '.') -ge [version]'3.11.0')
}

function Test-NodeVersion([string]$Version) {
    try { return [version]$Version -ge [version]'22.12.0' } catch { return $false }
}

function Find-BasePython {
    $candidates = @(
        @{Name = 'py.exe'; Arguments = @('-3.11')},
        @{Name = 'python.exe'; Arguments = @()},
        @{Name = 'py.exe'; Arguments = @('-3')}
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) { continue }
        $probe = Invoke-PythonProbe -Executable $command.Source -Arguments $candidate.Arguments
        if (-not (Test-PythonVersion $probe)) { continue }
        # Use the base interpreter, not an activated venv which may be moved below.
        $base = Invoke-PythonProbe -Executable $probe.base_executable
        if (Test-PythonVersion $base) { return $base }
    }
    throw 'Please install Python 3.11 or later and enable its launcher/PATH, then rerun this installer.'
}

function Get-NodeTools {
    $node = Get-Command node.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $npm = Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $node -or -not $npm) { throw 'Please install Node.js 22.12+ (recommended: Node.js 24 LTS), including npm and PATH.' }
    $versionText = & $node.Source -p 'process.versions.node'
    if ($LASTEXITCODE -ne 0 -or -not (Test-NodeVersion $versionText)) {
        throw 'Node.js 22.12 or later is required to build this application.'
    }
    return @{ Node = $node.Source; Npm = $npm.Source; Version = $versionText }
}

function Get-ProjectVenvState([string]$Root, [scriptblock]$Probe = { param($file) Invoke-PythonProbe -Executable $file }) {
    $venv = Get-ProjectChildPath $Root '.venv'
    if (-not (Test-Path -LiteralPath $venv)) { return @{ State = 'missing'; Reason = 'No project virtual environment.' } }
    if (-not (Test-Path -LiteralPath $venv -PathType Container)) { return @{ State = 'invalid'; Reason = '.venv is not a directory.' } }
    $interpreter = Join-Path $venv 'Scripts\python.exe'
    $configFile = Join-Path $venv 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $interpreter -PathType Leaf) -or -not (Test-Path -LiteralPath $configFile -PathType Leaf)) {
        return @{ State = 'invalid'; Reason = 'The project interpreter or pyvenv.cfg is missing.' }
    }
    $info = & $Probe $interpreter
    if (-not (Test-PythonVersion $info)) { return @{ State = 'invalid'; Reason = 'The project interpreter is broken or older than Python 3.11.' } }
    if (-not ([IO.Path]::GetFullPath($info.prefix)).Equals($venv, [StringComparison]::OrdinalIgnoreCase) -or $info.prefix -eq $info.base_prefix) {
        return @{ State = 'invalid'; Reason = 'The interpreter belongs to a different environment.' }
    }
    $marker = Join-Path $venv '.shopper-venv.json'
    try {
        if (Test-Path -LiteralPath $marker -PathType Leaf) {
            $record = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
            $original = [string]$record.path
        } else {
            $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8
            $match = [regex]::Match($config, '(?m)^command\s*=.*?\s-m\s+venv\s+(?:"(?<quoted>[^"]+)"|(?<plain>[^\r\n]+))\s*$')
            if (-not $match.Success) { return @{ State = 'invalid'; Reason = 'The original venv location cannot be verified.' } }
            $original = if ($match.Groups['quoted'].Success) { $match.Groups['quoted'].Value } else { $match.Groups['plain'].Value.Trim() }
        }
        if (-not [IO.Path]::IsPathRooted($original) -or -not ([IO.Path]::GetFullPath($original)).Equals($venv, [StringComparison]::OrdinalIgnoreCase)) {
            return @{ State = 'invalid'; Reason = 'The project venv was copied or moved from another location.' }
        }
    } catch { return @{ State = 'invalid'; Reason = 'The original venv metadata is invalid.' } }
    return @{ State = 'ready'; Reason = 'The existing project environment is usable.' }
}

function Backup-ProjectVenv([string]$Root) {
    $source = Get-ProjectChildPath $Root '.venv'
    if (-not (Test-Path -LiteralPath $source)) { return $null }
    $name = '.venv.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $destination = Get-ProjectChildPath $Root $name
    if (Test-Path -LiteralPath $destination) { throw 'The venv backup destination already exists.' }
    # Both fully resolved targets were checked as direct project children above.
    # Rename in place; do not recursively delete old environments or user data.
    Move-Item -LiteralPath $source -Destination $destination -ErrorAction Stop
    return $destination
}

function Install-ProjectDependencies([string]$Root, [switch]$OnlyCheck) {
    $rootPath = (Resolve-Path -LiteralPath $Root).Path
    $python = Find-BasePython
    $node = Get-NodeTools
    $state = Get-ProjectVenvState $rootPath
    Write-Host ('Python ' + ($python.version -join '.') + '; Node.js ' + $node.Version)
    Write-Host ('Project environment: ' + $state.State + '. ' + $state.Reason)
    if ($OnlyCheck) { return }

    $venv = Get-ProjectChildPath $rootPath '.venv'
    if ($state.State -ne 'ready') {
        $backup = Backup-ProjectVenv $rootPath
        if ($backup) { Write-Host ('Previous environment preserved as ' + [IO.Path]::GetFileName($backup)) }
        & $python.executable -I -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Creating the Python environment failed. Any previous environment remains in its backup folder.' }
    }
    $interpreter = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-PythonVersion (Invoke-PythonProbe $interpreter))) { throw 'The project Python interpreter failed its health check.' }
    @{ path = $venv; version = 1 } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $venv '.shopper-venv.json') -Encoding UTF8

    # File.Copy with overwrite=false preserves an existing key even if another
    # process creates .env between the check and copy. Never read the actual key.
    $envFile = Join-Path $rootPath '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { [IO.File]::Copy((Join-Path $rootPath '.env.example'), $envFile, $false) }
    $cache = Get-ProjectChildPath $rootPath '.cache'
    $uv = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uv) {
        $previousCache = $env:UV_CACHE_DIR
        try {
            $env:UV_CACHE_DIR = Join-Path $cache 'uv'
            & $uv.Source pip install --python $interpreter -r (Join-Path $rootPath 'requirements.lock.txt')
            if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
        } finally { $env:UV_CACHE_DIR = $previousCache }
    } else {
        & $interpreter -m pip install --disable-pip-version-check --cache-dir (Join-Path $cache 'pip') -r (Join-Path $rootPath 'requirements.lock.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    }
    foreach ($folder in @('browser-runtime', 'frontend')) {
        Push-Location -LiteralPath (Join-Path $rootPath $folder)
        try {
            & $node.Npm ci --include=dev --cache (Join-Path $cache 'npm')
            if ($LASTEXITCODE -ne 0) { throw "npm install failed in $folder." }
            if ($folder -eq 'frontend') {
                & $node.Npm run build
                if ($LASTEXITCODE -ne 0) { throw 'The frontend build failed.' }
            }
        } finally { Pop-Location }
    }
    Write-Host 'Installation complete. Fill DEEPSEEK_API_KEY in .env if needed, then double-click the launcher.'
    Write-Host 'Existing .env values have been preserved. See the user guide in this folder for first-time Edge authorization.'
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Install-ProjectDependencies -Root $PSScriptRoot -OnlyCheck:$CheckOnly }
    catch { Write-Host ('Installation failed: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
}
