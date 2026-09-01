$ErrorActionPreference = "Stop"
Write-Host "Installing Python dependencies..."
py -m pip install -r requirements.txt
Write-Host "Running deep workspace validation..."
py tools\validate_workspace.py --deep
Write-Host "Done. Start Claude Code in this folder with: claude"
