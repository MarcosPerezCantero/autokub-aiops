# Arquitectura

Este documento describe el sistema tal como funciona hoy en el laboratorio. Cómo se instala y funciona en cualquier clúster: [despliegue.md](despliegue.md).

## Componentes

```mermaid
flowchart TB
    subgraph K8S["Clúster Kubernetes"]
        APP["Tienda (otel-demo)<br/>microservicios"]
        API["API de Kubernetes"]
        subgraph OBS["Observabilidad"]
            OTEL["OpenTelemetry Collector"]
            PROM["Prometheus + Alertmanager"]
            LOKI["Loki"]
            JAEGER["Jaeger"]
        end
    end

    subgraph AGENTE["Plano de decisión (no fiable)"]
        LG["Agente LangGraph<br/>diagnostica y propone"]
        LLM["LLM vía LiteLLM"]
    end

    subgraph CONTROL["Plano de control (determinista)"]
        EXE["Ejecutor<br/>valida, consulta y actúa"]
        OPA["OPA + políticas Rego"]
        LOG[("Registro de auditoría")]
    end

    APP -->|telemetría OTLP| OTEL
    OTEL --> PROM & LOKI & JAEGER
    PROM -->|alerta| LG
    LG <--> LLM
    LG -->|solo lectura| OBS
    LG -->|solo lectura| API
    LG -->|propuesta de acción| EXE
    EXE -->|¿autorizada?| OPA
    EXE -->|acción acotada| API
    EXE --> LOG
    API --> APP
```

La separación clave es entre dos planos:

- **Plano de decisión.** Se considera no fiable, porque su entrada incluye contenido que un atacante puede influir (logs, por ejemplo) y su razonamiento puede fallar. Solo tiene credenciales de lectura.
- **Plano de control.** Es código determinista, sin LLM. Es el único con permisos de escritura.

Esto convierte a OPA en un verdadero *reference monitor*: no se apoya en que el agente decida consultarlo, porque el agente no dispone de credenciales para actuar por otra vía.

## Flujo de un incidente

```mermaid
sequenceDiagram
    participant D as Detección
    participant A as Agente
    participant E as Ejecutor
    participant O as OPA
    participant K as Kubernetes

    D->>A: Incidente detectado
    A->>K: Lee estado, eventos, logs
    A->>E: Propone acción (JSON tipado)
    E->>E: Valida el esquema
    E->>K: Obtiene los hechos del clúster
    E->>O: Acción + hechos
    alt Denegada
        O-->>E: deny + motivos
        E-->>A: Bloqueada y registrada
        A->>E: Replantea o escala
    else Permitida
        O-->>E: allow
        E->>K: Ejecuta la acción
        D->>K: Verifica la recuperación
        alt Estable durante 30 s
            D->>D: Incidente cerrado
        else Sin recuperar en 2 min
            D->>A: Pide otra acción con el historial (máximo 2 intentos)
            A->>E: Nueva propuesta o escalado
        end
    end
```

La verificación es de solo lectura, por lo que la hace el detector. Un despliegue se considera sano si todas sus réplicas son de la revisión actual, todos sus contenedores están listos y los reinicios no aumentan durante 30 segundos. Esta ventana es necesaria porque los servicios no tienen readiness probes y `kubectl get deployment` puede mostrar 1/1 con una revisión rota mientras sigue activa la anterior.

## Frontera de seguridad

| Mecanismo | Qué garantiza |
|-----------|---------------|
| Credenciales separadas | El agente solo lee; el ejecutor es el único que escribe |
| RBAC del ejecutor | Limitado al namespace `otel-demo` y a los Deployments permitidos, sin Secrets |
| Operaciones fijas | El ejecutor implementa cada acción del catálogo; nunca ejecuta texto generado por el modelo |
| Validación de esquema | Una salida malformada del modelo se rechaza antes de llegar a OPA |
| Hechos obtenidos por el ejecutor | La política evalúa datos leídos del clúster, no lo que el modelo afirma |
| Denegación por defecto | Si OPA no responde o no hay regla, la acción no se ejecuta (*fail-closed*) |
| Límite de frecuencia | Corta los bucles de remediación |
| Registro de auditoría | Cada propuesta y decisión queda registrada, también las bloqueadas |

