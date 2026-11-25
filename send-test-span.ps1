<#
Smoke test for Agent 365 observability without touching any agent:
  1. gets an app-only (S2S) token for the agent identity (two steps through the blueprint when
     config.json has agentBlueprintId; client credentials of a standard Entra app otherwise),
  2. shows the relevant token claims (never the token),
  3. checks tenant eligibility,
  4. sends one synthetic run (invoke_agent + chat + execute_tool) and shows the per-span result.
#>
param([string] $UserUpn)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$tid = $cfg.tenantId; $app = $cfg.agentAppId

# 1. Token: with a blueprint, two steps (blueprint secret -> T1 for the agent identity -> Agent 365);
#    otherwise client credentials of a standard Entra app.
$blueprint = if ($cfg.agentBlueprintId) { $cfg.agentBlueprintId } else { $app }
$tokenUrl = "https://login.microsoftonline.com/$tid/oauth2/v2.0/token"
$secret = gcloud secrets versions access latest --secret $cfg.secretName --project $cfg.gcpProject
if ($cfg.agentBlueprintId) {
    $t1 = (Invoke-RestMethod -Method Post -Uri $tokenUrl -Body @{
        client_id = $blueprint; client_secret = $secret; grant_type = 'client_credentials'
        scope = 'api://AzureADTokenExchange/.default'; fmi_path = $app }).access_token
    $tok = (Invoke-RestMethod -Method Post -Uri $tokenUrl -Body @{
        client_id = $app; grant_type = 'client_credentials'; scope = "$($cfg.observabilityResource)/.default"
        client_assertion_type = 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'; client_assertion = $t1 }).access_token
    Remove-Variable t1
} else {
    $tok = (Invoke-RestMethod -Method Post -Uri $tokenUrl -Body @{
        client_id = $app; client_secret = $secret; grant_type = 'client_credentials'
        scope = "$($cfg.observabilityResource)/.default" }).access_token
}
Remove-Variable secret

# 2. Claims
$p = $tok.Split('.')[1].Replace('-', '+').Replace('_', '/'); $p += '=' * ((4 - $p.Length % 4) % 4)
$claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p)) | ConvertFrom-Json
"Token: aud=$($claims.aud) azp=$($claims.azp) tid=$($claims.tid) roles=$($claims.roles -join ',') scp=$($claims.scp)"
$h = @{ Authorization = "Bearer $tok" }

# 3. Eligibility
try {
    $el = Invoke-RestMethod -Uri "$($cfg.observabilityBaseUrl)/tenants/$tid/eligibility?api-version=1" -Headers $h
    "Eligibility: enabled=$($el.enabled)"
} catch { "Eligibility: HTTP $($_.Exception.Response.StatusCode.value__) $($_.ErrorDetails.Message)" }

# 4. One synthetic run
if (-not $UserUpn) { $UserUpn = az account show --query user.name -o tsv }
$user = az ad user show --id $UserUpn --query "{id:id,upn:userPrincipalName,name:displayName}" -o json | ConvertFrom-Json
$hex = { param($n) -join ((1..$n) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) }) }
$traceId = & $hex 16; $root = & $hex 8; $chat = & $hex 8; $tool = & $hex 8
$conv = [guid]::NewGuid().ToString()
$t0 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000
$ns = { param($ms) [string]($t0 + [int64]$ms * 1000000) }
$question = 'Prueba de observabilidad: analiza el riesgo de renovación de Contoso'
$answer = 'Respuesta sintética de la prueba de observabilidad (sin datos reales).'
$msgIn = ConvertTo-Json -Compress -Depth 5 @(@{ role = 'user'; parts = @(@{ type = 'text'; content = $question }) })
$msgOut = ConvertTo-Json -Compress -Depth 5 @(@{ role = 'assistant'; parts = @(@{ type = 'text'; content = $answer }) })

$common = [ordered]@{
    'microsoft.tenant.id' = $tid; 'gen_ai.agent.id' = $app; 'gen_ai.agent.name' = $cfg.agentName
    'microsoft.a365.agent.blueprint.id' = $blueprint; 'gen_ai.agent.description' = $cfg.agentDescription
    'user.id' = $user.id; 'user.email' = $user.upn; 'user.name' = $user.name
    'client.address' = '0.0.0.0'; 'gen_ai.conversation.id' = $conv; 'microsoft.channel.name' = $cfg.channel
    'server.address' = "$($cfg.gcpRegion)-aiplatform.googleapis.com"; 'server.port' = '443'
    'service.name' = $cfg.displayName
}
function Span($id, $parent, $name, $kind, $from, $to, $extra) {
    $a = [ordered]@{} + $common + $extra
    @{ traceId = $traceId; spanId = $id; parentSpanId = $parent; name = $name; kind = $kind
       startTimeUnixNano = (& $ns $from); endTimeUnixNano = (& $ns $to); status = @{ code = 1 }
       attributes = @($a.GetEnumerator() | ForEach-Object { @{ key = $_.Key; value = @{ stringValue = [string]$_.Value } } }) }
}
$spans = @(
    (Span $root '' "invoke_agent $($cfg.agentName)" 1 0 3000 ([ordered]@{
        'gen_ai.operation.name' = 'invoke_agent'; 'gen_ai.input.messages' = $msgIn; 'gen_ai.output.messages' = $msgOut
        'gen_ai.provider.name' = 'gcp.vertex_ai'; 'gen_ai.request.model' = 'gemini-2.5-flash' })),
    (Span $chat $root 'chat gemini-2.5-flash' 3 100 1500 ([ordered]@{
        'gen_ai.operation.name' = 'chat'; 'gen_ai.input.messages' = $msgIn; 'gen_ai.output.messages' = $msgOut
        'gen_ai.provider.name' = 'gcp.vertex_ai'; 'gen_ai.request.model' = 'gemini-2.5-flash'
        'gen_ai.usage.input_tokens' = '42'; 'gen_ai.usage.output_tokens' = '17' })),
    (Span $tool $root 'execute_tool get_account' 3 1600 2400 ([ordered]@{
        'gen_ai.operation.name' = 'execute_tool'; 'gen_ai.tool.name' = 'get_account'
        'gen_ai.tool.type' = 'function'; 'gen_ai.tool.call.id' = [guid]::NewGuid().ToString()
        'gen_ai.tool.call.arguments' = '{"name_or_id":"Contoso"}'; 'gen_ai.tool.call.result' = '{"synthetic":true}' }))
)
$body = @{ resourceSpans = @(@{ scopeSpans = @(@{ scope = @{ name = 'a365-smoke-test'; version = '1.0.0' }; spans = $spans }) }) } |
    ConvertTo-Json -Depth 12 -Compress
$url = "$($cfg.observabilityBaseUrl)/tenants/$tid/otlp/agents/$app/traces?api-version=1"
$r = Invoke-WebRequest -Method Post -Uri $url -Headers $h -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -SkipHttpErrorCheck
"POST traces: HTTP $($r.StatusCode)  conversationId=$conv  traceId=$traceId"
$res = $r.Content | ConvertFrom-Json
if ($res.partialSuccess) { "partialSuccess: rejectedSpans=$($res.partialSuccess.rejectedSpans) $($res.partialSuccess.errorMessage)" }
foreach ($x in $res.results) {
    $kind = @{ $root = 'invoke_agent'; $chat = 'chat'; $tool = 'execute_tool' }[$x.spanId]
    $x.sinks.PSObject.Properties | ForEach-Object { "  {0,-13} {1,-11} {2} {3}" -f $kind, $_.Name, $_.Value.status, $_.Value.reason }
}
if (-not $res.results) { $r.Content }
