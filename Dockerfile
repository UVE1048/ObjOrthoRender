FROM python:3.10-slim-bookworm

ARG APP_VERSION=1.0.3
ARG APT_MIRROR=https://mirrors.aliyun.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV APP_VERSION=${APP_VERSION}

LABEL org.opencontainers.image.title="ObjOrthoRender CLI"
LABEL org.opencontainers.image.version=${APP_VERSION}
LABEL org.opencontainers.image.authors="UVE"

WORKDIR /app

RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        printf '%s\n' \
        "Types: deb" \
        "URIs: ${APT_MIRROR}/debian" \
        "Suites: bookworm bookworm-updates" \
        "Components: main" \
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg" \
        "" \
        "Types: deb" \
        "URIs: ${APT_MIRROR}/debian-security" \
        "Suites: bookworm-security" \
        "Components: main" \
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg" \
        > /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}/debian|g; s|https://deb.debian.org/debian|${APT_MIRROR}/debian|g; s|http://security.debian.org/debian-security|${APT_MIRROR}/debian-security|g; s|https://security.debian.org/debian-security|${APT_MIRROR}/debian-security|g" /etc/apt/sources.list; \
    fi; \
    apt-get -o Acquire::Retries=5 update; \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        gdal-bin \
        libgdal-dev \
        build-essential \
        xauth \
        xvfb \
        mesa-utils \
        libgl1 \
        libgl1-mesa-dri \
        libegl1 \
        libgles2 \
        libx11-6 \
        libxext6 \
        libxrender1 \
        libxi6 \
        libsm6 \
        libxrandr2 \
    ; \
    mkdir -p /data; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENTRYPOINT ["/bin/sh", "-lc", "set -eu; echo \"ObjOrthoRender CLI v${APP_VERSION} starting...\"; Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & xvfb_pid=$!; export DISPLAY=:99; python -u /app/render_orthophotos.py \"$@\"; rc=$?; kill \"$xvfb_pid\" >/dev/null 2>&1 || true; exit $rc", "--"]
