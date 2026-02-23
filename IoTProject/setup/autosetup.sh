#!/usr/bin/env bash

set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$PROJECT_DIR/autosetup.log"
PHASE="${PHASE:-1}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Phase 1 ──────────────────────────────────────────────────────────────────

phase1() {
    if [[ $EUID -ne 0 ]]; then
        echo "Run as root: sudo bash setup/autosetup.sh" >&2
        exit 1
    fi

    REAL_USER="${SUDO_USER:-}"
    if [[ -z "$REAL_USER" ]]; then
        echo "Run with sudo, not directly as root (e.g. sudo bash setup/autosetup.sh)" >&2
        exit 1
    fi

    USER_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"

    log "=== Mesh Gateway Setup — Phase 1 ==="
    log "User: $REAL_USER | Home: $USER_HOME | Project: $PROJECT_DIR"

    # Step 1: Python venv and dependencies
    log "Creating Python virtual environment..."
    sudo -u "$REAL_USER" python3 -m venv "$PROJECT_DIR/venv"

    log "Installing Python dependencies..."
    sudo -u "$REAL_USER" "$PROJECT_DIR/venv/bin/pip" install \
        -r "$PROJECT_DIR/requirements.txt"

    # Step 2: Patch gateway.service with the real username and project path,
    #         then install and enable it
    log "Installing gateway.service..."
    local tmp_svc
    tmp_svc=$(mktemp)
    sed \
        -e "s|__USER__|$REAL_USER|g" \
        -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        "$PROJECT_DIR/setup/gateway.service" > "$tmp_svc"
    cp "$tmp_svc" /etc/systemd/system/gateway.service
    rm "$tmp_svc"
    systemctl daemon-reload
    systemctl enable gateway
    log "gateway.service enabled."

    # Step 3: Pre-generate WiFi credentials here, while SSH is still alive.
    #         setup_ap.sh will use this passphrase via the PASSPHRASE env var.
    #         This avoids the chicken-and-egg problem where the password is
    #         printed to a log you can't access until you're already connected.
    local ssid passphrase
    ssid="MeshGateway-$(hostname)"
    passphrase="password"

    echo ""
    echo "================================================"
    echo "  Save these — printed once, not stored on disk"
    echo "  SSID     : $ssid"
    echo "  Password : $passphrase"
    echo "  URL      : http://192.168.4.1:5000"
    echo "================================================"
    echo ""

    # Step 4: Stop any leftover setup unit from a previous run
    systemctl stop mesh-gateway-setup 2>/dev/null || true

    # Step 5: Launch Phase 2 as a systemd transient unit.
    #         It runs independently of this SSH session — when wlan0 becomes
    #         an AP and SSH drops, Phase 2 keeps running unaffected.
    log "Launching Phase 2 as a systemd unit (survives SSH disconnect)..."
    log "To watch progress after reconnecting via WiFi:"
    log "  journalctl -u mesh-gateway-setup -f"
    log "  or: tail -f $LOG_FILE"

    systemd-run \
        --unit=mesh-gateway-setup \
        --description="Mesh Gateway Phase 2 Setup" \
        --setenv=PHASE=2 \
        --setenv=PROJECT_DIR="$PROJECT_DIR" \
        --setenv=REAL_USER="$REAL_USER" \
        --setenv=PASSPHRASE="$passphrase" \
        bash "$SCRIPT_PATH"

    log "Phase 2 is running. SSH will disconnect when the AP comes up — this is expected."
    log "Connect to '$ssid' using the password above, then open http://192.168.4.1:5000"
}

# ── Phase 2 ──────────────────────────────────────────────────────────────────

phase2() {
    if [[ -z "${PASSPHRASE:-}" ]]; then
        echo "ERROR: PASSPHRASE not set. Do not invoke Phase 2 directly." >&2
        exit 1
    fi

    log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

    log "=== Mesh Gateway Setup — Phase 2 ==="

    log "Configuring WiFi access point..."
    PASSPHRASE="$PASSPHRASE" bash "$PROJECT_DIR/setup/setup_ap.sh" 2>&1 \
        | tee -a "$LOG_FILE"

    log "Starting gateway service..."
    systemctl start gateway

    log "=== Verification ==="
    log "hostapd : $(systemctl is-active hostapd 2>/dev/null || echo unknown)"
    log "dnsmasq : $(systemctl is-active dnsmasq 2>/dev/null || echo unknown)"
    log "gateway : $(systemctl is-active gateway 2>/dev/null || echo unknown)"
    local wlan_ip
    wlan_ip=$(ip addr show wlan0 2>/dev/null | awk '/inet / {print $2}') || wlan_ip="not found"
    log "wlan0   : $wlan_ip"
    log "=== Setup complete ==="
}

# ── Entry point ──────────────────────────────────────────────────────────────

case "$PHASE" in
    1) phase1 ;;
    2) phase2 ;;
    *) echo "Unknown PHASE=$PHASE" >&2; exit 1 ;;
esac
