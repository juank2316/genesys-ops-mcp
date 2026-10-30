# genesys-ops-mcp

Servidor MCP para que Claude administre orgs de **Genesys Cloud**: usuarios, colas, skills, idiomas,
wrap-up codes y scripts, y tenga acceso genérico a toda la Platform API (más de 3.000 operaciones) a
través del swagger oficial. Soporta **varias orgs** (lab, clientes…), cada una con su propio modo de seguridad.

## Instalación para el equipo

Requisitos: Windows, [`uv`](https://docs.astral.sh/uv/), Git, Claude (Code) y acceso de lectura a este repositorio privado.

**1. Instalar el MCP** (una sola vez):

```powershell
uv tool install git+https://github.com/juank2316/genesys-ops-mcp
```

**2. Cargar los secretos de los clientes.** Pedile al responsable el archivo `.secrets` y, **por otro canal**, su
contraseña. Luego:

```powershell
genesys-mcp secrets import "$HOME\Downloads\equipo.secrets"
```

Se crean tus variables de entorno de usuario con los secretos. Después borrá el archivo `.secrets`.

**3. Registrar el servidor en Claude** (una sola vez):

```powershell
claude mcp add --scope user genesys -- genesys-mcp
```

**4. Verificar.** Cerrá y volvé a abrir Claude, y después:

```powershell
genesys-mcp check
```

**Actualizar** (cuando haya versión nueva o clientes nuevos):

```powershell
uv tool upgrade genesys-ops-mcp
```

Después reiniciá Claude. La lista de clientes viaja con el paquete, así que llega con la actualización. Si se agregó un
cliente, además necesitás un `.secrets` nuevo.

### Para el responsable del MCP

- **Agregar un cliente:**
  1. Agregá la sección en [`src/genesys_mcp/orgs.toml`](src/genesys_mcp/orgs.toml).
  2. Creá la variable de entorno con el secreto.
  3. Subí el cambio y avisale al equipo que actualice.
  4. Generá un `.secrets` nuevo con `genesys-mcp secrets export equipo.secrets` y compartilo.
- **Cuando alguien deja el equipo:** rotá los client secrets en Genesys (Admin > OAuth) y generá un `.secrets`
  nuevo con otra contraseña. Mientras no haya un gestor de contraseñas compartido, este es el punto débil:
  cualquiera con el archivo y la contraseña tiene todos los secretos.
- **Nunca** pongas `client_secret` en `orgs.toml`. Un test lo verifica.

Datos locales de cada persona: la auditoría y la caché quedan en `%USERPROFILE%\.genesys_mcp\`. Cada registro de
`audit.jsonl` incluye `usuario@equipo`.

## Configuración de clientes (orgs)

En Genesys Cloud, cada cliente necesita un OAuth client: **Admin > Integrations > OAuth > Add Client**, con
**Grant type** = Client Credentials y un rol con los permisos de la tabla de abajo, asignado a las divisiones donde se
vaya a operar.

La lista compartida está en `src/genesys_mcp/orgs.toml`. Para probar algo sin tocarla, se puede poner una copia en
`.genesys_mcp/orgs.toml` (en el proyecto) o en `%USERPROFILE%\.genesys_mcp\orgs.toml`: tiene prioridad sobre la
del paquete. Cada cliente es una sección `[orgs.<nombre>]`:

```toml
[orgs.acme-prod]
display_name = "ACME Brasil"        # se muestra en cada plan y resultado
environment = "prod"                # lab | dev | qa | prod (default: prod)
aliases = ["acme"]                  # otros nombres válidos; únicos entre todas las orgs
region = "sa-east-1"                # us-east-1, sa-east-1 (São Paulo), mx-central-1 (México)...
client_id = "..."
client_secret_env = "GENESYS_ACME_PROD_SECRET"
read_only = true
allow_delete = false
```

**No hay org por defecto.** Todas las tools exigen `org` (el nombre de la sección o un alias), así que ninguna
operación puede caer en el cliente equivocado por omisión. Convención sugerida: `<cliente>-<ambiente>`.

Flags de seguridad por org:

| Flag | Efecto |
|---|---|
| `read_only = true` | Bloquea toda escritura. Las búsquedas, consultas y exportes siguen permitidos |
| `allow_delete = false` | Bloquea cualquier `DELETE` (usuarios, colas, scripts, skills de usuario…) |

En la org de un cliente, recomendamos arrancar con `read_only = true`.

Los secretos van en variables de entorno de Windows (`client_secret_env`), no en el archivo:

```powershell
[Environment]::SetEnvironmentVariable("GENESYS_ACME_PROD_SECRET", "<secreto>", "User")
```

Después de agregar un cliente, reiniciá Claude para que el MCP lo cargue.

## Uso

Pedile a Claude cosas como:

- "Creá la cola *Soporte Nivel 2* en lab con ACW obligatorio de 30 s y agregá a juan@x.com y ana@x.com"
- "Dá de alta a estos 5 agentes con el rol Agent, skill Ventas nivel 4 y la cola Ventas"
- "Exportá el script *Encuesta* del lab y copialo a la org del cliente como *Encuesta v2*"
- "¿Qué endpoint uso para ver el tiempo de espera estimado de una cola?"

## Tools

| Grupo | Tools |
|---|---|
| Orgs | `list_orgs`, `test_connection`, `list_divisions`, `list_roles` |
| Usuarios | `search_users`, `get_user`, `create_user` (onboarding completo), `update_user`, `set_user_roles`, `set_user_skills`, `set_user_languages`, `delete_user` |
| Colas | `list_queues`, `get_queue`, `create_queue`, `update_queue` (deep merge), `delete_queue`, `list_queue_members`, `add_queue_members`, `remove_queue_members`, `set_queue_wrapup_codes` |
| Routing | `list_skills`, `create_skills`, `list_languages`, `create_languages`, `list_wrapup_codes`, `create_wrapup_codes` |
| Troncales | `list_trunks` (con estado de *Suppress user name*), `trunk_properties` (nombres y valores permitidos), `update_trunks` (cambio masivo por ids, nombres o patrón con comodines) |
| DID pools | `list_did_pools`, `create_did_pools` (alta masiva de números E.164, idempotente) |
| Scripts | `list_scripts`, `get_script`, `export_script`, `import_script`, `clone_script` (también entre orgs), `publish_script`, `delete_script` |
| API genérica | `api_search`, `api_describe`, `api_get` (solo lectura), `api_call` |

Comportamiento común:

- Donde tiene sentido, las tools aceptan **nombres o ids**, y los usuarios también por **email**.
- Las creaciones son **idempotentes**: si algo con ese nombre (o email) ya existe, no se duplica.
- Cada respuesta indica la **org** sobre la que se actuó.

### Confirmación en dos pasos

Toda tool que cambia o borra algo (incluido `api_call` con POST/PUT/PATCH/DELETE) funciona así:

1. **Plan** (llamada sin `confirmation_token`): no cambia nada. Las lecturas van a Genesys, así que se resuelven
   nombres y se detectan datos faltantes, pero cada escritura solo se registra. Devuelve:
   - `bloqueado`: falta algo (división, cola o usuario inexistente, campo obligatorio del body, org en solo
     lectura…). No hay token.
   - `sin_cambios`: no hay nada que hacer (p.ej. la cola ya existía).
   - `requiere_confirmacion`: org con su ambiente, requests exactos que se harían, advertencias (org productiva,
     borrados irreversibles) y un `confirmation_token`.
2. **Ejecución** (misma llamada + token): solo después de que el usuario aprueba el plan. El token es de un solo
   uso, vence a los 10 minutos y queda atado a la tool, la org y los argumentos exactos; si algo cambia, hay que
   planificar de nuevo. El id del token queda en `audit.jsonl`.

### Scripts

La API pública **no tiene un endpoint para crear scripts**. Este MCP usa el mismo servicio de upload que el provider
oficial de Terraform (`/uploads/v2/scripter`). La forma práctica de crear scripts es tomar uno existente como plantilla:
`export_script`, después editar el JSON (o usar `substitutions` con marcadores `{{clave}}`), y por último
`import_script` o `clone_script`.

## Seguridad y auditoría

- **Reintentos:** las lecturas se reintentan ante 429, 5xx y errores de red. Las escrituras **solo** se reintentan
  ante 429. Si fallan con un 5xx, el resultado es incierto y se informa sin repetir la operación, para no duplicar nada.
- **Rate limit:** 4 req/s y 4 en paralelo por org, configurable con `rate_per_sec` y `max_concurrency`.
- **Auditoría:** cada escritura queda registrada en `%USERPROFILE%\.genesys_mcp\audit.jsonl` (quién, org, tool,
  método, path, status y correlationId). Nunca se registran secretos ni bodies.
- **Confirmación:** ver *Confirmación en dos pasos*. Es el servidor el que la exige, no depende del modelo.
- **Aprobaciones:** las tools llevan anotaciones de MCP (`readOnlyHint` / `destructiveHint`). En Claude Code se pueden
  aprobar automáticamente las de lectura y dejar que pregunte por las de escritura.

## Permisos del rol

| Área | Permisos |
|---|---|
| Usuarios | `directory:user:add`, `directory:user:edit`, `directory:user:delete`, `directory:user:view` |
| Roles | `authorization:role:view`, `authorization:grant:add`, `authorization:grant:delete` |
| Colas | `routing:queue:view/add/edit/delete`, `routing:queueMember:manage` |
| Skills e idiomas | `routing:skill:view`, `routing:skill:manage`, `routing:skill:assign`, `routing:language:assign` |
| Wrap-up | `routing:wrapupCode:view`, `routing:wrapupCode:add` |
| Troncales | `telephony:plugin:all` |
| Scripts | `scripter:script:view/add/edit/delete`, `scripter:publishedScript:view/add` |

Para `api_call`, se necesitan los permisos que `api_describe` muestra para cada endpoint.

## Desarrollo

```powershell
git clone https://github.com/juank2316/genesys-ops-mcp
cd genesys-ops-mcp
uv sync
uv run pytest
```

Desde la carpeta del proyecto, el `.mcp.json` levanta el servidor con el código local (`uv run`) en vez del instalado.

Estructura: `src/genesys_mcp/client.py` (HTTP, auth, reintentos, políticas, auditoría), `config.py` (perfiles de org),
`app.py` (instancia FastMCP y helpers), `tools/*.py` (un módulo por dominio).

Créditos: ver `NOTICE`. Partes adaptadas de `ava-mcp` de Genesys (MIT).