RBAC y OPA son capas complementarias: RBAC no distingue, por ejemplo, un cambio de memoria razonable de uno excesivo; OPA sí. Y si la política tuviera un error, RBAC sigue acotando el daño posible.

### Alternativa considerada: control de admisión

OPA Gatekeeper impone políticas en la propia API de Kubernetes, por lo que no depende de ningún componente propio. Se mantiene el ejecutor porque aplica con más facilidad límites de frecuencia y condiciones ligadas al incidente, y porque cada acción sigue siendo una operación fija. El control de admisión queda como posible capa adicional, no como sustituto.

## Contrato con OPA

Entrada que el ejecutor envía a la política:

```json
{
  "action": {
    "type": "rollout_undo",
    "namespace": "otel-demo",
    "target": "product-catalog",
    "params": {}
  },
  "facts": {
    "deployment_exists": true,
    "previous_revision": 4,
    "unhealthy_pods": 1,
    "actions_last_hour_on_target": 0
  }
}
```

`action` procede de la propuesta del agente; `facts` lo completa el ejecutor. `unhealthy_pods` cuenta los pods de la revisión actual que no están listos y acumulan reinicios: no depende del texto de estado, que alterna entre `Error` y `CrashLoopBackOff`. `set_memory_limit` usa además `oom_killed_recently` y `current_memory_limit_bytes`.

Respuesta:

```json
{ "allow": false, "deny_reasons": ["target no permitido"] }
```

La política está en `policies/remediation.rego` y se consulta en `POST /v1/data/autokub/remediation/decision`. `revert_drift` aún no está permitida, a la espera de ADR 0003.

## Trazabilidad del flujo

Cada componente emite un evento por cada paso del incidente, con un formato común:

```json
{
  "incident_id": "inc-0042",
  "timestamp": "2026-01-01T12:00:00Z",
  "component": "opa",
  "event": "action_denied",
  "summary": "target no permitido",
  "data": { "action": "rollout_undo", "target": "flagd" }
}
```

Los eventos son `incident_detected`, `agent_thinking`, `agent_text`, `agent_tool_call`, `agent_tool_result`, `agent_diagnosis`, `agent_failed`, `action_proposed`, `action_rejected`, `action_allowed`, `action_denied`, `action_executed`, `action_failed`, `verification_passed`, `verification_failed`, `incident_escalated` e `incident_closed`. El `incident_id` permite reconstruir el recorrido completo de cada incidente.

Sobre estos eventos se apoyan dos usos:

- **Auditoría y evaluación:** de ellos salen los tiempos y las acciones bloqueadas.
- **Vista del flujo:** una página web que muestra en directo por qué paso va cada incidente, dónde se ha detenido y qué ha leído y razonado el agente. El razonamiento es el resumen que devuelve la API, no el proceso interno en bruto.

Langfuse complementa esta vista con el detalle interno del agente: prompts, respuestas, herramientas y coste. La vista propia cubre lo que Langfuse no ve: el recorrido por OPA, el ejecutor y Kubernetes.

La vista es de **solo lectura**: no tiene ninguna forma de actuar sobre el clúster, para no abrir un camino alternativo al ejecutor, y el sistema funciona igual sin ella.

Los eventos pueden contener texto que procede de logs, por lo que la vista debe tratarlo como contenido no fiable y mostrarlo escapado.

## Prompt injection indirecta

Los logs y eventos son datos que pueden contener instrucciones maliciosas. Se asume que algún intento puede engañar al modelo, y la defensa no depende de evitarlo: aunque el agente proponga una acción dañina, tiene que superar la validación, OPA y RBAC. Marcar el contenido no fiable en el prompt reduce la probabilidad de engaño, pero no se cuenta como control de seguridad.

Esta defensa limita **qué** puede hacerse, no si la decisión es acertada: un modelo manipulado aún podría proponer una acción permitida pero innecesaria, como reiniciar un servicio sano. La exigencia de hechos que justifiquen la acción y el límite de frecuencia reducen ese margen, y la evaluación debe medirlo.
