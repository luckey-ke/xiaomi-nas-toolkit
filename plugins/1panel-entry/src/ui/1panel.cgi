#!/bin/sh
# 1panel.cgi — plugin.cgi 入口（协议对齐 dockerctl.cgi / sysmon.cgi）
#
# 为什么必须有这个文件：
#   plugin.cgi 的路由模板是 /data/plugin/www/<uid>/<插件名>/<插件名>.cgi，
#   缺少同名 .cgi 时任何请求都返回 404（页面表现为全空白）。
#
# 职责：
#   health   → 探测 127.0.0.1:18080 面板是否就绪，返回 JSON
#   其余请求 → 直接吐出插件目录内的静态文件（index.html / icon.png 等）
# 面板本体始终在 18080，由入口页 iframe 直连，本 CGI 不做反向代理。

# 不允许直接请求 cgi 自身
[ "$FILE_URI" = "1panel.cgi" ] && {
    printf "Status: 404 Not Found\r\nContent-type: text/html\r\n\r\n"
    echo "<html><body><h1>404 Not Found</h1></body></html>"
    exit 0
}

# 1. 面板就绪探测（供入口页轮询）
case "$FILE_URI" in
    health*)
        if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:18080/"; then
            up=true
        else
            up=false
        fi
        printf "Content-type: application/json\r\n"
        printf "Cache-Control: no-store\r\n"
        printf "\r\n"
        printf '{"up":%s,"port":18080}' "$up"
        exit 0
        ;;
esac

# 2. 静态文件
if [ -f "$FILE_REQ" ]; then
    ext="${FILE_REQ##*.}"
    case "$ext" in
        jpg|jpeg) mime="image/jpeg" ;;
        png)      mime="image/png" ;;
        ico)      mime="image/x-icon" ;;
        svg)      mime="image/svg+xml" ;;
        css)      mime="text/css" ;;
        js)       mime="application/javascript" ;;
        html|htm) mime="text/html; charset=utf-8" ;;
        json)     mime="application/json" ;;
        txt)      mime="text/plain; charset=utf-8" ;;
        *)        mime="$(file -b --mime-type "$FILE_REQ" 2>/dev/null || echo application/octet-stream)" ;;
    esac
    printf "Content-type: %s\r\n" "$mime"
    printf "Cache-Control: no-cache\r\n"
    printf "\r\n"
    cat "$FILE_REQ"
    exit 0
fi

# 3. 目录根（/plugin/<uid>/1panel/）默认给 index.html
if [ -f "${FILE_REQ%/}/index.html" ]; then
    printf "Content-type: text/html; charset=utf-8\r\n"
    printf "\r\n"
    cat "${FILE_REQ%/}/index.html"
    exit 0
fi

# 4. 404
printf "Status: 404 Not Found\r\nContent-type: text/html\r\n\r\n"
echo "<html><body><h1>404 Not Found</h1></body></html>"
