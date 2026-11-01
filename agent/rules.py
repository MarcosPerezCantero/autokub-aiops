"""Proponedor de reglas fijas: referencia sin LLM con la que se compara al agente.

Misma interfaz que tendrá el agente: recibe un incidente y devuelve una acción.
"""


def propose(incident):
    return {
        "type": "rollout_undo",
        "namespace": incident["namespace"],
        "target": incident["deployment"],
        "params": {},
    }
