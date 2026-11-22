# ADR 0009: Panel de operación

- Estado: aceptada
- Fecha: 2026-11-09

## Contexto

Hasta ahora, operar autokub en un clúster significa leer líneas JSON con `make try-events`. La vista del flujo solo funciona en el laboratorio, porque lee el fichero de eventos que comparten los procesos locales. Quien opera necesita además:

- ver los incidentes abiertos y su estado, el razonamiento del agente y las decisiones de OPA;
- en lugar de elegir entre "solo propone" y "ejecuta solo", poder **aprobar o rechazar** cada acción;
- **preguntar al agente** por un incidente sin abrir una consola.

Las dos últimas abren caminos nuevos hacia el clúster y hacia el modelo, así que hay que decidir cómo encajan en la frontera de seguridad ([arquitectura.md](../arquitectura.md#frontera-de-seguridad)).

## Decisión

### 1. La vista pasa a ser el panel y se instala con el chart

Un pod `autokub-panel` opcional (activado por defecto) con su propia ServiceAccount. Solo tiene permiso para leer pods y logs **de su propio namespace** (`autokub-system`): reconstruye los incidentes a partir de los eventos JSON que el agente y el ejecutor ya escriben en su salida estándar. En el laboratorio sigue leyendo el fichero de eventos. No tiene ningún permiso sobre los namespaces de la aplicación.

Los eventos pasan a llevar milisegundos en la marca de tiempo para poder ordenar los de dos pods que ocurren en el mismo segundo.

### 2. Un tercer modo: `approve`

| | `propose` | `approve` | `execute` |
|---|---|---|---|
| OPA evalúa la acción | Sí | Sí | Sí |
| Aplica la acción | No | Solo si una persona la aprueba | Sí |
| Permiso `patch` | No | Sí, sobre los `targets` | Sí, sobre los `targets` |

En `approve`, cuando OPA permite una acción, el ejecutor la guarda como pendiente y emite `action_pending_approval`. Una persona la aprueba o la rechaza desde el panel. Al aprobarla, el ejecutor **vuelve a leer los hechos y a consultar a OPA**, porque el clúster puede haber cambiado desde la propuesta, y solo entonces actúa.

La aprobación es una barrera más, nunca un atajo:

- Solo se puede aprobar lo que el agente propuso **y** OPA permitió. Una acción denegada no llega a pedir aprobación: una persona no puede saltarse la política desde el panel.
- El panel no envía acciones, solo el identificador de una pendiente y la decisión. No puede inventar una acción.
- Las pendientes caducan (`approval.timeout_seconds`, 15 min por defecto) y viven en la memoria del ejecutor: si se reinicia, se pierden y no se ejecuta nada (*fail-closed*).

El detector, en este modo, consulta al ejecutor el estado de la acción pendiente. Si se aplica, verifica como en `execute`; si se rechaza, el incidente queda abierto sin más acciones; si caduca, avisa a una persona.

### 3. Quién puede aprobar

El agente puede hablar con el ejecutor, así que si el ejecutor aceptara aprobaciones de cualquiera, el agente podría aprobar sus propias propuestas. Por eso:

- Las aprobaciones requieren un **token** que el chart genera en un Secret montado **solo** en el ejecutor y en el panel. El agente no lo tiene y su ServiceAccount no puede leer Secrets.
- El panel **escucha solo en `127.0.0.1` dentro de su pod** y no tiene Service. Se abre con `kubectl port-forward`, que exige permiso `pods/portforward` en `autokub-system`: quién puede operar lo decide el RBAC de Kubernetes, sin un sistema de usuarios propio.
- Frente a otras webs abiertas en el mismo navegador, el panel exige una cabecera propia en cada petición que modifica algo (el navegador no la envía entre orígenes sin permiso), comprueba `Origin` y acepta solo los `Host` locales, para evitar *DNS rebinding*.

El panel solo ofrece aprobar las pendientes que leyó **del pod del ejecutor**. El origen de cada evento lo asigna el panel según el pod del que lee el log, no el campo `component`, que declara quien escribe: así un agente manipulado no puede imprimir una pendiente falsa que muestre una acción inocua con el identificador de otra.

Ejecutor y panel leen el token en cada petición. `lookup` no está disponible en `helm template` ni en los renderizados de Argo CD o Flux, así que en esos casos el Secret se regenera en cada sincronización; leyéndolo cada vez, los dos pods acaban usando el mismo sin reiniciarse.

Limitaciones aceptadas:

- En el laboratorio (`make prototype`), el token está en `.local/approval.token` y los procesos corren con el mismo usuario: el agente podría leerlo. La separación solo es real en el clúster, con pods distintos.
- El token también queda en el Secret de la release de Helm, legible para quien pueda leer Secrets en `autokub-system`. Esas personas ya pueden operar el sistema.
- El panel puede leer los logs de todos los pods de `autokub-system`, porque el RBAC no permite acotar `pods/log` a pods con nombre variable. En ese namespace no hay otros datos sensibles.

El panel no identifica a cada persona. El nombre que se escribe al aprobar queda registrado como dato informativo, no como identidad verificada. Para identificar usuarios, el panel puede publicarse detrás de un proxy de autenticación (`panel.host: 0.0.0.0`), lo que queda a cargo de quien lo instala.

### 4. Chat con el agente, de solo lectura

El pod del agente expone un endpoint de preguntas que solo acepta tráfico del panel (NetworkPolicy). Responde con las mismas herramientas de lectura del diagnóstico, **sin `propose_action`**: desde el chat no se puede proponer ni ejecutar nada. Las preguntas y respuestas se registran como eventos (`chat_question`, `chat_answer`) en el incidente.

Atiende una pregunta a la vez y limita su longitud y los pasos del modelo, para acotar el coste.

## Alternativas descartadas

- **Aprobar desde el panel con permisos propios de escritura.** Abriría un segundo camino al clúster que no pasa por OPA.
- **Aprobar con anotaciones en el Deployment** (`kubectl annotate ... autokub.io/approve`). No requiere panel, pero la aprobación dependería de quien pueda anotar Deployments de la aplicación, y obliga al ejecutor a vigilar recursos. Se puede añadir más adelante para operar sin panel.
- **Usuarios y contraseñas en el panel.** Duplica lo que ya hace el RBAC de Kubernetes con `port-forward`, o un proxy de autenticación existente.
- **Guardar los eventos en una base de datos.** Más fiable que los logs de los pods, que se pierden al reiniciarlos, pero añade un componente con estado. En clústeres con un sistema de logs, los eventos ya se conservan allí.

## Consecuencias

- El panel deja de ser una herramienta del laboratorio y pasa a ser la forma normal de operar autokub.
- El historial del panel solo alcanza lo que conserven los logs de los pods del agente y del ejecutor.
- `approve` permite empezar a actuar sobre el clúster con una persona en el bucle, antes de confiar en `execute`.
- La política y el RBAC no cambian: `approve` usa los mismos permisos que `execute`.
