# ADR 0008 — Observabilidad sobre la pila que ya trae la aplicación

## Contexto
El plan preveía instalar kube-prometheus-stack y Loki. La tienda de demostración ya despliega su propia observabilidad: Prometheus (con las métricas de aplicación derivadas de las trazas por el Collector, *span metrics*, y las de contenedores), Grafana, Jaeger para trazas y OpenSearch para logs. Le faltan las métricas de estado de Kubernetes (reinicios, OOMKilled, réplicas), Alertmanager y las alertas del catálogo de incidentes.

## Alternativas consideradas
1. **kube-prometheus-stack + Loki**, como estaba previsto: la pila más habitual, pero duplica Prometheus y Grafana (más de 1 GB en el laboratorio) y deja las métricas de aplicación en un Prometheus distinto del de Kubernetes.
2. **Ampliar el Prometheus de la aplicación**: activar kube-state-metrics y Alertmanager en su chart y cargar ahí las alertas. Un solo Prometheus con métricas de aplicación y de Kubernetes.

## Decisión
Se adopta la alternativa 2. Es también lo habitual en una empresa: adaptarse a la observabilidad que ya existe en lugar de montar otra en paralelo.

- Las alertas viven en `observability/alerts.yml`, con tests unitarios de promtool (`make alerts-test`) que comprueban cuándo salta cada una y cuándo no.
- `infra/demo-values.py` las inyecta en el chart de la tienda junto a kube-state-metrics y Alertmanager, y añade una suma de control para que Prometheus se reinicie cuando cambian (ese Prometheus no recarga la configuración solo).
- Los logs se quedan en OpenSearch; no se instala Loki.

autokub no depende de esta pila: la versión 0.1 detecta leyendo el estado de los pods, y la detección por alertas usará el formato de Alertmanager, que es el mismo en cualquier instalación de Prometheus.

## Consecuencias
- Un solo Prometheus y un solo Grafana en el laboratorio.
- El chart de la tienda se aplica con `--force-conflicts`: los escenarios modifican Deployments con kubectl, y Helm 4 (server-side apply) se niega a pisar campos que no son suyos. Es el mismo problema de deriva que el incidente 5 del catálogo.
- Medido al probar las alertas con fallos reales:
  - Contar reinicios no sirve para CrashLoopBackOff sostenido, porque el back-off llega a 5 minutos entre reinicios; la alerta mira si el contenedor ha pasado por ese estado en los últimos 5 minutos.
  - Con `paymentFailure` al 50 %, la alerta de errores se dispara primero en `checkout` (el síntoma) mientras la de `payment` (la causa) sigue pendiente: el primer aviso apunta al servicio equivocado. Distinguir síntoma y causa exige seguir las llamadas en las trazas, lo que confirma la prioridad del incidente 2.
  - Las flags de la tienda no se pueden cambiar solo en su ConfigMap: flagd lo copia al arrancar, así que `scenarios/flags/set-flag.sh` lo reinicia. Y `make demo` vuelve a dejarlas todas en off.
  - La alerta `ContainerOOMKilled` detectó un incidente real no provocado: Grafana superaba su límite de 300 Mi. Se subió a 512 Mi en `deploy/otel-demo/values.yaml`.
  - Dos incidentes simultáneos se enmascaran: con `product-catalog` caído, los pedidos fallan antes de llegar a `payment`.
