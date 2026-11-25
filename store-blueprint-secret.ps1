<#
Stores the agent blueprint's client secret (created by `a365 setup all`) in GCP Secret Manager.

Run it from the folder where `a365 setup all` ran, with the same Windows user: the CLI keeps the
secret encrypted for that user and folder. The value is never printed; it only touches disk in a
temporary file that is deleted at once. Re-running it adds a new secret version.
#>
$ErrorActionPreference = 'Stop'
$cfg = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json

Push-Location $PSScriptRoot
try { $raw = a365 setup blueprint --show-secret 2>&1 | Out-String } finally { Pop-Location }
$m = [regex]::Match($raw, '(?im)secret[^:\r\n]*:\s*(\S{20,})')
if (-not $m.Success) { throw "Could not read the blueprint secret from 'a365 setup blueprint --show-secret'." }

$exists = gcloud secrets list --project $cfg.gcpProject --filter="name~/$($cfg.secretName)$" --format="value(name)"
if (-not $exists) {
    gcloud secrets create $cfg.secretName --project $cfg.gcpProject --replication-policy automatic `
        '--labels=purpose=agent365-observability' | Out-Null
}
$tmp = New-TemporaryFile
try {
    # No trailing newline: the value must be exactly the secret.
    [IO.File]::WriteAllText($tmp.FullName, $m.Groups[1].Value)
    gcloud secrets versions add $cfg.secretName --project $cfg.gcpProject "--data-file=$($tmp.FullName)" | Out-Null
} finally { Remove-Item $tmp.FullName -Force; Remove-Variable m, raw }
"Blueprint secret stored in Secret Manager: $($cfg.secretName) (project $($cfg.gcpProject))"
