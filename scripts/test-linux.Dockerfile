# The environment `scripts/test.py --linux` runs the backend suite in.
#
# The suite is authoritative on Linux: a handful of cases exercise POSIX process
# groups, `/bin/sh`, symlink semantics and Claude Code's project-slug encoding,
# none of which Windows can express. A bare `python:3.12-slim` carries neither
# `git` (the fork/rewind cases) nor the project's dependencies, and paying for an
# `apt-get install` + `pip install` on *every* run is what made this loop slow —
# roughly four minutes of installation around forty seconds of tests, repeated
# dozens of times in one session. Bake it once instead; `scripts/test.py --linux`
# builds this only when the image is missing.
#
#   docker build -f scripts/test-linux.Dockerfile -t octopus-test:3.12 .
#
# The two mirrors are build args so a network that needs one can supply it
# without this file having an opinion (pip's default index is used otherwise):
#
#   docker build --build-arg PIP_INDEX_URL=https://mirror/simple \
#                --build-arg APT_MIRROR=https://mirror.example.org ...
#
# `APT_MIRROR` is the mirror's *host*: the Debian archive paths (`/debian`,
# `/debian-security`) are kept from the base image, so pointing it at
# `https://mirror/debian` would build `/debian/debian-security` and fail.
#
# `BASE_IMAGE` is the same idea one level up: a box whose Docker registry mirror
# cannot reach Docker Hub overrides where the base comes from, instead of this
# file naming a registry that only works there.

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ARG APT_MIRROR=
ARG PIP_INDEX_URL=

# Repoint apt only when a mirror was given, and only if the base image still
# uses a sources file we recognise — `|| true` keeps a future layout change from
# breaking the build, which would then fall back to the upstream default.
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|https\?://deb.debian.org|$APT_MIRROR|g" \
        /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# The dependency list is read out of pyproject.toml rather than copied here, so
# the image cannot silently drift from what the suite needs. (`PIP_INDEX_URL` is
# assigned as a *prefix* to the command — the one form whose value is certainly
# in that command's environment.)
COPY pyproject.toml /tmp/pyproject.toml
RUN PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}" \
    python -c 'import os, pathlib, subprocess, sys, tomllib; p = tomllib.loads(pathlib.Path("/tmp/pyproject.toml").read_text())["project"]; deps = [*p["dependencies"], *p["optional-dependencies"]["test"]]; sys.exit(subprocess.call([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-i", os.environ["PIP_INDEX_URL"], *deps]))'

# The suite is run with the checkout mounted at /app and PYTHONPATH=/app, so no
# install of the project itself is needed — and the mounted source is what runs.
WORKDIR /app
