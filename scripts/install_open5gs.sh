#!/bin/bash
set -e

apt update
apt install -y python3-pip python3-setuptools python3-wheel ninja-build build-essential \
    flex bison git cmake libsctp-dev libgnutls28-dev libgcrypt-dev libssl-dev \
    libidn11-dev libmongoc-dev libbson-dev libyaml-dev libnghttp2-dev \
    libmicrohttpd-dev libcurl4-gnutls-dev libnghttp2-dev libtins-dev \
    libtalloc-dev meson gnupg curl software-properties-common ca-certificates net-tools

# MongoDB
echo "Installing MongoDB..."
# Retry logic for network operations
for i in {1..3}; do
    if curl -fsSL https://pgp.mongodb.com/server-8.0.asc | gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor; then
        break
    fi
    echo "Attempt $i failed, retrying in 5 seconds..."
    sleep 5
done

if [ ! -f /usr/share/keyrings/mongodb-server-8.0.gpg ]; then
    echo "Failed to download MongoDB GPG key. Please check your network connection."
    exit 1
fi

echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/8.0 multiverse" | tee /etc/apt/sources.list.d/mongodb-org-8.0.list
apt update
apt install -y mongodb-org
systemctl start mongod
systemctl enable mongod

# TUN device
cat > /etc/systemd/network/99-open5gs.netdev <<EOF
[NetDev]
Name=ogstun
Kind=tun
EOF

cat > /etc/systemd/network/99-open5gs.network <<EOF
[Match]
Name=ogstun
[Network]
Address=10.45.0.1/16
Address=2001:db8:cafe::1/48
EOF

systemctl enable systemd-networkd
systemctl restart systemd-networkd
sleep 2

echo "net.ipv6.conf.ogstun.disable_ipv6=0" > /etc/sysctl.d/30-open5gs.conf
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.d/30-open5gs.conf
sysctl -p /etc/sysctl.d/30-open5gs.conf

# Clone and build Open5GS.
# The paper uses a modified Open5GS with Gramine-friendly build flags.
# Set OPEN5GS_REPO / OPEN5GS_BRANCH to point at the modified fork; the
# default falls back to upstream, which will not include the modifications.
cd /opt
OPEN5GS_REPO="${OPEN5GS_REPO:-https://github.com/open5gs/open5gs.git}"
OPEN5GS_BRANCH="${OPEN5GS_BRANCH:-main}"
git clone -b "$OPEN5GS_BRANCH" "$OPEN5GS_REPO" open5gs
cd open5gs
meson build --prefix=/usr/local
ninja -C build
ninja -C build install
ldconfig

# Install systemd services from source
cp -r build/configs/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

# Node.js
mkdir -p /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list
apt update
apt install -y nodejs

# WebUI
cd /opt/open5gs/webui
npm ci --no-optional
npm run build

# Create runtime directories
mkdir -p /usr/local/var/log/open5gs
mkdir -p /usr/local/etc/open5gs
chmod -R 755 /usr/local/var/log/open5gs

# Copy configs to install location
cp /opt/open5gs/build/configs/open5gs/*.yaml /usr/local/etc/open5gs/

# NAT
iptables -t nat -A POSTROUTING -s 10.45.0.0/16 ! -o ogstun -j MASQUERADE
ip6tables -t nat -A POSTROUTING -s 2001:db8:cafe::/48 ! -o ogstun -j MASQUERADE

# Start services
# systemctl start open5gs-nrfd open5gs-scpd open5gs-amfd open5gs-smfd open5gs-upfd \
#     open5gs-ausfd open5gs-udmd open5gs-udrd open5gs-pcfd open5gs-nssfd open5gs-bsfd

echo "Open5GS built and installed"
echo "Config: /usr/local/etc/open5gs/"
echo "Logs: /usr/local/var/log/open5gs/"
echo "WebUI: cd /opt/open5gs/webui && npm run dev"
