# Reuse the deployed inference stack so checkpoints produced here are binary
# compatible with the local RF-DETR service, then add only the optional local
# training and augmentation extras.
ARG RFDETR_INFERENCE_IMAGE=taskplanner-rfdetr-perception:0.1.0
FROM ${RFDETR_INFERENCE_IMAGE}

RUN python -m pip install --no-cache-dir \
    'rfdetr[augment,train]==1.9.0'
