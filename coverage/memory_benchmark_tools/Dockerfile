# Memory benchmark image: a pinned Linux Python with a compiler for the prediction kernel and libfaketime for the
# frozen clock. Built once by coverage/memory_benchmark (the only step that uses the network); every run is offline.
FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3

RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ libfaketime procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
