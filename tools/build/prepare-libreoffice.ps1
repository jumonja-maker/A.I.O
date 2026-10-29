[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$pin=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'libreoffice-release.json') -Raw | ConvertFrom-Json
if ($pin.channel -cne 'stable' -or $pin.platform -cne 'windows-x86_64' -or $pin.sha256 -notmatch '^[0-9a-f]{64}$') {throw 'Invalid engine pin'}
$url=[Uri]$pin.url
if ($url.Scheme -cne 'https' -or $url.Host -cne 'download.documentfoundation.org') {throw 'Unapproved engine download host'}
$dest=[IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $dest) {throw 'Engine destination must be new; no overwrite'}
New-Item -ItemType Directory -Path $dest | Out-Null
$msi=Join-Path $dest 'LibreOffice.msi'
Invoke-WebRequest -Uri $pin.url -OutFile $msi
$actual=(Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -cne $pin.sha256) {throw 'Engine MSI SHA256 mismatch; refused extraction'}
$extract=Join-Path $dest 'payload'
$log=Join-Path $dest 'msi-extract.log'
$msiArgs=@('/a',('"'+$msi+'"'),'/qn',('TARGETDIR="'+$extract+'"'),'/l*v',('"'+$log+'"'))
$p=Start-Process msiexec.exe -ArgumentList $msiArgs -Wait -PassThru
if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {throw ('MSI administrative extraction failed: '+$p.ExitCode)}
$engines=@(Get-ChildItem -LiteralPath $extract -Filter soffice.com -Recurse -File)
if ($engines.Count -ne 1) {throw 'Expected one extracted LibreOffice executable'}
$engine=$engines[0].FullName
$root=Split-Path -Parent (Split-Path -Parent $engine)
foreach($part in @('program\soffice.exe','program\soffice.bin','program\python.exe','share')) {
  if (-not (Test-Path -LiteralPath (Join-Path $root $part))) {throw ('Incomplete engine payload: '+$part)}
}
$version=(& $engine --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch ('^LibreOffice\s+'+[regex]::Escape($pin.version)+'(?:\s|$)')) {throw ('Unexpected engine version: '+$version)}
$evidence=@{schema=1;version=$version;msiSha256=$actual;sourceRevision=$env:GITHUB_SHA;channel=$pin.channel;verified=$true}
$evidence | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dest 'engine-provenance.json') -Encoding utf8
if ($env:GITHUB_ENV) {
  @("AIO_LIBREOFFICE=$engine","AIO_QA_ENGINE=$engine","AIO_LIBREOFFICE_ROOT=$root","AIO_ENGINE_VERSION=$($pin.version)") | Out-File $env:GITHUB_ENV -Encoding utf8 -Append
}
Write-Host ('AIO_ENGINE_VERSION='+$version)
Write-Host ('AIO_ENGINE_MSI_SHA256='+$actual)
Write-Host ('AIO_LIBREOFFICE_ROOT='+$root)
