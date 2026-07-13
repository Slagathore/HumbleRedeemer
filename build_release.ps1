<#
Build + sign + package the Windows release locally.

  pwsh -File build_release.ps1 -Tag v1.2.3 [-SkipSign]

Mirrors the CI Windows leg (stamps, flags), then signs dist\HumbleRedeemer.exe
with Azure Artifact Signing and verifies the signature. Account details and
environment gotchas: see SIGNING.md -> CODE-SIGNING-PLAYBOOK.md.
#>
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [switch]$SkipSign
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ---- 1) version stamps (identical to .github/workflows/release.yml) ----
$Tag | Out-File -Encoding ascii -NoNewline static\version.txt
$V = $Tag -replace '^v', ''
$nums = @($V -split '\.' | ForEach-Object {
        $m = [regex]::Match($_, '^\d+'); if ($m.Success) { [int]$m.Value } else { 0 } })
$nums += 0, 0, 0
$A = $nums[0]; $B = $nums[1]; $C = $nums[2]
@"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($A, $B, $C, 0),
    prodvers=($A, $B, $C, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Slagathore'),
      StringStruct('FileDescription', 'Humble Steam Key Redeemer'),
      StringStruct('FileVersion', '$V'),
      StringStruct('ProductName', 'Humble Steam Key Redeemer'),
      StringStruct('ProductVersion', '$V'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'HumbleRedeemer.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"@ | Out-File -Encoding ascii version_info.txt

# ---- 2) build (flags mirror the CI Windows leg) ----
python -m PyInstaller --noconfirm --onefile --name HumbleRedeemer `
    --windowed --icon static/icon.ico --version-file version_info.txt `
    --hidden-import pystray --hidden-import pystray._win32 `
    --add-data "static;static" --collect-all selenium --collect-submodules steam `
    --hidden-import fuzzywuzzy app.py
if ($LASTEXITCODE) { throw "PyInstaller failed" }

# ---- 3) sign (playbook section 4B, incl. Mullvad/IPv6 workarounds) ----
if (-not $SkipSign) {
    $env:DOTNET_SYSTEM_NET_DISABLEIPV6 = '1'
    $env:PATH = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:PATH"
    Invoke-TrustedSigning `
        -Endpoint 'https://cus.codesigning.azure.net/' `
        -CodeSigningAccountName 'Slagathores-Apps' `
        -CertificateProfileName 'public' `
        -Files "$PSScriptRoot\dist\HumbleRedeemer.exe" `
        -TimestampRfc3161 'http://timestamp.acs.microsoft.com' `
        -TimestampDigest 'SHA256' -FileDigest 'SHA256' `
        -ExcludeEnvironmentCredential -ExcludeWorkloadIdentityCredential `
        -ExcludeManagedIdentityCredential -ExcludeSharedTokenCacheCredential `
        -ExcludeVisualStudioCredential -ExcludeVisualStudioCodeCredential `
        -ExcludeAzurePowerShellCredential -ExcludeAzureDeveloperCliCredential `
        -ExcludeInteractiveBrowserCredential
    $sig = Get-AuthenticodeSignature dist\HumbleRedeemer.exe
    Write-Host "Signature: $($sig.Status) — $($sig.SignerCertificate.Subject)"
    if ($sig.Status -ne 'Valid') { throw "Signature not valid: $($sig.StatusMessage)" }
}

# ---- 4) package like CI (exe + README in the zip) ----
Remove-Item -Recurse -Force pkg -ErrorAction SilentlyContinue
New-Item -ItemType Directory pkg | Out-Null
Copy-Item dist\HumbleRedeemer.exe, README.md pkg\
Push-Location pkg
python -m zipfile -c ..\HumbleRedeemer-windows.zip .
Pop-Location
Remove-Item -Recurse -Force pkg
Write-Host "Done: dist\HumbleRedeemer.exe + HumbleRedeemer-windows.zip ($Tag)"

# ---- 5) installer (skips cleanly if Inno Setup isn't installed) ----
$iscc = "C:\Users\Cole\AppData\Local\Programs\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    & $iscc "/DAppVersion=$V" "/DSourceExe=$PSScriptRoot\dist\HumbleRedeemer.exe" "packaging\installer.iss"
    if ($LASTEXITCODE) { throw "ISCC failed" }
    $setupExe = "dist\HumbleRedeemer-$V-Setup.exe"
    if (-not $SkipSign) {
        $env:DOTNET_SYSTEM_NET_DISABLEIPV6 = '1'
        $env:PATH = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:PATH"
        Invoke-TrustedSigning `
            -Endpoint 'https://cus.codesigning.azure.net/' `
            -CodeSigningAccountName 'Slagathores-Apps' `
            -CertificateProfileName 'public' `
            -Files "$PSScriptRoot\$setupExe" `
            -TimestampRfc3161 'http://timestamp.acs.microsoft.com' `
            -TimestampDigest 'SHA256' -FileDigest 'SHA256' `
            -ExcludeEnvironmentCredential -ExcludeWorkloadIdentityCredential `
            -ExcludeManagedIdentityCredential -ExcludeSharedTokenCacheCredential `
            -ExcludeVisualStudioCredential -ExcludeVisualStudioCodeCredential `
            -ExcludeAzurePowerShellCredential -ExcludeAzureDeveloperCliCredential `
            -ExcludeInteractiveBrowserCredential
        $setupSig = Get-AuthenticodeSignature $setupExe
        Write-Host "Installer signature: $($setupSig.Status) — $($setupSig.SignerCertificate.Subject)"
        if ($setupSig.Status -ne 'Valid') { throw "Installer signature not valid: $($setupSig.StatusMessage)" }
    }
    Write-Host "Done: $setupExe ($Tag)"
} else {
    Write-Host "Inno Setup not found at $iscc, skipping installer build."
}
