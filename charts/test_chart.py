"""Comprueba el chart renderizado con helm template. Requiere helm y PyYAML (incluido en el venv)."""
import json
import os
import subprocess
import sys
import unittest

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CHART = os.path.join(ROOT, "charts", "autokub")
sys.path.insert(0, os.path.join(ROOT, "infra"))
from rbac import manifests  # noqa: E402

with open(os.path.join(ROOT, "config", "lab.json"), encoding="utf-8") as file:
    LAB = json.load(file)


def render(values, expect_error=False):
    values_file = os.path.join(ROOT, ".local", "chart-test-values.json")
    os.makedirs(os.path.dirname(values_file), exist_ok=True)
    with open(values_file, "w", encoding="utf-8") as file:
        json.dump(values, file)
    result = subprocess.run(
        ["helm", "template", "autokub", CHART, "-n", "autokub-system", "-f", values_file],
        capture_output=True,
        text=True,
        check=False,
    )
    if expect_error:
        return result
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def strip(item):
    """Deja solo lo que importa para comparar RBAC (sin etiquetas del chart)."""
    item = json.loads(json.dumps(item))
    item["metadata"] = {k: v for k, v in item["metadata"].items() if k in ("name", "namespace")}
    return item


class ChartTest(unittest.TestCase):
    def setUp(self):
        self.lab_values = {"mode": LAB["mode"], "namespaces": LAB["namespaces"], "limits": LAB["limits"]}

    def rbac(self, docs):
        kinds = ("ServiceAccount", "Role", "RoleBinding")
        return sorted((strip(d) for d in docs if d["kind"] in kinds), key=json.dumps)

    def test_rbac_matches_lab_generator(self):
        for mode in ("execute", "propose"):
            with self.subTest(mode=mode):
                docs = render({**self.lab_values, "mode": mode})
                expected = [i for i in manifests({**LAB, "mode": mode})["items"] if i["kind"] != "Namespace"]
                self.assertEqual(self.rbac(docs), sorted(expected, key=json.dumps))

    def test_policy_is_the_repository_policy(self):
        docs = render(self.lab_values)
        policy = next(d for d in docs if d["metadata"]["name"] == "autokub-policy")["data"]["remediation.rego"]
        with open(os.path.join(ROOT, "policies", "remediation.rego"), encoding="utf-8") as file:
            self.assertEqual(policy.strip(), file.read().strip())

    def test_config_has_no_secrets_and_defaults_to_propose(self):
        docs = render({"namespaces": LAB["namespaces"]})
        config = json.loads(next(d for d in docs if d["metadata"]["name"] == "autokub-config")["data"]["config.json"])
        self.assertEqual(config["mode"], "propose")
        self.assertNotIn("apiKeySecret", json.dumps(config))
        self.assertEqual(set(config), {"mode", "namespaces", "limits", "detection", "model"})

    def test_key_only_reaches_agent_and_pods_are_hardened(self):
        docs = render(self.lab_values)
        deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
        for name, deployment in deployments.items():
            pod = deployment["spec"]["template"]["spec"]
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            self.assertEqual(deployment["spec"]["replicas"], 1)
            # Las variables automáticas de los Services pisaban AUTOKUB_EXECUTOR_PORT
            self.assertFalse(pod["enableServiceLinks"])
            for container in pod["containers"]:
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
                uses_secret = "secretKeyRef" in json.dumps(container.get("env", []))
                self.assertEqual(uses_secret, name == "autokub-agent", f"{name}/{container['name']}")
        self.assertEqual(deployments["autokub-agent"]["spec"]["template"]["spec"]["serviceAccountName"], "autokub-reader")
        opa = deployments["autokub-executor"]["spec"]["template"]["spec"]["containers"][1]
        self.assertIn("--addr=127.0.0.1:8181", opa["args"])

    def test_invalid_values_are_refused(self):
        for values in (
            {},
            {"namespaces": LAB["namespaces"], "mode": "yolo"},
            {"namespaces": {"a": {"targets": []}}},
            {"namespaces": LAB["namespaces"], "model": {"provider": "openai", "name": "m"}},
        ):
            with self.subTest(values=values):
                self.assertNotEqual(render(values, expect_error=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
