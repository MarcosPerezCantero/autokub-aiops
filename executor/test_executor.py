import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from executor import HISTORY_ANNOTATION, Executor, KubectlError, ask_opa, parse_memory, validate

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
        status = {"name": "cart", "ready": pod_ready, "restartCount": pod_restarts, "lastState": {}}
        if terminated:
            status["lastState"] = {"terminated": {"reason": terminated}}
        return {"metadata": {"ownerReferences": [{"name": owner}]}, "status": {"containerStatuses": [status]}}

    return {
        "deployment": deployment,
        "replicasets": {"items": [replica_set(1, "rs-1"), replica_set(2, "rs-2"), replica_set(revision, "rs-new")]},
        "pods": {"items": [pod("rs-2", True, 5), pod("rs-new", ready, restarts)]},
    }


class FakeKubectl:
    """Simula kubectl. Como el real, `rollout undo` borra las anotaciones propias del Deployment."""

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
        annotations = self.state["deployment"]["metadata"]["annotations"]
        if args[0] == "annotate":
            key, value = args[2].split("=", 1)
            annotations[key] = value
        elif args[:2] == ("rollout", "undo"):
            annotations.pop(HISTORY_ANNOTATION, None)
        return ""

    def actions(self):
        return [write for write in self.writes if write[0] != "annotate"]

    def history(self):
        raw = self.state["deployment"]["metadata"]["annotations"].get(HISTORY_ANNOTATION)
        return json.loads(raw) if raw else []


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
        self.assertEqual(kubectl.actions(), [("rollout", "undo", "deployment/cart", "--to-revision=2")])
        self.assertEqual(self.events(), ["action_proposed", "action_allowed", "action_executed"])
        self.assertEqual(len(kubectl.history()), 1)

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

    def test_rate_counter_survives_rollback_and_restart(self):
        kubectl = FakeKubectl(cluster())
        self.executor(kubectl).handle({"action": UNDO})
        # Un ejecutor nuevo (p. ej. tras reiniciar el pod) lee el historial del Deployment
        self.executor(kubectl).handle({"action": UNDO})
        self.assertEqual(self.opa_inputs[1]["facts"]["actions_last_hour_on_target"], 1)
        self.assertEqual(len(kubectl.history()), 2)

    def test_old_history_is_not_counted(self):
        state = cluster()
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        state["deployment"]["metadata"]["annotations"][HISTORY_ANNOTATION] = json.dumps([old])
        self.executor(FakeKubectl(state)).handle({"action": UNDO})
        self.assertEqual(self.opa_inputs[0]["facts"]["actions_last_hour_on_target"], 0)

    def test_corrupt_history_is_unknown(self):
        for raw in ("no-json", json.dumps(["2026-01-01T00:00:00"]), json.dumps(5)):
            with self.subTest(raw=raw):
                state = cluster()
                state["deployment"]["metadata"]["annotations"][HISTORY_ANNOTATION] = raw
                self.opa_inputs.clear()
                self.executor(FakeKubectl(state)).handle({"action": UNDO})
                self.assertIsNone(self.opa_inputs[0]["facts"]["actions_last_hour_on_target"])

    def test_history_is_recorded_before_acting_for_every_action(self):
        for action_type in ("rollout_undo", "rollout_restart"):
            with self.subTest(action=action_type):
                kubectl = FakeKubectl(cluster())
                self.executor(kubectl).handle({"action": {**UNDO, "type": action_type}})
                self.assertEqual(kubectl.writes[0][0], "annotate")
                self.assertEqual(len(kubectl.history()), 1)

    def test_failed_action_still_consumes_quota(self):
        class FailingAction(FakeKubectl):
            def __call__(self, namespace, *args, payload=None):
                if args[0] == "rollout":
                    raise KubectlError("boom")
                return super().__call__(namespace, *args, payload=payload)

        kubectl = FailingAction(cluster())
        code, _ = self.executor(kubectl).handle({"action": UNDO})
        self.assertEqual(code, 502)
        self.assertEqual(len(kubectl.history()), 1)

    def test_rollback_done_but_history_rewrite_fails_is_still_executed(self):
        class RewriteFails(FakeKubectl):
            def __call__(self, namespace, *args, payload=None):
                if args[0] == "annotate" and any(w[:2] == ("rollout", "undo") for w in self.writes):
                    raise KubectlError("conflict")
                return super().__call__(namespace, *args, payload=payload)

        code, body = self.executor(RewriteFails(cluster())).handle({"action": UNDO})
        self.assertEqual((code, body["status"]), (200, "executed"))
        self.assertIn("history_rewrite_failed", self.events())

    def test_future_and_non_list_history(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
        for raw, expected in ((json.dumps([future]), 1), (json.dumps("5"), None), (json.dumps({}), None)):
            with self.subTest(raw=raw):
                state = cluster()
                state["deployment"]["metadata"]["annotations"][HISTORY_ANNOTATION] = raw
                self.opa_inputs.clear()
                self.executor(FakeKubectl(state)).handle({"action": UNDO})
                self.assertEqual(self.opa_inputs[0]["facts"]["actions_last_hour_on_target"], expected)

    def test_history_corrupted_after_facts_blocks_the_action(self):
        class CorruptsAfterFacts(FakeKubectl):
            reads = 0

            def __call__(self, namespace, *args, payload=None):
                if args[:2] == ("get", "deployment"):
                    self.reads += 1
                    if self.reads == 2:
                        annotations = self.state["deployment"]["metadata"]["annotations"]
                        annotations[HISTORY_ANNOTATION] = "corrupto"
                return super().__call__(namespace, *args, payload=payload)

        kubectl = CorruptsAfterFacts(cluster())
        code, _ = self.executor(kubectl).handle({"action": UNDO})
        self.assertEqual(code, 502)
        self.assertEqual(kubectl.actions(), [])

    def test_oom_in_sidecar_or_old_revision_does_not_count(self):
        state = cluster(terminated="OOMKilled")
        for pod in state["pods"]["items"]:
            pod["status"]["containerStatuses"][0]["name"] = "sidecar"
        self.executor(FakeKubectl(state)).handle({"action": UNDO})
        self.assertFalse(self.opa_inputs[0]["facts"]["oom_killed_recently"])

        state = cluster()
        old_pod = state["pods"]["items"][0]  # pod de la revisión 2
        old_pod["status"]["containerStatuses"][0]["lastState"] = {"terminated": {"reason": "OOMKilled"}}
        self.opa_inputs.clear()
        self.executor(FakeKubectl(state)).handle({"action": UNDO})
        self.assertFalse(self.opa_inputs[0]["facts"]["oom_killed_recently"])

    def test_unrecordable_history_blocks_the_action(self):
        kubectl = FakeKubectl(cluster(), fail_writes=True)
        code, _ = self.executor(kubectl).handle({"action": UNDO})
        self.assertEqual(code, 502)
        self.assertEqual(kubectl.actions(), [])

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
        patch = json.loads(kubectl.actions()[0][3])
        container = patch["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container, {"name": "cart", "resources": {"limits": {"memory": "67108864"}}})

    def test_memory_patch_uses_real_container_name(self):
        state = cluster()
        state["deployment"]["spec"]["template"]["spec"]["containers"][0]["name"] = "server"
        kubectl = FakeKubectl(state)
        action = {"type": "set_memory_limit", "namespace": "otel-demo", "target": "cart", "params": {"memory_bytes": 67108864}}
        self.executor(kubectl).handle({"action": action})
        self.assertEqual(self.opa_inputs[0]["facts"]["target_container"], "server")
        patch = json.loads(kubectl.actions()[0][3])
        self.assertEqual(patch["spec"]["template"]["spec"]["containers"][0]["name"], "server")
        self.assertEqual(self.opa_inputs[0]["facts"]["current_memory_limit_bytes"], 32 * 2**20)

    def test_ambiguous_container_is_not_patched(self):
        state = cluster(ready=True, restarts=0)
        containers = state["deployment"]["spec"]["template"]["spec"]["containers"]
        containers.append({"name": "sidecar"})
        kubectl = FakeKubectl(state)
        action = {"type": "set_memory_limit", "namespace": "otel-demo", "target": "cart", "params": {"memory_bytes": 67108864}}
        code, _ = self.executor(kubectl).handle({"action": action})
        self.assertIsNone(self.opa_inputs[0]["facts"]["target_container"])
        self.assertEqual(code, 502)
        self.assertEqual(kubectl.actions(), [])

    def test_authorized_unknown_action_is_not_executed(self):
        kubectl = FakeKubectl(cluster())
        action = {"type": "delete_pod", "namespace": "otel-demo", "target": "cart"}
        code, _ = self.executor(kubectl).handle({"action": action})
        self.assertEqual(code, 502)
        self.assertEqual(kubectl.actions(), [])

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
