param([string]$OutputName = ('scepter-shopper-source-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip'))

$ErrorActionPreference = 'Stop'

function Get-DeliveryFiles([string]$Root) {
    $rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\', '/')
    $files = [Collections.Generic.List[string]]::new()
    $fixed = @(
        '.env.example', 'THIRD_PARTY_NOTICES.md', '用户使用说明.md',
        '安装依赖.cmd', '启动.cmd', '启动.vbs', '重启助手.vbs',
        'install-dependencies.ps1', 'restart-services.ps1',
        'app.py', 'launcher.cjs', 'service_host.cjs', 'runtime_build.cjs',
        'requirements.lock.txt',
        'browser-runtime/package.json', 'browser-runtime/package-lock.json',
        'frontend/package.json', 'frontend/package-lock.json', 'frontend/tsconfig.json',
        'frontend/vite.config.ts', 'frontend/index.html'
    )
    foreach ($relative in $fixed) { $files.Add($relative) }
    foreach ($name in @('shopper', 'browser-runtime')) {
        $directory = Join-Path $rootPath $name
        foreach ($item in Get-ChildItem -LiteralPath $directory -File -Force) {
            $extensions = if ($name -eq 'shopper') { @('.py', '.js') } else { @('.cjs', '.js') }
            if ($item.Extension -in $extensions) { $files.Add($name + '/' + $item.Name) }
        }
    }
    function Add-DeliveryTree([string]$Relative, [string[]]$Extensions) {
        $directory = Get-Item -LiteralPath (Join-Path $rootPath $Relative) -Force
        if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Symbolic links are not permitted in the delivery: $Relative" }
        foreach ($entry in Get-ChildItem -LiteralPath $directory.FullName -Force) {
            $next = $Relative + '/' + $entry.Name
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Symbolic links are not permitted in the delivery: $next" }
            if ($entry.PSIsContainer) {
                if ($entry.Name -match '^(?:node_modules|data|logs|qa|profiles?|\.venv(?:\..*)?|\.cache|__pycache__)$') { continue }
                Add-DeliveryTree $next $Extensions
            } elseif ($entry.Extension -in $Extensions) { $files.Add($next) }
        }
    }
    Add-DeliveryTree 'frontend/src' @('.ts', '.tsx', '.css', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.woff', '.woff2')
    Add-DeliveryTree 'frontend/dist' @('.html', '.js', '.css', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.ico', '.woff', '.woff2')
    if (Test-Path -LiteralPath (Join-Path $rootPath 'frontend/public')) {
        Add-DeliveryTree 'frontend/public' @('.svg', '.png', '.jpg', '.jpeg', '.webp', '.ico', '.woff', '.woff2')
    }
    if (-not $files.Contains('frontend/dist/index.html')) { throw 'Build frontend/dist before packaging.' }
    return @($files | Sort-Object -Unique)
}

function Assert-DeliveryFile([string]$Root, [string]$Relative) {
    if ($Relative -match '(?i)(?:^|/)(?:\.env|data|logs|qa|profiles?|browser-profile|app-profile|node_modules|\.venv(?:\..*)?|\.cache|__pycache__)(?:/|$)' -or $Relative -match '(?:^|/)\.\.(?:/|$)') {
        throw "Runtime/private files are forbidden in the archive: $Relative"
    }
    $rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\', '/')
    $full = [IO.Path]::GetFullPath((Join-Path $rootPath $Relative))
    if (-not $full.StartsWith($rootPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Delivery file escaped the project directory.' }
    $item = Get-Item -LiteralPath $full -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not an ordinary file: $Relative" }
    $parent = $item.Directory
    while ($parent.FullName -ne $rootPath) {
        if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Delivery parent is a symbolic link: $Relative" }
        $parent = $parent.Parent
        if (-not $parent) { throw 'Delivery parent escaped the project directory.' }
    }
    if ($item.Extension -notin @('.png', '.jpg', '.jpeg', '.webp', '.ico', '.woff', '.woff2')) {
        $content = [IO.File]::ReadAllText($full)
        # Detect development-machine roots without embedding such a root in this script.
        if ($content -match '(?i)[a-z]:[\\/]+Users[\\/]+[^\\/\s]+[\\/]' -or $content.Contains($rootPath)) {
            throw "Machine-specific absolute path found in: $Relative"
        }
        if ($Relative -eq '.env.example') {
            $keyLines = [regex]::Matches($content, '(?m)^[\t ]*DEEPSEEK_API_KEY[\t ]*=.*$')
            if ($keyLines.Count -ne 1 -or $keyLines[0].Value -notmatch '^DEEPSEEK_API_KEY=[\t ]*\r?$') {
                throw 'The example API key must be empty and occur exactly once.'
            }
        }
    }
    return $full
}

function New-StandalonePackage([string]$Root, [string]$Name) {
    if ($Name -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]*\.zip$') { throw 'OutputName must be a simple .zip filename.' }
    $rootPath = (Resolve-Path -LiteralPath $Root).Path
    $release = [IO.Path]::GetFullPath((Join-Path $rootPath 'release'))
    if (-not [IO.Path]::GetDirectoryName($release).Equals($rootPath.TrimEnd('\', '/'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid release directory.' }
    if (Test-Path -LiteralPath $release) {
        $item = Get-Item -LiteralPath $release -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'release must be an ordinary project directory.' }
    } else { $null = New-Item -ItemType Directory -Path $release }
    $archivePath = Join-Path $release $Name
    if (Test-Path -LiteralPath $archivePath) { throw 'This archive already exists; use another OutputName.' }
    $files = Get-DeliveryFiles $rootPath
    $sources = @{}
    foreach ($relative in $files) { $sources[$relative] = Assert-DeliveryFile $rootPath $relative }

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $stream = [IO.File]::Open($archivePath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $zip = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Create, $true, [Text.Encoding]::UTF8)
        try {
            foreach ($relative in $files) {
                $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $sources[$relative], $relative, [IO.Compression.CompressionLevel]::Optimal)
            }
        } finally { $zip.Dispose() }
    } finally { $stream.Dispose() }

    $check = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $actual = @($check.Entries | ForEach-Object { $_.FullName } | Sort-Object)
        if (@(Compare-Object $files $actual).Count -ne 0) { throw 'Archive entries do not match the whitelist.' }
        foreach ($entry in $check.Entries) {
            $relative = $entry.FullName
            if ($entry.Length -ne (Get-Item -LiteralPath $sources[$relative]).Length) { throw "Archive length mismatch: $relative" }
        }
    } finally { $check.Dispose() }
    $hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $utf8 = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText($archivePath + '.sha256', "$hash  $Name`n", $utf8)
    [IO.File]::WriteAllText($archivePath + '.manifest.json', (@{ archive = $Name; sha256 = $hash; files = @($files); count = $files.Count } | ConvertTo-Json -Depth 4), $utf8)
    return @{ archive = $archivePath; sha256 = $hash; files = $files.Count }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { New-StandalonePackage -Root $PSScriptRoot -Name $OutputName | ConvertTo-Json }
    catch { Write-Host ('Packaging failed: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
}
