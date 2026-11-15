#!/bin/sh
# Einrichtung im LXC (Debian 13). Erneut ausführen = Update.
set -e
apt-get install -y --no-install-recommends python3-venv ffmpeg git ca-certificates \
  libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core
id rezepte >/dev/null 2>&1 || useradd --system --home /var/lib/rezepte --shell /usr/sbin/nologin rezepte
[ -d /opt/rezepte/.git ] || git clone https://github.com/DrdotHouse2106/reelkoch /opt/rezepte
git -C /opt/rezepte fetch -q origin main && git -C /opt/rezepte reset -q --hard origin/main
[ -d /opt/rezepte/.venv ] || python3 -m venv /opt/rezepte/.venv
/opt/rezepte/.venv/bin/pip install -q -U -r /opt/rezepte/requirements.txt
chown -R rezepte: /opt/rezepte/.venv
install -d -o rezepte -g rezepte -m 750 /var/lib/rezepte
[ -f /etc/rezepte.env ] || install -m 600 /opt/rezepte/deploy/rezepte.env.beispiel /etc/rezepte.env
cp /opt/rezepte/deploy/*.service /opt/rezepte/deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now yt-dlp-update.timer
systemctl enable rezepte
grep -q '^ANTHROPIC_API_KEY=.' /etc/rezepte.env && systemctl restart rezepte \
  || echo "Bitte /etc/rezepte.env ausfüllen, dann: systemctl restart rezepte"
