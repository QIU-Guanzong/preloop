# Build from the repository root:
# docker build -f runtime-plugins/harness-preloop/Dockerfile --build-arg HARNESS=pi -t preloop-pi .
# Select HARNESS=deepseek for dsh, then set PI_IMAGE / DEEPSEEK_IMAGE on the worker.
FROM node:22-bookworm
ARG HARNESS=pi
COPY runtime-plugins/harness-preloop/package*.json /opt/preloop-harness/
WORKDIR /opt/preloop-harness
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund \
    && case "$HARNESS" in \
       pi) npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.85.1 ;; \
       deepseek) npm install -g --ignore-scripts @deepseek-ai/dsh@0.1.5-rc.2 ;; \
       *) exit 1 ;; \
       esac \
    && mkdir -p /workspace && chown 10000:10000 /workspace
COPY runtime-plugins/harness-preloop/*.mjs /opt/preloop-harness/
ENV PRELOOP_DISABLE_TELEMETRY=true DSH_TELEMETRY_DISABLED=true
WORKDIR /workspace
USER 10000:10000
ENTRYPOINT ["/bin/bash"]
