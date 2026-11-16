#!/bin/sh
# keepctl - manage the MINAS self-heal timer without editing systemd files.
#
# Two schedule modes are supported and can be switched at any time:
#   interval  run every N (e.g. 2min)  -> systemd OnUnitActiveSec
#   calendar  run at fixed clock times -> systemd OnCalendar
#
# Config lives in keep.conf (same directory). This script applies it to the
# systemd timer and reports status. Changes persist across reboots because the
# timer unit and keep.conf both live on the persistent overlay.
#
# Usage:
#   keepctl status                 show mode, interval/calendar, enabled, next run
#   keepctl set 2min               switch to interval mode (30s / 2min / 1h)
#   keepctl at 08,12,15,18:00      switch to calendar mode (daily at these times)
#   keepctl at "Mon..Fri 09:00"    calendar with weekday restriction
#   keepctl off                    disable the self-heal timer (persists)
#   keepctl on                     re-enable the self-heal timer
#   keepctl apply                  apply hand-edited keep.conf values
#   keepctl run                    run one heal pass right now

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONF="$DIR/keep.conf"
TIMER=/etc/systemd/system/keep-plugins.timer

die() { echo "keepctl: $*" >&2; exit 1; }

[ -f "$CONF" ] || die "config missing: $CONF"

conf_get() { sed -n "s/^$1=//p" "$CONF" | tail -1; }

conf_set() {
    if grep -q "^$1=" "$CONF"; then
        sed -i "s|^$1=.*|$1=$2|" "$CONF" || die "failed to update $CONF"
    else
        echo "$1=$2" >> "$CONF" || die "failed to append to $CONF"
    fi
}

valid_interval() { echo "$1" | grep -qE '^[0-9]+(s|min|h)$'; }

# Validate a calendar spec such as "08,12,15,18:00" or "Mon..Fri 09:00".
valid_calendar() {
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze calendar "*-*-* $1:00" >/dev/null 2>&1 && return 0
        systemd-analyze calendar "$1" >/dev/null 2>&1 && return 0
        return 1
    fi
    echo "$1" | grep -qE '^[A-Za-z0-9.,:*~/-]+( [0-9:]+)?$'
}

# Rewrite the timer unit for the requested mode.
write_timer() {
    mode=$1
    value=$2
    if [ "$mode" = "calendar" ]; then
        case "$value" in
            *' '*|*:*:*) spec="$value" ;;
            *) spec="*-*-* $value:00" ;;
        esac
        desc="Run MINAS plugin self-heal on calendar: $spec"
        body="OnCalendar=$spec
Persistent=true"
    else
        desc="Run MINAS plugin self-heal every $value"
        body="OnBootSec=90s
OnActiveSec=30s
OnUnitActiveSec=$value"
    fi
    cat > "$TIMER" <<EOF
[Unit]
Description=$desc

[Timer]
$body
Unit=keep-plugins.service

[Install]
WantedBy=timers.target
EOF
    [ -s "$TIMER" ] || die "failed to write $TIMER"
    systemctl daemon-reload || die "daemon-reload failed"
}

restart_timer() {
    systemctl enable keep-plugins.timer >/dev/null 2>&1
    systemctl restart keep-plugins.timer || die "timer restart failed"
}

cmd_status() {
    mode=$(conf_get MODE)
    [ -n "$mode" ] || mode=interval
    echo "config    : $CONF"
    echo "  MODE    = $mode"
    echo "  INTERVAL= $(conf_get INTERVAL)"
    echo "  CALENDAR= $(conf_get CALENDAR)"
    echo "  ENABLED = $(conf_get ENABLED)"
    echo "timer unit: $TIMER"
    echo "  enabled = $(systemctl is-enabled keep-plugins.timer 2>&1)"
    echo "  active  = $(systemctl is-active keep-plugins.timer 2>&1)"
    echo "  rule    :"
    sed -n '/^\[Timer\]/,/^\[Install\]/p' "$TIMER" | grep -E '^(On|Persistent)' | sed 's/^/    /'
    echo "schedule  :"
    systemctl list-timers keep-plugins.timer --no-pager 2>/dev/null | sed -n '2p' | sed 's/^/  /'
}

case "${1:-}" in
    status)
        cmd_status
        ;;
    set)
        [ -n "${2:-}" ] || die "usage: keepctl set <interval>  e.g. 2min"
        valid_interval "$2" || die "bad interval '$2' (use 30s / 2min / 1h)"
        write_timer interval "$2"
        conf_set MODE interval
        conf_set INTERVAL "$2"
        restart_timer
        echo "interval mode: every $2"
        ;;
    at)
        [ -n "${2:-}" ] || die "usage: keepctl at <calendar>  e.g. 08,12,15,18:00"
        valid_calendar "$2" || die "bad calendar '$2' (e.g. 08,12,15,18:00 or Mon..Fri 09:00)"
        write_timer calendar "$2"
        conf_set MODE calendar
        conf_set CALENDAR "$2"
        restart_timer
        echo "calendar mode: $2"
        ;;
    apply)
        mode=$(conf_get MODE)
        [ -n "$mode" ] || mode=interval
        if [ "$mode" = "calendar" ]; then
            value=$(conf_get CALENDAR)
            [ -n "$value" ] || die "CALENDAR missing in $CONF"
            valid_calendar "$value" || die "bad CALENDAR '$value'"
            write_timer calendar "$value"
        else
            value=$(conf_get INTERVAL)
            [ -n "$value" ] || die "INTERVAL missing in $CONF"
            valid_interval "$value" || die "bad INTERVAL '$value'"
            write_timer interval "$value"
        fi
        if [ "$(conf_get ENABLED)" = "no" ]; then
            systemctl disable --now keep-plugins.timer || die "disable failed"
            echo "applied: $mode=$value, timer disabled"
        else
            restart_timer
            echo "applied: $mode=$value, timer enabled"
        fi
        ;;
    off)
        systemctl disable --now keep-plugins.timer || die "disable failed"
        conf_set ENABLED no
        echo "self-heal timer DISABLED (persists across reboots)"
        echo "note: plugins keep running; only auto-repair is paused."
        echo "      re-enable anytime with: keepctl on"
        ;;
    on)
        systemctl enable keep-plugins.timer >/dev/null 2>&1 || die "enable failed"
        systemctl start keep-plugins.timer || die "start failed"
        conf_set ENABLED yes
        echo "self-heal timer enabled"
        ;;
    run)
        shift
        exec python3 "$DIR/keep-plugins.py" "$@"
        ;;
    *)
        sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
