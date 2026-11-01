import json
import os
import tempfile
import unittest

from executor import Executor, KubectlError, ask_opa, parse_memory, validate

UNDO = {"type": "rollout_undo", "namespace": "otel-demo", "target": "cart", "params": {}}
ALLOW = {"allow": True, "deny_reasons": []}


def cluster(revision=3, restarts=2, ready=False, terminated=None, memory="32Mi"):
    deployment = {
        "metadata": {"annotations": {"deployment.kubernetes.io/revision": str(revision)}},
        "spec": {
            "selector": {"matchLabels": {"app": "cart"}},
            "template": {"spec": {"containers": [{"name": "cart", "resources": {"limits": {"memory": memory}}}]}},
        },
    }

    def replica_set(number, name):
        return {
            "metadata": {
                "name": name,
                "annotations": {"deployment.kubernetes.io/revision": str(number)},
                "ownerReferences": [{"name": "cart"}],
            }
        }

    def pod(owner, pod_ready, pod_restarts):
        status = {"ready": pod_ready, "restartCount": pod_restarts, "lastState": {}}
        if terminated:
            status["lastState"] = {"terminated": {"reason": terminated}}
        return {"metadata": {"ownerReferences": [{"name": owner}]}, "status": {"containerStatuses": [status]}}

    return {
        "deployment": deployment,
        "replicasets": {"items": [replica_set(1, "rs-1"), replica_set(2, "rs-2"), replica_set(revision, "rs-new")]},
        "pods": {"items": [pod("rs-2", True, 5), pod("rs-new", ready, restarts)]},
    }


