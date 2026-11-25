# Observabilidad en Microsoft Agent 365 para agentes de Google ADK en Vertex AI Agent Engine

Este repositorio muestra cómo hacer que un agente creado con **Google Agent Development Kit (ADK)** y
desplegado en **Vertex AI Agent Engine** (Google Cloud) envíe su actividad a **Microsoft Agent 365**,
de forma que:

- cada ejecución del agente aparece en **Microsoft Defender** (búsqueda avanzada, tabla
  `CloudAppEvents`: `InvokeAgent`, `InferenceCall`, `ExecuteToolBySDK`), con el usuario que lo invocó,
  las preguntas, las respuestas, el modelo y cada herramienta usada;
- el agente aparece en el **inventario de agentes del centro de administración de Microsoft 365**
  como una instancia registrada de Agent 365, con su propia identidad de **Microsoft Entra Agent ID**.

No hace falta modificar el código del agente: un **plugin de ADK** observa cada ejecución y la envía a
Agent 365 por **OTLP/HTTP (JSON)** con autenticación **servicio a servicio (S2S)**.

> Probado en noviembre de 2026 con google-adk 2.4.0, google-cloud-aiplatform 1.160.0,
> Python 3.12, Agent 365 CLI 2.0.4 y Gemini 2.5 Flash.

## Índice

