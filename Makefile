CLUSTER := autokub
K3D_CONFIG := infra/k3d/cluster.yaml

.PHONY: up down status

up: ## Crea el clúster (idempotente)
	@if k3d cluster list $(CLUSTER) >/dev/null 2>&1; then \
		echo "El clúster $(CLUSTER) ya existe"; k3d cluster start $(CLUSTER); \
	else \
		k3d cluster create --config $(K3D_CONFIG); \
	fi
	kubectl wait --for=condition=Ready nodes --all --timeout=120s
	kubectl get nodes -o wide

down: ## Destruye el clúster
	k3d cluster delete $(CLUSTER)

status: ## Estado del clúster
	k3d cluster list
	kubectl get nodes
