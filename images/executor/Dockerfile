# Ejecutor: el único componente con permisos de escritura. Solo biblioteca estándar de Python
# y kubectl: ninguna dependencia de terceros, para que su superficie sea mínima.
# Construir desde la raíz del repositorio: docker build -f images/executor/Dockerfile .
ARG KUBECTL_VERSION=v1.35.5

FROM registry.k8s.io/kubectl:${KUBECTL_VERSION} AS kubectl

FROM python:3.12-slim-bookworm
COPY --from=kubectl /bin/kubectl /usr/local/bin/kubectl
WORKDIR /app
COPY executor/executor.py executor/
# Usuario sin privilegios; el sistema de ficheros se monta de solo lectura en el clúster
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AUTOKUB_EXECUTOR_HOST=0.0.0.0 AUTOKUB_KUBECONFIG="" AUTOKUB_EVENTS="" \
    AUTOKUB_CONFIG=/config/config.json
EXPOSE 8090
ENTRYPOINT ["python", "executor/executor.py"]
