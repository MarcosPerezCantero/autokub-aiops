package autokub.remediation_test

import data.autokub.remediation

# Misma forma que config/lab.json, con un segundo namespace para comprobar que no se mezclan
cfg := {
	"namespaces": {
		"otel-demo": {"targets": ["ad", "cart", "checkout", "currency", "email", "frontend", "payment", "product-catalog", "quote", "recommendation", "shipping"]},
		"tienda": {"targets": ["api"]},
	},
	"limits": {"max_actions_per_hour": 3, "max_memory_bytes": 536870912},
}

undo := {
	"action": {"type": "rollout_undo", "namespace": "otel-demo", "target": "product-catalog", "params": {}},
	"facts": {
		"deployment_exists": true,
		"previous_revision": 4,
		"unhealthy_pods": 1,
		"actions_last_hour_on_target": 0,
	},
}

memory := {
	"action": {
		"type": "set_memory_limit",
		"namespace": "otel-demo",
		"target": "email",
		"params": {"memory_bytes": 200000000},
	},
	"facts": {
		"deployment_exists": true,
		"oom_killed_recently": true,
		"current_memory_limit_bytes": 100000000,
		"actions_last_hour_on_target": 0,
	},
}

with_action(base, patch) := object.union(base, {"action": patch})

with_facts(base, patch) := object.union(base, {"facts": patch})

denied(case_input, reason) if {
	result := remediation.decision with input as case_input with data.autokub.config as cfg
	result.allow == false
	reason in result.deny_reasons
}

allowed(case_input) if {
	result := remediation.decision with input as case_input with data.autokub.config as cfg
	result.allow == true
	result.deny_reasons == []
}

# Acciones permitidas

test_undo_allowed if allowed(undo)

test_restart_allowed if allowed(with_action(undo, {"type": "rollout_restart"}))

test_memory_allowed if allowed(memory)

test_memory_at_double_allowed if allowed(with_action(memory, {"params": {"memory_bytes": 200000000}}))

test_escalate_allowed_without_facts if allowed({"action": {"type": "escalate", "params": {"summary": "fuera de catálogo"}}})

test_last_action_within_rate_allowed if allowed(with_facts(undo, {"actions_last_hour_on_target": 2}))

test_params_omitted_allowed if allowed({"action": {"type": "rollout_undo", "namespace": "otel-demo", "target": "cart"}, "facts": undo.facts})

# Denegación por defecto

test_empty_input_denied if denied({}, "acción no permitida")

test_unknown_action_denied if denied(with_action(undo, {"type": "delete_pod"}), "acción no permitida")

test_action_case_sensitive if denied(with_action(undo, {"type": "ROLLOUT_UNDO"}), "acción no permitida")

test_revert_drift_not_available_yet if denied(with_action(undo, {"type": "revert_drift"}), "acción no permitida")

test_non_string_action_denied if denied(with_action(undo, {"type": 1}), "acción no permitida")

# Namespace y objetivo

test_other_namespace_denied if denied(with_action(undo, {"namespace": "kube-system"}), "namespace no permitido")

test_missing_namespace_denied if {
	denied({"action": {"type": "rollout_undo", "target": "cart", "params": {}}, "facts": undo.facts}, "namespace no permitido")
}

test_excluded_targets_denied if {
	every target in ["flagd", "otel-collector", "kafka", "astronomy-db", "valkey-cart", "grafana", "frontend-proxy"] {
		denied(with_action(undo, {"target": target}), "target no permitido")
	}
}

test_target_lookalike_denied if denied(with_action(undo, {"target": "payment-x"}), "target no permitido")

test_non_string_target_denied if denied(with_action(undo, {"target": ["payment"]}), "target no permitido")

test_missing_target_denied if {
	denied({"action": {"type": "rollout_undo", "namespace": "otel-demo", "params": {}}, "facts": undo.facts}, "target no permitido")
}

# Hechos del clúster

test_missing_facts_denied if {
	denied({"action": undo.action}, "el deployment no existe")
	denied({"action": undo.action}, "no hay pods con fallos")
	denied({"action": undo.action}, "límite de frecuencia superado")
}

test_deployment_missing_denied if denied(with_facts(undo, {"deployment_exists": false}), "el deployment no existe")

test_truthy_string_not_accepted if denied(with_facts(undo, {"deployment_exists": "true"}), "el deployment no existe")

test_healthy_service_denied if denied(with_facts(undo, {"unhealthy_pods": 0}), "no hay pods con fallos")

test_restart_healthy_denied if {
	denied(with_facts(with_action(undo, {"type": "rollout_restart"}), {"unhealthy_pods": 0}), "no hay pods con fallos")
}

