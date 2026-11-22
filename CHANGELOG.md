# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones según [SemVer](https://semver.org/lang/es/).

## [Sin publicar]

## [0.2.0] - 2026-11-22

### Añadido
- **Panel de operación** ([ADR 0009](docs/adr/0009-panel-de-operacion.md)), instalado con el chart: incidentes abiertos y cerrados, recorrido por cada etapa, razonamiento del agente, hechos y decisiones de OPA y registro de eventos. Se abre con `kubectl port-forward` (`make try-panel`). Sustituye a la vista del flujo del laboratorio.
- **Modo `approve`**: lo que OPA permite espera a que una persona lo apruebe o lo rechace en el panel. Al aprobarla, el ejecutor vuelve a leer los hechos y a consultar a OPA antes de actuar. Las pendientes caducan (15 min por defecto) y entonces se avisa a una persona. `make try-approve`.
- **Chat de solo lectura con el agente** desde el panel: responde con las herramientas de lectura del diagnóstico, sin poder proponer ni ejecutar acciones. Preguntas y respuestas quedan registradas en el incidente.
- Marcas de tiempo de los eventos con milisegundos.

### Seguridad
- Solo el ejecutor y el panel tienen el token de aprobación (Secret generado por el chart): el agente no puede aprobar sus propias propuestas.
- El panel escucha solo en `127.0.0.1` dentro de su pod y no tiene Service: quién puede operar lo decide el RBAC de Kubernetes (`pods/portforward`). Rechaza peticiones de otros orígenes y nombres de host ajenos (*DNS rebinding*).
- Aprobar exige dos clics: la página se redibuja con cada evento y un clic dirigido a otro elemento no debe poder aplicar una acción.
- NetworkPolicies: el chat del agente solo acepta tráfico del panel; nadie llega al panel por la red.
- El panel solo muestra para aprobar las acciones pendientes que leyó del pod del ejecutor. El origen lo asigna el panel al leer los logs, no el contenido del evento: un agente manipulado no puede imprimir una pendiente falsa para que se apruebe otra sin verla.
- Una sola acción pendiente por incidente (la nueva sustituye a la anterior) y como máximo 100 a la vez; por encima, el incidente se escala.
- Ejecutor y panel leen el token de aprobación en cada petición: si el Secret se regenera (por ejemplo, en un renderizado de GitOps, donde `lookup` no está disponible), siguen usando el mismo sin reiniciarse.

### Cambiado
- `approval.timeoutSeconds` se puede pasar con `--set` (Helm lo lee como entero y la validación lo rechazaba).
- El panel acepta el origen `https://` cuando está detrás de un proxy con TLS.
- Los objetivos `make try-*` usan `--reset-then-reuse-values`: con `--reuse-values`, Helm no añade los valores por defecto nuevos del chart y la actualización falla.

### Probado
- 213 tests automáticos: política (48), ejecutor (50), agente, detector y chat (76), panel (18), RBAC (9) y chart renderizado (12).
- En k3d con el agente LLM: incidente real hasta la aprobación; el agente no puede aprobar desde su pod (403, sin token y sin acceso a Secrets); aprobación con nueva consulta a OPA, rollback y verificación en 2 min; rechazo sin cambios en el clúster; chat inaccesible desde otro namespace.

## [0.1.0] - 2026-11-09

Primera versión instalable en cualquier clúster de Kubernetes ([ADR 0006](docs/adr/0006-producto-instalable.md)). Cubre un tipo de incidente: pods en `CrashLoopBackOff` por una revisión defectuosa de un Deployment.

### Añadido
- **Instalación con Helm** (`oci://ghcr.io/marcosperezcantero/charts/autokub`) e imágenes para amd64 y arm64. Agente y ejecutor en pods con identidades distintas; OPA como sidecar del ejecutor, accesible solo desde `localhost`.
- **Modo propuesta, activado por defecto**: detecta, propone y consulta a OPA sin modificar el clúster. En este modo el ejecutor no tiene ningún permiso de escritura. `mode: execute` concede `patch` solo sobre los Deployments configurados.
- **Configuración en un fichero**: namespaces, Deployments permitidos, límites y detección. La política, el RBAC y el detector salen de la misma configuración.
- **Modelo intercambiable** ([ADR 0007](docs/adr/0007-interfaz-con-el-modelo.md)): API de Anthropic o cualquier API compatible con OpenAI (proxy de LiteLLM, Ollama, vLLM…). Sin clave, un proponedor de reglas fijas.
- **Demo en un comando**: `make try` crea un clúster k3d con una tienda de microservicios y autokub en modo propuesta.
- Historial del límite de frecuencia en el propio Deployment, para que sobreviva a reinicios del ejecutor y a rollbacks.
- NetworkPolicy: solo el pod del agente puede enviar propuestas al ejecutor.

### Seguridad
- `make try` solo actúa sobre el clúster de la demo (`k3d-autokub`), nunca sobre el contexto de kubectl que esté activo.
- Toda acción pasa por OPA, que deniega por defecto y también si falta la configuración o el historial no se puede interpretar.
- Los contenedores corren sin root, con sistema de ficheros de solo lectura y sin capacidades.
- La imagen del ejecutor, el único componente que escribe, no tiene dependencias de terceros.

### Probado
- 161 tests automáticos: política (48), ejecutor (33), agente y detector (63), vista (4), RBAC (8) y chart renderizado (5, con 14 combinaciones de valores inválidos). Se ejecutan en cada cambio.
- En un clúster k3d con la OpenTelemetry Demo: instalación desde un clúster vacío en menos de 4 minutos; propuesta sin cambios en el clúster; remediación en modo ejecución en 44 s, con verificación; 7 propuestas maliciosas bloqueadas (objetivo excluido, namespace ajeno, servicio sano, memoria sin OOM, acción fuera de catálogo, parámetro inyectado, inyección en el nombre); conexión al ejecutor rechazada desde otro namespace.
- Agente LLM real en el clúster: diagnóstico correcto de la causa (variable de entorno errónea en la última revisión) en 2 pasos y unos 5.300 tokens; acción aplicada 8 s después de la detección e incidente cerrado con verificación en 52 s.

### Limitaciones conocidas
- Solo Deployments y un tipo de incidente. OOMKilled tiene acción en la política, pero no se ha probado de punta a punta.
- Las pruebas usan siempre el mismo fallo provocado (variable de entorno errónea): todavía no se ha medido el acierto del agente sobre fallos variados.
- En clústeres con GitOps y autocorrección, el modo ejecución puede ser revertido por la herramienta de GitOps (ADR 0003, pendiente). El modo propuesta no tiene ese problema.
