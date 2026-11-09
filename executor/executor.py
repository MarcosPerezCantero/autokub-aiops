#!/usr/bin/env python3
"""Ejecutor: único componente con permisos de escritura sobre el clúster.

Recibe una propuesta del agente, valida su formato, lee los hechos reales del clúster,
consulta a OPA y, si la acción está autorizada, la aplica. No contiene LLM: cada acción
es una operación fija, y nunca ejecuta texto generado por el modelo.
Diseño: docs/arquitectura.md
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

OPA_URL = os.environ.get("AUTOKUB_OPA_URL", "http://127.0.0.1:8181/v1/data/autokub/remediation/decision")
EVENTS_FILE = os.environ.get("AUTOKUB_EVENTS", ".local/events.jsonl")
KUBECONFIG = os.environ.get("AUTOKUB_KUBECONFIG", ".local/executor.kubeconfig")
ADDRESS = ("127.0.0.1", int(os.environ.get("AUTOKUB_EXECUTOR_PORT", "8090")))

MAX_BODY_BYTES = 16384
K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
INCIDENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MEMORY_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "k": 10**3, "M": 10**6, "G": 10**9}
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
# Historial de acciones del ejecutor sobre cada Deployment, para el límite de frecuencia.
# Vive en el propio Deployment para sobrevivir a reinicios del ejecutor (docs/despliegue.md).
HISTORY_ANNOTATION = "autokub.io/action-history"
HISTORY_WINDOW = timedelta(hours=1)


class KubectlError(Exception):
    pass


def run_kubectl(namespace, *args, payload=None):
    command = ["kubectl", "--kubeconfig", KUBECONFIG, "-n", namespace, *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, input=payload, timeout=30)
    except subprocess.TimeoutExpired as error:
        raise KubectlError("kubectl agotó el tiempo de espera") from error
    if result.returncode != 0:
        raise KubectlError(result.stderr.strip()[:300])
    return result.stdout


def ask_opa(opa_input):
    """Devuelve la decisión de OPA. Cualquier fallo deniega la acción (fail-closed)."""
    request = urllib.request.Request(
        OPA_URL,
        data=json.dumps({"input": opa_input}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.load(response).get("result", {})
    except (OSError, ValueError, urllib.error.URLError):
        return {"allow": False, "deny_reasons": ["OPA no disponible"]}
    if result.get("allow") is not True:
        return {"allow": False, "deny_reasons": result.get("deny_reasons") or ["denegada por defecto"]}
    return {"allow": True, "deny_reasons": []}


def parse_memory(quantity):
    match = re.fullmatch(r"(\d+)([A-Za-z]*)", str(quantity))
    if not match:
        return None
    number, unit = int(match[1]), match[2]
    if not unit:
        return number
    return number * MEMORY_UNITS[unit] if unit in MEMORY_UNITS else None


def validate(proposal):
    """Comprueba el formato de la propuesta. Devuelve el motivo del rechazo, o None."""
    if not isinstance(proposal, dict) or set(proposal) - {"incident_id", "action"}:
        return "propuesta con campos no reconocidos"
    incident_id = proposal.get("incident_id")
    if incident_id is not None and not (isinstance(incident_id, str) and INCIDENT_ID.match(incident_id)):
        return "incident_id no válido"
    action = proposal.get("action")
    if not isinstance(action, dict) or set(action) - {"type", "namespace", "target", "params"}:
        return "acción con campos no reconocidos"
    if not isinstance(action.get("type"), str) or not isinstance(action.get("params", {}), dict):
        return "acción con tipos no válidos"
    if action["type"] == "escalate":
        return None
    for field in ("namespace", "target"):
        if not (isinstance(action.get(field), str) and K8S_NAME.match(action[field])):
            return f"{field} no válido"
    return None


class Executor:
    def __init__(self, kubectl=run_kubectl, opa=ask_opa, events_file=EVENTS_FILE):
        self.kubectl = kubectl
        self.opa = opa
        self.events_file = events_file

    def emit(self, incident_id, event, summary, **data):
        record = {
            "incident_id": incident_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "component": "executor",
            "event": event,
            "summary": str(summary)[:500],
            "data": data,
        }
        line = json.dumps(record, ensure_ascii=False)
        if self.events_file:  # En el clúster no hay fichero compartido: basta la salida estándar
            os.makedirs(os.path.dirname(self.events_file) or ".", exist_ok=True)
            with open(self.events_file, "a", encoding="utf-8") as events:
                events.write(line + "\n")
        print(line, flush=True)

    @staticmethod
    def recent_history(deployment, now):
        """Marcas de tiempo de la última hora. None si la anotación no se puede interpretar."""
        raw = deployment["metadata"].get("annotations", {}).get(HISTORY_ANNOTATION)
        if raw is None:
            return []
        try:
            values = json.loads(raw)
            if not isinstance(values, list):
                return None
            stamps = [datetime.fromisoformat(stamp) for stamp in values]
        except (TypeError, ValueError):
            return None
        if any(stamp.tzinfo is None for stamp in stamps):
            return None
        return [stamp for stamp in stamps if now - stamp < HISTORY_WINDOW]

    def record_action(self, namespace, target, history, now):
        stamps = [stamp.isoformat(timespec="seconds") for stamp in [*history, now]]
        value = json.dumps(stamps, separators=(",", ":"))
        self.kubectl(namespace, "annotate", f"deployment/{target}", f"{HISTORY_ANNOTATION}={value}", "--overwrite")

    def get_json(self, namespace, *args):
        return json.loads(self.kubectl(namespace, "get", *args, "-o", "json"))

    def collect_facts(self, namespace, target):
        """Hechos leídos del clúster: la política no se apoya en lo que afirme el modelo."""
        facts = {"deployment_exists": False, "actions_last_hour_on_target": None}
        try:
            deployment = self.get_json(namespace, "deployment", target)
            selector = ",".join(f"{k}={v}" for k, v in deployment["spec"]["selector"]["matchLabels"].items())
            replica_sets = self.get_json(namespace, "replicasets", "-l", selector)["items"]
            pods = self.get_json(namespace, "pods", "-l", selector)["items"]
        except (KubectlError, KeyError, ValueError):
            return facts

        revision = int(deployment["metadata"]["annotations"].get(REVISION_ANNOTATION, 0))
        revisions = {}
        for replica_set in replica_sets:
            owners = replica_set["metadata"].get("ownerReferences", [])
            if any(owner.get("name") == target for owner in owners):
                number = int(replica_set["metadata"].get("annotations", {}).get(REVISION_ANNOTATION, 0))
                revisions[number] = replica_set["metadata"]["name"]

        current_replica_set = revisions.get(revision)
        containers = deployment["spec"]["template"]["spec"]["containers"]
        container = self.target_container(containers, pods, current_replica_set)
        unhealthy = 0
        oom_killed = False
        for pod in pods:
            statuses = pod.get("status", {}).get("containerStatuses", [])
            owners = [owner.get("name") for owner in pod["metadata"].get("ownerReferences", [])]
            if current_replica_set not in owners:
                continue  # Los pods de revisiones anteriores no justifican actuar
            if any(not status.get("ready") and status.get("restartCount", 0) > 0 for status in statuses):
                unhealthy += 1
            for status in statuses:
                # Solo el OOM del contenedor sobre el que se actuaría, no el de un sidecar
                if (
                    container
                    and status.get("name") == container["name"]
                    and status.get("lastState", {}).get("terminated", {}).get("reason") == "OOMKilled"
                ):
                    oom_killed = True

        limit = container.get("resources", {}).get("limits", {}).get("memory") if container else None
        history = self.recent_history(deployment, datetime.now(timezone.utc))
        facts.update(
            deployment_exists=True,
            actions_last_hour_on_target=None if history is None else len(history),
            target_container=container["name"] if container else None,
            previous_revision=max((number for number in revisions if number < revision), default=0),
            unhealthy_pods=unhealthy,
            oom_killed_recently=oom_killed,
            current_memory_limit_bytes=parse_memory(limit) if limit else None,
        )
        return facts

    @staticmethod
    def target_container(containers, pods, current_replica_set):
        """Contenedor sobre el que actuar: el único del pod o, si hay varios, el que falla.

        No se supone que se llame como el Deployment. Si no se puede determinar, None.
        """
        if len(containers) == 1:
            return containers[0]
        failing = {
            status.get("name")
            for pod in pods
            if current_replica_set in [owner.get("name") for owner in pod["metadata"].get("ownerReferences", [])]
            for status in pod.get("status", {}).get("containerStatuses", [])
            if not status.get("ready") and status.get("restartCount", 0) > 0
        }
        candidates = [container for container in containers if container["name"] in failing]
        return candidates[0] if len(candidates) == 1 else None

    def apply(self, action, facts):
        namespace, target = action["namespace"], action["target"]
        resource = f"deployment/{target}"
        if action["type"] == "rollout_undo":
            if not facts.get("previous_revision"):
                raise KubectlError("no se conoce la revisión anterior")
            self.kubectl(namespace, "rollout", "undo", resource, f"--to-revision={facts['previous_revision']}")
        elif action["type"] == "rollout_restart":
            self.kubectl(namespace, "rollout", "restart", resource)
        elif action["type"] == "set_memory_limit":
            if not facts.get("target_container"):
                raise KubectlError("no se puede determinar el contenedor")
            memory = str(int(action["params"]["memory_bytes"]))
            containers = [{"name": facts["target_container"], "resources": {"limits": {"memory": memory}}}]
            patch = {"spec": {"template": {"spec": {"containers": containers}}}}
            self.kubectl(namespace, "patch", resource, "--patch", json.dumps(patch))
        else:
            raise KubectlError("acción autorizada sin implementación")

    def handle(self, proposal):
        """Procesa una propuesta. Devuelve el código HTTP y el cuerpo de la respuesta."""
        reason = validate(proposal)
        incident_id = proposal.get("incident_id") if isinstance(proposal, dict) else None
        if reason:
            self.emit(incident_id if isinstance(incident_id, str) else None, "action_rejected", reason)
            return 400, {"status": "rejected", "reasons": [reason]}

        incident_id = incident_id or f"inc-{uuid.uuid4().hex[:8]}"
        action = proposal["action"]
        self.emit(incident_id, "action_proposed", action["type"], **action)

        # Escalar no modifica el clúster: debe funcionar aunque OPA no esté disponible
        if action["type"] == "escalate":
            summary = action.get("params", {}).get("summary", "")
            self.emit(incident_id, "incident_escalated", summary)
            return 200, {"status": "escalated", "incident_id": incident_id}

        facts = self.collect_facts(action["namespace"], action["target"])
        decision = self.opa({"action": action, "facts": facts})
        if decision["allow"] is not True:
            self.emit(incident_id, "action_denied", ", ".join(decision["deny_reasons"]), **action)
            return 403, {"status": "denied", "incident_id": incident_id, "reasons": decision["deny_reasons"]}
        self.emit(incident_id, "action_allowed", action["type"], facts=facts, target=action["target"])

        try:
            # Se registra antes de actuar: si el historial no se puede guardar, no se actúa.
            # Si la acción falla después, la marca se queda: cuenta como intento (conservador).
            now = datetime.now(timezone.utc)
            deployment = self.get_json(action["namespace"], "deployment", action["target"])
            history = self.recent_history(deployment, now)
            if history is None:
                raise KubectlError("historial de acciones no interpretable")
            self.record_action(action["namespace"], action["target"], history, now)
            self.apply(action, facts)
        except (KubectlError, KeyError, ValueError) as error:
            self.emit(incident_id, "action_failed", error, target=action["target"])
            return 502, {"status": "failed", "incident_id": incident_id, "reasons": [str(error)]}

        if action["type"] == "rollout_undo":
            # El rollback copia las anotaciones de la revisión antigua y borra las demás. La acción
            # ya se aplicó: si no se puede reescribir el historial, se avisa, pero no se da por fallida.
            for _ in range(3):
                try:
                    self.record_action(action["namespace"], action["target"], history, now)
                    break
                except KubectlError as error:
                    last_error = error
            else:
                self.emit(incident_id, "history_rewrite_failed", last_error, target=action["target"])
        self.emit(incident_id, "action_executed", action["type"], target=action["target"])
        return 200, {"status": "executed", "incident_id": incident_id}


def make_handler(executor):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body):
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if self.path != "/actions" or not 0 < length <= MAX_BODY_BYTES:
                self.respond(400, {"status": "rejected", "reasons": ["petición no válida"]})
                return
            try:
                proposal = json.loads(self.rfile.read(length))
            except ValueError:
                self.respond(400, {"status": "rejected", "reasons": ["JSON no válido"]})
                return
            self.respond(*executor.handle(proposal))

        def log_message(self, *args):
            pass

    return Handler


def main():
    server = HTTPServer(ADDRESS, make_handler(Executor()))
    print(f"Ejecutor en http://{ADDRESS[0]}:{ADDRESS[1]}/actions", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
