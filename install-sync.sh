#!/usr/bin/env bash
# Установка сервера синхронизации «Лад» на VPS (Ubuntu/Debian).
# Запуск:  curl -fsSL https://ridalsultanov.github.io/songs/install-sync.sh | sudo bash
set -euo pipefail
main(){
SRC="https://ridalsultanov.github.io/songs/lad_sync.py"
DIR=/opt/lad; DATA=/var/lib/lad; PORT=8787
if [ -f /etc/systemd/system/lad-sync.service ]; then PORT=$(grep -o "LAD_PORT=[0-9]*" /etc/systemd/system/lad-sync.service | cut -d= -f2 || echo 8787); fi
say(){ printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }
die(){ printf '\n\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запусти через sudo"
command -v python3 >/dev/null || { say "Ставлю Python 3"; apt-get update -qq && apt-get install -y -qq python3; }

if ! [ -f /etc/systemd/system/lad-sync.service ] && command -v ss >/dev/null && ss -ltn | grep -q ":$PORT "; then PORT=8797; fi
say "Скачиваю сервер"
mkdir -p "$DIR" "$DATA"
curl -fsSL "$SRC" -o "$DIR/lad_sync.py"
id lad >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin lad
chown -R lad:lad "$DATA"; chmod 700 "$DATA"

say "Создаю службу lad-sync"
cat > /etc/systemd/system/lad-sync.service <<EOF
[Unit]
Description=Lad songbook sync
After=network.target
[Service]
User=lad
Environment=LAD_DATA=$DATA LAD_PORT=$PORT LAD_ORIGINS=https://ridalsultanov.github.io
ExecStart=/usr/bin/env python3 $DIR/lad_sync.py
Restart=always
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$DATA
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable lad-sync >/dev/null 2>&1; systemctl restart lad-sync
sleep 1
curl -fs "http://127.0.0.1:$PORT/api" >/dev/null || die "Служба не отвечает: journalctl -u lad-sync -n 30"

say "Пароли"
PW=""
for u in ridal david; do
  if [ -f "$DATA/$u.auth" ]; then PW="$PW\n  $u: уже создан раньше (сброс: sudo -u lad LAD_DATA=$DATA python3 $DIR/lad_sync.py adduser $u)"
  else PW="$PW\n  $(sudo -u lad LAD_DATA=$DATA python3 $DIR/lad_sync.py adduser $u)"; fi
done

IP=$(curl -4 -fs --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
HOST="lad.$(echo "$IP" | tr . -).sslip.io"
BLOCK="$HOST {
	reverse_proxy 127.0.0.1:$PORT
}"

say "Подключаю HTTPS через Caddy ($HOST)"
CF=/etc/caddy/Caddyfile
if [ -f "$CF" ] && systemctl is-active --quiet caddy; then
  if grep -q "$HOST" "$CF"; then echo "  уже подключено"
  else
    cp "$CF" "$CF.bak.$(date +%s)"
    printf '\n%s\n' "$BLOCK" >> "$CF"
    if caddy validate --config "$CF" --adapter caddyfile >/dev/null 2>&1; then systemctl reload caddy
    else cp "$(ls -t $CF.bak.* | head -1)" "$CF"; die "Caddy не принял настройку, вернул как было. Пришли вывод: caddy validate --config $CF"; fi
  fi
else
  echo "  Caddy как служба не найден (возможно, он в Docker)."
  echo "  Добавь в его Caddyfile этот блок и перезагрузи Caddy:"
  echo "$BLOCK"
  echo "  Если Caddy в Docker, вместо 127.0.0.1 укажи host.docker.internal или IP хоста."
  printf "\n  Пароли:%b\n" "$PW"
  return 0
fi

say "Проверяю HTTPS (сертификат может выпускаться до минуты)"
OK=""
for i in $(seq 1 20); do curl -fs --max-time 5 "https://$HOST/api" >/dev/null && { OK=1; break; }; sleep 3; done
[ -n "$OK" ] && echo "  https://$HOST работает" || echo "  HTTPS пока не отвечает. Проверь, что порты 80 и 443 открыты, и повтори через минуту: curl https://$HOST/api"

printf "\n\033[1;32m✓ Готово\033[0m\n  Адрес сервера: https://%s\n  Пароли:%b\n\n  Сохрани пароли в Заметках. В «Ладе»: ⋯ → Синхронизация.\n" "$HOST" "$PW"
}
main "$@" </dev/null
