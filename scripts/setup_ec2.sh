#!/bin/bash
# EC2 Setup Script for Hyperliquid Copy Trading Bot
# Run on a fresh Amazon Linux 2023 instance in ap-northeast-1 (Tokyo)
set -euo pipefail

echo "=== Hyperliquid Copy Trading Bot - EC2 Setup ==="

# 1. System updates
sudo dnf update -y

# 2. Install Python 3.11+
sudo dnf install -y python3.11 python3.11-pip python3.11-devel git

# 3. Create dedicated user
sudo useradd -r -s /bin/bash -m copybot || true

# 4. Create application directory
sudo mkdir -p /opt/hl-copybot
sudo chown copybot:copybot /opt/hl-copybot

# 5. Clone repository (update URL as needed)
echo "Clone your repo to /opt/hl-copybot or scp the code"

# 6. Setup virtual environment
sudo -u copybot bash -c '
    cd /opt/hl-copybot
    python3.11 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
'

# 7. Create systemd service
sudo tee /etc/systemd/system/hl-copybot.service > /dev/null << 'EOF'
[Unit]
Description=Hyperliquid Copy Trading Bot
After=network.target

[Service]
Type=simple
User=copybot
Group=copybot
WorkingDirectory=/opt/hl-copybot
ExecStart=/opt/hl-copybot/venv/bin/python -m copybot.main
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/hl-copybot
PrivateTmp=yes

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hl-copybot

[Install]
WantedBy=multi-user.target
EOF

# 8. Reload systemd
sudo systemctl daemon-reload
sudo systemctl enable hl-copybot

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy your code to /opt/hl-copybot/"
echo "  2. Set secrets in environment or AWS SSM:"
echo "     - HL_PAIR_0_AGENT_PRIVATE_KEY"
echo "     - HL_PAIR_0_FOLLOWER_ADDRESS"
echo "     - DISCORD_WEBHOOK_URL"
echo "  3. Edit /opt/hl-copybot/copybot/config/settings.yaml"
echo "  4. Start: sudo systemctl start hl-copybot"
echo "  5. Logs:  journalctl -u hl-copybot -f"
