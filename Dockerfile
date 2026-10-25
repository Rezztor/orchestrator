# Four images from one file: docker build --target core|ansible|tofu|postgres .
# Ours run as an unprivileged user and hold only what their service needs. scripts/build_release.sh builds all four.
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/orch orch  # the code in /app stays root's
WORKDIR /app

FROM base AS core
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY LICENSE THIRD-PARTY-NOTICES.md ./
COPY orchestrator orchestrator
COPY plugins plugins
COPY skills skills
COPY agents agents
COPY evals evals
USER orch
ENV ORCH_WEB_LISTEN=0.0.0.0:8443
EXPOSE 8443
CMD ["python", "-m", "orchestrator.web"]

FROM base AS executor
COPY LICENSE THIRD-PARTY-NOTICES.md ./
COPY executors executors

FROM executor AS ansible
# SSH for device connections; network collections (cisco.ios and the like) go in an image built FROM this one.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r executors/requirements.txt
USER orch
ENV ORCH_ANSIBLE_LISTEN=0.0.0.0:8470 ORCH_ANSIBLE_INVENTORY=/etc/orch/inventory ORCH_ANSIBLE_PLAYBOOKS=/etc/orch/playbooks
EXPOSE 8470
CMD ["python", "-m", "executors.ansible_executor"]

FROM executor AS tofu
ARG TOFU_VERSION=1.10.6
ARG TOFU_SHA256=15b7bed76420b50da3e121769c43341df8cd57d751ca14e6dbe9c850124c6dac
# shortcut: linux/amd64 only; add the arm64 sum (a32f653d...) and a TARGETARCH switch when a customer needs arm64.
ADD https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip /tmp/tofu.zip
RUN echo "${TOFU_SHA256}  /tmp/tofu.zip" | sha256sum -c - \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/tofu.zip').extract('tofu', '/usr/local/bin')" \
    && chmod 0755 /usr/local/bin/tofu && rm /tmp/tofu.zip \
    && mkdir -p /var/lib/orch-tofu && chown orch:orch /var/lib/orch-tofu
USER orch
ENV ORCH_TOFU_LISTEN=0.0.0.0:8471 ORCH_TOFU_MODULES=/etc/orch/modules ORCH_TOFU_STATE=/var/lib/orch-tofu
EXPOSE 8471
CMD ["python", "-m", "executors.tofu_executor"]

# The database, pinned and shipped with the release like the rest (a digest does not survive docker save/load).
FROM postgres:16@sha256:9e2cf6287f25c3151f4c9421ccce2b17f3e60061c111a29165e0d2cdd9ff0a97 AS postgres
