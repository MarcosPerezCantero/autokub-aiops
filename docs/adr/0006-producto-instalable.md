# ADR 0006 — Versión instalable antes de ampliar el catálogo

## Contexto
El prototipo vertical (ADR 0005) resuelve un CrashLoopBackOff de principio a fin, pero solo en este laboratorio. El código tiene fijados el namespace `otel-demo` y la lista de servicios de la demo, supone que cada contenedor se llama como su Deployment, se ejecuta como procesos en la máquina local con kubeconfigs temporales y solo habla con un proveedor de modelos.

El objetivo es que cualquier persona pueda probar el sistema en su propio clúster. Eso exige que sea configurable e instalable, y también que sea seguro probarlo: nadie deja que un agente desconocido modifique su clúster el primer día.

## Alternativas consideradas
1. **Mantener el orden previsto**: observabilidad, línea base, GitOps y resto del catálogo, y la instalación al final. El sistema solo funciona en este laboratorio hasta el cierre.
2. **Versión instalable primero, con el catálogo actual**: una versión 0.1 que solo cubre CrashLoopBackOff, pero funciona en cualquier clúster.
3. **Versión instalable con el catálogo completo**: más valor por versión, pero retrasa mucho tener algo que otras personas puedan probar.

## Decisión
Se adopta la alternativa 2. La versión 0.1 incluye:

1. **Configuración en un fichero**: namespace, servicios permitidos y límites. OPA los recibe como datos, el RBAC se genera desde la misma lista y no queda ningún dato de la demo en el código.
2. **Modo solo propuesta por defecto**: el sistema detecta, propone y consulta a OPA, y registra qué habría hecho sin ejecutarlo. La ejecución se activa de forma explícita.
3. **Instalación con un chart de Helm** e imágenes publicadas en GHCR. Agente, ejecutor y OPA corren como pods, con identidades propias.
4. **Modelo intercambiable** vía LiteLLM, con la clave de quien lo instala.
5. **Demo de prueba en un comando**: clúster k3d, tienda, sistema e incidente provocado.
6. **Licencia MIT** y release 0.1 con changelog.

Lo que no cambia es la frontera de seguridad: el agente solo lee, toda acción pasa por OPA y OPA y el ejecutor deniegan ante cualquier fallo.

## Consecuencias
- El modo solo propuesta permite probarlo en clústeres gestionados con GitOps sin resolver antes el ADR 0003: si no se ejecuta nada, Argo CD no tiene nada que revertir.
- Se adelanta parte de la fase del agente completo (pods separados en el clúster, LiteLLM), y se retrasan observabilidad, línea base y GitOps.
- La evaluación se hará sobre un sistema que ya se instala en cualquier clúster.
- La versión 0.1 sigue limitada a Deployments y a un solo tipo de incidente; los demás tipos de carga quedan como trabajo futuro.
