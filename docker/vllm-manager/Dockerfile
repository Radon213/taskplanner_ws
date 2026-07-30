ARG VLLM_IMAGE=vllm/vllm-openai:v0.26.0@sha256:ffb2d59b1c059a5bd8d781320c9f5189de8293693b7d95da54befddaa54abf52
FROM ${VLLM_IMAGE}

WORKDIR /opt/taskplanner-vllm-manager

COPY app.py /opt/taskplanner-vllm-manager/app.py
COPY models.json /opt/taskplanner-vllm-manager/models.json

ENTRYPOINT ["python3", "/opt/taskplanner-vllm-manager/app.py"]
