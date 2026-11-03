# syntax=docker/dockerfile:1

# ---- Build: install dependencies into a venv ----
FROM python:3.13-alpine AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /src
COPY pyproject.toml README.md ./
COPY sds800xhd_mcp ./sds800xhd_mcp
RUN pip install . \
 # pip/setuptools are not needed at runtime
 && pip uninstall -y pip setuptools \
 && find /opt/venv -depth \( -name tests -o -name __pycache__ \) -type d -exec rm -rf {} + \
 && python -m compileall -q /opt/venv

# ---- Runtime ----
FROM python:3.13-alpine

RUN adduser -D -H -u 10001 sds \
 && mkdir /data && chown sds /data \
 # remove the base image's pip, not needed
 && rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/bin/pip*

COPY --from=build /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SDS_MCP_HOST=0.0.0.0 \
    SDS_MCP_PORT=8800 \
    SDS_MCP_DATA_DIR=/data

USER sds
VOLUME /data
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"SDS_MCP_PORT\"]}/api/state', timeout=4)"

ENTRYPOINT ["sds800xhd-mcp"]
