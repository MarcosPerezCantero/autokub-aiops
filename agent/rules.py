"""Proponedor de reglas fijas: referencia sin LLM con la que se compara al agente.

Misma interfaz que el agente: recibe un incidente y devuelve una acción.
"""


def propose(incident):
    if incident.get("previous_attempts"):
        summary = f"{incident['deployment']}: el rollback no ha resuelto el incidente"
        return {"type": "escalate", "params": {"summary": summary}}
    return {
        "type": "rollout_undo",
        "namespace": incident["namespace"],
        "target": incident["deployment"],
        "params": {},
    }
