CLUSTER := autokub
K3D_CONFIG := infra/k3d/cluster.yaml

DEMO_NAMESPACE := otel-demo
DEMO_CHART := open-telemetry/opentelemetry-demo
# Chart 0.42.3 = aplicación 3.1.0 (la misma versión probada en Docker Compose)
DEMO_CHART_VERSION := 0.42.3
DEMO_VALUES := deploy/otel-demo/values.yaml

# Python 3.12 para el venv del agente (en macOS: make venv PYTHON=python3.12)
PYTHON ?= python3

.PHONY: up stop down status demo demo-down try try-check try-incident try-events try-execute try-reset test lint chart-test chart-sync images policies-test venv agent-test auth prototype prototype-llm

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

test: policies-test agent-test chart-test ## Todos los tests (sin clúster)

lint: ## Formato y errores de Rego y Python, y configuración del laboratorio
	opa check --strict policies
	opa fmt --fail --list policies
	ruff check agent executor viewer infra charts demo
	python3 infra/rbac.py config/lab.json > /dev/null
	helm lint --quiet charts/autokub --set 'namespaces.ns.targets={a}'
	@# La política del chart debe ser la del repositorio
	cmp policies/remediation.rego charts/autokub/files/remediation.rego

policies-test: ## Comprueba las políticas y ejecuta sus tests
	opa check --strict policies
	opa fmt --fail --list policies
	opa test policies
	@# La política con el fichero de configuración real del laboratorio: falla si no autoriza
	opa eval --fail-defined -f raw -d policies/remediation.rego -d autokub.config:config/lab.json \
		-i config/testdata/undo-product-catalog.json 'not data.autokub.remediation.decision.allow' > /dev/null

venv: ## Crea el entorno de Python del agente con versiones fijadas
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --quiet --require-virtualenv -r agent/requirements.lock

agent-test: ## Ejecuta los tests del ejecutor, del agente, de la vista y del RBAC
	cd executor && python3 -m unittest
	cd agent && ../.venv/bin/python -m unittest
	cd viewer && python3 -m unittest
	cd infra && python3 -m unittest

chart-test: ## Renderiza el chart y lo compara con el RBAC y la política del laboratorio
	.venv/bin/python charts/test_chart.py

chart-sync: ## Copia la política al chart
	cp policies/remediation.rego charts/autokub/files/remediation.rego

images: ## Construye las imágenes del agente y del ejecutor
	docker build -f images/executor/Dockerfile -t ghcr.io/marcosperezcantero/autokub-executor:dev .
	docker build -f images/agent/Dockerfile -t ghcr.io/marcosperezcantero/autokub-agent:dev .

try: ## Prueba autokub en un clúster local en un solo comando
	./demo/try.sh

# Los objetivos try-* solo actúan sobre el clúster de la demo
TRY_CONTEXT := k3d-autokub
try-check:
	@test "$$(kubectl config current-context)" = "$(TRY_CONTEXT)" || \
		{ echo "El contexto de kubectl no es $(TRY_CONTEXT): ejecuta make try o kubectl config use-context $(TRY_CONTEXT)"; exit 1; }

try-incident: try-check ## Provoca un CrashLoopBackOff en product-catalog
	./scenarios/crashloop/inject.sh

try-events: try-check ## Eventos de autokub en directo (Ctrl+C para salir)
	kubectl -n autokub-system logs -f --prefix --max-log-requests=4 -l app.kubernetes.io/name=autokub --all-containers \
		| python3 demo/events.py

try-execute: try-check ## Pasa autokub a modo ejecución
	helm upgrade autokub charts/autokub --kube-context $(TRY_CONTEXT) -n autokub-system --reuse-values --set mode=execute

try-reset: try-check ## Revierte el incidente y vuelve a modo propuesta
	./scenarios/crashloop/revert.sh || true
	helm upgrade autokub charts/autokub --kube-context $(TRY_CONTEXT) -n autokub-system --reuse-values --set mode=propose

auth: ## Aplica el RBAC generado desde config/lab.json y crea los kubeconfig temporales (24 h)
	./infra/service-accounts.sh

prototype: ## Arranca la cadena con reglas fijas (Ctrl+C para detener)
	./agent/prototype.sh rules

prototype-llm: ## Arranca la cadena con el agente LLM (Ctrl+C para detener)
	./agent/prototype.sh llm
