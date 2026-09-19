#!/usr/bin/env python3
"""测试页 UI 冒烟测试 —— 不开浏览器，也能验证前端渲染管线。

## 为什么需要它

修改 `api/test_page.html` 后，最常见的故障不是语法错误，而是**运行期白屏**：
元素 id 拼错、字段名写错、某个 `undefined` 上取属性。这类问题 `curl` 看 HTML 源码
发现不了，装浏览器又重（Chromium 500MB）。

本工具用 Node + 最小 DOM 桩，把测试页**真实的 JS** 跑起来，喂**真实的接口数据**
（从运行中的服务抓 `/api/map` 与 `/api/state`），然后断言各个面板确实被填充。

## 用法

    # 1) 先起服务（另一个终端）
    .venv/bin/python main.py --serve

    # 2) 跑冒烟测试
    .venv/bin/python tools/ui_smoke_test.py
    .venv/bin/python tools/ui_smoke_test.py --url http://127.0.0.1:8000

## 退出码

    0  全部通过
    1  有断言失败（或捕获到运行期异常）
    2  环境/数据错误（服务没起、页面读不到等）
    3  跳过（找不到可用的 node）

## 覆盖范围

    地图渲染（分区 / 路点 / 规划路径 / 用户位姿 / 动态障碍）
    KPI 四卡（位置 / 速度 / 进度 / 风险）
    导航与环境面板
    播报文本 + 决策来源徽章 + 允许前进徽章
    行动流事件：**同时钉死两条路径** —— CONTINUE 抑制刷屏 + 非 CONTINUE 必须插入
    （含 data-a 属性，缺了会导致 CSS 高亮失效）
    摄像头画面：三条互斥路径分别钉死 —— 本机推流 / 服务端帧 / 空态，
    外加「无 getUserMedia 时按钮必须禁用并写明原因」与「高频刷新不得越权改显隐」
    统计面板 / 时钟 / 顶部状态 chip / 原始数据导出
    手动驾驶（WASD）：首次按键切手动 / 组合键聚合 / 松键回直行 / 输入框不劫持 /
    小地图高亮圈跟随服务端状态 / 按钮绑定
    大模型诊断面板（走真实的 auth_error 分支）
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE = PROJECT_ROOT / "api" / "test_page.html"

# 本机若设了 HTTP_PROXY，访问 127.0.0.1 必须绕过，否则会被代理以 502 拒绝
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_json(url: str, timeout: float = 10.0) -> dict:
    """抓取 JSON（显式绕过代理，避免本机 HTTP_PROXY 干扰）。"""
    with _OPENER.open(url, timeout=timeout) as resp:  # noqa: S310 - 仅访问用户指定的本机地址
        return json.loads(resp.read().decode("utf-8"))


# =====================================================================
# DOM / 浏览器桩：只实现测试页用到的那部分 API
# =====================================================================
HARNESS_STUB = r"""
const _realSetTimeout = global.setTimeout;
const _errors = [];
const _fetchLog = [];   // 记录每次 fetch(url, opts)，供"按键有没有上行"这类断言使用
global.__fetchLog = _fetchLog;
const _text = {};   // id -> textContent
const _html = {};   // id -> innerHTML
const _cls  = {};   // id -> Set(className)

