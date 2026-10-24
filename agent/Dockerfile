FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends iproute2 tini iptables && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/network-swarm

# Copy src modules for reuse in agent
COPY src/ /opt/network-swarm/src/

# Copy agent
COPY agent/agent.py /opt/network-swarm/agent.py

# Create config directory
RUN mkdir -p /etc/network-swarm /tmp/network-swarm

# Per-device control API (GET /status, POST /power/on, POST /power/off)
EXPOSE 8080

# Use tini as init process for proper signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run the agent
CMD ["python3", "/opt/network-swarm/agent.py"]
