FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY smsgw ./smsgw
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --home-dir /data smsgw \
 && mkdir -p /data && chown smsgw /data

USER 10001
VOLUME /data
EXPOSE 29340 29341
ENTRYPOINT ["python", "-m", "smsgw"]
CMD ["-c", "/data/config.yaml"]
