# Robust Database Export Script for large PostGIS DB
# Uses Directory Format and Parallel Jobs for high performance

$containerName = "postgis_city"
$dbUser = "admin"
$dbName = "city_data"
$threads = 8  # Increased for faster export as requested
$scriptDir = $PSScriptRoot
$projectRoot = Split-Path $scriptDir -Parent
$timestamp = Get-Date -Format "yyyyMMdd_HHmm"
$backupDirName = "city_data_backup_$timestamp"
$containerDest = "/docker-entrypoint-initdb.d/$backupDirName"
$hostDest = Join-Path $projectRoot "db\$backupDirName"
$logFile = Join-Path $projectRoot "export_log_$timestamp.txt"

Write-Host "--- Robust Database Export Started ---" -ForegroundColor Cyan
Write-Host "Container: $containerName"
Write-Host "Database:  $dbName"
Write-Host "Threads:   $threads"
Write-Host "Target:    $hostDest"
Write-Host "Logging to: $logFile"

# Ensure the log file is created
"Starting export at $(Get-Date)" | Out-File $logFile

# Check if container is running
$isRunning = docker ps --filter "name=$containerName" --format "{{.Names}}"
if (-not $isRunning) {
    $err = "Error: Container '$containerName' is not running."
    Write-Host $err -ForegroundColor Red
    $err | Out-File $logFile -Append
    exit 1
}

Write-Host "Executing pg_dump in parallel (Directory Format)..." -ForegroundColor Yellow
Write-Host "This process will continue even if the IDE is closed." -ForegroundColor Green

# We use Start-Process to run the docker command so it's less tied to the current shell if needed,
# but for maximum reliability, we'll run it directly and advise the user to run this script in a separate PowerShell window.

$startTime = Get-Date
docker exec $containerName pg_dump -U $dbUser -d $dbName -Fd -j $threads -f $containerDest 2>&1 | Out-File $logFile -Append

$endTime = Get-Date
$duration = $endTime - $startTime

if ($LASTEXITCODE -eq 0) {
    Write-Host "Success! Export completed in $($duration.TotalMinutes.ToString('F2')) minutes." -ForegroundColor Green
    Write-Host "Backup location: $hostDest" -ForegroundColor Green
    "Export completed successfully in $($duration.TotalMinutes) minutes." | Out-File $logFile -Append
} else {
    Write-Host "Error occurred during export. Check $logFile for details." -ForegroundColor Red
    "Export failed with exit code $LASTEXITCODE" | Out-File $logFile -Append
}

Write-Host "--- Export Finished ---" -ForegroundColor Cyan
