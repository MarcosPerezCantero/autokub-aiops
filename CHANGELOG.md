# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones según [SemVer](https://semver.org/lang/es/).

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
