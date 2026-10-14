#!/bin/bash
set -e
echo "=== Installation de RayBot ==="
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git
cd /root
rm -rf RayBot
git clone https://github.com/trrayane/RayBot.git
cd RayBot/verify_bot
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
cp "../M1 IV A (Liste Affichage).xlsx" . 2>/dev/null || true
cat > /etc/systemd/system/raybot.service << 'EOF'
[Unit]
Description=RayBot Discord
After=network.target
[Service]
Type=simple
User=root
WorkingDirectory=/root/RayBot/verify_bot
ExecStart=/root/RayBot/verify_bot/venv/bin/python -u bot.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable raybot
systemctl start raybot
echo ""
echo "=== Bot installé ! ==="
echo "Statut : systemctl status raybot"
echo "Logs   : journalctl -u raybot -f"