test_unhealthy_string_denied if denied(with_facts(undo, {"unhealthy_pods": "1"}), "no hay pods con fallos")

test_no_previous_revision_denied if denied(with_facts(undo, {"previous_revision": 0}), "no existe una revisión anterior")

test_null_previous_revision_denied if denied(with_facts(undo, {"previous_revision": null}), "no existe una revisión anterior")

# Límite de frecuencia

test_rate_limit_denied if denied(with_facts(undo, {"actions_last_hour_on_target": 3}), "límite de frecuencia superado")

test_null_rate_denied if denied(with_facts(undo, {"actions_last_hour_on_target": null}), "límite de frecuencia superado")

test_negative_rate_denied if denied(with_facts(undo, {"actions_last_hour_on_target": -1}), "límite de frecuencia superado")

# Parámetros

test_undo_extra_params_denied if {
	case_input := {"action": {"type": "rollout_undo", "namespace": "otel-demo", "target": "cart", "params": {"to_revision": 1}}, "facts": undo.facts}
	denied(case_input, "parámetros no válidos")
}

test_memory_extra_params_denied if {
	case_input := with_action(memory, {"params": {"memory_bytes": 150000000, "image": "evil"}})
	denied(case_input, "parámetros no válidos")
}

test_params_not_object_denied if {
	case_input := {"action": {"type": "rollout_undo", "namespace": "otel-demo", "target": "cart", "params": "x"}, "facts": undo.facts}
	denied(case_input, "parámetros no válidos")
}

# Memoria

test_memory_decrease_denied if denied(with_action(memory, {"params": {"memory_bytes": 50000000}}), "límite de memoria no válido")

test_memory_same_denied if denied(with_action(memory, {"params": {"memory_bytes": 100000000}}), "límite de memoria no válido")

test_memory_over_double_denied if denied(with_action(memory, {"params": {"memory_bytes": 200000001}}), "límite de memoria no válido")

test_memory_over_cap_denied if {
	case_input := with_facts(with_action(memory, {"params": {"memory_bytes": 600000000}}), {"current_memory_limit_bytes": 400000000})
	denied(case_input, "límite de memoria no válido")
}

test_memory_string_denied if denied(with_action(memory, {"params": {"memory_bytes": "1Gi"}}), "límite de memoria no válido")

test_memory_zero_current_denied if denied(with_facts(memory, {"current_memory_limit_bytes": 0}), "límite de memoria no válido")

test_memory_without_oom_denied if denied(with_facts(memory, {"oom_killed_recently": false}), "no hay un OOMKilled reciente")

test_memory_missing_oom_fact_denied if {
	case_input := {"action": memory.action, "facts": {"deployment_exists": true, "current_memory_limit_bytes": 100000000, "actions_last_hour_on_target": 0}}
	denied(case_input, "no hay un OOMKilled reciente")
}

# Varios motivos a la vez

test_all_reasons_reported if {
	case_input := {"action": {"type": "rollout_undo", "namespace": "kube-system", "target": "flagd", "params": {}}, "facts": {}}
	result := remediation.decision with input as case_input with data.autokub.config as cfg
	result.allow == false
	count(result.deny_reasons) >= 5
}

# Configuración

test_second_namespace_allowed if allowed(with_action(undo, {"namespace": "tienda", "target": "api"}))

test_targets_do_not_cross_namespaces if {
	denied(with_action(undo, {"namespace": "tienda", "target": "cart"}), "target no permitido")
	denied(with_action(undo, {"target": "api"}), "target no permitido")
}

test_without_config_everything_denied if {
	result := remediation.decision with input as undo with data.autokub.config as {}
	result.allow == false
	"namespace no permitido" in result.deny_reasons
	"target no permitido" in result.deny_reasons
	"límite de frecuencia superado" in result.deny_reasons
}

test_without_config_escalate_allowed if {
	result := remediation.decision with input as {"action": {"type": "escalate"}} with data.autokub.config as {}
	result.allow == true
}

test_rate_limit_comes_from_config if {
	strict := object.union(cfg, {"limits": {"max_actions_per_hour": 1}})
	result := remediation.decision with input as with_facts(undo, {"actions_last_hour_on_target": 1}) with data.autokub.config as strict
	"límite de frecuencia superado" in result.deny_reasons
}

test_memory_cap_comes_from_config if {
	small := object.union(cfg, {"limits": {"max_memory_bytes": 150000000}})
	result := remediation.decision with input as memory with data.autokub.config as small
	"límite de memoria no válido" in result.deny_reasons
}

test_non_numeric_limit_denied if {
	broken := object.union(cfg, {"limits": {"max_actions_per_hour": "3"}})
	result := remediation.decision with input as undo with data.autokub.config as broken
	"límite de frecuencia superado" in result.deny_reasons
}
