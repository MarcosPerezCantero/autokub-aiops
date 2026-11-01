# ADR 0004 — Alcance del agente: incidentes y acciones permitidas

## Contexto
El valor del sistema depende de poder evaluar por completo cada caso que declare cubrir. Un agente que "resuelve incidentes" en general no se puede ni evaluar ni autorizar: OPA solo puede imponer una política si el conjunto de acciones posibles es finito y conocido.

Hay que fijar dos listas cerradas: qué incidentes se abordan y qué puede hacer el agente sobre el clúster.

## Alternativas consideradas
1. **Herramienta genérica (`kubectl` libre) filtrada por OPA**: máxima flexibilidad, pero la política tendría que razonar sobre comandos arbitrarios, y cualquier hueco es una vía de escape.
2. **Catálogo amplio de incidentes y acciones**: más vistoso, pero con evaluación superficial de cada caso.
3. **Catálogo pequeño y acciones tipadas**: cada acción es una operación con parámetros validados. Menos cobertura, pero evaluable por completo.

## Decisión
Se adopta la alternativa 3.

### Incidentes

| # | Incidente | Provocación | Remediación |
|---|-----------|-------------|-------------|
| 1 | `CrashLoopBackOff` tras un cambio | Revisión defectuosa de un Deployment | `rollout_undo` |
| 2 | Fallo con síntoma y causa en servicios distintos | Revisión defectuosa de `payment`, con el síntoma visible en `checkout` | `rollout_undo` sobre el servicio causante |
| 3 | `OOMKilled` | Flag `emailMemoryLeak` | `set_memory_limit` y aviso a una persona |
| 4 | Despliegue que degrada la latencia | Revisión lenta de un servicio | `rollout_undo` |
| 5 | Drift de configuración (opcional) | Cambio manual sobre un recurso gestionado | `revert_drift` (mecanismo según ADR 0003) |

El orden es el de implementación. Lo que quede fuera de estos casos se escala, no se improvisa.

El incidente 2 es el caso central: la remediación es sencilla, pero exige localizar el servicio correcto, que es donde el diagnóstico del agente debe aportar frente a las reglas fijas.

El incidente 5 es opcional porque Argo CD con self-heal ya corrige el drift sin agente: es el primer recorte si falta tiempo.

### Relación con el self-healing de Kubernetes
Kubernetes reinicia contenedores y mantiene las réplicas, pero solo garantiza el estado que se le ha pedido, sea o no correcto. El agente actúa cuando eso no basta: su función es cambiar el estado deseado. Por eso reiniciar no es la remediación de una revisión defectuosa, ya que Kubernetes ya lo está intentando.

### Acciones

| Acción | Parámetros | Límites impuestos por la política |
|--------|------------|-----------------------------------|
| `rollout_restart` | Deployment | Solo Deployments de la lista permitida. Para casos concretos, como un pod bloqueado o una configuración que solo se aplica al arrancar |
| `rollout_undo` | Deployment | Debe existir una revisión anterior |
| `set_memory_limit` | Deployment, valor | Solo aumenta, como máximo ×2 y bajo un tope absoluto |
| `revert_drift` | Recurso | Solo recursos gestionados por el proyecto |
| `escalate` | Resumen | Siempre permitida: no modifica el clúster |

A todas las acciones que modifican el clúster se les aplica además:

- Denegación por defecto: lo que no esté permitido explícitamente se bloquea.
- Solo el namespace `otel-demo`.
- Lista permitida de Deployments de la aplicación. Quedan fuera `flagd`, el Collector, los almacenes de datos y la observabilidad.
- Límite de frecuencia por recurso, para evitar bucles de remediación.

### Fuera de alcance
Borrar recursos, `exec` en contenedores, leer o cambiar Secrets, tocar RBAC o nodos, cambiar imágenes a versiones nuevas, escalar réplicas, modificar feature flags y cualquier cambio en el código de los servicios.

Se descarta el escalado de réplicas, previsto inicialmente para OOMKilled: una fuga de memoria se reproduce en cada réplica, de modo que no mitiga el problema y sí aumenta el consumo. Tampoco se permite cambiar flags, porque en este entorno son el mecanismo de inyección de fallos y no algo que un agente de operaciones deba controlar.

## Consecuencias
- OPA razona sobre un conjunto finito de acciones tipadas, y las políticas se pueden probar caso por caso.
- El incidente 3 se **mitiga pero no se resuelve**: más memoria solo retrasa el fallo si hay una fuga. Por eso termina avisando a una persona, y la evaluación debe distinguir "mitigado" de "resuelto".
- Un incidente real fuera del catálogo acaba en `escalate`. Es el comportamiento deseado, y se evalúa como tal.
- `revert_drift` queda pendiente de ADR 0003.
