FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# The two real showcase aircraft: excerpts with positions and device ids removed (DATA.md).
COPY tests/fixtures ./tests/fixtures
RUN pip install --no-cache-dir . && adduser --disabled-password --gecos "" uasw \
    && mkdir -p /data && chown uasw /data

USER uasw
ENV UASW_DB=/data/fleet.sqlite UASW_FIXTURES=/app/tests/fixtures
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uasw", "serve", "--host", "0.0.0.0", "--port", "8000"]
