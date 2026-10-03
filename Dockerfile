FROM python:3.12-slim
LABEL org.opencontainers.image.title="MANY Hub" \
      org.opencontainers.image.source="https://github.com/ishizakahiroshi/many-hub" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY manyhub ./manyhub
RUN python -m pip install --no-cache-dir . \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin manyhub \
    && mkdir -p /data \
    && chown 10001:10001 /data \
    && chmod 700 /data
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
ENTRYPOINT ["manyhub", "--database", "/data/manyhub.sqlite3"]
CMD ["serve", "--local"]
