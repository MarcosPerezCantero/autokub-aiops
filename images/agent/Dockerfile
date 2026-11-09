# Detector y agente: solo leen. Llevan las dependencias del agente LLM, fijadas en el lock.
# Construir desde la raíz del repositorio: docker build -f images/agent/Dockerfile .
ARG KUBECTL_VERSION=v1.35.5

FROM registry.k8s.io/kubectl:${KUBECTL_VERSION} AS kubectl

FROM python:3.12-slim-bookworm AS deps
COPY agent/requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.lock

FROM python:3.12-slim-bookworm
COPY --from=kubectl /bin/kubectl /usr/local/bin/kubectl
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY agent/watcher.py agent/rules.py agent/llm_agent.py agent/
USER 65532:65532
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AUTOKUB_READER_KUBECONFIG="" AUTOKUB_EVENTS="" AUTOKUB_CONFIG=/config/config.json
ENTRYPOINT ["python", "agent/watcher.py"]
