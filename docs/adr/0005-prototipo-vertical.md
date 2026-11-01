# ADR 0005 — Prototipo vertical antes de completar la plataforma

## Contexto
El plan inicial construía el sistema por capas: despliegue, observabilidad, línea base manual, CI/CD y GitOps, y solo entonces el agente. Es un orden lógico, pero deja la pieza de mayor riesgo para el final: no se sabe si la cadena completa funciona hasta que todo lo demás está hecho, y es ahí donde aparecen los problemas inesperados.

## Alternativas consideradas
1. **Mantener el orden por capas**: base más sólida, pero el riesgo principal se descubre tarde.
2. **Prototipo vertical primero**: un único incidente recorre toda la cadena con lo mínimo imprescindible, y después se amplía.
3. **Desarrollar el agente contra un clúster simulado**: rápido, pero no demuestra que funcione en Kubernetes real.

## Decisión
Se adopta la alternativa 2. El prototipo cubre **un incidente de principio a fin**: detectar → proponer → autorizar con OPA → ejecutar en Kubernetes → verificar.

Simplificaciones admitidas, cada una con su sustitución prevista:

| En el prototipo | En el sistema final |
|-----------------|---------------------|
| Detección consultando el estado de los pods | Alertas de Prometheus / Alertmanager |
| Contexto del agente: estado, eventos y logs vía API | Métricas, logs de Loki y trazas |
| Procesos fuera del clúster, con ServiceAccounts propias | Pods separados dentro del clúster |
| Ejecución directa por la API de Kubernetes | Según ADR 0003 |
| Tienda desplegada con el chart oficial | Manifiestos propios y Argo CD |
| Trazas del agente en logs estructurados | Langfuse |
| SDK de Anthropic directo | LiteLLM como pasarela |

Lo que **no** se simplifica es la frontera de seguridad: denegación por defecto, separación entre quien propone y quien ejecuta, y tests de las políticas. Es la tesis del proyecto, y añadirla después invalidaría lo aprendido.

### Pasos
1. Recuperar el clúster y añadir OPA al bootstrap.
2. Escenario `scenarios/crashloop` repetible, y su reversión. Hay que comprobar su impacto real: en un rolling update el pod anterior sigue activo hasta que el nuevo está listo, de modo que la tienda podría seguir funcionando mientras la revisión falla. Si es así, la recuperación se mide sobre la salud del despliegue.
3. Políticas Rego con tests, sin agente.
4. Ejecutor: recibe una propuesta, consulta a OPA, actúa y registra. Desde este paso, cada componente emite eventos JSON con un formato común (ver `docs/arquitectura.md`).
5. Cadena completa con un proponedor de reglas fijas, sin LLM.
6. Sustituir ese proponedor por el agente LangGraph.
7. Verificación posterior y reintento acotado.
8. Opcional: vista web que muestre el flujo en directo a partir de los eventos.

El paso 5 es deliberado: demuestra la cadena sin depender del modelo, y deja una referencia con la que comparar al agente.

El paso 8 queda fuera del criterio de terminado. Los eventos se emiten desde el principio porque cuestan poco y sirven también como auditoría; la vista se aborda solo cuando la cadena funciona.

### Criterio de terminado
- Un comando provoca el incidente y el sistema lo resuelve sin intervención humana.
- Una acción fuera de política, forzada a propósito, se bloquea y queda registrada.
- La ejecución se puede repetir desde un clúster recién creado.

## Consecuencias
- El riesgo técnico principal se enfrenta pronto.
- Observabilidad, línea base y GitOps se construyen después, ya sobre un sistema que funciona.
- La línea base manual sigue siendo necesaria para comparar, y debe medirse antes de la evaluación.
- Hay deuda temporal, recogida en la tabla anterior, que debe saldarse antes de evaluar.
- La evaluación se hace sobre el sistema final, no sobre el prototipo.
