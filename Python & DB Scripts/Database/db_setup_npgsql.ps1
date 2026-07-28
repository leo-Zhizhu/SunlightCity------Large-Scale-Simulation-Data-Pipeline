# Downloads the Npgsql client and its dependencies from NuGet into a Unity project's
# Assets/Plugins/Npgsql folder, picking netstandard2.0 builds for Unity compatibility.
#
# Usage:
#   .\db_setup_npgsql.ps1 -ProjectRoot "C:\path\to\YourUnityProject"
#
# ProjectRoot must be the folder containing Assets/. Previously this path was hardcoded to one
# developer's machine, so the script either failed or wrote to the wrong place for everyone else.
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $ProjectRoot "Assets"))) {
    Write-Error "No 'Assets' folder under '$ProjectRoot'. Point -ProjectRoot at the Unity project root."
    exit 1
}

$pluginsDir = Join-Path $ProjectRoot "Assets\Plugins\Npgsql"

# 1. Clean up any previous install.
# Sanity-check the path before a recursive force delete so a bad -ProjectRoot can never wipe
# something unrelated.
if (Test-Path $pluginsDir) {
    if ($pluginsDir -notmatch 'Assets[\\/]Plugins[\\/]Npgsql$') {
        Write-Error "Refusing to delete '$pluginsDir' - it is not an Assets\Plugins\Npgsql folder."
        exit 1
    }
    Write-Host "Removing existing $pluginsDir ..."
    Remove-Item -Path $pluginsDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $pluginsDir | Out-Null

$baseUri = "https://www.nuget.org/api/v2/package"

# 2. Npgsql 4.1.12 is pinned deliberately. Newer 5.x/6.x+ releases target framework features
#    Unity's Mono/.NET Standard 2.0 runtime does not provide; the transitive versions below are
#    the matching set that works in-Editor. Do not bump these casually.
$packages = @(
    @{ name = "Npgsql"; version = "4.1.12" },
    @{ name = "System.Runtime.CompilerServices.Unsafe"; version = "4.6.0" },
    @{ name = "System.Threading.Tasks.Extensions"; version = "4.5.4" },
    @{ name = "System.Buffers"; version = "4.5.1" },
    @{ name = "System.Memory"; version = "4.5.4" },
    @{ name = "Microsoft.Bcl.AsyncInterfaces"; version = "1.1.1" },
    @{ name = "System.Text.Json"; version = "4.7.2" },
    @{ name = "System.Text.Encodings.Web"; version = "4.7.2" }
)

foreach ($pkg in $packages) {
    $name = $pkg.name
    $ver = $pkg.version
    $url = "$baseUri/$name/$ver"
    $zipPath = "$pluginsDir\$name.$ver.zip"
    
    Write-Host "Downloading $name $ver (Unity Stable)..."
    Invoke-WebRequest -Uri $url -OutFile $zipPath
    
    $extractPath = "$pluginsDir\$name-temp"
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
    
    # Prioritize netstandard2.0 for best Unity compatibility
    $dllSource = Get-ChildItem -Path "$extractPath\lib\netstandard2.0\*.dll" -Recurse | Select-Object -First 1
    if (!$dllSource) {
        $dllSource = Get-ChildItem -Path "$extractPath\lib\*.dll" -Recurse | Select-Object -First 1
    }
    
    if ($dllSource) {
        Copy-Item -Path $dllSource.FullName -Destination "$pluginsDir\$($dllSource.Name)" -Force
        Write-Host "Success: $($dllSource.Name)"
    }
    
    Remove-Item -Path $zipPath -Force
    Remove-Item -Path $extractPath -Recurse -Force
}

Write-Host "Stable Npgsql 4.1.12 setup complete!"