class FakeKubectl:
    def __init__(self, state=None, fail_writes=False):
        self.state = state
        self.fail_writes = fail_writes
        self.writes = []

    def __call__(self, namespace, *args, payload=None):
        if args[0] == "get":
            if self.state is None:
                raise KubectlError("forbidden")
            return json.dumps(self.state[args[1]])
        if self.fail_writes:
            raise KubectlError("forbidden")
        self.writes.append(args)
        return ""


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events_file = os.path.join(self.tmp.name, "events.jsonl")
        self.opa_inputs = []

    def executor(self, kubectl=None, decision=ALLOW):
        def opa(opa_input):
            self.opa_inputs.append(opa_input)
            return decision

        return Executor(kubectl=kubectl or FakeKubectl(cluster()), opa=opa, events_file=self.events_file)

    def events(self):
        with open(self.events_file, encoding="utf-8") as events:
            return [json.loads(line)["event"] for line in events]

    def test_authorized_undo_uses_computed_revision(self):
        kubectl = FakeKubectl(cluster())
        code, body = self.executor(kubectl).handle({"incident_id": "inc-1", "action": UNDO})
        self.assertEqual((code, body["status"]), (200, "executed"))
        self.assertEqual(kubectl.writes, [("rollout", "undo", "deployment/cart", "--to-revision=2")])
        self.assertEqual(self.events(), ["action_proposed", "action_allowed", "action_executed"])

    def test_denied_action_does_not_touch_cluster(self):
        kubectl = FakeKubectl(cluster())
        code, body = self.executor(kubectl, {"allow": False, "deny_reasons": ["target no permitido"]}).handle(
            {"action": UNDO}
        )
        self.assertEqual((code, body["reasons"]), (403, ["target no permitido"]))
        self.assertEqual(kubectl.writes, [])
        self.assertEqual(self.events(), ["action_proposed", "action_denied"])

    def test_non_boolean_allow_is_denied(self):
        kubectl = FakeKubectl(cluster())
        code, _ = self.executor(kubectl, {"allow": "true", "deny_reasons": []}).handle({"action": UNDO})
        self.assertEqual(code, 403)
        self.assertEqual(kubectl.writes, [])

    def test_failed_write_is_reported(self):
        code, body = self.executor(FakeKubectl(cluster(), fail_writes=True)).handle({"action": UNDO})
        self.assertEqual((code, body["status"]), (502, "failed"))
        self.assertIn("action_failed", self.events())
        self.assertNotIn("action_executed", self.events())

    def test_facts_come_from_cluster_not_from_proposal(self):
        self.executor().handle({"action": UNDO})
        facts = self.opa_inputs[0]["facts"]
        self.assertEqual(facts["previous_revision"], 2)
        self.assertEqual(facts["unhealthy_pods"], 1)
        self.assertEqual(facts["current_memory_limit_bytes"], 32 * 2**20)

    def test_old_revision_pods_do_not_count_as_unhealthy(self):
        self.executor(FakeKubectl(cluster(restarts=0, ready=True))).handle({"action": UNDO})
        self.assertEqual(self.opa_inputs[0]["facts"]["unhealthy_pods"], 0)

    def test_oom_detected(self):
        self.executor(FakeKubectl(cluster(terminated="OOMKilled"))).handle({"action": UNDO})
        self.assertTrue(self.opa_inputs[0]["facts"]["oom_killed_recently"])

    def test_unreadable_deployment_reports_missing(self):
        code, _ = self.executor(FakeKubectl(state=None)).handle({"action": UNDO})
        self.assertFalse(self.opa_inputs[0]["facts"]["deployment_exists"])
        self.assertEqual(code, 502)

    def test_rate_counter_counts_executed_actions_by_target(self):
        executor = self.executor()
        executor.handle({"action": UNDO})
        executor.handle({"action": UNDO})
        self.assertEqual(self.opa_inputs[1]["facts"]["actions_last_hour_on_target"], 1)
        self.assertEqual(executor.actions_last_hour("payment"), 0)

    def test_escalate_does_not_need_opa(self):
        code, body = self.executor().handle({"action": {"type": "escalate", "params": {"summary": "x"}}})
        self.assertEqual((code, body["status"]), (200, "escalated"))
        self.assertEqual(self.opa_inputs, [])
        self.assertIn("incident_escalated", self.events())

    def test_memory_patch(self):
        kubectl = FakeKubectl(cluster())
        action = {
            "type": "set_memory_limit",
            "namespace": "otel-demo",
            "target": "cart",
            "params": {"memory_bytes": 67108864},
        }
        self.executor(kubectl).handle({"action": action})
        patch = json.loads(kubectl.writes[0][3])
        container = patch["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container, {"name": "cart", "resources": {"limits": {"memory": "67108864"}}})

    def test_authorized_unknown_action_is_not_executed(self):
        kubectl = FakeKubectl(cluster())
        action = {"type": "delete_pod", "namespace": "otel-demo", "target": "cart"}
        code, _ = self.executor(kubectl).handle({"action": action})
        self.assertEqual(code, 502)
        self.assertEqual(kubectl.writes, [])

    def test_malformed_proposal_reaches_neither_opa_nor_cluster(self):
        for proposal in (
            [],
            {"action": "rollout_undo"},
            {"action": {**UNDO, "command": "rm"}},
            {"action": UNDO, "extra": 1},
            {"action": {**UNDO, "target": "--kubeconfig=/tmp/x"}},
            {"action": {**UNDO, "target": "cart;id"}},
            {"action": {**UNDO, "namespace": ""}},
            {"action": {**UNDO, "type": 1}},
            {"action": {**UNDO, "params": "x"}},
            {"incident_id": "../../x", "action": UNDO},
        ):
            with self.subTest(proposal=proposal):
                code, body = self.executor().handle(proposal)
                self.assertEqual((code, body["status"]), (400, "rejected"))
        self.assertEqual(self.opa_inputs, [])


class HelpersTest(unittest.TestCase):
    def test_opa_unavailable_fails_closed(self):
        import executor

        original, executor.OPA_URL = executor.OPA_URL, "http://127.0.0.1:1/none"
        self.addCleanup(setattr, executor, "OPA_URL", original)
        self.assertEqual(ask_opa({}), {"allow": False, "deny_reasons": ["OPA no disponible"]})

    def test_parse_memory(self):
        self.assertEqual(parse_memory("32Mi"), 33554432)
        self.assertEqual(parse_memory("1Gi"), 2**30)
        self.assertEqual(parse_memory("1000"), 1000)
        self.assertIsNone(parse_memory("1.5Gi"))
        self.assertIsNone(parse_memory("10Xi"))

    def test_escalate_is_valid_without_target(self):
        self.assertIsNone(validate({"action": {"type": "escalate"}}))


if __name__ == "__main__":
    unittest.main()
