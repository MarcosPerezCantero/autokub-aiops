# Política de remediación: decide si una acción propuesta por el agente se puede ejecutar.
# Alcance y límites: docs/adr/0004-alcance-del-agente.md
# Contrato de entrada y salida: docs/arquitectura.md
#
# Cada comprobación es una regla positiva y su motivo de denegación se dispara con `not`.
# Así, un dato ausente o de tipo inesperado deniega la acción en lugar de permitirla.
package autokub.remediation

namespace := "otel-demo"

# Servicios de la aplicación. Quedan fuera flagd, el Collector, los almacenes de datos,
# la observabilidad, el proxy de entrada y el generador de carga.
allowed_targets := {
	"ad",
	"cart",
	"checkout",
	"currency",
	"email",
	"frontend",
	"payment",
	"product-catalog",
	"quote",
	"recommendation",
	"shipping",
}

max_actions_per_hour := 3

max_memory_bytes := 536870912 # 512 Mi

# Parámetros exactos de cada acción que modifica el clúster
mutating_actions := {
	"rollout_restart": set(),
	"rollout_undo": set(),
	"set_memory_limit": {"memory_bytes"},
}

default allow := false

allow if input.action.type == "escalate"

allow if {
	mutating_action
	count(deny_reasons) == 0
}

decision := {
	"allow": allow,
	"deny_reasons": sort(deny_reasons),
}

deny_reasons contains "acción no permitida" if {
	input.action.type != "escalate"
	not mutating_action
}

deny_reasons contains "acción no permitida" if not input.action.type

deny_reasons contains "namespace no permitido" if {
	mutating_action
	not namespace_ok
}

deny_reasons contains "target no permitido" if {
	mutating_action
	not target_ok
}

deny_reasons contains "el deployment no existe" if {
	mutating_action
	not deployment_ok
}

deny_reasons contains "parámetros no válidos" if {
	mutating_action
	not params_ok
}

deny_reasons contains "límite de frecuencia superado" if {
	mutating_action
	not rate_ok
}

deny_reasons contains "no hay pods con fallos" if {
	input.action.type in {"rollout_restart", "rollout_undo"}
	not unhealthy_ok
}

deny_reasons contains "no existe una revisión anterior" if {
	input.action.type == "rollout_undo"
	not previous_revision_ok
}

deny_reasons contains "no hay un OOMKilled reciente" if {
	input.action.type == "set_memory_limit"
	input.facts.oom_killed_recently != true
}

deny_reasons contains "no hay un OOMKilled reciente" if {
	input.action.type == "set_memory_limit"
	not input.facts.oom_killed_recently
}

deny_reasons contains "límite de memoria no válido" if {
	input.action.type == "set_memory_limit"
	not memory_ok
}

mutating_action if mutating_actions[input.action.type]

namespace_ok if input.action.namespace == namespace

target_ok if input.action.target in allowed_targets

deployment_ok if input.facts.deployment_exists == true

params_ok if {
	params := object.get(input.action, "params", {})
	is_object(params)
	{key | some key, _ in params} == mutating_actions[input.action.type]
}

rate_ok if {
	actions := input.facts.actions_last_hour_on_target
	is_number(actions)
	actions >= 0
	actions < max_actions_per_hour
}

unhealthy_ok if {
	is_number(input.facts.unhealthy_pods)
	input.facts.unhealthy_pods > 0
}

previous_revision_ok if {
	is_number(input.facts.previous_revision)
	input.facts.previous_revision >= 1
}

# Solo aumenta, como máximo al doble y bajo el tope absoluto
memory_ok if {
	requested := input.action.params.memory_bytes
	current := input.facts.current_memory_limit_bytes
	is_number(requested)
	is_number(current)
	current > 0
	requested > current
	requested <= 2 * current
	requested <= max_memory_bytes
}
