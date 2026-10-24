#!/bin/bash
# The API listens on the host network so Home Assistant Core can reach it.
# Core cannot use the host's loopback, so discovery announces a host address.
set -uo pipefail

install_integration() {
  local dest="$1"
  if [[ ! -d "${dest}" ]]; then
    return 1
  fi
  if [[ ! -f "${dest}/configuration.yaml" && ! -d "${dest}/.storage" && ! -d "${dest}/custom_components" ]]; then
    return 1
  fi
  mkdir -p "${dest}/custom_components"
  rm -rf "${dest}/custom_components/bridgething"
  cp -a /opt/custom_components/bridgething "${dest}/custom_components/bridgething"
  echo "Installed the Bridgething integration into ${dest}/custom_components/bridgething"
  return 0
}

if ! install_integration /config && ! install_integration /homeassistant; then
  echo "Home Assistant config is not mounted, so the integration was not copied" >&2
fi

# Older builds dropped this port except for one Docker subnet. That blocked
# Home Assistant Core, which reaches the host through the Docker bridge.
if command -v iptables >/dev/null 2>&1; then
  while iptables -D INPUT -p tcp --dport 8770 -j DROP 2>/dev/null; do true; done
  while iptables -D INPUT -p tcp --dport 8770 -s 172.30.0.0/16 -j ACCEPT 2>/dev/null; do true; done
  while iptables -D INPUT -p tcp --dport 8770 -s 127.0.0.0/8 -j ACCEPT 2>/dev/null; do true; done
fi

if [[ -n "${SUPERVISOR_TOKEN:-}" ]]; then
  host_ip="$(ip -4 -o addr show | awk '$4 ~ /^172\.30\.32\.1\// {print "172.30.32.1"; exit}')"
  if [[ -z "${host_ip}" ]]; then
    host_ip="$(ip -4 -o addr show | awk '$2 != "lo" && $4 !~ /^10\.42\.1\./ {split($4, a, "/"); print a[1]; exit}')"
  fi
  if [[ -n "${host_ip}" ]]; then
    curl -fsS -X POST "http://supervisor/discovery/bridgething" \
      -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{\"host\":\"${host_ip}\",\"port\":8770}" || true
  fi
fi

cd /opt/bridgething
exec python3 -m api.server
