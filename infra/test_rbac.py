import json
import os
import unittest

from rbac import EXECUTOR, READER, SYSTEM_NAMESPACE, manifests

CONFIG = {"namespaces": {"tienda": {"targets": ["api", "web"]}, "pagos": {"targets": ["cobros"]}}}


def find(items, kind, name, namespace):
    return next(i for i in items if i["kind"] == kind and i["metadata"] == {"name": name, "namespace": namespace})


class RbacTest(unittest.TestCase):
    def setUp(self):
        self.items = manifests(CONFIG)["items"]

    def test_executor_can_only_patch_configured_targets(self):
        rules = find(self.items, "Role", EXECUTOR, "tienda")["rules"]
        patchable = [r for r in rules if "patch" in r["verbs"]]
        self.assertEqual(patchable, [{**patchable[0], "resourceNames": ["api", "web"]}])
        self.assertEqual(find(self.items, "Role", EXECUTOR, "pagos")["rules"][0]["resourceNames"], ["cobros"])

    def test_reader_never_writes(self):
        for item in self.items:
            if item["kind"] == "Role" and item["metadata"]["name"] == READER:
                verbs = {verb for rule in item["rules"] for verb in rule["verbs"]}
                self.assertEqual(verbs, {"get", "list"})

    def test_no_secrets_wildcards_or_cluster_roles(self):
        text = json.dumps(self.items)
        self.assertNotIn("secrets", text)
        self.assertNotIn('"*"', text)
        self.assertNotIn("ClusterRole", text)
        self.assertNotIn("delete", text)

    def test_bindings_point_to_system_accounts(self):
        subjects = find(self.items, "RoleBinding", READER, "pagos")["subjects"]
        self.assertEqual(subjects, [{"kind": "ServiceAccount", "name": READER, "namespace": SYSTEM_NAMESPACE}])

    def test_invalid_config_fails(self):
        for config in ({}, {"namespaces": {"a": {}}}, {"namespaces": {"a": {"targets": []}}}, {"namespaces": {"a": {"targets": [1]}}}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                manifests(config)

    def test_lab_config_targets_match_policy_data(self):
        path = os.path.join(os.path.dirname(__file__), "..", "config", "lab.json")
        with open(path, encoding="utf-8") as file:
            config = json.load(file)
        role_targets = find(manifests(config)["items"], "Role", EXECUTOR, "otel-demo")["rules"][0]["resourceNames"]
        self.assertEqual(role_targets, sorted(config["namespaces"]["otel-demo"]["targets"]))


if __name__ == "__main__":
    unittest.main()
