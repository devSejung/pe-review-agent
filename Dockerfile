FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS build

# Optional corporate PyPI mirror support. When these build args are omitted,
# pip keeps its normal default index behavior.
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY requirements.lock pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip wheel setuptools \
    && python -m pip wheel --wheel-dir /wheels -r requirements.lock \
    && python -m pip wheel --wheel-dir /wheels --no-deps .

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS runtime

# Optional corporate Debian mirror support. python:3.12-slim is Debian-based,
# so these must point at Debian repositories, not the Ubuntu repositories used
# by the host OS. When omitted, the image keeps its normal Debian sources.
ARG APT_DEBIAN_MIRROR_URL
ARG APT_DEBIAN_SECURITY_MIRROR_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PE_REVIEW_CONFIG=/etc/pe-review-agent/config.yaml

RUN set -eux; \
    for sources in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
      [ -f "$sources" ] || continue; \
      if [ -n "${APT_DEBIAN_SECURITY_MIRROR_URL}" ]; then \
        sed -i -E "s#https?://deb\.debian\.org/debian-security#${APT_DEBIAN_SECURITY_MIRROR_URL}#g" "$sources"; \
      fi; \
      if [ -n "${APT_DEBIAN_MIRROR_URL}" ]; then \
        sed -i -E "s#https?://deb\.debian\.org/debian#${APT_DEBIAN_MIRROR_URL}#g" "$sources"; \
      fi; \
    done; \
    apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin pe-review-agent

COPY --from=build /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels gerrit-ai-reviewer \
    && rm -rf /wheels

COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations

RUN mkdir -p /var/lib/pe-review-agent/repos /var/lib/pe-review-agent/work \
    && chown -R pe-review-agent:pe-review-agent /var/lib/pe-review-agent

USER pe-review-agent
WORKDIR /app

ENTRYPOINT ["pe-review-agent"]
CMD ["worker"]
