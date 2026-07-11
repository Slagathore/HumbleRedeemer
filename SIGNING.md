# Signing the Windows build

Releases are signed with **Azure Artifact Signing** under the shared
`Slagathores-Apps` account. All account details, machine setup, VPN/IPv6
gotchas, auth, and quota rules live in the central playbook —
`C:\Users\Cole\CodeStuff\CODE-SIGNING-PLAYBOOK.md` — don't duplicate them here.

## Local signed release (the current flow)

```powershell
pwsh -File build_release.ps1 -Tag v1.2.3
```

That stamps the version, builds `dist\HumbleRedeemer.exe` (windowed tray app,
icon, VERSIONINFO), signs it per playbook §4B, verifies the signature is
`Valid` / `CN=Charles Chambers`, and packages `HumbleRedeemer-windows.zip`.
Then:

```powershell
git tag v1.2.3; git push origin v1.2.3     # CI builds macOS/Linux + creates the release
gh run watch                                # wait for the release workflow
gh release upload v1.2.3 dist\HumbleRedeemer.exe HumbleRedeemer-windows.zip --clobber
```

The `--clobber` upload replaces CI's unsigned Windows assets with the signed
ones. macOS/Linux artifacts and the auto source archives come from CI as-is.

## CI signing (optional, not enabled yet)

`.github/workflows/release.yml` already carries a secret-gated
`azure/trusted-signing-action@v2` step. To enable: create a service principal
per playbook §6, grant it the **"Artifact Signing Certificate Profile Signer"**
role on the signing account, and set the `AZURE_*` repo secrets listed in the
workflow comment. Until then that step self-skips and the local flow above is
how releases get signed.
