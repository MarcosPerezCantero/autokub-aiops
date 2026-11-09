#!/usr/bin/env python3
"""Genera el RBAC del lector y del ejecutor a partir de la configuración.

Las cuentas viven en autokub-system. En cada namespace configurado se crea un Role por cuenta:
- lector: solo lectura de pods, logs, eventos, Deployments y ReplicaSets, sin Secrets;
- ejecutor: lectura para obtener los hechos y, solo en modo execute, patch sobre los Deployments
  permitidos. En modo propose (el de por defecto) no tiene ningún permiso de escritura.
La lista de Deployments sale del mismo fichero que lee OPA, para que no haya copias.

Uso: python3 infra/rbac.py config/lab.json | kubectl apply -f -
"""
import json
import sys

SYSTEM_NAMESPACE = "autokub-system"
READER, EXECUTOR = "autokub-reader", "autokub-executor"


def role(name, namespace, rules):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": name, "namespace": namespace},
        "rules": rules,
    }


def binding(name, namespace):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace},
        "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": SYSTEM_NAMESPACE}],
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
    }


def manifests(config):
    mode = config.get("mode", "propose")
    if mode not in ("propose", "execute"):
        raise ValueError(f"modo no válido: {mode!r}")
    namespaces = config.get("namespaces")
    if not isinstance(namespaces, dict) or not namespaces:
        raise ValueError("la configuración no define namespaces")
    items = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": SYSTEM_NAMESPACE}},
        *(
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name, "namespace": SYSTEM_NAMESPACE}}
            for name in (READER, EXECUTOR)
        ),
    ]
    for namespace, settings in sorted(namespaces.items()):
        targets = settings.get("targets") if isinstance(settings, dict) else None
        if not isinstance(targets, list) or not targets or not all(isinstance(t, str) and t for t in targets):
            raise ValueError(f"{namespace}: targets debe ser una lista de nombres")
        reader_rules = [
            {"apiGroups": [""], "resources": ["pods", "pods/log", "events"], "verbs": ["get", "list"]},
            {"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["get", "list"]},
        ]
        executor_rules = [
            {
                "apiGroups": ["apps"],
                "resources": ["deployments"],
                "verbs": ["get", "patch"] if mode == "execute" else ["get"],
                "resourceNames": sorted(targets),
            },
            {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["get", "list"]},
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
        ]
        items += [
            role(READER, namespace, reader_rules),
            binding(READER, namespace),
            role(EXECUTOR, namespace, executor_rules),
            binding(EXECUTOR, namespace),
        ]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main():
    with open(sys.argv[1] if len(sys.argv) > 1 else "config/lab.json", encoding="utf-8") as file:
        print(json.dumps(manifests(json.load(file)), indent=2))


if __name__ == "__main__":
    main()
