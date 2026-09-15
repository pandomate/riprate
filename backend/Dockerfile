FROM python:3.12-slim

# System packages:
#   ffmpeg           — HLS/DASH downloads
#   openvpn          — OpenVPN profile pool client
#   iproute2,iptables— routing + kill-switch when a VPN is enabled
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    openvpn \
    iproute2 \
    iptables \
    procps \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONPATH=/opt/vpn_profiles

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY vpn_profiles /opt/vpn_profiles
COPY backend .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
