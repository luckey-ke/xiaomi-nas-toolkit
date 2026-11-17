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

BOOTSTRAP = r"""<script>
/* ───────────────────────── 1Panel 插件引导 ─────────────────────────
   由 mirror_assets.py 注入到 1Panel 的 index.html，面板升级后重跑该脚本即可保留。
   1) 把 location 归一到 1Panel 的 router base（客户端 URL 带 /D:/ 盘符前缀）；
   2) 修正客户端给错的视口尺寸（fit）；
   3) 侧边栏兜底样式（sidebarGuard）；
   4) 清掉 element-plus 残留在 body 上的错误宽度锁（bodyFix）；
   5) 原生桥 hs_AppCallWeb 的透传记录（App 用它通知页面，如"返回"）；
   6) 出错时在页面上给出可截图的诊断；
   7) 把运行环境 / 越界元素回报到 nginx 访问日志（/diag?v=13）。 */
(function () {
  var BASE = '%(prefix)s/';
  var errs = [];
  var OP = location.pathname + location.search + location.hash;   /* 归一前的原始 URL */
  var OPNAV = (function () {
    try { var n = performance.getEntriesByType('navigation')[0]; return n ? n.type : '-'; } catch (e) { return '-'; }
  })();

  /* 上报：普通情况用 Image（GET，进 access.log）；卸载前用 sendBeacon 保证发出 */
  function beaconUrl(tag, obj) {
    var q = Object.keys(obj).map(function (k) {
      return k + '=' + encodeURIComponent(obj[k] == null ? '-' : obj[k]);
    }).join('&');
    return BASE + 'diag?v=13&tag=' + tag + '&' + q + '&t=' + Date.now();
  }
  function beacon(tag, obj) { try { (new Image()).src = beaconUrl(tag, obj); } catch (e) {} }
  function beaconBye(tag, obj) {
    try {
      var u = beaconUrl(tag, obj);
      if (navigator.sendBeacon && navigator.sendBeacon(u)) { return; }
      (new Image()).src = u;
    } catch (e) {}
  }

  /* 1) URL 归一
     客户端给的是 /D:/plugin/981760641/1panel/…（盘符前缀），micro-app 有时还会
     把绝对地址再拼一次（前缀重复）。这里统一裁到 base 开头，并去掉 index.html。
     注意：这一步只是"尽量改对"，micro-app 的虚拟路由可能拦截 history，改不动；
     所以后面还有 routeFix() 用应用自己的 router 做兜底。 */
  function normPath() {
    var p = location.pathname, i = p.indexOf('/plugin/');
    var t = i >= 0 ? p.slice(i) : BASE;
    var rest = t.slice(BASE.length).replace(/\/plugin\/981760641\/1panel\/?/g, '');
    t = BASE + rest.replace(/^\/+/, '');
    if (t.slice(-10).toLowerCase() === 'index.html') { t = t.slice(0, -10) || BASE; }
    if (t.indexOf(BASE) !== 0) { t = BASE; }
    return t;
  }
  var NORM = { from: OP, to: '', ok: 0 };
  try {
    var want = normPath();
    NORM.to = want;
    if (want !== location.pathname) {
      history.replaceState(null, '', want + location.search + location.hash);
    }
    NORM.ok = location.pathname === want ? 1 : 0;
  } catch (e) { errs.push('norm:' + e.message); }
  beacon('init', { op: OP, nav: OPNAV, ok: NORM.ok, to: NORM.to, ref: document.referrer || '-' });

  /* 记录 history 调用：点按钮后若发生 URL 变化，能直接看到是谁改的 */
  (function histTrace() {
    ['pushState', 'replaceState'].forEach(function (m) {
      try {
        var orig = history[m];
        history[m] = function (s, t, u) {
          if (errs.length < 40) { errs.push('HIST:' + m + ':' + String(u || '-').slice(0, 60)); }
          return orig.apply(this, arguments);
        };
      } catch (e) {}
    });
  })();

  /* 2) 视口修正
     应用市场客户端（Electron）会把 webview 的 zoomFactor 设成 0.8 —— 于是
     innerWidth = 窗口设备宽 / 0.8；dpr 也报 0.8。窗口从大变小后，webview 的
     视口不一定跟着变，页面就按更大的尺寸渲染，右侧被窗口切掉。
     判据：outerWidth/outerHeight 在客户端报的是**设备像素**，除以 dpr 才是可见 CSS 尺寸。
     做法：视口确实比可见区大 → 钉住布局尺寸 + 等比缩放。
     正常浏览器 outerWidth ≥ innerWidth、dpr ≥ 1，这段永不触发。 */
  var FIT = window.__fit = { iw: 0, ih: 0, ow: 0, oh: 0, realW: 0, realH: 0, dpr: 1, z: 1, fixed: 0 };
  function fit() {
    var de = document.documentElement;
    /* 手机端直接跳过：手机 WebView 里 innerWidth=980(layout viewport)、
       outerWidth=440(可视宽)、dpr=3 是正常特征，不是视口错误。
       对它做 zoom 补偿会把完整页面压成左上角一小块 + 大片黑屏。
       手机本身由 WebView 按 980 布局视口整页缩放显示，UI 本来就正常。 */
    if (/iPhone|iPad|Android|Mobile/i.test(navigator.userAgent || '')) {
      de.style.zoom = ''; de.style.width = ''; de.style.height = ''; de.style.overflow = '';
      FIT.z = 1; FIT.fixed = 0;
      return;
    }
    var num = function (v) { return (typeof v === 'number' && isFinite(v) && v > 100) ? Math.round(v) : 0; };
    de.style.zoom = '';                    // 先复位，免得量到上次的形状
    de.style.width = '';
    de.style.height = '';
    de.style.overflow = '';

    var dpr = window.devicePixelRatio || 1;
    var iw = num(window.innerWidth) || 1;
    var ih = num(window.innerHeight) || 1;
    var ow = num(window.outerWidth);
    var oh = num(window.outerHeight);
    var realW = ow ? (dpr < 0.95 ? Math.round(ow / dpr) : ow) : 0;
    var realH = oh ? (dpr < 0.95 ? Math.round(oh / dpr) : oh) : 0;

    FIT.iw = iw; FIT.ih = ih; FIT.ow = ow; FIT.oh = oh;
    FIT.realW = realW; FIT.realH = realH; FIT.dpr = Math.round(dpr * 100) / 100;
    FIT.z = 1; FIT.fixed = 0;

    var z = 1;
    if (realW && iw - realW > 16) { z = Math.min(z, realW / iw); }
    if (realH && ih - realH > 16) { z = Math.min(z, realH / ih); }
    if (z >= 0.97) { return; }             // 视口正常，什么都不做

    z = Math.max(0.35, z);
    de.style.zoom = z;
    de.style.width = iw + 'px';
    de.style.height = ih + 'px';
    de.style.overflow = 'hidden';
    FIT.z = Math.round(z * 1000) / 1000;
    FIT.fixed = 1;
  }
  fit();
  var rt = 0;
  window.addEventListener('resize', function () {
    clearTimeout(rt);
    rt = setTimeout(function () { fit(); probe('resize'); }, 300);
  });

  /* 3) 侧边栏兜底：规避 el-menu 折叠/展开动画在客户端里卡住导致整块空白。
     写的都是"本来就该是"的值，正常情况无副作用。 */
  (function sidebarGuard() {
    var st = document.createElement('style');
    st.textContent = '.app-sidebar,.app-sidebar .sidebar-container,.app-sidebar .el-scrollbar__view,'
      + '.app-sidebar .el-menu,.app-sidebar .el-menu-item,.app-sidebar .el-sub-menu__title,'
      + '.app-sidebar .logo,.app-sidebar .user-actions,.app-sidebar .node-selector'
      + '{opacity:1!important;visibility:visible!important}'
      /* 折叠按钮在 el-affix 里，而 .app-sidebar 是 z-index:2 的 fixed 层。
         客户端里 logo 的 SVG 盖在按钮上，只露半个圆，点击落到 logo
         （logo 的 onClick = router.push('home',{t}) → 看起来像整页刷新）。
         把 affix 提到侧边栏之上，按钮即可完整显示并命中。 */
      + '.app-wrapper > .el-affix,.app-wrapper .el-affix--fixed{z-index:2100!important}'
      + '.app-wrapper .el-affix--fixed button,.app-wrapper button.el-tooltip__trigger'
      + '{z-index:2101!important;pointer-events:auto!important}';
    (document.head || document.documentElement).appendChild(st);
  })();

  /* 3.1) 折叠按钮点击兜底
     上面提高层级后按钮一般能直接点到；但客户端里 logo 的 svg 仍可能抢走事件，
     于是在捕获阶段判断：点击落在 logo 内、且横坐标处于按钮所在区域（侧边栏
     logo 右缘 ~24px）时，直接触发 1Panel 自己的 setCollapse（通过点击真按钮），
     并阻止事件继续传到 logo，避免误跳 home。 */
  (function foldGuard() {
    var armed = false;
    document.addEventListener('click', function (e) {
      if (!armed) { return; }
      var logo = document.querySelector('.app-sidebar .logo');
      if (!logo) { return; }
      var lr = logo.getBoundingClientRect();
      /* 不依赖 e.target.closest（SVG path 上没有），直接用坐标判断是否点在 logo 上 */
      if (e.clientX < lr.left || e.clientX > lr.right
          || e.clientY < lr.top || e.clientY > lr.bottom) { return; }
      /* 按钮中心正好在 logo 右缘（按钮范围 lr.right ± 12），
         实测客户端点击 x=178 时 logo.right=180、x=248 时 logo.right=251 */
      if (e.clientX < lr.right - 18 || e.clientX > lr.right + 8) { return; }
      var btn = document.querySelector('.app-wrapper .el-affix--fixed button') || document.querySelector('.app-wrapper button.el-tooltip__trigger');
      if (!btn) { return; }
      try {
        e.preventDefault(); e.stopPropagation();
        btn.click();
        errs.push('FOLDGUARD');
        beacon('fg', { x: Math.round(e.clientX), wrap: (document.querySelector('.app-wrapper') || {}).className || '-' });
      } catch (x) {}
    }, true);   /* 捕获阶段，抢在 logo 的 onClick 之前 */
    setTimeout(function () { armed = true; }, 1200);
  })();

  /* 3.5) micro-app 容器修正
     PC 客户端是用微前端容器（micro-app）把插件页面嵌进去的：实测页面里存在
     <micro-app-body>，宽 1088、left -26，而窗口视口是 1440。
     body 只有 1088，1Panel 的布局按 1088 排（正确），但它的侧边栏/抽屉/弹层都是
     position:fixed —— fixed 的参照是**视口**（1440），两套坐标系差 352px，
     于是侧边栏画到了容器外面，压在客户端自己的界面上，看起来就是"树形菜单跑到
     1Panel 窗口外侧"。
     给容器加一个空 transform 会让它变成 fixed 后代的 containing block，
     fixed 就回到容器坐标系里，宽度/位置都和内容对齐。
     注：transform 不参与布局，加它不会改变任何盒子尺寸。 */
  function microGuard() {
    var mb = document.querySelector('micro-app-body');
    var env = !!window.__MICRO_APP_ENVIRONMENT__ || !!mb;
    if (!env) { return; }
    var list = [];
    if (mb) { list.push(mb); }
    if (document.body && document.body !== mb) { list.push(document.body); }
    for (var k = 0; k < list.length; k++) {
      var el = list[k];
      if (!el || el.__p1tz) { continue; }
      el.__p1tz = 1;
      el.style.transform = 'translateZ(0)';
      el.style.transformOrigin = '0 0';
    }
  }

  /* 3.6) body 宽度锁修复
     element-plus 的 useLockscreen 在弹窗/抽屉打开时会写
       document.body.style.width = calc(100%% - Npx)      // N = 滚动条宽
     客户端里有一次把 N 算成了 352（页面曾被缩放，innerWidth 与
     documentElement.clientWidth 不同单位），关闭后没恢复，body 就永远只有
     视口宽 - 352。1Panel 用 document.body.clientWidth < 1200 判断折叠侧边栏，
     于是侧边栏常年收成图标条、内容区只占窗口 75%%，右侧留一条空白 ——
     看上去就是"菜单跑到 1Panel 窗口外面去了"。
     这里只清理明显不合理的值（> 40px）：正常滚动条（≤ 20px）原样保留。 */
  var bodyFixed = 0;
  function bodyFix() {
    var b = document.body;
    if (!b) { return; }
    var de = document.documentElement;
    var w = (b.style.width || '').trim();
    var m = /^calc\(100%%\s*-\s*([\d.]+)px\)$/i.exec(w);
    var bad = m ? parseFloat(m[1]) > 40 : false;
    /* micro-app 容器本身就比视口窄（实测 1088 vs 1440），差值不是错误，不能清 */
    var isMicro = !!document.querySelector('micro-app-body') || !!window.__MICRO_APP_ENVIRONMENT__;
    if (!bad && !isMicro && de.clientWidth && b.clientWidth && de.clientWidth - b.clientWidth > 40) { bad = true; }
    if (!bad) { return; }
    try {
      b.style.width = '';
      if (b.style.paddingRight) { b.style.paddingRight = ''; }
      bodyFixed++;
      errs.push('BODYFIX:' + (m ? Math.round(parseFloat(m[1])) : ('diff' + (de.clientWidth - b.clientWidth))));
      window.dispatchEvent(new Event('resize'));   // 让 1Panel 重新按真实宽度算折叠状态
    } catch (e) {}
  }
  (function initBodyFix() {
    var start = function () {
      microGuard();
      [60, 300, 1200, 3000].forEach(function (t) { setTimeout(microGuard, t); });
      bodyFix();
      [120, 400, 1000, 2500, 5000, 9000].forEach(function (t) { setTimeout(bodyFix, t); });
      try {
        var bmo = new MutationObserver(function () { if (bodyFixed < 20) { bodyFix(); } });
        bmo.observe(document.body, { attributes: true, attributeFilter: ['style'] });
      } catch (e) {}
    };
    /* head 里的脚本执行时 body 还不存在，等 DOM 解析完再挂观察器 */
    if (document.body) { start(); } else { document.addEventListener('DOMContentLoaded', start, { once: true }); }
  })();

  /* 4.5) 路由自愈
     实测（1Panel v2.5）：首页路由是 `/`（name=home），**没有 /home 这个路径**。
     访问路由表里不存在的路径时，1Panel 会跳到 /error/404，也就是界面上那句
     "抱歉，您访问的页面不存在"。客户端打开插件时给的 URL 带 /D:/ 盘符前缀，
     micro-app 的虚拟路由可能让前面的 replaceState 失效 → 路由解析不到 → 404。
     这里在应用启动后拿它自己的 router 把脏路径 replace 回 `/`：
       · 有 .not-container（404 视图）或当前路由是 error/404；
       · URL 还带着 /D:/ 前缀；
       · 路径以 /home 结尾（旧版本才会用，现在是非法路由）。
     用 router.replace 而不是 location，避免整页重载。 */
  function getRouter() {
    try {
      var el = document.getElementById('app');
      var vm = el && el.__vue_app__;
      var gp = vm && vm.config.globalProperties;
      return (gp && gp.$router) || null;
    } catch (e) { return null; }
  }
  var routeFixed = 0;
  function routeFix() {
    if (routeFixed) { return; }
    var r = getRouter();
    if (!r) { return; }
    var cur = r.currentRoute && r.currentRoute.value;
    var p = location.pathname;
    var bad = !!document.querySelector('.not-container')
      || !!(cur && /(^|\/)error\/404/.test(cur.path || ''))
      || /\/home$/i.test(p)
      || p.indexOf('/D:') >= 0
      || p.indexOf('/plugin/') !== 0;
    if (!bad) { return; }
    try {
      r.replace('/');
      routeFixed = 1;
      errs.push('ROUTEFIX:' + ((cur && cur.fullPath) || '-') + '>' + p);
      beacon('rf', { cur: (cur && cur.fullPath) || '-', p: p });
    } catch (e) {}
  }
  window.__routeFix = routeFix;
  [250, 700, 1400, 2500, 4000, 6500, 9000].forEach(function (t) { setTimeout(routeFix, t); });

  /* 4.6) 点击 / 卸载轨迹（只在左上角区域记录，避免刷日志） */
  (function trace() {
    function cls(el) {
      var c = (el.className && el.className.toString) ? el.className.toString() : '';
      return el.tagName.toLowerCase() + (c ? '.' + c.split(' ')[0] : '');
    }
    function wrapCls() {
      var w = document.querySelector('.app-wrapper');
      return w ? w.className : '-';
    }
    document.addEventListener('click', function (e) {
      try {
        var t = e.target;
        if (!t || !t.getBoundingClientRect) { return; }
        var r = t.getBoundingClientRect();
        if (r.left > 340 || r.top > 180) { return; }
        var chain = [], n = t;
        for (var i = 0; i < 4 && n; i++) { chain.push(cls(n)); n = n.parentElement; }
        var info = {
          x: Math.round(e.clientX), y: Math.round(e.clientY),
          el: chain.join('<'), dp: e.defaultPrevented ? 1 : 0, wrap: wrapCls(), p: location.pathname
        };
        errs.push('CLK:' + info.el);
        beacon('clk', info);
        setTimeout(function () {
          var s = document.querySelector('.app-sidebar');
          beacon('clk2', {
            wrap: wrapCls(),
            side: s ? Math.round(s.getBoundingClientRect().width) : -1,
            p: location.pathname
          });
        }, 700);
      } catch (x) {}
    }, true);
    window.addEventListener('pagehide', function () {
      beaconBye('bye', {
        p: location.pathname + location.search,
        last: (errs.slice(-3).join(' | ') || '-').slice(0, 170),
        wrap: wrapCls()
      });
    });
  })();

  /* 5) 原生桥透传
     手机 App（WKWebView）会往插件页面注入脚本，通过 window.hs_AppCallWeb 通知页面
     事件（已实测到 App 调用 hs_AppCallWeb('changeTheme','light',1)）。如果这个函数
     不存在，注入脚本会抛 ReferenceError，后续逻辑（包括返回按钮）全部中断 ——
     表现就是"只能进、退不出去"。
     这里放一个透明代理：记录调用参数并立即上报（不等探针周期，点返回就能抓到），
     再原样转交给 App 真实注册的实现（如果有）。 */
  (function bridgeGuard() {
    var real = (typeof window.hs_AppCallWeb === 'function') ? window.hs_AppCallWeb : null;
    var conv = function (a) {
      try { return typeof a === 'string' ? a : JSON.stringify(a); } catch (e) { return String(a); }
    };
    var proxy = function () {
      var out = [];
      for (var k = 0; k < arguments.length; k++) { out.push(conv(arguments[k])); }
      var s = out.join('|').slice(0, 90);
      errs.push('BR:' + s);
      try { (new Image()).src = BASE + 'diag?v=13&tag=br&arg=' + encodeURIComponent(s) + '&t=' + Date.now(); } catch (e) {}
      if (real) { try { return real.apply(window, arguments); } catch (e) {} }
    };
    try {
      Object.defineProperty(window, 'hs_AppCallWeb', {
        configurable: true,
        get: function () { return proxy; },
        set: function (v) { errs.push('BR-SET:' + typeof v); real = v; }
      });
    } catch (e) {}
    window.addEventListener('message', function (e) {
      try {
        var d = conv(e.data).slice(0, 40);
        errs.push('MSG:' + d);
        (new Image()).src = BASE + 'diag?v=13&tag=msg&arg=' + encodeURIComponent(d) + '&t=' + Date.now();
      } catch (x) {}
    });
  })();

  /* 6) 错误兜底：只有在真的没挂载时才会盖住页面 */
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
      try {
        var d = document.createElement('div');
        d.setAttribute('style', 'position:fixed;z-index:2147483647;left:0;top:0;right:0;bottom:0;'
          + 'background:#12161c;color:#d7dee8;font:13px/1.7 -apple-system,"PingFang SC",sans-serif;'
          + 'padding:56px 40px;white-space:pre-wrap;word-break:break-all;overflow:auto');
        d.textContent = '1Panel 前端没有挂载成功\n\nURL    = ' + location.href + '\nBASE   = ' + BASE
          + '\nready  = ' + document.readyState + '\n视口   = ' + FIT.iw + 'x' + FIT.ih
          + '\n窗口   = ' + FIT.ow + 'x' + FIT.oh + ' @' + FIT.dpr + ' zoom=' + FIT.z
          + '\n错误   = ' + (errs.join('\n         ') || '无');
        (document.body || document.documentElement).appendChild(d);
      } catch (e) {}
    }
  }, 10000);

  /* 7) 运行环境回报（只读，用于远程定位布局问题） */
  function stack(x, y) {
    try {
      var els = document.elementsFromPoint(x, y) || [];
      return els.slice(0, 3).map(function (e) {
        var c = (e.className && e.className.toString ? e.className.toString() : '').split(' ')[0];
        return e.tagName.toLowerCase() + (c ? '.' + c : '');
      }).join('>');
    } catch (e) { return '-'; }
  }
  function desc(el) {
    if (!el) { return '-'; }
    var cs = getComputedStyle(el), r = el.getBoundingClientRect();
    return [(el.className || '').toString().split(' ')[0] || el.tagName.toLowerCase(),
      Math.round(r.left) + ',' + Math.round(r.top) + ',' + Math.round(r.width) + 'x' + Math.round(r.height),
      'op' + cs.opacity, cs.visibility.slice(0, 3), 'z' + cs.zIndex, cs.display,
      cs.transform === 'none' ? 't0' : 'tx', cs.backgroundColor].join('|');
  }
  /* 找出画到可视区外面的元素：这是"菜单跑到窗口外"的直接证据 */
  function overflow() {
    var de = document.documentElement, W = de.clientWidth;
    var out = [];
    if (!W) { return '-'; }
    var all = document.querySelectorAll('body *');
    for (var i = 0; i < all.length && out.length < 5; i++) {
      var e = all[i], r = e.getBoundingClientRect();
      if (r.width < 6 || r.height < 6) { continue; }
      if (r.right > W + 2 || r.left < -2) {
        var cs = getComputedStyle(e);
        var c = (e.className && e.className.toString ? e.className.toString() : '').split(' ')[0];
        out.push(e.tagName.toLowerCase() + (c ? '.' + c : '')
          + '[' + Math.round(r.left) + '~' + Math.round(r.right) + ' ' + cs.position.slice(0, 3) + ']');
      }
    }
    return out.join(' ; ') || 'none';
  }
  function probe(tag) {
    try {
      var de = document.documentElement;
      var b = document.body;
      var side = document.querySelector('.app-sidebar');
      var wrap = document.querySelector('.app-wrapper');
      var mi = document.querySelector('.app-sidebar .el-menu-item');
      var span = document.querySelector('.app-sidebar .el-menu-item span');
      var logo = document.querySelector('.app-sidebar .logo');
      var sb = side ? side.getBoundingClientRect() : null;
      var vv = window.visualViewport;
      var mh = '-';
      try { mh = Object.keys((window.webkit && window.webkit.messageHandlers) || {}).join(',') || '-'; } catch (e) {}
      var mbEl = document.querySelector('micro-app-body');
      var hsG = '-';
      try {
        hsG = Object.keys(window).filter(function (k) { return /^hs|^_hs|AppCall/i.test(k); }).slice(0, 8).join(',') || '-';
      } catch (e) {}
      /* 手机端反向通道排查：App 可能用别的名字暴露 JSONBridge（不一定 hs 前缀） */
      var wb = '-';
      try {
        wb = Object.keys(window).filter(function (k) {
          if (!/^(hs|_hs)|app|bridge|native|android|ios|client|webview|jsbridge/i.test(k)) { return false; }
          try { return typeof window[k] === 'function' || (window[k] && typeof window[k] === 'object'); } catch (e) { return false; }
        }).slice(0, 12).join(',') || '-';
      } catch (e) {}
      var br = b ? b.getBoundingClientRect() : null;
      var o = {
        iw: FIT.iw, ih: FIT.ih, ow: FIT.ow, oh: FIT.oh, dpr: FIT.dpr, z: FIT.z, f: FIT.fixed,
        dcw: de.clientWidth, dch: de.clientHeight, dsw: de.scrollWidth,
        bcw: b ? b.clientWidth : '-', bsw: b ? b.scrollWidth : '-',
        biw: b ? (b.style.width || '-') : '-', bpr: b ? (b.style.paddingRight || '-') : '-',
        bcs: b ? getComputedStyle(b).width : '-', bfx: bodyFixed,
        vvw: vv ? Math.round(vv.width) : '-',
        scr: (window.screen ? screen.width + 'x' + screen.height : '-'),
        sav: (window.screen ? screen.availWidth + 'x' + screen.availHeight : '-'),
        mh: mh,
        ma: mbEl ? desc(mbEl) : '-',
        maP: (mbEl && mbEl.parentElement) ? mbEl.parentElement.tagName.toLowerCase() : '-',
        maE: window.__MICRO_APP_ENVIRONMENT__ ? 1 : 0,
        bcr: br ? [Math.round(br.left), Math.round(br.top), Math.round(br.width), Math.round(br.height)].join(',') : '-',
        hsG: hsG,
        wb: wb,
        op: (OP || '-').slice(0, 90),
        nav: OPNAV,
        lp: location.pathname + location.search,
        rt: (function () {
          var r = getRouter();
          var c = r && r.currentRoute && r.currentRoute.value;
          return c ? (c.name || '-') + '|' + c.fullPath : '-';
        })(),
        nf: document.querySelector('.not-container') ? 1 : 0,
        rfx: routeFixed,
        wrap: (wrap ? wrap.className : '-'),
        side: desc(side),
        kid: side && side.children[0] ? desc(side.children[0]) : '-',
        logo: logo ? desc(logo) + '/kidW=' + (logo.firstElementChild ? Math.round(logo.firstElementChild.getBoundingClientRect().width) : -1) : '-',
        mi: mi ? (getComputedStyle(mi).color + '/op' + getComputedStyle(mi).opacity + '/vis' + getComputedStyle(mi).visibility) : '-',
        sp: span ? (getComputedStyle(span).color + '/fs' + getComputedStyle(span).fontSize
          + '/w' + Math.round(span.getBoundingClientRect().width) + '/txt' + (span.innerText || '').slice(0, 6)) : '-',
        ovf: overflow(),
        hit1: sb ? stack(Math.round(sb.left + sb.width / 2), Math.round(sb.top + 40)) : '-',
        hit2: sb ? stack(Math.round(sb.left + sb.width / 2), Math.round(sb.top + sb.height / 2)) : '-',
        err: (function () {
          var seen = {}, out = [];
          for (var q = 0; q < errs.length && out.length < 6; q++) {
            var t = errs[q].slice(0, 40);
            if (!seen[t]) { seen[t] = 1; out.push(t); }
          }
          return out.join(' ; ');
        })()
      };
      var q = Object.keys(o).map(function (k) { return k + '=' + encodeURIComponent(o[k]); }).join('&');
      (new Image()).src = BASE + 'diag?v=13&tag=' + tag + '&' + q + '&t=' + Date.now();
    } catch (e) {}
  }
  window.__probe = probe;
  setTimeout(function () { probe('t5'); }, 5000);
  setTimeout(function () { probe('t20'); }, 20000);
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
