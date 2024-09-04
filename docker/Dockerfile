# Dockerfile to build the matrixdotorg/sygnal docker images.
#
# To build the image, run `docker build` command from the root of the
# sygnal repository:
#
#    docker build -f docker/Dockerfile .
#

ARG PYTHON_VERSION=3.11

###
### Stage 0: generate requirements.txt
###
# We hardcode the use of Debian bookworm here because this could change upstream.
FROM docker.io/library/python:${PYTHON_VERSION}-slim-bookworm as requirements

# We install poetry in its own build stage to avoid its dependencies conflicting with
# sygnal's dependencies.
RUN --mount=type=cache,target=/root/.cache/pip \
  pip install --user "poetry==1.8.3"

WORKDIR /sygnal

# Copy just what we need to run `poetry export`...
COPY pyproject.toml poetry.lock /sygnal/

# If specified, we won't verify the hashes of dependencies.
# This is only needed if the hashes of dependencies cannot be checked for some
# reason, such as when a git repository is used directly as a dependency.
ARG TEST_ONLY_SKIP_DEP_HASH_VERIFICATION

# If specified, we won't use the Poetry lockfile.
# Instead, we'll just install what a regular `pip install` would from PyPI.
ARG TEST_ONLY_IGNORE_POETRY_LOCKFILE

# Export the dependencies, but only if we're actually going to use the Poetry lockfile.
# Otherwise, just create an empty requirements file so that the Dockerfile can
# proceed.
RUN if [ -z "$TEST_ONLY_IGNORE_POETRY_LOCKFILE" ]; then \
  /root/.local/bin/poetry export -o /sygnal/requirements.txt ${TEST_ONLY_SKIP_DEP_HASH_VERIFICATION:+--without-hashes}; \
  else \
  touch /sygnal/requirements.txt; \
  fi

###
### Stage 0: builder
###
FROM python:3.11-slim as builder

# Install git; Sygnal uses it to obtain the package version from the state of the
# git repository.
RUN apt-get update && apt-get install -y git

# install sygnal and all of the python deps to /install.
RUN git clone --branch calvinvw/hcm https://github.com/AnAlpaca/sygnal.git /sygnal/
#COPY . /sygnal/

RUN pip install --prefix="/install" --no-warn-script-location /sygnal

###
### Stage 1: runtime
###

FROM python:3.11-slim
COPY --from=builder /install /usr/local

EXPOSE 5000/tcp

ENTRYPOINT ["python", "-m", "sygnal.sygnal"]
