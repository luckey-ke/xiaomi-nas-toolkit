#!/bin/sh
# keepctl —— 自愈定时器的管理命令，不用手改 systemd 文件。
#
# 支持两种调度模式，随时可切换：
#   interval  每隔一段时间跑一次（如 2min）→ systemd OnUnitActiveSec
#   calendar  每天固定时间点跑             → systemd OnCalendar
#
# 配置保存在同目录的 keep.conf。本脚本负责把它应用到 systemd 定时器。
# 定时器单元和 keep.conf 都在持久层（/data 的 overlay upper），重启不丢。
#
# 用法：
#   keepctl status                 查看模式、间隔/时间点、启用状态、下次运行
#   keepctl set 2min               切到间隔模式（支持 30s / 2min / 1h）
#   keepctl at 08,12,15,18:00      切到日历模式（每天这些时间点）
#   keepctl at "Mon..Fri 09:00"    日历模式 + 限定工作日
#   keepctl off                    停用自愈（重启后保持停用）
#   keepctl on                     重新启用
#   keepctl apply                  手改 keep.conf 后应用
#   keepctl run                    立即手动跑一次巡检

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
