# Fresh-install verification for relaydeck.
#
# Builds a clean container and installs relaydeck the way an operator would —
# `uv tool install` into an isolated venv — then runs scripts/install-smoke.sh
# (boot the daemon, hit /healthz + the API, run `relaydeck doctor`). If this
# passes, a clean machine can install + run relaydeck.
#
#   # from the repo root (so the build context has the source):
#   docker build -f tests/install/Dockerfile -t relaydeck-install-test .
#   docker run --rm relaydeck-install-test
#
# Or just: scripts/test-install.sh

FROM python:3.13-slim

# git: the real installer pulls from git; curl: bootstraps uv + drives the API;
# ca-certificates: TLS for both.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# uv — the same tool scripts/install.sh bootstraps.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Install from the local source tree, isolated in its own tool venv — exactly
# how `uv tool install relaydeck@git+…` works for a released version, minus the
# network so the test pins THIS checkout.
WORKDIR /src
COPY . /src
RUN uv tool install /src

COPY scripts/install-smoke.sh /usr/local/bin/install-smoke.sh
RUN chmod +x /usr/local/bin/install-smoke.sh

CMD ["bash", "/usr/local/bin/install-smoke.sh"]