1. [Arquitectura](#arquitectura)
2. [Contenido del repositorio](#contenido-del-repositorio)
3. [Requisitos](#requisitos)
4. [Guía paso a paso](#guía-paso-a-paso)
5. [Instrumentar tu propio agente](#instrumentar-tu-propio-agente)
6. [Qué se envía a Agent 365](#qué-se-envía-a-agent-365)
7. [Resultados en un agente real](#resultados-en-un-agente-real)
8. [Problemas conocidos y soluciones](#problemas-conocidos-y-soluciones)
9. [Alternativas evaluadas](#alternativas-evaluadas)
10. [Seguridad y datos](#seguridad-y-datos)
11. [Limpieza](#limpieza)
12. [Referencias](#referencias)

## Arquitectura

```text
 Usuario / aplicación
        │  async_stream_query(user_id = objectId de Entra, message)
        ▼
┌─────────────────────── Vertex AI Agent Engine (GCP) ────────────────────────┐
│  AdkApp(agent=root_agent, plugins=[A365ObservabilityPlugin])                │
│     │ callbacks de ADK: run, modelo, herramientas                           │
│     ▼                                                                       │
│  Plugin: invoke_agent (raíz) + chat por llamada al modelo                   │
│          + execute_tool por herramienta, en una misma traza                 │
│     │ 1. secreto del blueprint ← Secret Manager (solo ese secreto)          │
│     │ 2. token en dos pasos ← Microsoft Entra ID                            │
│     │ 3. POST OTLP/JSON                                                     │
└─────┼───────────────────────────────────────────────────────────────────────┘
      ▼
 Agent 365 Observability (S2S)  /tenants/{tenant}/otlp/agents/{agent-identity}/traces
      ├──► Microsoft Defender (CloudAppEvents)
      ├──► Centro de administración de Microsoft 365 (inventario y actividad)
      └──► Microsoft Purview
```

**Identidad.** Agent 365 exige que coincidan tres valores: el cliente del token (`azp`), el
`{agentId}` de la URL y el atributo `gen_ai.agent.id` de los spans. Los tres son el `appId` de la
**identidad de agente**. Esa identidad no tiene credenciales propias; las tiene su **blueprint**:

1. Con el secreto del blueprint se pide un token para `api://AzureADTokenExchange/.default`,
   indicando `fmi_path = <appId de la identidad de agente>`.
2. La identidad de agente usa ese token como `client_assertion` para pedir el token de Agent 365
   (`9b975845-388f-4429-889e-eab1ef63949c/.default`).

El token resultante no lleva `roles` ni `scp`: Agent 365 lo acepta porque la identidad es una
**instancia registrada**, no por un permiso.

## Contenido del repositorio

| Fichero | Para qué |
|---|---|
| `agent/a365_observability.py` | **El plugin de ADK.** Convierte cada ejecución en spans de Agent 365, obtiene el token y los envía. Sin dependencias de OpenTelemetry |
| `agent/a365_entrypoint.py` | Punto de entrada de Agent Engine: `AdkApp(agent=root_agent, plugins=[plugin])` |
| `agent/sample_agent/` | Agente de ejemplo: analista de riesgo de renovación con cuatro herramientas y datos simulados |
| `agent/requirements.txt` | Versiones fijadas que se instalan en Agent Engine (y en local) |
| `config.example.json` | Plantilla de `config.json`: tenant, identidades, proyecto de GCP, secreto, nombre del despliegue |
| `a365.config.example.json` | Plantilla de `a365.config.json` para la CLI de Agent 365 |
| `new-agent-identity.ps1` | Crea la identidad de agente con la credencial del blueprint (paso 4) |
| `store-blueprint-secret.ps1` | Guarda el secreto del blueprint en Secret Manager sin mostrarlo (paso 8) |
| `send-test-span.ps1` | Prueba sin agente: token, y una ejecución sintética enviada a Agent 365 (paso 10) |
| `run_local.py` | Ejecuta el agente instrumentado en local; sin `--send` guarda las peticiones OTLP en `out/` |
| `deploy.py` | Crea o actualiza el agente en Agent Engine; sin `--apply` solo muestra el plan |
| `query.py` | Pregunta al agente desplegado |
| `gcp_auth.py` | Credenciales de GCP para los scripts (ADC o, si fallan, el token de `gcloud`) |

`config.json`, `a365.config.json`, `a365.generated.config.json`, `deployment.json` y `out/` son
locales y están en `.gitignore`: contienen identificadores de tu tenant y de tu proyecto, y `out/`
contiene preguntas y respuestas.

## Requisitos

**Microsoft 365 / Entra ID**

- Un tenant con **Microsoft Agent 365** habilitado (programa Frontier y una licencia que lo incluya,
  por ejemplo Microsoft 365 E7).
- Una cuenta con rol **Administrador global** para el registro (basta *Agent ID Developer* para crear
  el blueprint, pero el registro y los consentimientos piden más).
- Acceso a **Microsoft Defender** (búsqueda avanzada) para comprobar la actividad.

**Herramientas locales** (los comandos de esta guía son de PowerShell 7 en Windows)

- [.NET SDK 10](https://dotnet.microsoft.com/download) y la CLI de Agent 365:
  `dotnet tool install -g Microsoft.Agents.A365.DevTools.Cli`
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- Módulos de PowerShell `Microsoft.Graph.Authentication` y `Microsoft.Graph.Applications` (misma versión)
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) (`gcloud`)
- Python 3.12

**Google Cloud**

- Un proyecto con facturación y permisos para habilitar API, crear cuentas de servicio, secretos y
  agentes de Agent Engine.

## Guía paso a paso

En los comandos, sustituye lo que va entre `< >`.

### Paso 0. Preparar el repositorio

```powershell
git clone https://github.com/<tu-usuario>/agent365-vertex-adk-observability.git
cd agent365-vertex-adk-observability

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r agent/requirements.txt

Copy-Item config.example.json config.json
Copy-Item a365.config.example.json a365.config.json
```

Elige ya el nombre del agente. En los ejemplos es `a365-renewal-risk-sample`; se usa como
`displayName` en `config.json` y como base de los nombres del blueprint y de la identidad en
`a365.config.json`.

### Paso 1. Herramientas de Microsoft e inicio de sesión

```powershell
dotnet tool install -g Microsoft.Agents.A365.DevTools.Cli
a365 --version

Install-PSResource Microsoft.Graph.Authentication, Microsoft.Graph.Applications -Scope CurrentUser
Get-Module -ListAvailable Microsoft.Graph.Authentication, Microsoft.Graph.Applications   # misma versión

az login --tenant <tenant-id>      # con el administrador global
```

> Si `Save-Module`/`Install-Module` dicen `Unable to find repository 'PSGallery'`, usa
> `Install-PSResource` (PSResourceGet). Si la red bloquea `cdn.powershellgallery.com` (error TLS),
> instala desde un repositorio interno de tu organización o copia la carpeta de los módulos desde
> otro equipo a `Documents\PowerShell\Modules`.

### Paso 2. Configurar la CLI de Agent 365

Edita `a365.config.json`:

| Campo | Valor |
|---|---|
| `tenantId` | Id. del tenant |
| `clientAppId` | Aplicación cliente que usa la CLI (ver abajo) |
| `agentIdentityDisplayName` | Nombre de la identidad, p. ej. `a365-renewal-risk-sample Identity` |
| `agentBlueprintDisplayName` | Nombre del blueprint, p. ej. `a365-renewal-risk-sample Blueprint` |
| `aiTeammate` / `useBlueprint` | `false` / `true` (agente con blueprint, no *AI Teammate*) |

Comprueba los requisitos (no crea nada):

```powershell
a365 setup requirements
```

La línea `Client App Configuration (<id>)` muestra la aplicación cliente; pon ese id en
`clientAppId`. Si no hay ninguna (ni la gestionada por Microsoft ni una del tenant llamada
*Agent 365 CLI*), el comando la pide y un administrador global puede crearla respondiendo `C`.
El aviso `Frontier Preview Program - Tenant enrollment cannot be verified automatically` es normal.

### Paso 3. Crear el blueprint

Simula y después ejecuta:

```powershell
a365 setup all --authmode s2s --dry-run
a365 setup all --authmode s2s
```

`--authmode s2s` indica que el agente se autentica como servicio (sin usuario). El comando:

1. crea el **blueprint** (aplicación de Entra), su entidad de servicio y un **secreto de cliente**,
   que guarda cifrado para tu usuario de Windows en esta carpeta (`a365.generated.config.json` y
   `a365 setup blueprint --show-secret`);
2. configura los **permisos heredables** del blueprint;
3. abre el navegador para el **consentimiento de administrador de permisos delegados** (Microsoft
   Graph: Mail.ReadWrite, Mail.Send, Chat.ReadWrite, Files.ReadWrite.All…; Agent 365 Tools; Power
   Platform API);
4. crea la **identidad de agente** y la **registra** en Agent 365.

Esos permisos delegados sirven para que el agente actúe en nombre de usuarios (herramientas de Work IQ,
correo, Teams). **La telemetría S2S no los necesita.** Tienes dos opciones:

- **Mínimo privilegio (recomendada).** No aceptes el consentimiento: espera a que caduque (180 s) y
  responde `N` a `Add these permissions to the blueprint programmatically?`. La CLI termina con
  `Setup cancelled`, pero el blueprint y su secreto ya existen. Sigue con el paso 4.
- **Aceptar el consentimiento.** La CLI continúa y crea y registra la identidad. Salta al paso 6.

### Paso 4. Crear la identidad de agente (si paraste en el consentimiento)

```powershell
.\new-agent-identity.ps1                          # patrocinador: el usuario de az
# .\new-agent-identity.ps1 -SponsorUpn alguien@contoso.com
```

El script lee el blueprint de `a365.generated.config.json`, obtiene un token de aplicación del propio
blueprint (que incluye `AgentIdentity.CreateAsManager`) y llama a
`POST https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentity` con el nombre
de `agentIdentityDisplayName` y un **patrocinador** (obligatorio). Muestra el `appId` de la identidad.

> `a365 setup all --agent-registration-only --dry-run` dice que crearía la identidad, pero en la
> versión 2.0.4 no lo hace: sin identidad previa falla con `Agent identity ID is not set in config`.

### Paso 5. Registrar la identidad en Agent 365

```powershell
a365 setup all --authmode s2s --agent-registration-only
```

Busca la identidad por nombre y la registra. Resultado esperado:

```text
Found agent identity (ID: <identity-appId>). Using it for registration.
Agent registered (ID: <registration-id>)
Setup completed successfully
```

### Paso 6. Anotar los identificadores

Completa en `config.json`:

| Campo | Valor |
|---|---|
| `tenantId` | Id. del tenant |
| `agentAppId` | `appId` de la **identidad de agente** (pasos 4 o 3) |
| `agentBlueprintId` | `agentBlueprintId` de `a365.generated.config.json` |
| `agentName`, `agentDescription` | Nombre y descripción que verás en Defender y en el inventario |
| `testUserId` | `objectId` de Entra del usuario que hará las pruebas (`az ad user show --id <upn> --query id -o tsv`) |

### Paso 7. Preparar Google Cloud

```powershell
$PROJECT = "<gcp-project-id>"; $REGION = "us-central1"; $SA = "a365-adk-agent"
$SA_EMAIL = "$SA@$PROJECT.iam.gserviceaccount.com"

gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com iam.googleapis.com `
    logging.googleapis.com --project $PROJECT

# Identidad con la que se ejecuta el agente en Agent Engine
gcloud iam service-accounts create $SA --project $PROJECT --display-name "ADK agent with Agent 365 observability"
foreach ($role in 'roles/aiplatform.user', 'roles/logging.logWriter') {
    gcloud projects add-iam-policy-binding $PROJECT --member "serviceAccount:$SA_EMAIL" --role $role --condition=None | Out-Null
}

# Tu usuario necesita poder desplegar con esa cuenta de servicio (iam.serviceAccounts.actAs)
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL --project $PROJECT `
    --member "user:<tu-cuenta-de-gcp>" --role roles/iam.serviceAccountUser
```

- `roles/aiplatform.user` permite al agente llamar a Gemini; `roles/logging.logWriter`, escribir en
  Cloud Logging (donde el plugin deja el resultado de cada envío).
- El acceso al secreto lo concede `deploy.py` **solo sobre ese secreto** (paso 12).
- Opcional: `roles/cloudtrace.agent` y `roles/monitoring.metricWriter` si quieres la telemetría propia
  de Agent Engine; sin ellos verás avisos `Failed to export span/metrics batch code: 403`, que no
  afectan a Agent 365.

Completa en `config.json`: `gcpProject`, `gcpRegion`, `serviceAccount` (`$SA_EMAIL`), `displayName`
(nombre del agente en Agent Engine) y `secretName` (p. ej. `a365-blueprint-secret`).

### Paso 8. Guardar el secreto del blueprint en Secret Manager

```powershell
.\store-blueprint-secret.ps1
```

Lee el secreto con `a365 setup blueprint --show-secret` (por eso debe ejecutarse en esta carpeta y con
el mismo usuario de Windows que ejecutó el paso 3), crea el secreto en Secret Manager si no existe y
añade una versión. El valor no se muestra ni queda en disco.

### Paso 9. Iniciar sesión en Google Cloud

```powershell
gcloud auth login
gcloud auth application-default login     # opcional, ver nota
gcloud config set project <gcp-project-id>
```

> Los scripts usan las *Application Default Credentials* (ADC). Si no existen o piden
> reautenticación (`RefreshError: Reauthentication is needed`), `gcp_auth.py` usa el token de la
> cuenta activa de `gcloud` (válido una hora) y lo indica con `ADC unavailable ...`.

### Paso 10. Probar el envío sin agente

```powershell
.\send-test-span.ps1
```

Obtiene el token en dos pasos, muestra sus *claims* (nunca el token) y envía una ejecución
sintética (`invoke_agent` + `chat` + `execute_tool`). Resultado esperado:

```text
Token: aud=9b975845-388f-4429-889e-eab1ef63949c azp=<identity-appId> tid=<tenant-id> roles= scp=
Eligibility: HTTP 403
POST traces: HTTP 200  conversationId=<guid>  traceId=<hex>
partialSuccess: rejectedSpans=0
  invoke_agent  flashpoint  sent
  ...
```

- `azp` debe ser la identidad de agente; `roles` y `scp` vacíos es lo correcto.
- El 403 de *Eligibility* es normal con este token (la consulta de elegibilidad pide el rol
  `Agent365.Observability.OtelWrite`, que una instancia registrada no necesita).
- Un HTTP 200 no basta: cada span debe aparecer como `sent` en `results`.

### Paso 11. Probar el agente en local

```powershell
python run_local.py                                       # no envía: guarda el OTLP en out/
python run_local.py "Assess renewal risk for Fabrikam" --send
```

Usa el mismo punto de entrada que Agent Engine y llama a Gemini en tu proyecto. Sin `--send` verás:

```text
  -> tool get_account
  -> tool list_incidents
  -> tool get_usage_trend
  -> tool get_contract_terms
Renewal Risk: **High** ...
[dry run] 8 spans -> otlp-<fecha>-1.json: chat, chat, chat, execute_tool:get_account, ..., invoke_agent
```

Revisa el JSON de `out/` para ver exactamente qué se enviaría.

### Paso 12. Desplegar en Agent Engine

```powershell
python deploy.py            # plan, sin cambios
python deploy.py --apply    # permiso sobre el secreto + creación (o actualización) del agente
```

El plan muestra proyecto, nombre, cuenta de servicio, el permiso sobre el secreto y las variables de
entorno (`A365_*`). Con `--apply`:

1. concede a la cuenta de servicio `roles/secretmanager.secretAccessor` **solo sobre el secreto**;
2. sube el código (`sample_agent`, plugin, punto de entrada, `requirements.txt`) y crea el agente
   (0–1 instancias, 1 CPU, 2 GiB), o lo **actualiza** si ya lo desplegó este script;
3. guarda el recurso en `deployment.json`.

La creación tarda varios minutos.

### Paso 13. Usar el agente desplegado

```powershell
python query.py "Assess renewal risk for Contoso"
python query.py "Compare renewal risk between Contoso and Northwind Traders"
```

Pasa como `user_id` el `objectId` de Entra de quien pregunta (por defecto `testUserId`): así Defender
atribuye la ejecución a ese usuario.

Comprueba el envío en Cloud Logging:

```powershell
$ENGINE_ID = (Get-Content deployment.json | ConvertFrom-Json).resource.Split('/')[-1]
gcloud logging read "resource.type=`"aiplatform.googleapis.com/ReasoningEngine`" AND resource.labels.reasoning_engine_id=`"$ENGINE_ID`" AND textPayload:`"Agent 365`"" `
    --project <gcp-project-id> --limit 5 --format "value(timestamp,textPayload)"
```

Cada ejecución deja una línea como:

```text
INFO a365_observability: Agent 365 export HTTP 200 rejectedSpans=0 invoke_agent=sent chat=sent execute_tool:get_account=sent ...
```

### Paso 14. Verificar en Defender

Espera entre 5 y 30 minutos. En **security.microsoft.com → Investigación y respuesta → Búsqueda →
Búsqueda avanzada**:

```kusto
let agentIdToFind = "<identity-appId>";
CloudAppEvents
| where Timestamp > ago(1d)
| where ActionType in ("InvokeAgent", "InferenceCall", "ExecuteToolBySDK", "ExecuteToolByGateway", "ExecuteToolByMCPServer")
| extend resData = parse_json(tostring(RawEventData))
| where resData.AgentId == agentIdToFind or resData.TargetAgentId == agentIdToFind or resData.PlatformTargetAgentId == agentIdToFind
| project Timestamp, ActionType, AccountObjectId, resData
| order by Timestamp desc
```

Resumen por tipo:

```kusto
let agentIdToFind = "<identity-appId>";
CloudAppEvents
| where Timestamp > ago(1d)
| where ActionType in ("InvokeAgent", "InferenceCall", "ExecuteToolBySDK")
| extend resData = parse_json(tostring(RawEventData))
| where resData.AgentId == agentIdToFind or resData.TargetAgentId == agentIdToFind
| summarize Eventos = count() by ActionType, bin(Timestamp, 10m)
| order by Timestamp asc
```

Una ejecución concreta (el `conversationId` es el id de sesión de Agent Engine):

```kusto
CloudAppEvents
| where Timestamp > ago(1d)
| where tostring(RawEventData) has "<conversationId>"
| project Timestamp, ActionType, AccountObjectId, RawEventData
| order by Timestamp asc
```

### Paso 15. Verificar el inventario

En **admin.microsoft.com → Agentes → Todos los agentes → Registro**, busca el agente por nombre o por
**Id. de agente** (`<identity-appId>`). En nuestras pruebas apareció menos de una hora después del
registro.

## Instrumentar tu propio agente

1. Copia `agent/a365_observability.py` junto a tu agente.
2. En el punto de entrada, registra el plugin:

   ```python
   from vertexai.agent_engines import AdkApp
   from a365_observability import A365ObservabilityPlugin
   from my_agent.agent import root_agent

   app = AdkApp(agent=root_agent, plugins=[A365ObservabilityPlugin.from_env()])
   ```

   Con `deploy.py`: cambia el `import` de `agent/a365_entrypoint.py`, pon tu paquete en
   `agent/<paquete>/`, ajusta `AGENT_PACKAGE` en `deploy.py` y añade tus dependencias a
   `agent/requirements.txt`. Las variables que necesite tu agente van en `agentEnv` de `config.json`.
3. Variables de entorno del plugin:

   | Variable | Obligatoria | Valor |
   |---|---|---|
   | `A365_TENANT_ID` | Sí | Tenant |
   | `A365_AGENT_APP_ID` | Sí | `appId` de la identidad de agente |
   | `A365_BLUEPRINT_ID` | Sí, con Agent ID | `appId` del blueprint (activa el token en dos pasos) |
   | `A365_AGENT_NAME` | Sí | Nombre visible |
   | `A365_SECRET_NAME` | Sí | Secreto de Secret Manager con la credencial |
   | `A365_SECRET_PROJECT` | No | Proyecto del secreto (por defecto, el del agente) |
   | `A365_AGENT_DESCRIPTION`, `A365_CHANNEL` (`web`), `A365_SERVICE_NAME` | No | Descripción, canal y `service.name` |
   | `A365_DEFAULT_USER_ID` | No | `user.id` cuando el `user_id` de la sesión no es un objectId |
   | `A365_DRY_RUN=1` | No | Construye y registra los spans, pero no envía nada |

4. Llama al agente con `user_id` = **objectId de Entra** del usuario. Opcionalmente, guarda en el
   estado de la sesión `a365_user_email`, `a365_user_name` y `a365_client_ip`.

**Cómo funciona el plugin**

| Callback de ADK | Qué hace |
|---|---|
| `before_run_callback` | Abre el span raíz `invoke_agent` y los atributos comunes |
| `on_event_callback` | Recoge el texto final de la respuesta |
| `before_model_callback` / `after_model_callback` | Abre y cierra un span `chat` por llamada al modelo (tokens, motivo de fin) |
| `before_tool_callback` / `after_tool_callback` | Abre y cierra un span `execute_tool` (argumentos, resultado; tipo `function` o `MCP Server` con su host) |
| `on_model_error_callback` / `on_tool_error_callback` | Marca el error y cierra la ejecución |
| `after_run_callback` | Envía la ejecución completa (en lotes de < 900 KB) |

- Es de **mejor esfuerzo**: cualquier fallo de telemetría se registra y nunca llega al agente.
- No usa el SDK de OpenTelemetry (construye el OTLP/JSON directamente), así que no hay conflictos de
  versiones con ADK.
- Recorta mensajes a 32 000 caracteres y argumentos y resultados de herramientas a 16 000, manteniendo
  JSON válido.
- Sin `A365_BLUEPRINT_ID` usa *client credentials* de una aplicación estándar de Entra
  (ver [Alternativas](#alternativas-evaluadas)).

## Qué se envía a Agent 365

| Span | En Defender | Atributos principales |
|---|---|---|
| `invoke_agent <nombre>` (raíz) | `InvokeAgent` | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.request.model`, `gen_ai.execution.type=HumanToAgent` |
| `chat <modelo>` | `InferenceCall` | `gen_ai.request.model`, `gen_ai.provider.name=gcp.vertex_ai`, mensajes, `gen_ai.usage.input_tokens`/`output_tokens` |
| `execute_tool <herramienta>` | `ExecuteToolBySDK` | `gen_ai.tool.name`, `gen_ai.tool.type`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |

Comunes a todos: `gen_ai.agent.id`, `gen_ai.agent.name`, `microsoft.a365.agent.blueprint.id`,
`microsoft.tenant.id`, `user.id`, `gen_ai.conversation.id` y `microsoft.session.id` (id de sesión de
Agent Engine), `microsoft.channel.name`, `server.address`, `service.name`.

## Resultados en un agente real

El método se validó primero con un agente real de análisis de riesgo de renovación (ADK en Agent
Engine), con herramientas MCP de CRM y de tickets y búsquedas en Vertex AI Search, sin cambiar su
código:

- Una ejecución típica genera unos 15 spans: 1 `invoke_agent`, 5–7 `chat` y 6–12 `execute_tool`, y
  llegan como el mismo número de eventos en Defender.
- Tras registrar la identidad con Entra Agent ID, el agente apareció en el inventario del centro de
  administración; con la aplicación estándar de Entra solo aparecía su actividad en Defender.
- Diez consultas seguidas: todas con `HTTP 200`, `rejectedSpans=0` y todos los spans `sent`.

Observaciones:

- Solo `InvokeAgent` lleva el usuario resuelto (`AccountObjectId`, UPN); los eventos hijos lo dejan
  vacío aunque el span envíe `user.id`.
- Las herramientas MCP también llegan como `ExecuteToolBySDK`, aunque el span lleve
  `gen_ai.tool.type = MCP Server`.
- La ingesta en Defender tarda **entre 5 y 30 minutos**; el inventario puede tardar más.
- Los ids de sesión numéricos de Agent Engine sirven como `gen_ai.conversation.id`.

## Problemas conocidos y soluciones

| Síntoma | Causa | Solución |
|---|---|---|
| `Save-Module`: `Unable to find repository 'PSGallery'`; descarga con error TLS | PowerShellGet sin PSGallery registrado o red que bloquea su CDN | `Install-PSResource`; repositorio interno o copia de los módulos (paso 1) |
| `a365 setup all` termina en `Setup cancelled` tras el consentimiento | No se aceptaron los permisos delegados | Pasos 4 y 5 (no hacen falta para la telemetría) |
| `--agent-registration-only`: `Agent identity ID is not set in config` | La identidad aún no existe | `new-agent-identity.ps1` y repetir |
| Token, paso 1: HTTP 400 | `client_id` no es el blueprint o falta/está mal `fmi_path` | `client_id` = blueprint, `fmi_path` = identidad de agente |
| `AADSTS7000215` (secreto no válido) | Secreto distinto del del blueprint | `store-blueprint-secret.ps1` de nuevo |
| POST traces: 403 `insufficient_scope` | Identidad no registrada (o aplicación estándar sin el rol `OtelWrite`) | `a365 setup all --authmode s2s --agent-registration-only` |
| `Eligibility: HTTP 403` | Normal con una instancia registrada sin rol | Ninguna |
| HTTP 200 pero nada en Defender | Ingesta en curso, o spans no `sent` | Esperar hasta 30 min; revisar `results` y `partialSuccess` |
| Actividad en Defender pero no en el inventario | Aplicación estándar de Entra, no instancia registrada | Pasos 3–5 |
| `RefreshError: Reauthentication is needed` | ADC caducadas | `gcloud auth application-default login`, o dejar que `gcp_auth.py` use el token de `gcloud` |
| `deploy.py`: permiso `iam.serviceAccounts.actAs` | Falta `roles/iam.serviceAccountUser` sobre la cuenta de servicio | Paso 7 |
| `deploy.py`: `already exists and was not deployed from here` | Hay otro agente con ese `displayName` | Cambia `displayName` |
| Logs: `Failed to export span/metrics batch code: 403` | Telemetría propia de Agent Engine sin roles de Cloud Trace/Monitoring | Ignorar u otorgar los roles opcionales (paso 7) |

## Alternativas evaluadas

| Opción | Resultado |
|---|---|
| **Plugin de ADK + identidad registrada de Entra Agent ID** (este repositorio) | Actividad en Defender y agente en el inventario, sin cambiar el código del agente y sin permisos delegados |
| Plugin de ADK + **aplicación estándar de Entra** con el rol de aplicación `Agent365.Observability.OtelWrite` y consentimiento | Funciona para la actividad en Defender, pero el agente **no aparece** en el inventario: una aplicación estándar no es una instancia registrada. El plugin lo admite si no se define `A365_BLUEPRINT_ID` |
| **Plataforma conectada Google Vertex AI** (centro de administración → Agentes → Plataformas conectadas) | Pide una **clave JSON** de cuenta de servicio (a menudo bloqueada por la política `iam.disableServiceAccountKeyCreation`), importa **todos** los agentes del proyecto y la región, requiere permisos de borrado y obtiene la telemetría por otra vía (Cloud Trace + exportación a BigQuery) |
| Microsoft OpenTelemetry Distro | No instrumenta ADK automáticamente; habría que añadir los *scopes* a mano en el agente |

## Seguridad y datos

- El único secreto es el del blueprint: vive en **Secret Manager** y solo la cuenta de servicio del
  agente puede leerlo. Los scripts nunca lo muestran. Renuévalo antes de que caduque (`a365 setup
  blueprint`, y después `store-blueprint-secret.ps1`).
- No se conceden permisos delegados ni el rol `OtelWrite`: la instancia registrada basta para enviar
  telemetría.
- Los spans incluyen **preguntas, respuestas y resultados de herramientas**, que quedan en Defender y
  Purview. Si contienen datos sensibles, recórtalos o enmascáralos en el plugin (`_messages_json`,
  `_clip_json`).
- `out/`, `config.json` y los ficheros de la CLI de Agent 365 contienen identificadores y datos de
  prueba: no los subas al repositorio (ya están en `.gitignore`).

## Limpieza

```powershell
# Agent Engine
$RESOURCE = (Get-Content deployment.json | ConvertFrom-Json).resource
Invoke-RestMethod -Method Delete -Headers @{ Authorization = "Bearer $(gcloud auth print-access-token)" } `
    -Uri "https://<region>-aiplatform.googleapis.com/v1/$RESOURCE"

# Secreto y cuenta de servicio
gcloud secrets delete <secretName> --project <gcp-project-id>
gcloud iam service-accounts delete <serviceAccount> --project <gcp-project-id>

# Registro, identidad y blueprint de Entra (desde esta carpeta)
a365 cleanup
```

## Referencias

- [Agent 365: integración directa con OpenTelemetry](https://learn.microsoft.com/microsoft-agent-365/developer/direct-open-telemetry-integration)
- [Agent 365: modelo de datos y conceptos de observabilidad](https://learn.microsoft.com/microsoft-agent-365/developer/observability-concepts)
- [Agent 365: referencia de atributos](https://learn.microsoft.com/microsoft-agent-365/developer/observability-attribute-reference)
- [Agent 365: solución de problemas de OTel directo](https://learn.microsoft.com/microsoft-agent-365/developer/direct-open-telemetry-troubleshooting)
- [Agent 365: identidad de agentes](https://learn.microsoft.com/microsoft-agent-365/developer/identity) y [registro](https://learn.microsoft.com/microsoft-agent-365/developer/registration)
- [CLI de Agent 365: `setup`](https://learn.microsoft.com/microsoft-agent-365/developer/reference/cli/setup)
- [Microsoft Graph: crear `agentIdentity`](https://learn.microsoft.com/graph/api/agentidentity-post)
- [Agent 365: conectar Google Vertex AI](https://learn.microsoft.com/microsoft-agent-365/admin/connected-platforms-google-vertex-ai)
- [Google ADK: plugins](https://google.github.io/adk-docs/plugins/)
- [Vertex AI Agent Engine](https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/overview)