function mkEl(id) {
  const el = {
    id,
    className: "", style: {}, dataset: {}, title: "",
    children: [], value: "", disabled: false, width: 0, height: 0,
    classList: {
      add(c){ (_cls[id] = _cls[id] || new Set()).add(c); },
      remove(c){ (_cls[id] = _cls[id] || new Set()).delete(c); },
      toggle(c, on){ const s = (_cls[id] = _cls[id] || new Set()); on ? s.add(c) : s.delete(c); },
      contains(c){ return (_cls[id] || new Set()).has(c); },
    },
    // 用「父id::选择器」当稳定子元素 id，保证多次查询拿到同一个对象
    querySelector(sel){ return mkEl(id + "::" + sel); },
    querySelectorAll(){ return []; },
    setAttribute(k, v){ (this._attrs = this._attrs || {})[k] = String(v); },
    getAttribute(k){ return (this._attrs || {})[k]; },
    appendChild(c){ this.children.push(c); return c; },
    insertBefore(c){ this.children.unshift(c); return c; },
    removeChild(c){ this.children = this.children.filter(x => x !== c); },
    addEventListener(){}, removeEventListener(){},
    getContext(){ return { drawImage(){} }; },
    toBlob(cb){ cb(null); },
    play(){ return Promise.resolve(); },
    remove(){},
  };
  Object.defineProperty(el, "textContent", {
    get(){ return _text[id] || ""; }, set(v){ _text[id] = String(v); } });
  Object.defineProperty(el, "innerHTML", {
    get(){ return _html[id] || ""; }, set(v){ _html[id] = String(v); } });
  return el;
}

const _els = {};
global.document = {
  getElementById(id){ return _els[id] || (_els[id] = mkEl(id)); },
  createElement(){ return mkEl("el_" + Math.random().toString(36).slice(2)); },
  querySelectorAll(){ return []; },
  addEventListener(){},
};
global.location = { origin: __ORIGIN__, host: __HOST__, protocol: __PROTO__ };
global.navigator = { mediaDevices: null, clipboard: { writeText: async () => {} } };

/* 页面用 window.addEventListener 接管 WASD 键盘。
   Node 里没有 window，必须给一个**可手动触发**的桩 ——
   否则页面 JS 一执行到这里就 ReferenceError，整个冒烟测试全红。 */
const _winH = {};
global.window = {
  addEventListener(t, fn){ (_winH[t] = _winH[t] || []).push(fn); },
  removeEventListener(){},
  _fire(t, ev){ (_winH[t] || []).slice().forEach((fn) => fn(ev)); },
};
global.performance = { now: () => Date.now() };
global.URL = { createObjectURL: () => "blob:x", revokeObjectURL: () => {} };
global.Image = class { set src(v){} };
global.FormData = class { append(){} };
global.setInterval = () => 0;          // 后台轮询不启动，由测试手动驱动
global.clearInterval = () => {};
global.setTimeout = (fn, ms) => _realSetTimeout(fn, ms || 0);

const MAP_JSON  = __MAP__;
const TICK_JSON = __TICK__;

global.fetch = async (url, opts) => {
  const u = String(url);
  let body;
  if (u.indexOf("/api/map") >= 0) body = MAP_JSON;
  else if (u.indexOf("/api/state") >= 0) body = TICK_JSON;
  else if (u.indexOf("/api/camera") >= 0) body = { stats:{accepted:3,rejected:0,throttled:0}, snapshot:{bytes:9000} };
  else if (u.indexOf("/api/llm_check") >= 0) body = {
    configured:true, category:"auth_error", ok:false,
    http_status:401, latency_s:0.9, provider:"openai_compatible", model:"gpt-4o-mini",
    base_url:"https://api.openai.com/v1", key_masked:"sk-***", key_length:60,
    vision:false, error:"Incorrect API key provided", hint:"认证失败：Key 无效。" };
  else if (u.indexOf("/api/control") >= 0) {
    // 控制端点的响应必须**回显生效结果**（尤其 manual）：
    // 前端 setManual() 是"等响应回来才认定已切手动"的，桩里不回显就永远切不过去。
    let req = {};
    try { req = JSON.parse((opts && opts.body) || "{}"); } catch (e) { req = {}; }
    body = { ok:true, paused:false, action:req.action || "" };
    if (req.action === "manual") body.manual = !!req.enabled;
    if (req.action === "drive") body.manual = true;
  } else body = { ok:true };
  _fetchLog.push({ url:u, body:(opts && opts.body) || null, resp:body });
  return { ok:true, status:200, json: async () => body, blob: async () => ({}), text: async () => "" };
};

