#!/usr/bin/env python3
"""Detector: vigila los pods, y ante un incidente pide una acción y la envía al ejecutor.

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

NAMESPACE = "otel-demo"
EVENTS_FILE = os.environ.get("AUTOKUB_EVENTS", ".local/events.jsonl")
KUBECONFIG = os.environ.get("AUTOKUB_READER_KUBECONFIG", ".local/reader.kubeconfig")
EXECUTOR_URL = os.environ.get("AUTOKUB_EXECUTOR_URL", "http://127.0.0.1:8090/actions")
POLL_SECONDS = 5
# Un reinicio aislado puede ser transitorio y Kubernetes ya lo gestiona
MIN_RESTARTS = 2


def get_json(resource):
    command = ["kubectl", "--kubeconfig", KUBECONFIG, "-n", NAMESPACE, "get", resource, "-o", "json"]
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


def find_incidents(pods, replica_sets):
    """Devuelve un incidente por cada ReplicaSet con pods que fallan de forma repetida.

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
        failing = [s for s in statuses if not s.get("ready") and s.get("restartCount", 0) >= MIN_RESTARTS]
        pod_owners = [o.get("name") for o in pod["metadata"].get("ownerReferences", [])]
        replica_set = next((name for name in pod_owners if name in owners), None)
        if failing and replica_set and replica_set not in incidents:
            terminated = failing[0].get("lastState", {}).get("terminated", {})
            incidents[replica_set] = {
                "namespace": NAMESPACE,
                "deployment": owners[replica_set],
                "replica_set": replica_set,
                "pod": pod["metadata"]["name"],
                "container": failing[0].get("name"),
                "restarts": failing[0].get("restartCount"),
                "last_reason": terminated.get("reason"),
                "exit_code": terminated.get("exitCode"),
            }
    return incidents


class Watcher:
    def __init__(self, propose=rules.propose, send=post_action, events_file=EVENTS_FILE):
        self.propose = propose
        self.send = send
        self.events_file = events_file
        self.active = {}

    def emit(self, incident_id, event, summary, component="detector", **data):
        record = {
            "incident_id": incident_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "component": component,
            "event": event,
            "summary": str(summary)[:500],
            "data": data,
        }
        os.makedirs(os.path.dirname(self.events_file) or ".", exist_ok=True)
        with open(self.events_file, "a", encoding="utf-8") as events:
            events.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def tick(self, pods, replica_sets):
        incidents = find_incidents(pods, replica_sets)
        # Si el fallo vuelve a aparecer tras resolverse, es un incidente nuevo
        for replica_set in set(self.active) - set(incidents):
            state = self.active.pop(replica_set)
            if state["handled"]:
                self.emit(state["id"], "incident_closed", "sin pods con fallos", replica_set=replica_set)

        for replica_set, incident in incidents.items():
            state = self.active.get(replica_set)
            if state is None:
                state = {"id": f"inc-{uuid.uuid4().hex[:8]}", "handled": False}
                self.active[replica_set] = state
                self.emit(state["id"], "incident_detected", incident["deployment"], **incident)
            if not state["handled"]:
                self.remediate(state, incident)

    def remediate(self, state, incident):
        action = self.propose({**incident, "id": state["id"]})
        result = self.send({"incident_id": state["id"], "action": action})
        if result.get("status") == "unreachable":
            return  # Se reintenta en la siguiente vuelta
        state["handled"] = True
        if result.get("status") not in ("executed", "escalated"):
            reasons = ", ".join(result.get("reasons", [])) or "sin motivo"
            summary = f"{incident['deployment']}: {result.get('status')} ({reasons})"
            self.send({"incident_id": state["id"], "action": {"type": "escalate", "params": {"summary": summary}}})


def main():
    watcher = Watcher()
    if os.environ.get("AUTOKUB_PROPOSER") == "llm":
        import llm_agent

        watcher.propose = lambda incident: llm_agent.propose(incident, emit=watcher.emit)
    print(f"Detector en marcha (proponedor: {os.environ.get('AUTOKUB_PROPOSER', 'rules')})", flush=True)
    while True:
        try:
            watcher.tick(get_json("pods")["items"], get_json("replicasets")["items"])
        except (subprocess.SubprocessError, ValueError) as error:
            print(f"Error al leer el clúster: {error}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
