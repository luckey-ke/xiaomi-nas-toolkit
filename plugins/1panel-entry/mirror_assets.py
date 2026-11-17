#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 1Panel 前端镜像到插件自己的 ui 目录，并改写成插件子路径。

为什么要离线镜像：
  应用市场客户端把插件页加载在 https://127.0.0.1:<port> 这个本机代理里，
  且只放行「插件入口这一个文档」——内嵌 iframe、页面跳转都会被拦（实测请求
  根本不到 nginx），但同源的 fetch / script / css 正常。因此不能把面板放在
  iframe 或别的路径，只能让插件入口页本身就是 1Panel 单页应用。

映射关系：
  插件入口      /plugin/<uid>/1panel/index.html   ← 1Panel 的 index.html（改写后）
  静态资源      /plugin/<uid>/1panel/assets/...
  API           /plugin/<uid>/1panel/api/v2/...   → 反代 127.0.0.1:18080

做三件事：
  1. 从 127.0.0.1:18080 的 /<安全入口> 递归抓取 index.html 及全部资源
     （js / css / 字体 / 图片，含 import() 懒加载的路由分片，缺一个就白屏）
  2. 改写：/assets/ → 前缀 + /assets/；/api/v2 → 前缀 + /api/v2；
     router base `history:xxx(`/`)` → 前缀
  3. 在 index.html 里注入引导脚本：把 location 归一到 router base，
     并在前端挂载失败时把错误显示出来（否则客户端只有一片黑，无从查起）

用法：python3 mirror_assets.py [--check]
"""
import os
import re
import sys
import ssl
import urllib.parse
import urllib.request

BASE = 'http://127.0.0.1:18080'
CTX = ssl._create_unverified_context()
OUT = '/data/minas/plugins/1panel/src/ui'          # 由 plugin.cgi 直出
PREFIX = '/plugin/981760641/1panel'                # 插件入口路径（不带 /panel）
ENTRY = '/index'                                   # 1Panel 安全入口

CHECK = '--check' in sys.argv

BOOTSTRAP = """<script>
/* 插件引导：把 location 归一到 1Panel 的 router base。
   客户端里文档 URL 形如 /D:/plugin/<uid>/1panel/index.html，
   不改写的话 vue-router 的 base 对不上，页面不会渲染。 */
