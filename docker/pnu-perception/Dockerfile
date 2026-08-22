ARG PNU_BASE_IMAGE=taskplanner-rfdetr-perception:unified-compat
FROM ${PNU_BASE_IMAGE}

COPY docker/pnu-perception/requirements.txt /tmp/pnu-perception-requirements.txt
RUN python -m pip install --no-cache-dir \
      --requirement /tmp/pnu-perception-requirements.txt

COPY src/pnu_perception_worker/pnu_perception_worker \
     /opt/taskplanner/pnu_perception_worker

ENV PNU_UPSTREAM_ROOT=/opt/hand-blood-tools \
    PNU_MODEL_ROOT=/models \
    PNU_HOST=0.0.0.0 \
    PNU_PORT=8020 \
    PNU_DEVICE_POLICY=cuda_required

EXPOSE 8020

# Liveness remains visible as HTTP 200 while the JSON readiness bit fails the
# container health gate when source, assets, or a requested model did not load.
HEALTHCHECK --interval=10s --timeout=3s --start-period=120s --retries=3 \
  CMD python -c "import json,urllib.request; p=json.load(urllib.request.urlopen('http://127.0.0.1:8020/v1/health',timeout=2)); raise SystemExit(0 if p.get('ready') is True else 1)"

ENTRYPOINT ["python", "-m", "pnu_perception_worker.cli"]
CMD ["--host", "0.0.0.0", "--port", "8020"]
