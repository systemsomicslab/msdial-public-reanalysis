param(
    [string]$Python = "",
    [string]$InteractiveRoot = "D:\0_SourceCode\msdial_interactive_app",
    [string]$CatalogRoot = "D:\0_SourceCode\msdial_repository_catalog",
    [string]$ReanalysisRoot = "D:\13_MSDIAL_Public_Reanalysis\analysis"
)

$ErrorActionPreference = "Stop"

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
if ([System.IO.Path]::GetPathRoot($ReanalysisRoot) -ne "D:\") {
    throw "The repository reanalysis workspace must be an absolute path on D: $ReanalysisRoot"
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
