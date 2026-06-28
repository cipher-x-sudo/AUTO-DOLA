FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    CHROME_PROFILE_DIR=/data/browser-profile

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        docker.io \
        fluxbox \
        netcat-openbsd \
        novnc \
        openvpn \
        procps \
        iproute2 \
        python3-websockify \
        socat \
        x11vnc \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY start-browser.sh /app/start-browser.sh
COPY browser_manager.py /app/browser_manager.py
RUN chmod +x /app/start-browser.sh

EXPOSE 6080 7070
CMD ["/app/start-browser.sh"]
