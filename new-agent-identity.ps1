<#
Creates the agent identity from the blueprint, using the blueprint's own credential.

Use it when `a365 setup all` stopped at the delegated-permissions consent (step 3 of the README):
the blueprint exists, but the agent identity was not created. The blueprint's app-only Graph token
carries AgentIdentity.CreateAsManager, so no extra permission or consent is needed.
The identity gets the display name from a365.config.json, so `a365 setup all
--agent-registration-only` finds it afterwards. Idempotent: it does nothing if it already exists.

    .\new-agent-identity.ps1                       # sponsor = signed-in az user
    .\new-agent-identity.ps1 -SponsorUpn someone@contoso.com
#>
param([string] $SponsorUpn)
$ErrorActionPreference = 'Stop'
$a365 = Get-Content (Join-Path $PSScriptRoot 'a365.config.json') -Raw | ConvertFrom-Json
$generated = Get-Content (Join-Path $PSScriptRoot 'a365.generated.config.json') -Raw | ConvertFrom-Json
$tenant = $a365.tenantId; $blueprint = $generated.agentBlueprintId; $name = $a365.agentIdentityDisplayName
if (-not $blueprint) { throw 'a365.generated.config.json has no agentBlueprintId: run a365 setup all first.' }

$filter = [uri]::EscapeDataString("displayName eq '$name'")
$found = az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=$filter&`$select=appId" `
    --query "value[].appId" -o tsv
if ($found) { "Agent identity '$name' already exists: $found"; return }

if (-not $SponsorUpn) { $SponsorUpn = az account show --query user.name -o tsv }
$sponsorId = az ad user show --id $SponsorUpn --query id -o tsv
if (-not $sponsorId) { throw "Sponsor $SponsorUpn not found." }

Push-Location $PSScriptRoot
try { $raw = a365 setup blueprint --show-secret 2>&1 | Out-String } finally { Pop-Location }
$m = [regex]::Match($raw, '(?im)secret[^:\r\n]*:\s*(\S{20,})')
if (-not $m.Success) { throw "Could not read the blueprint secret from 'a365 setup blueprint --show-secret'." }

$token = (Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$tenant/oauth2/v2.0/token" -Body @{
    client_id = $blueprint; client_secret = $m.Groups[1].Value; grant_type = 'client_credentials'
    scope = 'https://graph.microsoft.com/.default' }).access_token
Remove-Variable m, raw

$body = @{
    displayName = $name
    agentIdentityBlueprintId = $blueprint
    'sponsors@odata.bind' = @("https://graph.microsoft.com/v1.0/users/$sponsorId")
} | ConvertTo-Json
$identity = Invoke-RestMethod -Method Post -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentity' `
    -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' -Body $body
"Agent identity created: $($identity.displayName)  appId=$($identity.appId)"
"Put this appId in config.json as agentAppId, then run: a365 setup all --authmode s2s --agent-registration-only"
