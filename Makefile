CLUSTER := autokub
K3D_CONFIG := infra/k3d/cluster.yaml

DEMO_NAMESPACE := otel-demo
DEMO_CHART := open-telemetry/opentelemetry-demo
# Chart 0.42.3 = aplicación 3.1.0 (la misma versión probada en Docker Compose)
DEMO_CHART_VERSION := 0.42.3
DEMO_VALUES := deploy/otel-demo/values.yaml

.PHONY: up stop down status demo demo-down policies-test agent-test auth prototype

up: ## Crea el clúster (idempotente)
	@if k3d cluster list $(CLUSTER) >/dev/null 2>&1; then \
		echo "El clúster $(CLUSTER) ya existe"; k3d cluster start $(CLUSTER); \
	else \
		k3d cluster create --config $(K3D_CONFIG); \
	fi
	kubectl wait --for=condition=Ready nodes --all --timeout=120s
	kubectl get nodes -o wide

stop: ## Para el clúster sin borrarlo (make up lo vuelve a arrancar tal cual)
	k3d cluster stop $(CLUSTER)

down: ## Destruye el clúster
	k3d cluster delete $(CLUSTER)

status: ## Estado del clúster
	k3d cluster list
	kubectl get nodes

demo: ## Despliega la tienda con el chart oficial (idempotente)
	helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts --force-update >/dev/null
	helm upgrade --install otel-demo $(DEMO_CHART) --version $(DEMO_CHART_VERSION) \
		--namespace $(DEMO_NAMESPACE) --create-namespace -f $(DEMO_VALUES)

demo-down: ## Desinstala la tienda
	helm uninstall otel-demo --namespace $(DEMO_NAMESPACE)

policies-test: ## Comprueba las políticas y ejecuta sus tests
	opa check --strict policies
	opa fmt --fail --list policies
	opa test policies

agent-test: ## Ejecuta los tests del ejecutor y del detector
	cd executor && python3 -m unittest
	cd agent && python3 -m unittest

auth: ## Aplica el RBAC y genera los kubeconfig temporales (24 h)
	./infra/service-accounts.sh

prototype: ## Arranca OPA, ejecutor y detector (Ctrl+C para detener)
	./agent/prototype.sh
