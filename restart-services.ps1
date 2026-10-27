param([switch]$StopOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path

function Test-ProjectScript([string]$commandLine, [string]$relativePath) {
    $scriptPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $relativePath))
    if (-not $scriptPath.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Service script is outside the project directory.'
    }
    # Match a complete argument, never just a folder or a PID from an old file.
    $pattern = '(?i)(?:^|\s)"?' + [regex]::Escape($scriptPath) + '"?(?=\s|$)'
    return $commandLine -match $pattern
}

# Stop only this project's supervisor, then its backend and broker. Browser
# processes and user profiles are deliberately outside this restart operation.
$scripts = @('service_host.cjs', 'app.py', 'browser-runtime\bridge.cjs')
foreach ($relativePath in $scripts) {
    $expectedName = if ($relativePath -eq 'app.py') { '^python(?:w)?\.exe$' } else { '^node\.exe$' }
    $targets = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match $expectedName -and $_.CommandLine -and (Test-ProjectScript $_.CommandLine $relativePath)
    })
    foreach ($target in $targets) {
        if ($relativePath -eq 'browser-runtime\bridge.cjs') {
            # Let the current broker release only its own persistent shopping
            # profile. Never echo its local authentication token or response.
            try {
                $descriptorPath = Join-Path $projectRoot 'data\browser-bridge.json'
                $bridgeInfo = Get-Content -LiteralPath $descriptorPath -Raw | ConvertFrom-Json
                $bridgePort = [int]$bridgeInfo.port
                $ownedListener = Get-NetTCPConnection -State Listen -LocalPort $bridgePort -ErrorAction Stop |
                    Where-Object { $_.OwningProcess -eq $target.ProcessId -and $_.LocalAddress -in @('127.0.0.1', '::1') }
                if ($bridgePort -ge 1024 -and $bridgePort -le 65535 -and $ownedListener -and $bridgeInfo.token) {
                    $shutdownBody = @{ id = [guid]::NewGuid().ToString(); client_id = [guid]::NewGuid().ToString('N'); command = 'shutdown' } | ConvertTo-Json -Compress
                    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$bridgePort/command" -Method Post -ContentType 'application/json' -Body $shutdownBody -Headers @{ Authorization = "Bearer $($bridgeInfo.token)" } -TimeoutSec 8
                    Wait-Process -Id $target.ProcessId -Timeout 8 -ErrorAction SilentlyContinue
                }
            } catch {
                # Legacy brokers do not support shutdown. Fall back to the
                # exact project process match below, never a browser process.
            }
        }
        # Recheck command identity immediately before stopping, in case a PID was reused.
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($target.ProcessId)"
        if ($current -and $current.Name -match $expectedName -and (Test-ProjectScript $current.CommandLine $relativePath)) {
            Stop-Process -Id $current.ProcessId -ErrorAction Stop
            try {
                Wait-Process -Id $current.ProcessId -Timeout 10 -ErrorAction Stop
            } catch {
                if (Get-Process -Id $current.ProcessId -ErrorAction SilentlyContinue) { throw }
            }
        }
    }
}

if ($StopOnly) { exit 0 }

Set-Location -LiteralPath $projectRoot
$nodeExecutable = (Get-Command node.exe -ErrorAction Stop).Source
& $nodeExecutable (Join-Path $projectRoot 'launcher.cjs')
exit $LASTEXITCODE
