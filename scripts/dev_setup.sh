#!/usr/bin/env bash
set -euo pipefail

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
  echo -e "${GREEN}[*] $1${NC}"
}

warn() {
  echo -e "${YELLOW}[!] $1${NC}"
}

log "=== Updating system packages ==="
sudo apt update && sudo apt upgrade -y

log "=== Installing core tools ==="
sudo apt install -y \
  git curl wget unzip zip \
  build-essential pkg-config \
  software-properties-common \
  ca-certificates \
  fzf bat exa ripgrep zsh

# Zellij: Install via apt, fallback to manual
log "=== Installing Zellij ==="
if ! command -v zellij >/dev/null 2>&1; then
  if sudo apt install -y zellij; then
    log "Zellij installed via apt"
  else
    log "Installing Zellij manually from GitHub..."
    curl -fsSL https://github.com/zellij-org/zellij/releases/latest/download/zellij-x86_64-unknown-linux-musl.tar.gz -o /tmp/zellij.tar.gz
    tar -xzf /tmp/zellij.tar.gz -C /tmp
    sudo mv /tmp/zellij /usr/local/bin/
    rm -f /tmp/zellij.tar.gz /tmp/zellij
    log "Zellij installed manually"
  fi
else
  log "Zellij already installed"
fi

# Yazi: Install via official binary
log "=== Installing Yazi ==="
if ! command -v yazi >/dev/null 2>&1; then
  YAZI_URL="https://github.com/sxyazi/yazi/releases/latest/download/yazi-x86_64-unknown-linux-gnu.tar.xz"
  curl -fsSL "$YAZI_URL" -o /tmp/yazi.tar.xz
  tar -xJf /tmp/yazi.tar.xz -C /tmp
  mkdir -p ~/.local/bin
  mv /tmp/yazi ~/.local/bin/
  rm -f /tmp/yazi.tar.xz
  log "Yazi installed via binary"
else
  log "Yazi already installed"
fi

# Update PATH immediately and persist
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"

# Ensure Yazi config dir exists (auto-generated on first run)
mkdir -p ~/.config/yazi

# Zsh + Oh My Zsh + Starship
log "=== Setting up Zsh, Oh My Zsh, and Starship ==="

# Install Oh My Zsh (unattended)
if [ ! -d "$HOME/.oh-my-zsh" ]; then
  export RUNZSH=no
  export CHSH=no
  sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended --keep-zshrc
  log "Oh My Zsh installed"
else
  log "Oh My Zsh already installed"
fi

# Install Starship
if ! command -v starship >/dev/null 2>&1; then
  curl -fsSL https://starship.rs/install.sh | sh -s -- --bin-dir ~/.local/bin --yes
  log "Starship installed"
else
  log "Starship already installed"
fi

# Add Starship to zshrc
if ! grep -q "starship init zsh" ~/.zshrc 2>/dev/null; then
  echo 'eval "$(starship init zsh)"' >> ~/.zshrc
fi

# Set Zsh as default shell (only if interactive)
if [ -t 1 ]; then
  if [ "$SHELL" != "$(which zsh)" ]; then
    log "Setting Zsh as default shell..."
    sudo chsh -s "$(which zsh)" "$USER"
  else
    log "Zsh already default shell"
  fi
else
  warn "Non-interactive shell: run 'chsh -s $(which zsh)' manually to set Zsh as default"
fi

# Python & Node.js
log "=== Installing Python, Node.js, and dev tools ==="
sudo apt install -y python3 python3-pip python3-venv
sudo apt install -y nodejs npm
sudo npm install -g yarn pnpm

# Docker
log "=== Installing Docker ==="
sudo apt install -y apt-transport-https gnupg lsb-release

# Add Docker GPG key
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor --batch --yes -o /usr/share/keyrings/docker.gpg

# Add Docker repo
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add user to docker group
if ! groups $USER | grep -q docker; then
  sudo usermod -aG docker $USER
  warn "Docker installed. You must log out and back in for group changes to take effect."
else
  log "User already in docker group"
fi

# Neovim (latest stable)
log "=== Installing Neovim (latest stable) ==="
if ! command -v nvim >/dev/null 2>&1; then
  sudo add-apt-repository ppa:neovim-ppa/stable -y
  sudo apt update
  sudo apt install -y neovim
  log "Neovim installed"
else
  log "Neovim already installed"
fi

# LazyGit
log "=== Installing LazyGit ==="
if ! command -v lazygit >/dev/null 2>&1; then
  LAZYGIT_VER=$(curl -s "https://api.github.com/repos/jesseduffield/lazygit/releases/latest" | grep -Po '"tag_name": "\K.*?(?=")')
  curl -Lo /tmp/lazygit.tar.gz "https://github.com/jesseduffield/lazygit/releases/download/${LAZYGIT_VER}/lazygit_${LAZYGIT_VER#v}_Linux_x86_64.tar.gz"
  tar xf /tmp/lazygit.tar.gz -C /tmp lazygit
  sudo install /tmp/lazygit /usr/local/bin/lazygit
  rm -f /tmp/lazygit.tar.gz /tmp/lazygit
  log "LazyGit installed"
else
  log "LazyGit already installed"
fi

# Cleanup
log "=== Cleaning up ==="
sudo apt autoremove -y
sudo apt clean

# Final Message
echo
echo "Setup complete!"
echo
echo "Restart your shell or run 'exec zsh' to start using the new environment."
echo
echo "Launch tools with:"
echo "   zellij      - terminal multiplexer"
echo "   yazi        - file manager"
echo "   nvim        - text editor"
echo "   lazygit     - git TUI"
echo "   docker      - container runtime (after re-login)"
echo
echo "Tip: Run 'yazi' once to generate config files in ~/.config/yazi"
