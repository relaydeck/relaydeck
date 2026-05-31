# Makefile — convenience entry points for Docker-based testing.
#
# Docker tests live under tests/docker/ and are opt-in: a bare
# `pytest` on the host stays unchanged (pyproject deselects `-m docker`).
#
# Two iteration modes:
#   make test-docker BUCKET=zero      # dev path — bind-mount /src, fast rebuild
#   make test-docker-ci BUCKET=zero   # CI path — COPY at build, self-contained
#
# Drop into the container to repro a failure manually:
#   make test-docker-shell BUCKET=zero
#
# Bucket → image map. Phase 1 ships only `zero`; later phases extend this.
DOCKER_IMAGE_zero := relaydeck-test:base
DOCKER_IMAGE_pi   := relaydeck-test:pi
DOCKER_IMAGE_all  := relaydeck-test:all

BUCKET ?= zero
IMAGE  := $(DOCKER_IMAGE_$(BUCKET))

# Where Docker mounts the host source in dev mode. Tests live under
# /work/tests (matches the WORKDIR in Dockerfile.base) so pytest's cwd is
# /work, while the editable install reads from /src.
DEV_MOUNTS := -v "$(PWD):/src:ro" -v "$(PWD)/tests:/work/tests:ro"

# Default goal lists the docker targets so `make` alone prints something useful.
.DEFAULT_GOAL := help

.PHONY: help test-docker-base test-docker-pi test-docker test-docker-ci \
        test-docker-all test-docker-shell test-docker-cli-manifest

help:
	@printf 'Docker testing targets (see TESTING.md, tests/docker/):\n'
	@printf '  make test-docker-base               build the base image\n'
	@printf '  make test-docker BUCKET=zero        dev mode (bind-mount source)\n'
	@printf '  make test-docker-ci BUCKET=zero     CI mode (COPY at build)\n'
	@printf '  make test-docker-all                run every bucket in parallel\n'
	@printf '  make test-docker-shell BUCKET=zero  drop into a shell in a bucket\n'
	@printf '  make test-docker-cli-manifest       regenerate cli-manifest.yml\n'

# Build the foundation image. Cached layers mean this is fast after the
# first build unless `relaydeck/`, `relaydeck_plugins/`, `pyproject.toml`,
# or `scripts/install.sh` change.
test-docker-base:
	docker build -f tests/docker/Dockerfile.base -t relaydeck-test:base .

# Build variant images — each is `FROM relaydeck-test:base` + a thin
# `RUN` to add harness CLIs (or other deps). Variant builds are cheap
# because they reuse the base layer cache; the only churn is the one
# `RUN` line at the top of each variant file.
test-docker-pi: test-docker-base
	docker build -f tests/docker/Dockerfile.pi -t relaydeck-test:pi .

# Dev path: bind-mount source so test code can iterate without a rebuild.
# Note: the `relaydeck` *binary* was installed at image build time, so code
# changes inside `relaydeck/` only propagate after `make test-docker-base`.
# Tests-only changes iterate immediately because the bind mount points to
# the current host source. The torch wheel in our deps is ~2GB; reinstalling
# on every run was exhausting Docker's disk, so we don't.
test-docker: test-docker-base
	@if [ -z "$(IMAGE)" ]; then \
		echo "unknown BUCKET=$(BUCKET); add DOCKER_IMAGE_$(BUCKET) in the Makefile" >&2; \
		exit 2; \
	fi
	@# Per-variant build hook: BUCKET=zero uses :base directly (built
	@# above); other variants need their own image. Add another
	@# branch here when a new bucket lands.
	@if [ "$(BUCKET)" = "pi-only" ]; then $(MAKE) -s test-docker-pi; fi
	docker run --rm \
		-e RELAYDECK_TEST_BUCKET=$(BUCKET) \
		$(DEV_MOUNTS) \
		$(IMAGE) \
		pytest -m docker --confcutdir=/work/tests/docker -c /src/pyproject.toml -o addopts= -p no:cacheprovider /work/tests/docker/ -v

# CI path: the image is self-contained; no bind mount. Reproducible across
# runners but every code change costs a full rebuild.
test-docker-ci: test-docker-base
	docker run --rm \
		-e RELAYDECK_TEST_BUCKET=$(BUCKET) \
		$(IMAGE) \
		pytest -m docker --confcutdir=/src/tests/docker -c /src/pyproject.toml -o addopts= -p no:cacheprovider /src/tests/docker/ -v

# Run every implemented bucket in parallel via docker compose. Phase 1 only
# wires `zero`; later phases add more services to tests/docker/compose.yml.
test-docker-all: test-docker-base
	docker compose -f tests/docker/compose.yml up --abort-on-container-exit --build

# Drop into a shell in a bucket's image for manual repro. Useful when a CI
# failure doesn't reproduce under `test-docker` (which is rare, but).
test-docker-shell: test-docker-base
	@if [ -z "$(IMAGE)" ]; then \
		echo "unknown BUCKET=$(BUCKET); add DOCKER_IMAGE_$(BUCKET) in the Makefile" >&2; \
		exit 2; \
	fi
	docker run --rm -it \
		-e RELAYDECK_TEST_BUCKET=$(BUCKET) \
		$(DEV_MOUNTS) \
		$(IMAGE) \
		bash

# Regenerate the CLI smoke manifest from Click introspection. Phase 3 ships
# the codegen + the manifest itself; Phase 1 leaves this as a placeholder
# target so the help text mentions it.
test-docker-cli-manifest:
	@echo 'cli-manifest codegen not yet implemented' >&2
	@exit 2
