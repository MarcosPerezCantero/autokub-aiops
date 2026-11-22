# ADR 0003 — Remediación con GitOps: rollback por la API y propuesta de cambio al repositorio

- Estado: aceptada. Implementación pendiente: llega con la fase de GitOps.
- Fecha: 2026-11-09 (decisión); 2026-11-22 (redacción).

## Contexto

En un proyecto gestionado con GitOps (Argo CD o Flux), el estado deseado vive en un repositorio Git, y la herramienta lo aplica al clúster. Hoy autokub remedia por la API de Kubernetes (`kubectl rollout undo`). Eso choca con GitOps de dos formas:

- Con **autocorrección** (`selfHeal` en Argo CD), la herramienta deshace el rollback en cuanto detecta que el clúster no coincide con Git: la remediación dura segundos o minutos.
- Sin autocorrección, el rollback se mantiene, pero Git sigue declarando la revisión defectuosa. El siguiente cambio que se sincronice la vuelve a desplegar, y el repositorio deja de describir lo que corre.

El modo propuesta (ADR 0006) evita el conflicto porque no modifica nada. Para `approve` y `execute` hay que decidir cómo se remedia.

## Alternativas consideradas

1. **Solo API directa**, como ahora. Rápida, pero Git y el clúster divergen, y con autocorrección no funciona.
2. **Solo cambio en Git** (commit o PR que fija la versión anterior). Coherente con GitOps, pero lenta: la mitigación depende de que alguien revise y fusione, y de la sincronización. Un incidente en curso no puede esperar.
3. **Pausar la sincronización y actuar por la API.** Funciona con autocorrección, pero exige que el ejecutor pueda modificar recursos de Argo CD o Flux: amplía sus permisos más allá de los Deployments permitidos.
4. **Mixta: rollback por la API para mitigar ya, y una PR al repositorio GitOps para que Git recoja el cambio.** La API resuelve el incidente; la PR lo hace duradero, y una persona la revisa.

## Decisión

Se adopta la alternativa 4.

1. El ejecutor aplica el rollback por la API, como hoy, tras la autorización de OPA.
2. Después abre una **PR** (nunca un commit directo ni una fusión) en el repositorio GitOps, que fija en el manifiesto del Deployment la imagen y la configuración de la revisión a la que se volvió. La PR enlaza el incidente y la decisión de OPA.
3. Fusionarla es decisión de una persona. Es una aprobación más, sobre el estado deseado y no sobre la acción.
4. La configuración indica, por namespace, si la aplicación se gestiona con GitOps y dónde está su repositorio. Sin esa sección, el comportamiento es el actual.

Abrir la PR es una acción más del catálogo, que OPA autoriza como las demás: solo sobre los repositorios y rutas configurados.

### Autocorrección

Con `selfHeal` activado, el rollback por la API se revertiría antes de fusionar la PR. En ese caso autokub **no actúa por la API**: solo abre la PR y avisa a una persona. La mitigación es más lenta, pero no se crea un bucle en el que autokub y Argo CD se deshacen el uno al otro. Pausar la sincronización (alternativa 3) queda descartado por los permisos que exigiría.

## Consecuencias

- El ejecutor necesita una credencial nueva: un token con permiso para crear ramas y PR en el repositorio GitOps, y nada más (ni fusionar ni escribir en la rama principal). Vive en un Secret montado solo en el ejecutor, como el token de aprobación.
- La política gana una acción (`open_pull_request`) y hechos nuevos: si el namespace usa GitOps y si tiene autocorrección.
- El incidente 5 del catálogo (drift de configuración) deja de necesitar `revert_drift`: Argo CD con autocorrección ya lo corrige sin agente. Queda como primer recorte del catálogo (ADR 0004).
- El ADR 0005 preveía la ejecución "según ADR 0003": la API se mantiene, y la PR se añade cuando el namespace usa GitOps.
- Mientras no se implemente, en clústeres con GitOps se recomienda el modo propuesta o `approve`.
