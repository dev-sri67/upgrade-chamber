ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}

ARG PYTHON_IMAGE
RUN case "$PYTHON_IMAGE" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) echo 'PYTHON_IMAGE must be a digest-pinned Python 3.11 image' >&2; exit 1 ;; esac \
    && python -c 'import sys; assert sys.version_info[:2] == (3, 11)' \
    && groupadd --gid 10001 chamber \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin chamber \
    && mkdir -p /work /tmp /opt/upgrade_chamber \
    && chown 10001:10001 /work /tmp

COPY execution/runner.py /opt/upgrade_chamber/runner.py
COPY execution/attempt.py /opt/upgrade_chamber/attempt.py
COPY src/upgrade_chamber/baseline.py /opt/upgrade_chamber/baseline.py
COPY scripts/prepare_baseline.py /opt/upgrade_chamber/prepare_baseline.py
USER 10001:10001
WORKDIR /work
ENV PYTHONDONTWRITEBYTECODE=1
