ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        iproute2 \
        iptables \
        isc-dhcp-client \
        network-manager \
        dbus \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY api /opt/bridgething/api
COPY custom_components /opt/custom_components
COPY run.sh /run.sh

RUN sed -i 's/\r$//' /run.sh \
    && chmod 0755 /run.sh \
    && mkdir -p /etc/dhcp/dhclient-enter-hooks.d

WORKDIR /opt/bridgething
CMD ["/run.sh"]