const _wsInstances = [];
global.WebSocket = class {
  constructor(url){ this.url = url; this.readyState = 0; this._sent = []; _wsInstances.push(this); }
  send(d){ this._sent.push(d); }
  close(){ this.readyState = 3; if (this.onclose) this.onclose(); }
  _open(){ this.readyState = 1; if (this.onopen) this.onopen(); }
  _msg(d){ if (this.onmessage) this.onmessage({ data: JSON.stringify(d) }); }
};
// 页面用 WebSocket.OPEN 判连接：桩里不定义的话它 === undefined，
// 所有 readyState===OPEN 的判断都会假、把 WS 分支悄悄退化成 HTTP 分支。
global.WebSocket.OPEN = 1;
global.__ws = _wsInstances;
global.__errors = _errors;
process.on("uncaughtException", (e) => { _errors.push("uncaught: " + ((e && e.stack) || e)); });
process.on("unhandledRejection", (e) => { _errors.push("unhandled: " + e); });
"""

ASSERTIONS = r"""
(async () => {
  await new Promise(r => _realSetTimeout(r, 80));   // 等 boot() 里的 loadMap 完成

  const R = [];
  const check = (name, fn) => {
    try { fn(); R.push(["ok", name, ""]); }
    catch (e) { R.push(["fail", name, (e && e.message) || String(e)]); }
  };
  const el = (id) => document.getElementById(id);
  const T = (id) => (el(id).textContent || "").trim();
  const H = (id) => (el(id).innerHTML || "").trim();

  /* --- 地图几何（尚无 tick 时也应能渲染）--- */
  check("地图：分区与路点文字", () => {
    const s = H("svg-map");
    if (s.length < 500) throw new Error("SVG 过短: " + s.length);
    // ★不要写死分区/路点名★ —— 地图是 config 数据驱动的，换地图不该让前端测试变红。
    // 选"面积最大的分区"（它一定够大、一定被写上名字），路点则全部标名。
    const zones = (MAP_JSON.zones || []);
    if (!zones.length) throw new Error("地图无分区");
    const big = zones.slice().sort((a, b) =>
      ((b.x_max - b.x_min) * (b.y_max - b.y_min)) -
      ((a.x_max - a.x_min) * (a.y_max - a.y_min)))[0];
    if (big.name && s.indexOf(big.name) < 0) throw new Error("最大分区名未渲染: " + big.name);
    const lms = (MAP_JSON.landmarks || []);
    if (!lms.length) throw new Error("地图无路点");
    if (!lms.some((lm) => s.indexOf(lm.name) >= 0)) throw new Error("路点名一个都没渲染");
  });

  /* --- 推送真实 tick --- */
  const ws = global.__ws.filter(w => w.url.indexOf("/ws/agent") >= 0)[0];
  if (!ws) { console.log("  \u2717 未建立 /ws/agent 连接"); process.exit(2); }
  ws._open();
  check("推送真实 tick 不抛异常", () => ws._msg(TICK_JSON));

  /* --- 小地图动态元素 --- */
  check("地图：规划路径 polyline", () => {
    if (H("svg-map").indexOf("<polyline") < 0) throw new Error("缺少 polyline");
  });
  check("地图：用户位姿标记", () => {
    if (H("svg-map").indexOf("\u4f60") < 0) throw new Error("缺少用户标记");
  });
  check("地图：动态障碍", () => {
    // 不依赖"服务端此刻恰好有动态障碍"（spawn 间隔 6s，随时可能是空的 —— 那会变成
    // 时通时不通的假失败）。自己喂一帧带障碍的 tick，把渲染路径钉死。
    // 用 narrow_passage：它的短标签「窄」在静态物体/分区名里都不出现，
    // 否则断言会被地图上本来就有的「椅」蒙混过关。
    const t = JSON.parse(JSON.stringify(TICK_JSON));
    const u = (t.state && t.state.user && t.state.user.position) || [1, 1];
    t.obstacles_dyn = [{ type: "narrow_passage", x: u[0] + 1.6, y: u[1] + 1.0, dynamic: true }];
    t.elapsed = (Number(t.elapsed) || 0) + 0.5;
    ws._msg(t);
    if (H("svg-map").indexOf("\u7a84") < 0) throw new Error("动态障碍未渲染（短标签缺失）");
  });

  /* --- KPI（页面用 innerHTML 赋值）--- */
  check("KPI：位置", () => { if (H("k-pos").indexOf("<small>") < 0) throw new Error("k-pos=" + H("k-pos")); });
  check("KPI：风险", () => { if (!T("k-risk") || T("k-risk") === "\u2014") throw new Error("k-risk=" + T("k-risk")); });
  check("KPI：进度", () => { if (H("k-prog").indexOf("%") < 0) throw new Error("k-prog=" + H("k-prog")); });
  check("KPI：速度", () => { if (H("k-speed").indexOf("m/s") < 0) throw new Error("k-speed=" + H("k-speed")); });
  check("KPI：区域副标题", () => { if (T("k-zone").indexOf("\u533a\u57df") < 0) throw new Error("k-zone=" + T("k-zone")); });

  /* --- 导航 / 环境 --- */
  check("导航：指令", () => { if (!T("nav-inst")) throw new Error("nav-inst 空"); });
  check("导航：剩余距离", () => { if (T("nav-dist").indexOf("m") < 0) throw new Error("nav-dist=" + T("nav-dist")); });
  check("环境：前方净空", () => { if (H("env-front").length < 3) throw new Error("env-front=" + H("env-front")); });
  check("环境：语义场景", () => { if (!T("env-semantic")) throw new Error("env-semantic 空"); });
  check("环境：置信度", () => { if (!T("env-conf") || T("env-conf") === "\u2014") throw new Error("env-conf 空"); });

  /* --- 播报与行动流 --- */
  check("播报：文本", () => { if (!T("say-text")) throw new Error("say-text 空"); });
  check("播报：决策来源徽章", () => { if (T("bdg-src").indexOf("source:") < 0) throw new Error(T("bdg-src")); });
  check("播报：允许前进徽章", () => { if (T("bdg-motion").indexOf("\u5141\u8bb8\u524d\u8fdb") < 0) throw new Error(T("bdg-motion")); });
  /* 行动流有「防 1Hz 刷屏」逻辑：CONTINUE 且内容未变时**故意不追加**。
     因此不能直接断言 feed 非空——那会随真实 tick 当前状态时通时不通（假失败）。
     正确做法：把「抑制」和「插入」两条路径分别钉死。 */
  const _at0 = (TICK_JSON.action || {}).action_type;
  if (_at0 === "CONTINUE") {
    check("行动流：CONTINUE 不刷屏（抑制重复）", () => {
      if (el("feed").children.length) {
        throw new Error("CONTINUE 被错误追加 " + el("feed").children.length + " 条（1Hz 会刷屏）");
      }
    });
  } else {
    check("行动流：非 CONTINUE 首轮即插入", () => {
      if (!el("feed").children.length) throw new Error("tick 为 " + _at0 + " 却未插入事件");
    });
  }

  // 注入一个「非 CONTINUE」事件，验证插入路径与 data-a 属性
  const _tickEv = JSON.parse(JSON.stringify(TICK_JSON));
  _tickEv.action = Object.assign({}, _tickEv.action, {
    action_type: "SPEAK",
    message: "（冒烟测试）前方有障碍物，请先停下。",
    urgency: "high",
    source: "llm",
    permits_motion: false,
  });
  _tickEv.elapsed = (Number(_tickEv.elapsed) || 0) + 1;
  check("行动流：非 CONTINUE 事件插入", () => { ws._msg(_tickEv); });
  check("行动流：事件带 data-a", () => {
    const c = el("feed").children[0];
    if (!c) throw new Error("feed 为空（未插入事件）");
    if (!c.dataset || !c.dataset.a) throw new Error("缺少 data-a（CSS 高亮会失效）");
    if (c.dataset.a !== "SPEAK") throw new Error("data-a=" + c.dataset.a + "，期望 SPEAK");
  });

  /* --- 摄像头画面：三条互斥路径必须分别钉死 ---
     这里**不能**依赖运行时状态（服务端此刻有没有帧、手机有没有在推流），
     否则用例会时通时不通；直接手动喂 snapshot 驱动三条分支。 */
  check("摄像头：本机无摄像头 API 时按钮禁用并写明原因", () => {
    // DOM 桩里 navigator.mediaDevices = null，boot() 的能力预检应当命中
    if (!el("btn-cam-start").disabled) throw new Error("按钮未禁用（用户会以为点了没反应）");
    if (!el("btn-cam-start").title) throw new Error("未给出禁用原因");
  });
  check("摄像头：有帧时切到服务端画面", () => {
    refreshServerPreview({ image_available: true, age_s: 0.4, stale: false, source: "websocket" });
    if (el("cam-phone").style.display !== "block") throw new Error("cam-phone 未显示");
    if (!el("cam-src").classList.contains("show")) throw new Error("cam-src 未标记 show");
    if (el("cam-empty").style.display !== "none") throw new Error("空态未隐藏");
    if (T("cam-src-age").indexOf("s") < 0) throw new Error("帧龄未渲染: " + T("cam-src-age"));
    if (String(el("cam-phone").src || "").indexOf("/api/frame") < 0) {
      throw new Error("未指向 /api/frame: " + el("cam-phone").src);
    }
  });
  check("摄像头：帧过期时标记 stale", () => {
    refreshServerPreview({ image_available: true, age_s: 72.1, stale: true, source: "websocket" });
    if (!el("cam-src").classList.contains("stale")) throw new Error("过期未标记 stale");
  });
  check("摄像头：无帧时退回空态", () => {
    refreshServerPreview({ image_available: false, age_s: null, stale: true, source: "none" });
    if (el("cam-phone").style.display !== "none") throw new Error("cam-phone 未隐藏");
    if (el("cam-src").classList.contains("show")) throw new Error("cam-src 仍显示");
    if (el("cam-empty").style.display !== "flex") throw new Error("空态未恢复");
  });
  check("摄像头：快速刷新不得自作主张改显隐状态", () => {
    // 高频路径（250ms）只负责换图，它不知道服务端还有没有帧；
    // 一旦它越权改状态，就会在推流中断时造成画面反复闪现。
    refreshServerPreview({ image_available: false, age_s: null, stale: true, source: "none" });
    el("cam-phone").src = "";
    refreshFrameImage();
    if (el("cam-phone").style.display !== "none") throw new Error("快速路径把画面又显示出来了");
    if (el("cam-phone").src) throw new Error("无帧时仍去取图: " + el("cam-phone").src);
  });

  /* --- 手动驾驶（WASD）---
     设计约定：按键**只上行"哪几个键按着"**（forward/turn ∈ -1/0/1），位移积分全部在
     服务端主循环里做。这几条把「首次按键切手动 / 组合键 / 松键回直行 /
     输入框不劫持 / 小地图高亮圈跟随服务端状态」逐条钉死。 */
  const fire = (t, ev) => global.window._fire(t, ev);
  const kd = (code, tag) => fire("keydown", { code, target: { tagName: tag || "BODY" }, preventDefault(){} });
  const ku = (code, tag) => fire("keyup", { code, target: { tagName: tag || "BODY" } });
  const lastDrive = () => {
    const arr = (ws._sent || [])
      .map((s) => { try { return JSON.parse(s); } catch (e) { return null; } })
      .filter((m) => m && m.type === "drive");
    return arr.length ? arr[arr.length - 1] : null;
  };

  check("手动：默认显示为自动导航", () => {
    if (T("drv-tag") !== "\u81ea\u52a8\u5bfc\u822a") throw new Error("drv-tag=" + T("drv-tag"));
  });

  kd("KeyW");
  await new Promise(r => _realSetTimeout(r, 40));
  check("手动：首次按 W 自动切换到手动", () => {
    if (T("drv-tag") !== "\u624b\u52a8\uff08\u4f60\u63a7\u5236\uff09") throw new Error("未切手动：" + T("drv-tag"));
    if (!el("drv-w").classList.contains("down")) throw new Error("W 指示灯未点亮");
  });
  check("手动：W 上行 forward=1 / turn=0", () => {
    const m = lastDrive();
    if (!m) throw new Error("未发出 drive 指令");
    if (m.forward !== 1 || m.turn !== 0) throw new Error(JSON.stringify(m));
  });

  kd("KeyD");
  await new Promise(r => _realSetTimeout(r, 30));
  check("手动：W+D 同按 → 前进并右转", () => {
    const m = lastDrive();
    if (!m || m.forward !== 1 || m.turn !== 1) throw new Error(JSON.stringify(m));
  });

  ku("KeyD");
  await new Promise(r => _realSetTimeout(r, 30));
  check("手动：松开 D → 恢复直行且指示灯灭", () => {
    const m = lastDrive();
    if (!m || m.forward !== 1 || m.turn !== 0) throw new Error(JSON.stringify(m));
    if (el("drv-d").classList.contains("down")) throw new Error("D 灯未熄灭");
  });

  ku("KeyW");
  await new Promise(r => _realSetTimeout(r, 30));
  check("手动：全部松开 → 上行 0/0（人应停下）", () => {
    const m = lastDrive();
    if (!m || m.forward !== 0 || m.turn !== 0) throw new Error(JSON.stringify(m));
  });

  const _sentBefore = (ws._sent || []).length;
  kd("KeyW", "INPUT");
  await new Promise(r => _realSetTimeout(r, 30));
  check("手动：焦点在输入框时不劫持按键", () => {
    if ((ws._sent || []).length !== _sentBefore) throw new Error("输入框里的 W 被当成驾驶指令发了出去");
  });

  check("手动：手动中小地图画高亮圈", () => {
    const t = JSON.parse(JSON.stringify(TICK_JSON));
    t.stats = Object.assign({}, t.stats, { drive: { manual: true, forward: 0, turn: 0, pending_s: [0, 0] } });
    t.elapsed = (Number(t.elapsed) || 0) + 2;
    ws._msg(t);
    if (H("svg-map").indexOf("data-manual-ring") < 0) throw new Error("手动态未画高亮圈");
  });
  check("手动：回到自动时高亮圈消失", () => {
    const t = JSON.parse(JSON.stringify(TICK_JSON));
    t.stats = Object.assign({}, t.stats, { drive: { manual: false, forward: 0, turn: 0, pending_s: [0, 0] } });
    t.elapsed = (Number(t.elapsed) || 0) + 3;
    ws._msg(t);
    if (H("svg-map").indexOf("data-manual-ring") >= 0) throw new Error("自动导航却还画着手动圈");
    if (T("drv-tag") !== "\u81ea\u52a8\u5bfc\u822a") throw new Error("drv-tag 未回到自动：" + T("drv-tag"));
  });
  check("手动：「回到自动导航」按钮已绑定", () => {
    if (typeof el("btn-manual-off").onclick !== "function") throw new Error("按钮未绑定 onclick");
  });

  /* --- 统计 / 时钟 / chip --- */
  check("统计面板渲染", () => { if (H("stat-grid").indexOf("sitem") < 0) throw new Error("stat-grid 未渲染"); });
  check("时钟已更新", () => {
    const t = el("c-clock").querySelector(".txt").textContent || "";
    if (t.indexOf("tick") < 0) throw new Error("时钟文案=" + t);
  });
  check("大模型 chip 已更新", () => {
    if (!(el("c-llm").querySelector(".txt").textContent || "")) throw new Error("c-llm 空");
  });
  check("原始数据已导出", () => {
    if (T("raw-out").indexOf("ws_last_frame") < 0) throw new Error("raw-out 未含原始帧");
  });

  /* --- 诊断面板（auth_error 分支）--- */
  const btn = el("btn-llm-basic");
  if (typeof btn.onclick !== "function") {
    R.push(["fail", "诊断按钮未绑定 onclick", ""]);
  } else {
    await btn.onclick();
    check("诊断：分类", () => { if (T("diag-cat").indexOf("\u8ba4\u8bc1\u5931\u8d25") < 0) throw new Error(T("diag-cat")); });
    check("诊断：HTTP 码", () => { if (T("diag-http").indexOf("401") < 0) throw new Error(T("diag-http")); });
    check("诊断：原始错误原文", () => {
      if (T("diag-body").indexOf("Incorrect API key") < 0) throw new Error("未展示原始错误");
    });
    check("诊断：修复提示", () => { if (!T("diag-hint")) throw new Error("diag-hint 空"); });
  }

  const failed = R.filter(r => r[0] === "fail");
  R.forEach(([s, n, m]) => console.log((s === "ok" ? "  \u2713 " : "  \u2717 ") + n + (m ? "  \u2192 " + m : "")));
  if (global.__errors.length) {
    console.log("\n捕获到运行期错误:");
    global.__errors.forEach(e => console.log("  ! " + String(e).split("\n")[0]));
  }
  console.log("\n结果: 通过 " + (R.length - failed.length) + " / " + R.length);
  process.exit(failed.length === 0 && global.__errors.length === 0 ? 0 : 1);
})();
"""


def extract_page_js(html: str) -> str:
    """取出测试页里的内联 JS（最后一个 <script> 块）。"""
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not blocks:
        raise RuntimeError("test_page.html 里找不到内联 <script> 块")
    return blocks[-1]


def build_harness(js: str, map_json: dict, tick_json: dict, origin: str) -> str:
    """拼出可在 Node 下执行的完整脚本：桩 + 页面 JS + 断言。"""
    stub = HARNESS_STUB
    stub = stub.replace("__MAP__", json.dumps(map_json, ensure_ascii=False))
    stub = stub.replace("__TICK__", json.dumps(tick_json, ensure_ascii=False))
    stub = stub.replace("__ORIGIN__", json.dumps(origin))
    stub = stub.replace("__HOST__", json.dumps(origin.split("//", 1)[-1]))
    stub = stub.replace("__PROTO__", json.dumps("https:" if origin.startswith("https") else "http:"))
    return stub + "\n" + js + "\n" + ASSERTIONS


def main() -> int:
    ap = argparse.ArgumentParser(description="测试页 UI 冒烟测试（Node + DOM 桩）")
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="运行中的服务地址")
    ap.add_argument("--node", default=None, help="node 可执行文件路径（默认自动探测）")
    args = ap.parse_args()

    node = args.node or shutil.which("node")
    if not node:
        print("[跳过] 找不到 node，无法执行 UI 冒烟测试。", file=sys.stderr)
        print("       可安装 Node 18+，或用 --node /path/to/node 指定。", file=sys.stderr)
        return 3

    if not PAGE.is_file():
        print(f"[错误] 找不到测试页: {PAGE}", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")
    try:
        map_json = fetch_json(f"{base}/api/map")
        tick_json = fetch_json(f"{base}/api/state")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[错误] 无法从 {base} 取数据：{e}", file=sys.stderr)
        print("       请先在另一个终端运行：.venv/bin/python main.py --serve", file=sys.stderr)
        return 2

    if tick_json.get("type") != "tick":
        print(f"[错误] /api/state 尚未产出 tick（type={tick_json.get('type')!r}），稍后重试。", file=sys.stderr)
        return 2

    try:
        page_js = extract_page_js(PAGE.read_text(encoding="utf-8"))
    except (OSError, RuntimeError) as e:
        print(f"[错误] 读取测试页失败：{e}", file=sys.stderr)
        return 2

    harness = build_harness(page_js, map_json, tick_json, base)

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        tmp_path = Path(f.name)

    print("=" * 68)
    print(f"测试页 UI 冒烟测试  |  {base}")
    print(f"页面: {PAGE.name}   节点: {node}")
    print("=" * 68)
    try:
        proc = subprocess.run([node, str(tmp_path)], check=False)
        code = proc.returncode
    finally:
        tmp_path.unlink(missing_ok=True)

    if code != 0:
        print("\n[失败] 前端渲染管线存在问题，见上面的 ✗ 项。", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
