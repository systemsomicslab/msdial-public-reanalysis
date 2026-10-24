param(
    [string]$Python = "",
    [string]$InteractiveRoot = "D:\0_SourceCode\msdial_interactive_app",
    [string]$CatalogRoot = "D:\0_SourceCode\msdial_repository_catalog",
    [string]$ReanalysisRoot = ""
)

$ErrorActionPreference = "Stop"

function Test-WithinFolder {
    param([string]$Path, [string]$Folder)
    if ([string]::IsNullOrWhiteSpace($Folder)) { return $false }
    $base = [System.IO.Path]::GetFullPath($Folder).TrimEnd('\')
    return $Path.Equals($base, [System.StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($base + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

# Why a folder cannot be a repository reanalysis workspace root, or "" where it can. The root is chosen per
# campaign and passed explicitly (D:\13_MSDIAL_Public_Reanalysis\analysis keeps the pilots and preflights);
# wherever it is, raw downloads and MS-DIAL outputs stay off C:, out of the user profile and temporary
# folders, and out of every code checkout.
function Get-WorkspaceRootProblem {
    param(
        [string]$Root,
        [string[]]$SystemDrives = @("C:", $env:SystemDrive),
        [string[]]$Profiles = @($env:USERPROFILE),
        [string[]]$TempFolders = @($env:TEMP, $env:TMP, [System.IO.Path]::GetTempPath()),
        [string[]]$Checkouts = @()
    )
    if ([string]::IsNullOrWhiteSpace($Root)) {
        return "no root was given: pass -ReanalysisRoot, the workspace root the campaigns on this PC use"
    }
    if ($Root -notmatch '^[A-Za-z]:[\\/]' -and $Root -notmatch '^\\\\[^\\]+\\[^\\]+') {
        return "it is not an absolute path"
    }
    try {
        $full = [System.IO.Path]::GetFullPath($Root)
    }
    catch {
        return "it is not a valid path"
    }
    $drive = [System.IO.Path]::GetPathRoot($full).TrimEnd('\')
    $full = $full.TrimEnd('\')
    foreach ($systemDrive in $SystemDrives) {
        if (-not [string]::IsNullOrWhiteSpace($systemDrive) -and $drive.Equals($systemDrive.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
            return "it is on $drive"
        }
    }
    foreach ($folder in $Profiles) {
        if (Test-WithinFolder -Path $full -Folder $folder) { return "it is inside the user profile $folder" }
    }
    foreach ($folder in $TempFolders) {
        if (Test-WithinFolder -Path $full -Folder $folder) { return "it is inside the temporary folder $folder" }
    }
    foreach ($folder in $Checkouts) {
        if (Test-WithinFolder -Path $full -Folder $folder) { return "it is inside the code checkout $folder" }
    }
    $folder = $full
    while (-not [string]::IsNullOrEmpty($folder)) {
        if (Test-Path -LiteralPath (Join-Path $folder ".git")) { return "it is inside the code checkout $folder" }
        $parent = [System.IO.Path]::GetDirectoryName($folder)
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $folder) { break }
        $folder = $parent
    }
    return ""
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $Python = $pythonCommand.Source
    }
    else {
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            $resolved = & $pyLauncher.Source -3 -c "import sys; print(sys.executable)"
            if ($LASTEXITCODE -eq 0) {
                $Python = ($resolved | Select-Object -Last 1).Trim()
            }
        }
    }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python 3 was not found. Install Python 3.10+ or pass -Python C:\path\to\python.exe."
}
if (-not (Test-Path -LiteralPath $InteractiveRoot -PathType Container)) {
    throw "MS-DIAL Interactive checkout was not found: $InteractiveRoot"
}
if (-not (Test-Path -LiteralPath $CatalogRoot -PathType Container)) {
    throw "MS-DIAL Repository Catalog checkout was not found: $CatalogRoot"
}
$problem = Get-WorkspaceRootProblem -Root $ReanalysisRoot -Checkouts @($PSScriptRoot, $InteractiveRoot, $CatalogRoot)
if ($problem) {
    throw "The repository reanalysis workspace root cannot be '$ReanalysisRoot': $problem."
}
New-Item -ItemType Directory -Path $ReanalysisRoot -Force | Out-Null
Write-Host "Repository reanalysis workspace: $ReanalysisRoot"

$interactiveSpec = "{0}[mcp]" -f $InteractiveRoot
$catalogSpec = "{0}[mcp]" -f $CatalogRoot

Write-Host "Installing editable MCP packages into: $Python"
& $Python -m pip install -e $interactiveSpec
if ($LASTEXITCODE -ne 0) { throw "Interactive MCP installation failed." }
& $Python -m pip install -e $catalogSpec
if ($LASTEXITCODE -ne 0) { throw "Catalog MCP installation failed." }

Write-Host "Checking Python imports..."
& $Python -c "import mcp, msdial_app.mcp_server, msdial_repository_catalog.mcp_server; print('MCP imports: OK')"
if ($LASTEXITCODE -ne 0) { throw "MCP import check failed." }

$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($null -eq $claude) {
    Write-Warning "Claude Code is not installed or is not on PATH. Install/authenticate it separately, then run 'claude' from this directory."
    Write-Host "Official setup: https://docs.anthropic.com/en/docs/claude-code/getting-started"
} else {
    Write-Host "Claude Code: $($claude.Source)"
    & claude --version
}

Write-Host "Setup complete. Start Claude Code in: $PSScriptRoot"
Write-Host "First action: /msdial-repository-batch audit"
