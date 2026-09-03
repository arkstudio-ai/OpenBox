#!/bin/sh
# Install the isolated reverse-tunnel sshd on the gateway host.
# Usage: INTERNAL_API_TOKEN=... sh wuying_relay_setup.sh
set -eu

: "${INTERNAL_API_TOKEN:?INTERNAL_API_TOKEN is required}"

if ! id obxtunnel >/dev/null 2>&1; then
  useradd --create-home --shell /usr/sbin/nologin obxtunnel
fi
# Keep password login disabled in sshd while avoiding OpenSSH's locked-account
# rejection before AuthorizedKeysCommand gets a chance to inspect the key.
passwd -d obxtunnel >/dev/null

install -d -m 755 /etc/openbox
printf 'INTERNAL_API_TOKEN=%s\n' "$INTERNAL_API_TOKEN" > /etc/openbox/authkeys.env
chown root:nogroup /etc/openbox/authkeys.env
chmod 640 /etc/openbox/authkeys.env

cat > /usr/local/bin/obx-authkeys <<'EOF'
#!/bin/sh
set -eu
. /etc/openbox/authkeys.env
exec /usr/bin/curl --fail --silent --show-error --get \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN" \
  --data-urlencode "fingerprint=$1" \
  http://127.0.0.1:8080/api/internal/tunnel-keys
EOF
chown root:root /usr/local/bin/obx-authkeys
chmod 755 /usr/local/bin/obx-authkeys

cat > /etc/ssh/sshd_tunnel_config <<'EOF'
Port 2222
ListenAddress 0.0.0.0
Protocol 2
HostKey /etc/ssh/ssh_host_ed25519_key
PidFile /run/sshd-tunnel.pid
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin no
PermitTTY no
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding remote
PermitOpen none
GatewayPorts clientspecified
ClientAliveInterval 30
ClientAliveCountMax 3
AuthorizedKeysFile none
AuthorizedKeysCommand /usr/local/bin/obx-authkeys %f
AuthorizedKeysCommandUser nobody
AllowUsers obxtunnel
LogLevel VERBOSE
EOF

cat > /etc/systemd/system/openbox-tunnel-sshd.service <<'EOF'
[Unit]
Description=OpenBox per-desktop reverse tunnel SSH server
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=/usr/sbin/sshd -t -f /etc/ssh/sshd_tunnel_config
ExecStart=/usr/sbin/sshd -D -f /etc/ssh/sshd_tunnel_config
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now openbox-tunnel-sshd.service
sshd -T -f /etc/ssh/sshd_tunnel_config | grep -Ei \
  '^(port|authorizedkeyscommand|authorizedkeyscommanduser|gatewayports|allowtcpforwarding|permitopen) '
