# loockit runtime image.
#
# BLE control runs against the *host's* Bluetooth adapter via BlueZ over D-Bus,
# so the container must share the host network and the system D-Bus socket (see
# docker-compose.yml). For a quick try without hardware, run with `--simulate`.
FROM python:3.12-slim

# BlueZ provides the D-Bus interface bleak talks to; dbus client libs required.
# avahi-utils supplies avahi-publish-service, used by the Matter bridge (mDNS).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bluez \
        libdbus-1-3 \
        libglib2.0-0 \
        avahi-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Install the core plus the REST, MQTT, and Matter extras so the container can
# run any interface; enable each via config.toml.
RUN pip install --no-cache-dir ".[rest,mqtt,matter]"

# Default config path; mount your real config over this.
ENV LOOCKIT_CONFIG=/config/config.toml

EXPOSE 50051
EXPOSE 8080
EXPOSE 5541/udp

ENTRYPOINT ["loockit"]
CMD ["run", "--config", "/config/config.toml", "-v"]