(function () {
  var BASE = '%(prefix)s/';
  try {
    var p = location.pathname, i = p.indexOf('/plugin/');
    var t = i >= 0 ? p.slice(i) : p;
    if (t.slice(-11) === 'index.html' || t.indexOf(BASE) !== 0) { t = BASE; }
    if (t !== location.pathname) {
      history.replaceState(null, '', t + location.search + location.hash);
    }
  } catch (e) {}
  var errs = [];
  function box(title, detail) {
    try {
      var d = document.createElement('div');
      d.setAttribute('style', 'position:fixed;z-index:2147483647;left:0;top:0;right:0;bottom:0;'
        + 'background:#12161c;color:#d7dee8;font:13px/1.7 -apple-system,"PingFang SC",sans-serif;'
        + 'padding:56px 40px;white-space:pre-wrap;word-break:break-all;overflow:auto');
      d.textContent = title + '\\n\\n' + detail;
      (document.body || document.documentElement).appendChild(d);
    } catch (e) {}
  }
  window.addEventListener('error', function (e) {
    if (e && e.target && e.target.tagName) {
      errs.push('资源加载失败: ' + (e.target.src || e.target.href || e.target.tagName));
      return;
    }
    errs.push((e.message || 'error') + ' @ ' + (e.filename || '') + ':' + (e.lineno || 0));
  }, true);
  window.addEventListener('unhandledrejection', function (e) {
    var r = e.reason; errs.push('promise: ' + ((r && r.message) || r));
  });
  setTimeout(function () {
    var app = document.getElementById('app');
    var loading = document.querySelector('.first-loading-wrap');
    if (!app || loading || app.children.length === 0) {
      box('1Panel 前端没有挂载成功',
        'URL    = ' + location.href + '\\nBASE   = ' + BASE
        + '\\nready  = ' + document.readyState + '\\n错误   = ' + (errs.join('\\n         ') || '无'));
    }
  }, 10000);
  /* 诊断信标：把 cookie 情况回报到 nginx 访问日志，便于远程确认
     客户端的 webview 到底存不存 Set-Cookie。 */
  setTimeout(function () {
    try {
      var ck = (document && document.cookie) || '';
      var q = 'hw=1&k=' + (ck.indexOf('panel_public_key=') >= 0 ? 1 : 0)
        + '&len=' + ck.length + '&c=' + encodeURIComponent(ck.slice(0, 160));
      (new Image()).src = BASE + 'diag?' + q + '&t=' + Date.now();
    } catch (e) {}
  }, 8000);
})();
</script>
""" % {'prefix': PREFIX}

# 可能出现在字符串字面量里的资源引用
ASSET_RE = re.compile(
    r'''["'`(]([^"'`()\s]{1,200}?\.(?:js|css|woff2|woff|ttf|eot|svg|png|jpe?g|gif|ico|webp|json|wasm))["'`)]'''
)
CSS_URL_RE = re.compile(r'''url\(\s*["']?([^"')]+)["']?\s*\)''')
HTML_REF_RE = re.compile(r'''(?:src|href)="([^"]+)"''')

seen = set()
stats = {'html': 0, 'js': 0, 'css': 0, 'bin': 0, 'rewritten': 0, 'bytes': 0, 'refs': 0, 'patched': 0}
problems = []
missing = []
PUBKEY = ''


def fetch_public_key():
    """取面板登录用的 RSA 公钥。

    1Panel v2.5 起，登录密码不再是明文：前端从 cookie panel_public_key 里取
    PEM（base64），用 RSA 加密一个随机 AES 密钥，再用该密钥 AES-256-CBC 加密
    密码，最终 password 字段 = `rsa:iv:密文`。后端对明文直接判 ErrAuth。
    这里把公钥抓下来写进前端 JS，客户端不存 cookie 也能完成加密。
    """
    try:
        req = urllib.request.Request(BASE + '/api/v2/core/auth/captcha',
                                     headers={'Accept-Encoding': 'identity'})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            for k, v in r.headers.items():
                if k.lower() == 'set-cookie' and 'panel_public_key=' in v:
                    m = re.search(r'panel_public_key=([^;]+)', v)
                    if m:
                        return m.group(1)
    except Exception as e:  # noqa: BLE001
        problems.append('取 panel_public_key 失败: %s' % e)
    return ''


def patch_login_chunks(text):
    """把两处登录相关逻辑改成不依赖 cookie 的版本。"""
    n = 0
    if PUBKEY:
        old = 'let t=Jh(`panel_public_key`);if(!t)return e;'
        new = 'let t=Jh(`panel_public_key`)||`%s`;' % PUBKEY
        if old in text:
            text = text.replace(old, new)
            n += 1
    old2 = 'e.headers.EntranceCode=Pd(t.entrance)'
    new2 = 'e.headers.EntranceCode=Pd(t.entrance||`%s`)' % ENTRY.strip('/')
    if old2 in text:
        text = text.replace(old2, new2)
        n += 1
    return text, n


def fetch(path):
    req = urllib.request.Request(BASE + path, headers={'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return r.read(), r.headers.get('Content-Type', '')


def save(path, data):
    if CHECK:
        stats['bytes'] += len(data)
        return
    dst = OUT + path
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(dst, 'wb') as f:
        f.write(data)
    stats['bytes'] += len(data)


def rewrite(text):
    before = text
    text = text.replace('/assets/', PREFIX + '/assets/')
    text = re.sub(
        r'history:([A-Za-z_$][\w$]*)\(`/`\)',
        lambda m: 'history:%s(`%s/`)' % (m.group(1), PREFIX),
        text,
    )
    text = text.replace('`/api/v2', '`' + PREFIX + '/api/v2')
    text = text.replace('"/api/v2', '"' + PREFIX + '/api/v2')
    text = text.replace("'/api/v2", "'" + PREFIX + '/api/v2')
    if text != before:
        stats['rewritten'] += 1
    return text


def enqueue(raw, path, kind):
    if kind == 'html':
        refs = HTML_REF_RE.findall(raw)
    elif kind == 'css':
        refs = CSS_URL_RE.findall(raw)
    else:
        refs = [m.group(1) for m in ASSET_RE.finditer(raw)]
    for r in refs:
        r = r.strip()
        if not r or r.startswith(('data:', 'http:', 'https:', '//', '#')):
            continue
        r = r.split('#')[0].split('?')[0]
        if not r:
            continue
        dep = urllib.parse.urljoin(path, r)
        if not dep.startswith('/') or dep.startswith('/plugin/'):
            continue
        # 有些引用写成 "assets/js/xxx.js"（无前导斜杠），相对解析会拼出
        # /assets/js/assets/js/xxx.js 这种重复路径，直接跳过。
        if dep.count('/assets/') > 1:
            continue
        stats['refs'] += 1
        crawl(dep)


def crawl(path):
    if path in seen:
        return
    seen.add(path)
    try:
        data, _ = fetch(path)
    except Exception as e:  # noqa: BLE001
        problems.append('%s: %s' % (path, e))
        missing.append(path)
        return

    low = path.lower()
    if low.endswith(('.js', '.css', '.html')) or low.endswith('/'):
        raw = data.decode('utf-8', 'replace')
        kind = 'css' if low.endswith('.css') else ('html' if not low.endswith('.js') else 'js')
        stats[kind] += 1
        enqueue(raw, path, kind)
        out = rewrite(raw)
        if low.endswith('.js'):
            out, n = patch_login_chunks(out)
            stats['patched'] += n
        save(path, out.encode('utf-8'))
    else:
        stats['bin'] += 1
        save(path, data)


def main():
    global PUBKEY
    PUBKEY = fetch_public_key()
    print('面板公钥：%s' % ('已获取（%d 字节）' % len(PUBKEY) if PUBKEY else '获取失败'))
    try:
        data, _ = fetch(ENTRY)
    except Exception as e:  # noqa: BLE001
        print('错误：抓不到入口 %s（%s）' % (ENTRY, e))
        return
    raw = data.decode('utf-8', 'replace')
    if 'Access Temporarily Unavailable' in raw:
        print('错误：被 1Panel 安全入口拦截，请先 systemctl restart 1panel-core 再试')
        return

    stats['html'] += 1
    enqueue(raw, '/index.html', 'html')
    seen.add(ENTRY)

    html = rewrite(raw)
    # 注入引导脚本（放在 <head> 之后，早于 app 的 module 脚本执行）
    idx = html.find('<head>')
    html = html[:idx + 6] + '\n' + BOOTSTRAP + html[idx + 6:] if idx >= 0 else BOOTSTRAP + html
    save('/index.html', html.encode('utf-8'))

    print('入口页 %d 字节 → 注入后 %d 字节' % (len(raw), len(html)))
    print('抓取完成：html=%(html)d js=%(js)d css=%(css)d 其它=%(bin)d' % stats)
    print('资源引用=%(refs)d 改写文件数=%(rewritten)d 总字节=%(bytes)d' % stats)
    print('登录相关注入 %(patched)d 处' % stats)
    if problems:
        print('抓取异常 %d 条（前 10 条）：' % len(problems))
        for p in problems[:10]:
            print('  -', p)
    else:
        print('无抓取异常')
    print('落盘文件总数=%d' % (len(seen) - len(missing)))


if __name__ == '__main__':
    main()
