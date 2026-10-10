# ADR 0002 — Entorno del laboratorio: Ubuntu en WSL2 con Docker Engine

## Contexto
El host de desarrollo es Windows 11 Pro (i7-9700K, 8 núcleos, 32 GB de RAM). k3s nativo requiere Linux, por lo que el clúster se ejecutará con k3d (k3s dentro de contenedores Docker). Hay que decidir dónde corre Docker y desde qué sistema se trabaja. También se planteó si el entorno debía vivir en una máquina virtual para poder migrarlo.

Un contenedor no es una máquina virtual: es un proceso de Linux aislado por el kernel mediante *namespaces* (qué ve) y *cgroups* (cuántos recursos consume; es lo que hay detrás de los `requests/limits` de Kubernetes y del OOMKill). Las imágenes de la OpenTelemetry Demo y los nodos de k3s son Linux, así que en Windows siempre se ejecutan dentro de una VM Linux (incluso con Docker Desktop, que usa su propia distro de WSL2). La cuestión real no es "Windows o Linux", sino si se trabaja controlando esa VM desde fuera (Windows/PowerShell) o desde dentro (Linux).

## Alternativas consideradas
1. **Docker Desktop en Windows**: montaje mínimo, pero se trabaja desde PowerShell mientras que scripts, Makefile y CI son bash. Además, su VM (`docker-desktop`) es una caja negra no administrable.
2. **Ubuntu en WSL2 con Docker Engine instalado a mano**: Linux real, igual que los runners de GitHub Actions; Docker se instala y administra como servicio (systemd); la distro se puede exportar con `wsl --export`.
3. **VM completa (Hyper-V) con Ubuntu**: aporta snapshots, pero reserva RAM fija, añade gestión de red para acceder a las UIs desde Windows y es una capa más que mantener.

## Decisión
Ubuntu 24.04 LTS en WSL2 con Docker Engine (sin Docker Desktop). El repositorio vive dentro del sistema de archivos de Linux (`~/autokub`), no en `C:\`. Se elige 24.04 frente a 26.04 por madurez y compatibilidad probada del stack.

La portabilidad del laboratorio no se confía a la máquina, sino al código: todo el entorno debe poder recrearse desde Git (`git clone` + `make up`). La máquina es desechable ("cattle, not pets").

Los recursos de la VM de WSL2 se fijan en `%USERPROFILE%\.wslconfig` (20 GB de RAM, 8 CPU, 4 GB de swap, recuperación gradual de memoria) para que las mediciones de la evaluación sean reproducibles y evitar OOMKills ajenos a los escenarios.

## Consecuencias
- Entorno de trabajo homogéneo con la CI y la industria (bash, make, systemd).
- Docker Desktop no debe ejecutarse a la vez (conflicto por el comando `docker`).
- Trabajar sobre `/mnt/c` es lento: el código debe estar en el sistema de archivos de Linux.
- Las UIs (Grafana, Argo CD, Jaeger…) son accesibles desde el navegador de Windows en `localhost`.
- Sin snapshots: la vuelta atrás se hace destruyendo y recreando el clúster desde código.
- Todas las distros de WSL comparten una única VM y kernel; el límite de `.wslconfig` es global.
