# Panel de operación: solo biblioteca estándar de Python y kubectl para leer los logs de autokub.
# Construir desde la raíz del repositorio: docker build -f images/panel/Dockerfile .
ARG KUBECTL_VERSION=v1.35.5

FROM registry.k8s.io/kubectl:${KUBECTL_VERSION} AS kubectl

FROM python:3.12-slim-bookworm
COPY --from=kubectl /bin/kubectl /usr/local/bin/kubectl
WORKDIR /app
COPY panel/server.py panel/index.html panel/
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AUTOKUB_PANEL_SOURCE=kubernetes AUTOKUB_PANEL_KUBECONFIG="" AUTOKUB_CONFIG=/config/config.json
EXPOSE 8091
ENTRYPOINT ["python", "panel/server.py"]
