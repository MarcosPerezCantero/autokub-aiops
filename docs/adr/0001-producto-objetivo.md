# ADR 0001 — Producto objetivo: fork de la OpenTelemetry Demo

## Contexto
El proyecto necesita un producto desplegado en Kubernetes sobre el que provocar y remediar incidentes, y que permita simular el trabajo DevOps de una empresa: llevar un producto a Kubernetes, construir su CI/CD, GitOps y observabilidad. El valor del agente LLM se demuestra sobre todo cuando el diagnóstico exige razonar entre servicios (síntoma y causa en sitios distintos).

## Alternativas consideradas
1. **App hecha a medida**: control total, pero se percibe como diseñada para que el agente gane.
2. **OpenTelemetry Demo**: microservicios en varios lenguajes, comunicación síncrona y asíncrona, instrumentación OpenTelemetry completa, fallos por feature flags, generador de carga y chart de Helm mantenido. Referencia conocida en la industria (usada por los principales vendors de observabilidad).
3. **Benchmarks de agentes (AIOpsLab, ITBench)**: útiles para validación externa, no como producto que operar.

## Decisión
Usar un fork de la OpenTelemetry Demo como producto objetivo, tratado como el software de la empresa. AIOpsLab queda como validación externa opcional en la evaluación.

## Consecuencias
- Hay que construir a mano parte del despliegue (manifiestos propios) para no limitarse a usar lo que trae hecho.
- El fork permite montar un pipeline de CI/CD realista para un repositorio con muchos servicios.
- Requisito de recursos alto: ~6 GB de RAM solo la demo; no levantar todo a la vez si la máquina va justa y desactivar componentes no usados.
- No cubre desarrollo propio de la aplicación ni nube (EKS/AKS, Terraform, IAM): se reconoce como limitación del laboratorio.
