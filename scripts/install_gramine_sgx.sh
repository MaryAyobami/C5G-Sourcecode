#!/bin/bash
set -e

if [ -z "$SUDO_USER" ]; then
    echo "Error: This script must be run with sudo."
    exit 1
fi

apt update
apt install -y build-essential autoconf bison gawk meson nasm pkg-config \
    python3 python3-click python3-jinja2 python3-pyelftools python3-tomli \
    python3-tomli-w python3-voluptuous wget libunwind8 musl-tools \
    python3-pytest libcurl4-openssl-dev libprotobuf-c-dev protobuf-c-compiler

curl -fsSLo /etc/apt/keyrings/intel-sgx-deb.asc https://download.01.org/intel-sgx/sgx_repo/ubuntu/intel-sgx-deb.key
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/intel-sgx-deb.asc] https://download.01.org/intel-sgx/sgx_repo/ubuntu jammy main" | tee /etc/apt/sources.list.d/intel-sgx.list

curl -fsSLo /etc/apt/keyrings/gramine-keyring-jammy.gpg https://packages.gramineproject.io/gramine-keyring-jammy.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/gramine-keyring-jammy.gpg] https://packages.gramineproject.io/ jammy main" | tee /etc/apt/sources.list.d/gramine.list

apt update
apt install -y libsgx-epid libsgx-quote-ex libsgx-dcap-ql libsgx-urts \
    libsgx-dcap-quote-verify-dev gramine

USER_HOME=$(eval echo ~${SUDO_USER})
mkdir -p $USER_HOME/.config/gramine
chown -R $SUDO_USER $USER_HOME/.config/gramine
sudo -u $SUDO_USER gramine-sgx-gen-private-key $USER_HOME/.config/gramine/enclave-key.pem

echo "Gramine installed. Check: is-sgx-available"