# deploy.ps1 — git bundle deploy to the Pi over its own WiFi AP (no internet needed)
#
# Workflow:
#   1. git commit + push on your laptop as normal
#   2. Connect laptop to 'MeshGateway-*' WiFi AP  (Pi stays running, no teardown)
#   3. Run: .\deploy.ps1
#
# The script bundles the current branch, SCPs it to the Pi, applies it via git,
# installs any new pip packages, then restarts the gateway service.
#
# Usage:
#   .\deploy.ps1                      # default: 192.168.4.1, branch from git
#   .\deploy.ps1 -PiHost 10.0.0.5    # override IP (e.g. home network)

param(
    [string]$PiHost   = "192.168.4.1",
    [string]$PiUser   = "jeraldgoh99",
    [string]$RemoteDir = "/home/jeraldgoh99/IoTProject",
    [string]$Branch   = ""   # leave empty to auto-detect current branch
)

$ErrorActionPreference = "Stop"
$SshTarget  = "${PiUser}@${PiHost}"
$BundleFile = "deploy_latest.bundle"

# Auto-detect current branch if not specified
if (-not $Branch) {
    $Branch = git rev-parse --abbrev-ref HEAD
}

Write-Host ""
Write-Host "==> Bundling branch '$Branch' ..." -ForegroundColor Cyan
git bundle create $BundleFile "HEAD" $Branch
if ($LASTEXITCODE -ne 0) { Write-Error "git bundle failed"; exit 1 }

Write-Host "==> Copying bundle to Pi at $PiHost ..." -ForegroundColor Cyan
scp $BundleFile "${SshTarget}:/tmp/${BundleFile}"
if ($LASTEXITCODE -ne 0) { Write-Error "scp failed — is your laptop connected to MeshGateway-* WiFi?"; exit 1 }
Remove-Item $BundleFile

Write-Host "==> Applying bundle on Pi ..." -ForegroundColor Cyan
$ApplyCmd = @"
set -e
cd $RemoteDir
# First-time setup: initialise git from bundle if repo not yet cloned
if [ ! -d .git ]; then
    git clone /tmp/$BundleFile .
else
    git fetch /tmp/$BundleFile '$Branch':'$Branch'
    git checkout '$Branch'
    git reset --hard '$Branch'
fi
rm -f /tmp/$BundleFile
"@
ssh $SshTarget $ApplyCmd
if ($LASTEXITCODE -ne 0) { Write-Error "git apply on Pi failed"; exit 1 }

Write-Host "==> Installing any new requirements ..." -ForegroundColor Cyan
ssh $SshTarget "cd $RemoteDir && sudo venv/bin/pip install -q -r requirements.txt"

Write-Host "==> Restarting gateway service ..." -ForegroundColor Cyan
ssh $SshTarget "sudo systemctl restart gateway"

Write-Host ""
Write-Host "==> Deploy done! Tailing logs (Ctrl+C to stop) ..." -ForegroundColor Green
ssh $SshTarget "sudo journalctl -u gateway -f -n 40"
