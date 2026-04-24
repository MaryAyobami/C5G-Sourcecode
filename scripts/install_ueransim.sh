#!/bin/bash
set -e

echo "UERANSIM Installation Script"
echo ""

# Update and install dependencies
echo "[1/3] Installing dependencies..."
apt update
apt install -y make g++ libsctp-dev lksctp-tools iproute2 git cmake \
    build-essential pkg-config libssl-dev net-tools

# Clone UERANSIM
echo "[2/3] Cloning UERANSIM..."
cd /opt
if [ -d "UERANSIM" ]; then
    echo "UERANSIM directory exists, removing..."
    rm -rf UERANSIM
fi

git clone https://github.com/aligungr/UERANSIM
cd UERANSIM

# Build UERANSIM
echo "[3/3] Building UERANSIM..."
make

# Set permissions
chmod -R 755 /opt/UERANSIM

echo ""
echo "UERANSIM Installation Complete!"
echo ""
echo "Installation Directory: /opt/UERANSIM"
echo "Binaries: /opt/UERANSIM/build/"
echo ""
echo "Usage:"
echo "  gNB: cd /opt/UERANSIM && sudo ./build/nr-gnb -c config/your-gnb.yaml"
echo "  UE:  cd /opt/UERANSIM && sudo ./build/nr-ue -c config/your-ue.yaml"
