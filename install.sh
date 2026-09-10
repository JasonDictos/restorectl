#!/usr/bin/env bash
# restorectl installer — idempotent, safe to re-run.
#
#   sudo ./install.sh              install everything appropriate for this host
#   sudo ./install.sh --client     CLI + tray only (a machine being backed up)
#   sudo ./install.sh --server     CLI + web UI (the machine holding the repos)
#   sudo ./install.sh --uninstall  remove everything except your data
#
# Never touches a restic repository. Never enables the nightly timer without
# asking, so installing on a fresh box cannot surprise you with disk I/O.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MODE=auto
ENABLE_TIMER=ask

for a in "$@"; do
    case "$a" in
        --client) MODE=client ;;
        --server) MODE=server ;;
        --all) MODE=all ;;
        --uninstall) MODE=uninstall ;;
        --enable-timer) ENABLE_TIMER=yes ;;
        --no-enable-timer) ENABLE_TIMER=no ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "install.sh: run as root" >&2; exit 1; }

say() { echo "  $*"; }

if [ "$MODE" = uninstall ]; then
    say "removing units"
    systemctl disable --now system-backup.timer 2>/dev/null || true
    systemctl disable --now restorectl-web 2>/dev/null || true
    rm -f /etc/systemd/system/system-backup.{service,timer} \
          /etc/systemd/system/restorectl-web.service \
          /etc/nginx/conf.d/restorectl.conf
    rm -f /usr/local/bin/restorectl /usr/local/sbin/{system-backup,capture-layout,bare-metal-restore}
    rm -rf /usr/local/lib/restorectl
    systemctl daemon-reload
    say "left alone: /etc/restic/*, /var/lib/restorectl/, your repositories"
    exit 0
fi

# Auto-detect role: a machine that holds repos is a server.
if [ "$MODE" = auto ]; then
    MODE=client
    for d in /PlatterArray/Backups/restic-profile /mnt/backup/restic-profile \
             /SsdArray/Archive/restic-profile; do
        [ -d "$d" ] && { MODE=all; break; }
    done
    say "detected role: $MODE"
fi

echo "restorectl installer — mode=$MODE"

# ---- dependencies -------------------------------------------------------
need=()
command -v restic  >/dev/null || need+=(restic)
command -v python3 >/dev/null || need+=(python3)
if [ ${#need[@]} -gt 0 ]; then
    say "missing: ${need[*]}"
    if command -v dnf >/dev/null; then dnf install -y "${need[@]}"
    elif command -v apt-get >/dev/null; then apt-get install -y "${need[@]}"
    else echo "install these manually: ${need[*]}" >&2; exit 1; fi
fi

# ---- CLI (always) -------------------------------------------------------
install -m 0755 "$HERE/bin/restorectl" /usr/local/bin/restorectl
say "installed /usr/local/bin/restorectl"

# ---- client: backup engine + tray --------------------------------------
if [ "$MODE" = client ] || [ "$MODE" = all ]; then
    install -m 0755 "$HERE/bin/system-backup"       /usr/local/sbin/
    install -m 0755 "$HERE/bin/capture-layout"      /usr/local/sbin/
    install -m 0755 "$HERE/bin/bare-metal-restore"  /usr/local/sbin/
    install -d -m 0755 /usr/local/lib/restorectl
    install -m 0755 "$HERE/web/restorectl-tray.py"  /usr/local/lib/restorectl/
    install -d -m 0755 /etc/restic
    [ -f /etc/restic/excludes.txt ] || install -m 0644 "$HERE/etc/excludes.txt" /etc/restic/excludes.txt
    install -m 0644 "$HERE/systemd/system-backup.service" /etc/systemd/system/
    install -m 0644 "$HERE/systemd/system-backup.timer"   /etc/systemd/system/
    systemctl daemon-reload
    say "installed backup engine + systemd units"

    if [ ! -f /etc/restic/password ]; then
        say "NOTE: no /etc/restic/password yet — run: restorectl setup"
    fi

    case "$ENABLE_TIMER" in
        yes) systemctl enable --now system-backup.timer; say "nightly timer ENABLED" ;;
        no)  say "nightly timer left disabled (--enable-timer to turn on)" ;;
        ask) if systemctl is-enabled system-backup.timer >/dev/null 2>&1; then
                 say "nightly timer already enabled"
             else
                 say "nightly timer NOT enabled yet — turn it on with:"
                 say "    restorectl enable"
             fi ;;
    esac
fi

# ---- server: web UI -----------------------------------------------------
if [ "$MODE" = server ] || [ "$MODE" = all ]; then
    install -d -m 0755 /usr/local/lib/restorectl
    install -m 0755 "$HERE/web/restorectl-web.py" /usr/local/lib/restorectl/
    install -m 0644 "$HERE/web/index.html"        /usr/local/lib/restorectl/
    install -m 0644 "$HERE/systemd/restorectl-web.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now restorectl-web
    say "web API running on 127.0.0.1:8723"

    if [ -d /etc/nginx/conf.d ]; then
        install -m 0644 "$HERE/web/nginx-restorectl.conf" /etc/nginx/conf.d/restorectl.conf
        if [ ! -f /etc/nginx/restorectl.htpasswd ]; then
            if command -v htpasswd >/dev/null; then
                pw=$(openssl rand -base64 18)
                htpasswd -bc /etc/nginx/restorectl.htpasswd "${SUDO_USER:-admin}" "$pw" >/dev/null 2>&1
                chmod 640 /etc/nginx/restorectl.htpasswd
                say "web credentials: ${SUDO_USER:-admin} / $pw   (SAVE THIS)"
            else
                say "WARNING: htpasswd not found — install apache2-utils/httpd-tools"
                say "the UI will 500 until /etc/nginx/restorectl.htpasswd exists"
            fi
        fi
        if nginx -t >/dev/null 2>&1; then
            systemctl reload nginx; say "nginx reloaded — UI on port 8088"
        else
            say "ERROR: nginx config test failed; restorectl.conf left in place for inspection"
            nginx -t 2>&1 | sed 's/^/      /'
        fi
    fi
fi

echo
say "next: restorectl wtf"
