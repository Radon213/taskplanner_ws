ARG RFDETR_BASE_IMAGE=pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime@sha256:e05438443ae3c407e8d04447091a959dbb6757b6290b128770c3c787d4bd442b
FROM ${RFDETR_BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/taskplanner

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY docker/rfdetr-perception/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /opt/taskplanner
COPY src/vlm_node/vlm_node /opt/taskplanner/vlm_node

CMD ["python", "-m", "vlm_node.rfdetr_service", "--host", "127.0.0.1", "--port", "8010"]
