#!/bin/bash
# ============================================================
#  install.sh — Polymarket Bot — Oracle Cloud Ubuntu
#  Usage : bash install.sh
# ============================================================

set -e  # arrête le script à la première erreur

# ── Couleurs ────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
info() { echo -e "${YELLOW}[..] $1${NC}"; }
err()  { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

echo "============================================================"
echo "  POLYMARKET BOT — Installation automatique"
echo "============================================================"
echo ""

# ── 1. Mise à jour du système ────────────────────────────────
info "Mise a jour du systeme Ubuntu..."
sudo apt-get update -qq && sudo apt-get upgrade -y -qq
ok "Systeme a jour"

# ── 2. Python 3 ─────────────────────────────────────────────
info "Installation de Python 3..."
sudo apt-get install -y -qq python3 python3-pip python3-venv
PYTHON_VERSION=$(python3 --version)
ok "Python installe : $PYTHON_VERSION"

# ── 3. Outils systeme ───────────────────────────────────────
info "Installation des outils systeme (git, screen, curl)..."
sudo apt-get install -y -qq git screen curl
ok "Outils systeme installes"

# ── 4. Dossier du bot ───────────────────────────────────────
BOT_DIR="$HOME/polymarket_bot"
info "Dossier du bot : $BOT_DIR"
mkdir -p "$BOT_DIR"

# Copie les fichiers si on est deja dans le dossier source
if [ -f "main.py" ]; then
    cp main.py copytrader.py wallet_tracker.py market_analyzer.py \
       telegram_notifier.py requirements.txt "$BOT_DIR/"
    ok "Fichiers Python copies dans $BOT_DIR"
else
    info "Fichiers main.py non trouves ici — a copier manuellement dans $BOT_DIR"
fi

cd "$BOT_DIR"

# ── 5. Environnement virtuel Python ─────────────────────────
info "Creation de l'environnement virtuel Python..."
python3 -m venv venv
ok "Environnement virtuel cree : $BOT_DIR/venv"

# ── 6. Dependances Python ───────────────────────────────────
info "Installation des dependances Python (requests)..."
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q
ok "Dependances installees"

# ── 7. Fichier performance.json vide ────────────────────────
if [ ! -f "$BOT_DIR/performance.json" ]; then
    info "Creation du fichier performance.json vide..."
    cat > "$BOT_DIR/performance.json" << 'EOF'
{"meta": {}, "cycles": [], "summary": {}}
EOF
    ok "performance.json cree"
fi

# ── 8. Service systemd (lancement automatique au reboot) ────
info "Configuration du service systemd..."

SERVICE_FILE="/etc/systemd/system/polymarket-bot.service"
sudo bash -c "cat > $SERVICE_FILE" << EOF
[Unit]
Description=Polymarket Copy Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python main.py
Restart=on-failure
RestartSec=30
StandardOutput=append:$BOT_DIR/bot.log
StandardError=append:$BOT_DIR/bot.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot
ok "Service systemd configure (polymarket-bot)"

# ── 9. Résumé ────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  INSTALLATION TERMINEE"
echo "============================================================"
echo ""
echo "  Dossier du bot  : $BOT_DIR"
echo "  Python          : $BOT_DIR/venv/bin/python"
echo "  Logs            : $BOT_DIR/bot.log"
echo ""
echo "  Commandes utiles :"
echo "    Demarrer    : sudo systemctl start polymarket-bot"
echo "    Arreter     : sudo systemctl stop polymarket-bot"
echo "    Statut      : sudo systemctl status polymarket-bot"
echo "    Logs live   : tail -f $BOT_DIR/bot.log"
echo ""
echo "  Test manuel (1 cycle) :"
echo "    cd $BOT_DIR && ./venv/bin/python main.py --cycles 1"
echo ""
echo "============================================================"
