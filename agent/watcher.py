#!/usr/bin/env python3
"""Detector: vigila los pods, pide una acción, la envía al ejecutor y verifica el resultado.

Solo tiene permisos de lectura: cualquier cambio en el clúster pasa por el ejecutor.
En el sistema final, la detección la harán las alertas de Prometheus (ADR 0005).
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import rules

CONFIG_FILE = os.environ.get("AUTOKUB_CONFIG", "config/lab.json")
EVENTS_FILE = os.environ.get("AUTOKUB_EVENTS", ".local/events.jsonl")
KUBECONFIG = os.environ.get("AUTOKUB_READER_KUBECONFIG", ".local/reader.kubeconfig")
EXECUTOR_URL = os.environ.get("AUTOKUB_EXECUTOR_URL", "http://127.0.0.1:8090/actions")

# Valores por defecto de la detección; la configuración (sección "detection") puede cambiarlos
DETECTION = {
    "poll_seconds": 5,
    # Un reinicio aislado puede ser transitorio y Kubernetes ya lo gestiona
    "min_restarts": 2,
    # Sin readiness probes, un pod defectuoso parece listo un instante: hay que exigir estabilidad
    "stable_seconds": 30,
    "verify_timeout_seconds": 120,
    "max_attempts": 2,
    # Un incidente escalado sigue abierto hasta que el fallo lleva este tiempo sin aparecer
    "clear_seconds": 60,
}
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

PENDING, VERIFYING, ESCALATED, PROPOSED = "pending", "verifying", "escalated", "proposed"
# Estados en los que el incidente sigue abierto sin que se haga nada más hasta que el fallo desaparece
WAITING = {ESCALATED: "escalado", PROPOSED: "propuesta registrada"}


def load_config(path=CONFIG_FILE):
    """Lee los namespaces a vigilar y los parámetros de detección. Falla si la configuración no sirve."""
    with open(path, encoding="utf-8") as file:
        config = json.load(file)
    namespaces = config.get("namespaces")
    if not isinstance(namespaces, dict) or not namespaces:
        raise ValueError("la configuración no define namespaces")
    detection = {**DETECTION, **config.get("detection", {})}
    if set(detection) != set(DETECTION) or not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in detection.values()
    ):
        raise ValueError("sección detection no válida")
    return sorted(namespaces), detection


def get_json(namespace, resource):
    # Sin kubeconfig (en un pod), kubectl usa la ServiceAccount del pod
    kubeconfig = ["--kubeconfig", KUBECONFIG] if KUBECONFIG else []
    command = ["kubectl", *kubeconfig, "-n", namespace, "get", resource, "-o", "json"]
    return json.loads(subprocess.run(command, capture_output=True, text=True, timeout=30, check=True).stdout)


def post_action(payload):
    request = urllib.request.Request(
        EXECUTOR_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.load(error)
    except (OSError, ValueError):
        return {"status": "unreachable"}


def find_incidents(pods, replica_sets, namespace, min_restarts=DETECTION["min_restarts"]):
    """Devuelve un incidente por cada Deployment con pods que fallan de forma repetida.

    Se usan los reinicios y no el texto de estado, que alterna entre Error y CrashLoopBackOff.
    Los pods sanos de una revisión anterior no cuentan aunque acumulen reinicios.
    """
    owners = {}
    for replica_set in replica_sets:
        for owner in replica_set["metadata"].get("ownerReferences", []):
            if owner.get("kind") == "Deployment":
                owners[replica_set["metadata"]["name"]] = owner["name"]

    incidents = {}
    for pod in pods:
        statuses = pod.get("status", {}).get("containerStatuses", [])
        failing = [s for s in statuses if not s.get("ready") and s.get("restartCount", 0) >= min_restarts]
        pod_owners = [o.get("name") for o in pod["metadata"].get("ownerReferences", [])]
        replica_set = next((name for name in pod_owners if name in owners), None)
        if failing and replica_set and owners[replica_set] not in incidents:
            terminated = failing[0].get("lastState", {}).get("terminated", {})
            incidents[owners[replica_set]] = {
                "namespace": namespace,
                "deployment": owners[replica_set],
                "replica_set": replica_set,
                "pod": pod["metadata"]["name"],
                "container": failing[0].get("name"),
                "restarts": failing[0].get("restartCount"),
                "last_reason": terminated.get("reason"),
                "exit_code": terminated.get("exitCode"),
            }
    return incidents


def deployment_health(deployment, replica_sets, pods):
    """Devuelve si el despliegue está sano y el total de reinicios de su revisión actual.

    `kubectl get deployment` puede mostrar 1/1 con una revisión rota, porque sigue activa
    la anterior: se exige que todas las réplicas sean de la revisión actual.
    """
    name = deployment["metadata"]["name"]
    spec_replicas = deployment["spec"].get("replicas", 1)
    status = deployment.get("status", {})
    revision = deployment["metadata"].get("annotations", {}).get(REVISION_ANNOTATION)
    current = {
        rs["metadata"]["name"]
        for rs in replica_sets
        if rs["metadata"].get("annotations", {}).get(REVISION_ANNOTATION) == revision
        and any(o.get("name") == name for o in rs["metadata"].get("ownerReferences", []))
    }
    current_pods = [
        pod
        for pod in pods
        if any(o.get("name") in current for o in pod["metadata"].get("ownerReferences", []))
    ]
    statuses = [s for pod in current_pods for s in pod.get("status", {}).get("containerStatuses", [])]
    healthy = (
        status.get("observedGeneration", 0) >= deployment["metadata"].get("generation", 0)
        and status.get("updatedReplicas", 0) == spec_replicas
        and status.get("readyReplicas", 0) == spec_replicas
        and status.get("replicas", 0) == spec_replicas
        and not status.get("unavailableReplicas")
        and len(current_pods) == spec_replicas
        and bool(statuses)
        and all(s.get("ready") for s in statuses)
    )
    return healthy, sum(s.get("restartCount", 0) for s in statuses)


class Watcher:
    def __init__(
        self, propose=rules.propose, send=post_action, events_file=EVENTS_FILE, clock=time.monotonic, detection=None
    ):
        self.propose = propose
        self.send = send
        self.events_file = events_file
        self.clock = clock
        self.detection = {**DETECTION, **(detection or {})}
        # Incidentes abiertos, por namespace y Deployment
        self.active = {}

    def emit(self, incident_id, event, summary, component="detector", **data):
        record = {
            "incident_id": incident_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "component": component,
            "event": event,
            "summary": str(summary)[:2000],
            "data": data,
        }
        line = json.dumps(record, ensure_ascii=False)
        if self.events_file:  # En el clúster no hay fichero compartido: basta la salida estándar
            os.makedirs(os.path.dirname(self.events_file) or ".", exist_ok=True)
            with open(self.events_file, "a", encoding="utf-8") as events:
                events.write(line + "\n")
        print(line, flush=True)

    def tick(self, pods, replica_sets, deployments, namespace):
        """Procesa una lectura de un namespace. Cada namespace se trata por separado."""
        incidents = find_incidents(pods, replica_sets, namespace, self.detection["min_restarts"])
        by_name = {deployment["metadata"]["name"]: deployment for deployment in deployments}

        for (state_namespace, name), state in list(self.active.items()):
            if state_namespace != namespace:
                continue
            if state["phase"] == VERIFYING and name in by_name:
                self.verify(name, state, by_name[name], replica_sets, pods, incidents.get(name))
            elif state["phase"] in WAITING:
                if name in incidents:
                    state["last_seen"] = self.clock()
                elif self.clock() - state["last_seen"] >= self.detection["clear_seconds"]:
                    del self.active[(namespace, name)]
                    summary = f"{WAITING[state['phase']]}; sin fallos desde entonces"
                    self.emit(state["id"], "incident_closed", summary, deployment=name)

        for name, incident in incidents.items():
            state = self.active.get((namespace, name))
            if state is None:
                state = {"id": f"inc-{uuid.uuid4().hex[:8]}", "phase": PENDING, "attempts": [], "namespace": namespace}
                self.active[(namespace, name)] = state
                self.emit(state["id"], "incident_detected", name, **incident)
            if state["phase"] == PENDING:
                self.remediate(state, incident)

    def remediate(self, state, incident):
        state["incident"] = incident
        action = self.propose({**incident, "id": state["id"], "previous_attempts": list(state["attempts"])})
        result = self.send({"incident_id": state["id"], "action": action})
        status = result.get("status")
        if status == "unreachable":
            return  # Se reintenta en la siguiente vuelta
        if status == "executed":
            state["attempts"].append({"action": action["type"], "target": action.get("target")})
            state.update(phase=VERIFYING, started=self.clock(), healthy_since=None, restarts=None)
        elif status == "escalated":
            state.update(phase=ESCALATED, last_seen=self.clock())
        elif status == "proposed":
            # Modo propuesta: no hay nada que verificar ni que reintentar
            state.update(phase=PROPOSED, last_seen=self.clock())
        else:
            reasons = ", ".join(result.get("reasons", [])) or "sin motivo"
            self.escalate(state, f"{incident['deployment']}: {status} ({reasons})")

    def escalate(self, state, summary):
        self.send({"incident_id": state["id"], "action": {"type": "escalate", "params": {"summary": summary}}})
        state.update(phase=ESCALATED, last_seen=self.clock())

    def verify(self, name, state, deployment, replica_sets, pods, incident):
        """Da el incidente por resuelto solo tras un periodo estable, sin nuevos reinicios."""
        healthy, restarts = deployment_health(deployment, replica_sets, pods)
        now = self.clock()
        if healthy and restarts == state["restarts"]:
            if state["healthy_since"] is None:
                state["healthy_since"] = now
            if now - state["healthy_since"] >= self.detection["stable_seconds"]:
                self.emit(state["id"], "verification_passed", f"{name} estable", deployment=name)
                self.emit(state["id"], "incident_closed", "recuperación verificada", deployment=name)
                del self.active[(state["namespace"], name)]
                return
        else:
            state["healthy_since"] = None
        state["restarts"] = restarts

        if now - state["started"] < self.detection["verify_timeout_seconds"]:
            return
        self.emit(state["id"], "verification_failed", f"{name} sigue sin recuperarse", deployment=name)
        current = incident or state["incident"]
        if len(state["attempts"]) < self.detection["max_attempts"]:
            state["phase"] = PENDING
            self.remediate(state, current)
        else:
            self.escalate(state, f"{name}: sin recuperar tras {len(state['attempts'])} intentos")


def main():
    namespaces, detection = load_config()
    watcher = Watcher(detection=detection)
    if os.environ.get("AUTOKUB_PROPOSER") == "llm":
        import llm_agent

        backend = llm_agent.backend_from_config(CONFIG_FILE)
        watcher.propose = lambda incident: llm_agent.propose(incident, emit=watcher.emit, backend=backend)
    proposer = os.environ.get("AUTOKUB_PROPOSER", "rules")
    print(f"Detector en marcha (proponedor: {proposer}; namespaces: {', '.join(namespaces)})", flush=True)
    while True:
        for namespace in namespaces:
            try:
                watcher.tick(
                    get_json(namespace, "pods")["items"],
                    get_json(namespace, "replicasets")["items"],
                    get_json(namespace, "deployments")["items"],
                    namespace,
                )
            except (subprocess.SubprocessError, ValueError) as error:
                print(f"Error al leer el namespace {namespace}: {error}", flush=True)
        time.sleep(detection["poll_seconds"])


if __name__ == "__main__":
    main()
