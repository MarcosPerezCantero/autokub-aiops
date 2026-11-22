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
        """RBAC sobre la aplicación. El del panel vive aparte y solo toca el namespace de autokub."""
        kinds = ("ServiceAccount", "Role", "RoleBinding")
        return sorted(
            (strip(d) for d in docs if d["kind"] in kinds and d["metadata"]["name"] != "autokub-panel"), key=json.dumps
        )

    def test_rbac_matches_lab_generator(self):
        for mode in ("execute", "approve", "propose"):
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
        self.assertEqual(set(config), {"mode", "namespaces", "limits", "detection", "model", "approval"})
        self.assertEqual(config["approval"], {"timeout_seconds": 900})

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
            {"namespaces": {"bad:ns\nkind: ClusterRole": {"targets": ["a"]}}},
            {"namespaces": {"Tienda": {"targets": ["a"]}}},
            {"namespaces": {"tienda": {"targets": "api"}}},
            {"namespaces": {"tienda": {"targets": ["api", "x; rm"]}}},
            {"namespaces": {"tienda": {"targets": [5]}}},
            {"namespaces": LAB["namespaces"], "approval": {"timeoutSeconds": 0}},
            {"namespaces": LAB["namespaces"], "approval": {"timeoutSeconds": "x"}},
            {"namespaces": LAB["namespaces"], "mode": "approve", "panel": {"enabled": False}},
        ):
            with self.subTest(values=values):
                self.assertNotEqual(render(values, expect_error=True).returncode, 0)


class PanelTest(unittest.TestCase):
    def docs(self, **values):
        return render({"namespaces": LAB["namespaces"], **values})

    @staticmethod
    def find(docs, kind, name):
        return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)

    @staticmethod
    def env(deployment):
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: e.get("value") for e in container.get("env", [])}

    @staticmethod
    def secret_volumes(deployment):
        return [v["secret"]["secretName"] for v in deployment["spec"]["template"]["spec"]["volumes"] if "secret" in v]

    def test_timeout_as_integer_from_set_is_accepted(self):
        """Con --set, Helm lee los números como int64 y no como float64, igual que el resto de valores."""
        result = subprocess.run(
            ["helm", "template", "autokub", CHART, "-n", "autokub-system",
             "--set", "namespaces.ns.targets={a}", "--set", "approval.timeoutSeconds=600"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('\\"timeout_seconds\\":600', result.stdout)

    def test_approval_token_only_in_executor_and_panel(self):
        docs = self.docs(mode="approve")
        mounts = {d["metadata"]["name"]: self.secret_volumes(d) for d in docs if d["kind"] == "Deployment"}
        self.assertEqual(
            mounts, {"autokub-agent": [], "autokub-executor": ["autokub-approval"], "autokub-panel": ["autokub-approval"]}
        )
        token = self.find(docs, "Secret", "autokub-approval")["data"]["token"]
        self.assertGreaterEqual(len(token), 48)

    def test_panel_only_reads_its_own_namespace(self):
        docs = self.docs()
        role = self.find(docs, "Role", "autokub-panel")
        self.assertEqual(role["metadata"]["namespace"], "autokub-system")
        verbs = {verb for rule in role["rules"] for verb in rule["verbs"]}
        self.assertEqual(verbs, {"get", "list"})
        self.assertNotIn("secrets", json.dumps(role))
        panel = [d for d in docs if "autokub-panel" in json.dumps(d.get("subjects", []))]
        self.assertEqual([d["metadata"]["namespace"] for d in panel], ["autokub-system"])

    def test_panel_listens_on_localhost_and_has_no_service(self):
        docs = self.docs()
        self.assertEqual(self.env(self.find(docs, "Deployment", "autokub-panel"))["AUTOKUB_PANEL_HOST"], "127.0.0.1")
        self.assertNotIn("autokub-panel", [d["metadata"]["name"] for d in docs if d["kind"] == "Service"])
        self.assertEqual(self.find(docs, "NetworkPolicy", "autokub-panel")["spec"]["ingress"], [])

    def test_chat_only_reachable_from_panel(self):
        docs = self.docs()
        self.assertEqual(self.env(self.find(docs, "Deployment", "autokub-agent"))["AUTOKUB_CHAT_PORT"], "8092")
        ingress = self.find(docs, "NetworkPolicy", "autokub-agent")["spec"]["ingress"]
        self.assertEqual(ingress[0]["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"], "panel")

    def test_without_llm_or_panel_there_is_no_chat(self):
        for values in ({"proposer": "rules"}, {"panel": {"enabled": False}}, {"panel": {"chat": False}}):
            with self.subTest(values=values):
                docs = self.docs(**values)
                agent = self.find(docs, "Deployment", "autokub-agent")
                self.assertNotIn("AUTOKUB_CHAT_PORT", self.env(agent))
                self.assertEqual(self.find(docs, "NetworkPolicy", "autokub-agent")["spec"]["ingress"], [])
                self.assertNotIn("autokub-agent", [d["metadata"]["name"] for d in docs if d["kind"] == "Service"])

    def test_executor_accepts_agent_and_panel(self):
        sources = self.find(self.docs(), "NetworkPolicy", "autokub-executor")["spec"]["ingress"][0]["from"]
        components = [s["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for s in sources]
        self.assertEqual(components, ["agent", "panel"])


if __name__ == "__main__":
    unittest.main()
