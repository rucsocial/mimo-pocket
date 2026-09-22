#!/usr/bin/env python3
"""phone-remote bridge: LAN HTTP inbox/outbox for MiMo Desktop.

CLI:
  python bridge.py start [--port N]
  python bridge.py stop
  python bridge.py status
  python bridge.py url
  python bridge.py pending
  python bridge.py complete --id ID --status done|error --text "..." | --file path
  python bridge.py board
  python bridge.py clear-done

HTTP (default 0.0.0.0:8765):
  GET  /                 shared board UI (phone + desktop same page)
  GET  /api/board        board JSON
  GET  /api/status       health + LAN URLs + pending count
  POST /api/send         {"text": "...", "source": "phone"|"desktop"}
  GET  /api/pending      queued inbox items (agent poll)
  POST /api/complete     {"id": "...", "status": "done"|"error", "text": "..."}
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import parse_qs

DEFAULT_PORT = int(os.environ.get("PHONE_REMOTE_PORT", "8765"))
DEFAULT_HOST = os.environ.get("PHONE_REMOTE_HOST", "0.0.0.0")
AUTO_DEFAULT = os.environ.get("PHONE_REMOTE_AUTO", "1") not in ("0", "false", "no")
WORKDIR = Path(
    os.environ.get("PHONE_REMOTE_WORKDIR")
    or Path(__file__).resolve().parents[1]
)

_HOME = Path(
    os.environ.get("PHONE_REMOTE_HOME")
    or (Path.home() / ".local" / "share" / "mimocode" / "phone-remote")
)
INBOX = _HOME / "inbox"
OUTBOX = _HOME / "outbox"
TMP = _HOME / "tmp"
PID_FILE = _HOME / "server.pid"
LOG_FILE = _HOME / "server.log"
BOARD_FILE = _HOME / "board.json"
SESSION_FILE = _HOME / "mimo_session_id.txt"
PHONE_SESSION_FILE = _HOME / "phone_session_id.txt"
MODEL_FILE = _HOME / "model.txt"
AUTO_FLAG = _HOME / "auto_enabled"
WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>MiMo Phone Remote</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --ink: #e8eaed;
    --muted: #9aa0a6;
    --accent: #7c9cff;
    --user: #1e2a44;
    --bot: #1a2332;
    --ok: #3dd68c;
    --err: #ff7b72;
    --warn: #e3b341;
    --border: #2a2f3a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--ink);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    position: sticky; top: 0; z-index: 10;
    background: rgba(15,17,21,.92);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
    padding: 12px 16px;
  }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; }
  header .meta { margin-top: 4px; font-size: 12px; color: var(--muted); }
  header .meta b { color: var(--accent); font-weight: 600; }
  #board {
    flex: 1;
    overflow-y: auto;
    padding: 16px 12px 120px;
    max-width: 820px;
    width: 100%;
    margin: 0 auto;
  }
  .msg {
    margin: 0 0 12px;
    padding: 12px 14px;
    border-radius: 12px;
    border: 1px solid var(--border);
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.55;
  }
  .msg.user { background: var(--user); }
  .msg.assistant { background: var(--bot); }
  .msg .head {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .msg .badge {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
  }
  .badge.queued { color: var(--warn); border-color: var(--warn); }
  .badge.processing { color: var(--accent); border-color: var(--accent); }
  .badge.done { color: var(--ok); border-color: var(--ok); }
  .badge.error { color: var(--err); border-color: var(--err); }
  .empty {
    text-align: center;
    color: var(--muted);
    padding: 48px 16px;
    font-size: 14px;
  }
  footer {
    position: fixed;
    left: 0; right: 0; bottom: 0;
    background: rgba(15,17,21,.96);
    border-top: 1px solid var(--border);
    padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
  }
  footer .row {
    max-width: 820px;
    margin: 0 auto;
    display: flex;
    gap: 8px;
  }
  footer .row + .row { margin-top: 8px; }
  footer .mrow-label {
    align-self: center;
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
  }
  footer #modelSel {
    flex: 1;
    min-width: 0;
    padding: 7px 8px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    font-size: 13px;
    outline: none;
  }
  textarea {
    flex: 1;
    min-height: 44px;
    max-height: 140px;
    resize: none;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--ink);
    padding: 10px 12px;
    font: inherit;
    outline: none;
  }
  textarea:focus { border-color: var(--accent); }
  button {
    border: 0;
    border-radius: 10px;
    background: var(--accent);
    color: #0b1020;
    font-weight: 600;
    padding: 0 16px;
    min-width: 72px;
    cursor: pointer;
  }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .hint {
    max-width: 820px;
    margin: 6px auto 0;
    font-size: 12px;
    color: var(--muted);
  }
</style>
</head>
<body>
<header>
  <h1>MiMo 手机远程 · 同屏看板</h1>
  <div class="meta" id="meta">连接中…</div>
  <a href="/history" style="display:inline-block;margin-top:6px;font-size:12px;color:var(--accent);text-decoration:none;">电脑端会话历史 →</a>
</header>
<div id="board"><div class="empty">等待第一条指令…</div></div>
<footer>
  <div class="row">
    <label class="mrow-label" for="modelSel">模型<span id="modelHint"></span></label>
    <select id="modelSel" aria-label="选择模型"><option>加载中…</option></select>
  </div>
  <div class="row">
    <textarea id="input" placeholder="输入要让电脑 MiMo Desktop 执行的文字…" rows="2"></textarea>
    <button id="sendBtn">发送</button>
  </div>
  <div class="hint" id="hint">自动处理已开启：手机发送后由电脑 MiMo 自动执行并写回本看板，请稍候刷新（约数秒～数十秒）。</div>
</footer>
<script>
const boardEl = document.getElementById('board');
const metaEl = document.getElementById('meta');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const source = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent) ? 'phone' : 'desktop';

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleString();
}
function badge(status) {
  return `<span class="badge ${esc(status)}">${esc(status)}</span>`;
}
function renderBoard(data) {
  const pending = (data.pending_count ?? 0);
  const urls = (data.urls || []).map(u => `<b>${esc(u)}</b>`).join(' · ');
  const autoOn = data.auto !== false;
  const pendCls = pending > 0 ? 'color:#e3b341' : '';
  const pendHint = pending > 0
    ? (autoOn ? ' · 自动处理中，请稍候…' : ' · 等待处理（自动已关）')
    : ' · 空闲';
  metaEl.innerHTML = `状态 <b>${esc(data.server || 'online')}</b> · 自动 <b>${autoOn ? 'ON' : 'OFF'}</b> · 待处理 <b style="${pendCls}">${pending}</b>${esc(pendHint)} · ${urls || '本机'}`;
  const hint = document.getElementById('hint');
  if (hint) {
    hint.textContent = autoOn
      ? '自动处理已开启：手机发送后由电脑 MiMo 自动执行并写回本看板，请稍候刷新。'
      : '自动处理已关闭：请在电脑 MiMo 说「处理手机指令」。';
  }
  const msgs = data.messages || [];
  if (!msgs.length) {
    boardEl.innerHTML = '<div class="empty">还没有消息。在下方输入文字并发送。</div>';
    return;
  }
  boardEl.innerHTML = msgs.map(m => {
    const role = m.role === 'assistant' ? 'assistant' : 'user';
    const who = m.role === 'assistant' ? 'MiMo Desktop' : (m.source === 'desktop' ? '电脑网页' : '手机');
    return `<div class="msg ${role}">
      <div class="head"><span>${esc(who)} · ${esc(fmtTime(m.updated_at || m.created_at))}</span>${badge(m.status || 'done')}</div>
      <div class="body">${esc(m.text || '')}</div>
    </div>`;
  }).join('');
  boardEl.scrollTop = boardEl.scrollHeight;
}
async function refresh() {
  try {
    const res = await fetch('/api/board', {cache: 'no-store'});
    if (!res.ok) throw new Error('board http ' + res.status);
    renderBoard(await res.json());
  } catch (e) {
    metaEl.textContent = '连接失败：' + e.message;
  }
}
async function send() {
  const text = inputEl.value.trim();
  if (!text) return;
  sendBtn.disabled = true;
  try {
    const res = await fetch('/api/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, source})
    });
    if (!res.ok) throw new Error('send http ' + res.status);
    inputEl.value = '';
    await refresh();
  } catch (e) {
    alert('发送失败：' + e.message);
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
}
sendBtn.addEventListener('click', send);
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send();
});
const modelEl = document.getElementById('modelSel');
const modelHint = document.getElementById('modelHint');
async function loadModels() {
  try {
    const res = await fetch('/api/models', {cache: 'no-store'});
    const d = await res.json();
    if (d.error) throw new Error(d.error);
    const cur = d.current;
    modelEl.innerHTML = (d.models || []).map(m =>
      '<option value="' + esc(m) + '"' + (m === cur ? ' selected' : '') + '>' + esc(m) + '</option>'
    ).join('');
    if (cur) modelEl.value = cur;
  } catch (e) {
    modelEl.innerHTML = '<option value="">模型列表加载失败</option>';
  }
}
async function saveModel() {
  modelHint.textContent = '…';
  modelHint.style.color = 'var(--muted)';
  try {
    const res = await fetch('/api/model', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: modelEl.value})
    });
    const d = await res.json();
    if (!d.ok) throw new Error(d.error || '保存失败');
    modelHint.textContent = '已切换 ' + d.model;
    modelHint.style.color = 'var(--ok)';
  } catch (e) {
    modelHint.textContent = '失败';
    modelHint.style.color = 'var(--err)';
    alert('切换失败：' + e.message);
  }
  setTimeout(() => { modelHint.textContent = ''; }, 5000);
}
modelEl.addEventListener('change', saveModel);
loadModels();
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


HISTORY_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>MiMo 会话 · 可阅读可对话</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --ink: #e8eaed;
    --muted: #9aa0a6;
    --accent: #7c9cff;
    --user: #1e2a44;
    --bot: #1a2332;
    --line: #232a38;
    --ok: #3dd68c;
    --err: #ff7b72;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 14px;
  }
  header {
    position: sticky;
    top: 0;
    z-index: 5;
    padding: 12px 14px 8px;
    border-bottom: 1px solid #22262f;
    background: linear-gradient(180deg, #12151b, #0f1115);
  }
  h1 { font-size: 16px; margin: 0 0 6px; }
  .meta { font-size: 12px; color: var(--muted); word-break: break-all; }
  .bar { display: flex; gap: 8px; padding: 10px 14px; align-items: center; flex-wrap: wrap; }
  select, button, a.btn {
    background: #1b1f28;
    color: var(--ink);
    border: 1px solid #2b3140;
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 13px;
    text-decoration: none;
  }
  select { flex: 1 1 220px; min-width: 0; }
  button { cursor: pointer; }
  a.btn { color: var(--accent); white-space: nowrap; }
  #list { padding: 0 14px 40px; }
  .msg {
    margin: 8px 0;
    padding: 10px 12px;
    border: 1px solid #232a38;
    border-radius: 10px;
    background: var(--bot);
  }
  .msg.user { background: var(--user); }
  .msg .head {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 6px;
    font-size: 11px;
    color: var(--muted);
  }
  .msg .body { white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
  .tools { margin-top: 6px; font-size: 11px; color: var(--muted); }
  .empty { padding: 16px 14px; color: var(--muted); }
  .err { padding: 16px 14px; color: var(--err); }
  .chat {
    display: flex;
    gap: 8px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
    background: var(--panel);
  }
  .chat textarea {
    flex: 1;
    min-width: 0;
    min-height: 42px;
    max-height: 130px;
    resize: none;
    border-radius: 8px;
    border: 1px solid var(--line);
    background: var(--bg);
    color: var(--ink);
    padding: 8px 10px;
    font: inherit;
    font-size: 13px;
    outline: none;
  }
  .chat textarea:focus { border-color: var(--accent); }
  .chat button {
    padding: 7px 14px;
    border: 0;
    border-radius: 8px;
    background: var(--accent);
    color: #0b1020;
    font-weight: 600;
    font-size: 13px;
    cursor: pointer;
  }
  .chat button:disabled { opacity: .5; cursor: not-allowed; }
  .chatstatus {
    min-height: 20px;
    padding: 6px 14px;
    font-size: 12px;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
  }
</style>
</head>
<body>
<header>
  <h1>MiMo 会话 · 可阅读，也可继续对话</h1>
  <div class="meta" id="meta">加载中…</div>
</header>
<div class="bar">
  <select id="sess"></select>
  <button id="reload">刷新</button>
  <a class="btn" href="/">← 返回看板</a>
</div>
<div class="chat">
  <textarea id="chatInput" rows="2" placeholder="在选中的会话里继续对话（会真的写进这个会话）"></textarea>
  <button id="chatSend">发送</button>
</div>
<div class="chatstatus" id="chatStatus"></div>
<div id="list"><div class="empty">加载中…</div></div>
<script>
const listEl = document.getElementById('list');
const metaEl = document.getElementById('meta');
const sessEl = document.getElementById('sess');
const params = new URLSearchParams(location.search);
let sid = params.get('sid') || '__SID__';
let lastHtml = '';

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmt(ts) {
  return ts ? new Date(ts).toLocaleString() : '';
}
async function loadSessions() {
  try {
    const res = await fetch('/api/sessions', {cache: 'no-store'});
    const data = await res.json();
    const items = data.sessions || [];
    const phone = data.phone_session || data.current;
    if (!sid) sid = data.default || (items[0] && items[0].id) || '';
    sessEl.innerHTML = items.map(s => {
      const tag = s.id === phone ? '（手机会话）' : '';
      const label = (s.title || '') + '  ·  ' + (s.updated || '') + tag;
      const sel = s.id === sid ? ' selected' : '';
      return '<option value="' + esc(s.id) + '"' + sel + '>' + esc(label) + '</option>';
    }).join('');
  } catch (e) {
    metaEl.textContent = '会话列表加载失败：' + e.message;
  }
}
async function loadHistory(force) {
  if (!sid) {
    listEl.innerHTML = '<div class="empty">没有可显示的会话。</div>';
    return;
  }
  const url = '/api/history?sid=' + encodeURIComponent(sid) + '&limit=60' + (force ? '&force=1' : '');
  try {
    const res = await fetch(url, {cache: 'no-store'});
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || ('http ' + res.status));
    const s = data.session || {};
    metaEl.textContent = (s.title || '') + ' · ' + (s.directory || '') +
      ' · 共 ' + data.total + ' 条，显示最后 ' + data.shown + ' 条';
    if (!data.messages.length) {
      listEl.innerHTML = '<div class="empty">这个会话还没有可显示的消息。</div>';
      return;
    }
    const html = data.messages.map(m => {
      const who = m.role === 'user' ? '用户（电脑端）' : 'MiMo';
      const cls = m.role === 'user' ? 'msg user' : 'msg bot';
      const tools = m.tool_count
        ? '<div class="tools">工具 ' + m.tool_count + ' 次' +
          (m.tools && m.tools.length ? '：' + esc(m.tools.join(' / ')) : '') + '</div>'
        : '';
      return '<div class="' + cls + '">' +
        '<div class="head"><span>' + esc(who) + '</span><span>' + esc(fmt(m.time)) + '</span></div>' +
        '<div class="body">' + esc(m.text || '') + '</div>' + tools + '</div>';
    }).join('');
    if (html === lastHtml) return;
    lastHtml = html;
    listEl.innerHTML = html;
  } catch (e) {
    listEl.innerHTML = '<div class="err">读取失败：' + esc(e.message) + '</div>';
    metaEl.textContent = '读取失败';
  }
}
sessEl.addEventListener('change', () => {
  sid = sessEl.value;
  lastHtml = '';
  history.replaceState(null, '', '/history?sid=' + encodeURIComponent(sid));
  loadHistory(false);
});
document.getElementById('reload').addEventListener('click', () => loadHistory(true));
loadSessions().then(() => loadHistory(false));
setInterval(() => loadHistory(false), 10000);

const chatInput = document.getElementById('chatInput');
const chatStatus = document.getElementById('chatStatus');
const chatSend = document.getElementById('chatSend');
let chatTask = null;
let chatStarted = 0;

function setChatStatus(text, color) {
  chatStatus.textContent = text || '';
  chatStatus.style.color = color || 'var(--muted)';
}
async function sendChat() {
  const text = chatInput.value.trim();
  if (!text) return;
  if (!sid) {
    setChatStatus('请先选择一个会话', 'var(--err)');
    return;
  }
  chatSend.disabled = true;
  setChatStatus('执行中… 模型较慢，通常需要 1～3 分钟');
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sid, text})
    });
    const d = await res.json();
    if (!d.ok) throw new Error(d.error || 'send failed');
    chatInput.value = '';
    chatTask = d.task_id;
    chatStarted = Date.now();
    pollChat();
  } catch (e) {
    setChatStatus('发送失败：' + e.message, 'var(--err)');
    chatSend.disabled = false;
  }
}
async function pollChat() {
  if (!chatTask) return;
  try {
    const res = await fetch('/api/chat/status?task_id=' + encodeURIComponent(chatTask), {cache: 'no-store'});
    const d = await res.json();
    if (!d.ok) throw new Error(d.error || 'status failed');
    if (d.status === 'running') {
      setChatStatus('执行中… 已 ' + Math.round((Date.now() - chatStarted) / 1000) + 's');
      setTimeout(pollChat, 3000);
      return;
    }
    if (d.status === 'done') {
      setChatStatus('完成，已写入该会话', 'var(--ok)');
      chatTask = null;
      chatSend.disabled = false;
      lastHtml = '';
      await loadHistory(true);
      return;
    }
    setChatStatus('失败：' + (d.reply || d.error || '未知错误'), 'var(--err)');
    chatTask = null;
    chatSend.disabled = false;
  } catch (e) {
    setChatStatus('状态查询失败：' + e.message + '（继续重试）');
    setTimeout(pollChat, 5000);
  }
}
chatSend.addEventListener('click', sendChat);
chatInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) sendChat();
});
</script>
</body>
</html>
"""


def ensure_dirs() -> None:
    for p in (INBOX, OUTBOX, TMP, _HOME):
        p.mkdir(parents=True, exist_ok=True)
    if not BOARD_FILE.exists():
        write_board({"messages": []})


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f"{time.time() % 1:.3f}"[1:]


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_board(board: dict) -> None:
    board = dict(board or {})
    board.setdefault("messages", [])
    board["updated_at"] = now_iso()
    board["pending_count"] = count_pending()
    board["urls"] = lan_urls(current_port())
    board["server"] = "online"
    write_json(BOARD_FILE, board)


def load_board() -> dict:
    board = read_json(BOARD_FILE, {"messages": []})
    if not isinstance(board, dict):
        board = {"messages": []}
    board.setdefault("messages", [])
    return board


def count_pending() -> int:
    if not INBOX.exists():
        return 0
    return sum(1 for p in INBOX.glob("*.json") if p.is_file())


def lan_urls(port: int) -> list[str]:
    urls = [f"http://127.0.0.1:{port}"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            urls.insert(0, f"http://{ip}:{port}")
    except Exception:
        pass
    # also try hostname-based guesses via getaddrinfo
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, port, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                url = f"http://{ip}:{port}"
                if url not in urls:
                    urls.append(url)
    except Exception:
        pass
    return urls[:4]


def new_id(prefix: str = "req") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{os.getpid()}"


def upsert_board_message(msg: dict) -> None:
    board = load_board()
    messages = board.get("messages") or []
    mid = msg.get("id")
    found = False
    for i, m in enumerate(messages):
        if m.get("id") == mid:
            messages[i] = {**m, **msg}
            found = True
            break
    if not found:
        messages.append(msg)
    board["messages"] = messages
    write_board(board)


def auto_enabled() -> bool:
    if AUTO_FLAG.exists():
        try:
            return AUTO_FLAG.read_text(encoding="utf-8").strip() not in ("0", "false", "off")
        except Exception:
            return AUTO_DEFAULT
    return AUTO_DEFAULT


def set_auto(enabled: bool) -> None:
    ensure_dirs()
    AUTO_FLAG.write_text("1" if enabled else "0", encoding="utf-8")


def resolve_mimo() -> list[str]:
    """Return argv prefix that launches mimo CLI."""
    custom = os.environ.get("PHONE_REMOTE_MIMO")
    if custom:
        return [custom]
    npm_mimo = Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@mimo-ai" / "cli" / "bin" / "mimo"
    node = Path.home() / "AppData" / "Roaming" / "npm" / "node.exe"
    if not node.exists():
        # fallback: PATH node
        node_path = None
        for cand in ("node", "node.exe"):
            found = shutil_which(cand)
            if found:
                node_path = found
                break
        if node_path and npm_mimo.exists():
            return [node_path, str(npm_mimo)]
    else:
        if npm_mimo.exists():
            return [str(node), str(npm_mimo)]
    for name in ("mimo.cmd", "mimo.exe", "mimo"):
        found = shutil_which(name)
        if found:
            return [found]
    return ["mimo"]


def shutil_which(cmd: str) -> str | None:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    exts = [""]
    if os.name == "nt":
        exts = os.environ.get("PATHEXT", ".EXE;.CMD;.BAT;.PS1").split(";")
    for p in paths:
        for ext in exts:
            cand = Path(p) / (cmd if cmd.lower().endswith(ext.lower()) or not ext else cmd + ext)
            if cand.is_file():
                return str(cand)
    return None


def read_session_id() -> str | None:
    try:
        sid = SESSION_FILE.read_text(encoding="utf-8").strip()
        return sid or None
    except Exception:
        return None


def write_session_id(sid: str) -> None:
    SESSION_FILE.write_text(sid, encoding="utf-8")


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_mimo_json_events(raw: str) -> tuple[str | None, str]:
    session_id = None
    texts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        sid = ev.get("sessionID") or (ev.get("part") or {}).get("sessionID")
        if sid:
            session_id = sid
        if ev.get("type") == "text":
            part = ev.get("part") or {}
            t = part.get("text") or ""
            if t.strip():
                texts.append(t)
    return session_id, "\n".join(texts).strip()


def parse_mimo_errors(raw: str) -> list[str]:
    """Collect {"type":"error"} events — the CLI exits 0 for them too."""
    errors: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "error":
            continue
        err = ev.get("error") or {}
        data = err.get("data") or {}
        msg = str(data.get("message") or err.get("message") or err.get("name") or "").strip()
        if msg and msg not in errors:
            errors.append(msg)
    return errors


def parse_mimo_default_output(raw: str) -> str:
    text = ANSI_RE.sub("", raw).replace("\r\n", "\n")
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        if s.startswith(">") and "·" in s:
            continue
        if s.startswith(">") and any(k in s.lower() for k in ("build", "plan", "compose")):
            continue
        cleaned.append(line.rstrip())
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned).strip()


DEFAULT_MODEL = os.environ.get("PHONE_REMOTE_MODEL") or "xiaomi/mimo-v2.6-pro"


def read_phone_session_id() -> str | None:
    try:
        sid = PHONE_SESSION_FILE.read_text(encoding="utf-8").strip()
        return sid or None
    except Exception:
        return None


def write_phone_session_id(sid: str) -> None:
    PHONE_SESSION_FILE.write_text(sid, encoding="utf-8")


def current_model() -> str:
    """Model for the next run: the page/CLI selection, else the code default."""
    try:
        chosen = MODEL_FILE.read_text(encoding="utf-8").strip()
        if chosen:
            return chosen
    except Exception:
        pass
    return DEFAULT_MODEL


def set_model(model: str) -> str:
    """Persist a model choice — takes effect on the next run, no restart."""
    model = (model or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._/-]*", model):
        raise RuntimeError(f"非法模型 id：{model!r}（应形如 provider/model）")
    MODEL_FILE.write_text(model, encoding="utf-8")
    return model


_MODEL_LIST_CACHE: dict[str, Any] = {"at": 0.0, "items": []}
MODEL_LIST_TTL = 60.0


def list_available_models(force: bool = False) -> list[str]:
    """Model ids from the providers the phone side is known to work with."""
    now = time.time()
    cached = _MODEL_LIST_CACHE.get("items") or []
    if not force and cached and now - float(_MODEL_LIST_CACHE.get("at") or 0) < MODEL_LIST_TTL:
        return cached
    raw = run_mimo_cli(["models"], timeout=90)
    allowed = ("xiaomi/", "deepseek/")
    items = sorted({
        line.strip()
        for line in raw.splitlines()
        if line.strip().startswith(allowed)
    })
    current = current_model()
    if current and current not in items:
        items.insert(0, current)
    _MODEL_LIST_CACHE.update(at=now, items=items)
    return items


def push_event(task_id: str | None, ev: dict) -> None:
    """Record one live event (memory + jsonl) so the phone can watch progress."""
    if not task_id:
        return
    ev = dict(ev)
    ev.setdefault("at", now_iso())
    with CHAT_LOCK:
        task = CHAT_TASKS.get(task_id) or {}
        task.setdefault("events", []).append(ev)
        CHAT_TASKS[task_id] = task
    try:
        d = chat_task_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / (task_id + ".events.jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def normalize_event(line: str) -> list[dict]:
    """One stdout JSONL line -> compact, phone-readable events."""
    line = (line or "").strip()
    if not line.startswith("{"):
        return []
    try:
        ev = json.loads(line)
    except Exception:
        return []
    kind = ev.get("type")
    part = ev.get("part") or {}
    out: list[dict] = []
    if kind == "text" or part.get("type") == "text":
        body = (part.get("text") or "").strip()
        if body:
            out.append({"kind": "text", "text": body})
    elif kind == "tool_use" or part.get("type") == "tool":
        state = part.get("state") or {}
        label = str(state.get("title") or "").strip() or str(part.get("tool") or "tool")
        out.append({
            "kind": "tool",
            "tool": str(part.get("tool") or ""),
            "label": label,
            "status": str(state.get("status") or "").strip(),
        })
    elif kind == "error":
        err = ev.get("error") or {}
        data = err.get("data") or {}
        msg = str(data.get("message") or err.get("message") or err.get("name") or "").strip()
        out.append({"kind": "error", "text": msg or "error"})
    elif kind == "step_start":
        out.append({"kind": "step", "text": "step start"})
    elif kind == "step_finish":
        out.append({"kind": "step", "text": "step finish"})
    return out


def run_agent_once(
    text: str, session_id: str | None, timeout: int, task_id: str | None = None
) -> tuple[bool, str, str | None]:
    """One headless mimo run. Returns (ok, reply, session_id_reported).

    stdout is consumed incrementally so tool/file activity reaches the phone as
    it happens; completion is driven by process wait + a short drain, never by
    pipe EOF (a lingering grandchild used to hang the task forever).
    """
    workdir = str(WORKDIR if WORKDIR.exists() else Path.cwd())
    model = current_model()
    cmd = [*resolve_mimo(), "run", "--dangerously-skip-permissions", "--format", "json", "--dir", workdir]
    if model:
        cmd.extend(["--model", model])
    if session_id:
        cmd.extend(["--session", session_id])
    else:
        cmd.extend(["--title", "phone-remote"])
    cmd.append(text)

    env = os.environ.copy()
    env.setdefault("MIMOCODE_DISABLE_CRON", "1")

    out_buf: list = []
    err_buf: list = []

    def _read_err() -> None:
        try:
            for line in proc.stderr:
                err_buf.append(line)
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=workdir,
            env=env,
        )
    except FileNotFoundError as e:
        return False, f"找不到 mimo CLI：{e}", None
    except Exception as e:
        return False, f"mimo run 启动失败：{e}", None

    if task_id:
        with RUNNING_LOCK:
            RUNNING_PROCS[task_id] = proc
    progress = {"text": False, "finish": False}

    def _read_out() -> None:
        try:
            for line in proc.stdout:
                out_buf.append(line)
                if '"type":"text"' in line:
                    progress["text"] = True
                if '"type":"step_finish"' in line:
                    progress["finish"] = True
                for ev in normalize_event(line):
                    push_event(task_id, ev)
        except Exception:
            pass

    try:
        t_out = threading.Thread(target=_read_out, daemon=True)
        t_err = threading.Thread(target=_read_err, daemon=True)
        t_out.start()
        t_err.start()
        # Do NOT wait for process exit: node/mimo can linger long after the
        # answer is complete (timers, child handles), which made the phone sit
        # at "thinking" for minutes after the reply was already streamed.
        code = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = proc.poll()
            if code is not None:
                break
            if progress["text"] and progress["finish"]:
                time.sleep(1.0)  # drain trailing lines
                break
            time.sleep(0.4)
        if code is None:
            if time.time() >= deadline:
                try:
                    proc.kill()
                except Exception:
                    pass
                return False, f"mimo run 超时（{timeout}s）", None
            # early completion: reap the lingering process
            code = 0
            try:
                proc.kill()
            except Exception:
                pass
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        raw = "".join(out_buf)
        err_text = ANSI_RE.sub("", "".join(err_buf)).strip()

        sid_out, reply = parse_mimo_json_events(raw)
        if reply:
            return code == 0, reply, sid_out
        model_errors = parse_mimo_errors(raw)
        if model_errors:
            return False, f"模型错误({model})：" + " / ".join(model_errors[:3]), sid_out
        fallback = parse_mimo_default_output(raw)
        if fallback:
            return code == 0, fallback, sid_out
        if err_text:
            return False, err_text, sid_out
        return False, f"mimo run 无文本输出 (exit={code}, model={model})", sid_out
    finally:
        if task_id:
            with RUNNING_LOCK:
                RUNNING_PROCS.pop(task_id, None)


def run_mimo_agent(text: str, timeout: int = 300) -> tuple[bool, str]:
    """Execute phone text in the phone's own dedicated session.

    Sharing the session the user has open in MiMo Desktop makes the headless run
    finish with no output at all, so the phone keeps a session of its own; the
    first run creates it. Say 「查看电脑端历史」 on the board to read the desktop
    conversation instead.
    """
    sid = read_phone_session_id()
    ok, reply = False, ""
    for attempt in (0, 1):
        ok, reply, sid_out = run_agent_once(text, sid, timeout)
        if sid_out:
            write_phone_session_id(sid_out)
        if reply:
            return ok, reply
        sid = None  # retry once in a brand-new session
        if attempt == 0:
            time.sleep(2)
    return ok, reply


CHAT_TASKS: dict[str, dict] = {}
CHAT_LOCK = threading.Lock()
RUNNING_PROCS: dict[str, Any] = {}
RUNNING_LOCK = threading.Lock()
CHAT_DIR_NAME = "chat_tasks"


def chat_task_dir() -> Path:
    return _HOME / CHAT_DIR_NAME


def save_chat_task(task: dict) -> None:
    """Persist so a bridge restart does not strand the app at 'thinking'."""
    try:
        ensure_dirs()
        d = chat_task_dir()
        d.mkdir(parents=True, exist_ok=True)
        write_json(d / (str(task.get("id") or "task") + ".json"), task)
    except Exception:
        pass


def load_chat_task(task_id: str) -> dict | None:
    if not re.fullmatch(r"chat_[A-Za-z0-9_]+", task_id or ""):
        return None
    return read_json(chat_task_dir() / (task_id + ".json"), None)


def reconcile_chat_tasks() -> None:
    """Mark in-flight tasks as interrupted at startup; they cannot finish now."""
    d = chat_task_dir()
    if not d.exists():
        return
    for p in d.glob("*.json"):
        task = read_json(p, None)
        if not isinstance(task, dict) or task.get("status") != "running":
            continue
        task["status"] = "error"
        task["reply"] = task.get("reply") or "服务重启，任务已中断，请重新发送。"
        task["done_at"] = now_iso()
        write_json(p, task)


RISK_PATTERNS = [
    (r"\brm\s+-|删除文件|删掉文件|清空目录", "删除文件"),
    (r"taskkill|shutdown|format\s|格式化磁盘", "系统级命令"),
    (r"git\s+push|git\s+reset\s+--hard|git\s+clean", "git 强推/清理"),
    (r"drop\s+table|truncate\s+table|delete\s+from\s", "数据库删除"),
    (r"--dangerously-skip-permissions|sudo\s", "提权/跳过权限"),
    (r"强制删除|删库|清库|卸载", "破坏性指令"),
]


def classify_risk(text: str) -> str:
    hits = [label for pat, label in RISK_PATTERNS if re.search(pat, text or "", re.I)]
    seen: list[str] = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return "、".join(seen)


def start_chat(session_id: str, text: str, force: bool = False) -> str:
    """Kick off a prompt against an arbitrary session; returns a task id.

    Risky instructions pause in `awaiting_approval` so the phone can say
    allow/deny before anything touches the machine.
    """
    if not re.fullmatch(r"ses_[A-Za-z0-9]+", session_id or ""):
        raise RuntimeError(f"非法会话 id：{session_id!r}")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("消息不能为空")
    risk = classify_risk(text)
    gated = bool(risk) and not force
    task_id = new_id("chat")
    with CHAT_LOCK:
        CHAT_TASKS[task_id] = {
            "id": task_id,
            "sid": session_id,
            "text": text,
            "status": "awaiting_approval" if gated else "running",
            "reply": "" if gated else "",
            "risk": risk,
            "events": [],
            "started_at": now_iso(),
            "done_at": "" if gated else "",
        }
    save_chat_task(CHAT_TASKS[task_id])
    push_event(task_id, {"kind": "risk" if gated else "step", "text": ("风险确认：" + risk) if gated else "开始执行"})
    if gated:
        return task_id

    def _run(task_id_: str, sid_: str, text_: str) -> None:
        try:
            # Same session, no fallback to a fresh one: the user picked this thread.
            ok, reply, _ = run_agent_once(text_, sid_, 300, task_id=task_id_)
        except Exception as e:
            ok, reply = False, f"执行失败：{e}"
        with CHAT_LOCK:
            task = CHAT_TASKS.get(task_id_) or {}
            task["status"] = "done" if ok else "error"
            task["reply"] = reply
            task["done_at"] = now_iso()
            CHAT_TASKS[task_id_] = task
        save_chat_task(task)

    threading.Thread(target=_run, args=(task_id, session_id, text), daemon=True).start()
    return task_id


def get_chat_task(task_id: str) -> dict | None:
    with CHAT_LOCK:
        task = CHAT_TASKS.get(task_id)
        if task:
            return dict(task)
    return load_chat_task(task_id)


def _finish_task(task_id_: str, ok: bool, reply: str) -> None:
    with CHAT_LOCK:
        t = CHAT_TASKS.get(task_id_) or {}
        if t.get("status") in ("cancelled", "rejected"):
            return
        t["status"] = "done" if ok else "error"
        t["reply"] = reply
        t["done_at"] = now_iso()
        CHAT_TASKS[task_id_] = t
    save_chat_task(t)


def cancel_chat(task_id: str) -> dict:
    """Stop a running task, or void one that is waiting for approval."""
    task = get_chat_task(task_id)
    if not task:
        raise RuntimeError("任务不存在")
    status = task.get("status")
    if status not in ("running", "awaiting_approval"):
        raise RuntimeError(f"任务状态不允许取消：{status}")
    with RUNNING_LOCK:
        proc = RUNNING_PROCS.get(task_id)
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    note = "已取消（执行被中断）" if status == "running" else "已取消（未执行）"
    with CHAT_LOCK:
        if task_id in CHAT_TASKS:
            CHAT_TASKS[task_id]["status"] = "cancelled"
            CHAT_TASKS[task_id]["reply"] = note
            CHAT_TASKS[task_id]["done_at"] = now_iso()
    save_chat_task(get_chat_task(task_id) or task)
    push_event(task_id, {"kind": "step", "text": "已取消"})
    return get_chat_task(task_id) or task


def list_chat_tasks(limit: int = 30) -> list[dict]:
    """Newest-first task cards for the phone (memory + persisted)."""
    items: list[dict] = []
    d = chat_task_dir()
    if d.exists():
        for p in d.glob("*.json"):
            if p.name.endswith(".events.jsonl"):
                continue
            t = read_json(p, None)
            if isinstance(t, dict) and t.get("id"):
                items.append(t)
    with CHAT_LOCK:
        for tid, t in CHAT_TASKS.items():
            items = [x for x in items if x.get("id") != tid]
            items.append(dict(t))
    items.sort(key=lambda x: str(x.get("started_at") or ""), reverse=True)
    out = []
    for t in items[: max(1, min(int(limit or 30), 100))]:
        out.append({
            "id": t.get("id"),
            "sid": t.get("sid"),
            "status": t.get("status"),
            "risk": t.get("risk") or "",
            "text": (t.get("text") or "")[:120],
            "reply": (t.get("reply") or "")[:160],
            "started_at": t.get("started_at"),
            "done_at": t.get("done_at"),
            "events": len(t.get("events") or []),
        })
    return out


def approve_chat(task_id: str) -> dict:
    """Release a task that paused for risk confirmation."""
    task = get_chat_task(task_id)
    if not task:
        raise RuntimeError("任务不存在")
    if task.get("status") != "awaiting_approval":
        raise RuntimeError(f"任务状态不允许确认：{task.get('status')}")
    with CHAT_LOCK:
        if task_id in CHAT_TASKS:
            CHAT_TASKS[task_id]["status"] = "running"
    save_chat_task(get_chat_task(task_id) or task)
    push_event(task_id, {"kind": "step", "text": "已允许，开始执行"})
    sid = str(task.get("sid") or "")
    text = str(task.get("text") or "")

    def _run(task_id_: str, sid_: str, text_: str) -> None:
        try:
            ok, reply, _ = run_agent_once(text_, sid_, 300, task_id=task_id_)
        except Exception as e:
            ok, reply = False, f"执行失败：{e}"
        _finish_task(task_id_, ok, reply)

    threading.Thread(target=_run, args=(task_id, sid, text), daemon=True).start()
    return get_chat_task(task_id) or task


def reject_chat(task_id: str) -> dict:
    task = get_chat_task(task_id)
    if not task:
        raise RuntimeError("任务不存在")
    if task.get("status") != "awaiting_approval":
        raise RuntimeError(f"任务状态不允许拒绝：{task.get('status')}")
    reply = "已拒绝执行（风险：" + (task.get("risk") or "?") + "）"
    with CHAT_LOCK:
        if task_id in CHAT_TASKS:
            CHAT_TASKS[task_id]["status"] = "rejected"
            CHAT_TASKS[task_id]["reply"] = reply
            CHAT_TASKS[task_id]["done_at"] = now_iso()
    save_chat_task(get_chat_task(task_id) or task)
    push_event(task_id, {"kind": "step", "text": "已拒绝执行"})
    return get_chat_task(task_id) or task


SESSION_LIST_TTL = 20.0
HISTORY_TTL = 20.0
MSG_CHAR_CAP = 6000
_SESSION_CACHE: dict[str, Any] = {"at": 0.0, "items": []}
_HISTORY_CACHE: dict[str, tuple[float, dict]] = {}


def run_mimo_cli(args: list[str], timeout: int = 180) -> str:
    """Run a read-only mimo CLI subcommand and return combined output."""
    workdir = str(WORKDIR if WORKDIR.exists() else Path.cwd())
    env = os.environ.copy()
    env.setdefault("MIMOCODE_DISABLE_CRON", "1")
    proc = subprocess.run(
        [*resolve_mimo(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=workdir,
        env=env,
    )
    out = ANSI_RE.sub("", proc.stdout or "")
    if proc.returncode != 0 and not out.strip():
        err = ANSI_RE.sub("", proc.stderr or "").strip()
        raise RuntimeError(err or f"mimo {' '.join(args)} exit={proc.returncode}")
    return out


def list_sessions(force: bool = False) -> list[dict]:
    """Recent MiMo sessions, newest first, parsed from `mimo session list`."""
    now = time.time()
    cached = _SESSION_CACHE.get("items") or []
    if not force and cached and now - float(_SESSION_CACHE.get("at") or 0) < SESSION_LIST_TTL:
        return cached
    items: list[dict] = []
    for line in run_mimo_cli(["session", "list"], timeout=90).splitlines():
        m = re.match(r"\s*(ses_[A-Za-z0-9]+)\s{2,}(.*?)(?:\s{2,}(\S.*?))?\s*$", line)
        if not m:
            continue
        items.append(
            {
                "id": m.group(1),
                "title": (m.group(2) or "").strip() or "(无标题)",
                "updated": (m.group(3) or "").strip(),
            }
        )
    if items:
        _SESSION_CACHE["at"] = now
        _SESSION_CACHE["items"] = items
    return items or cached


def default_history_session() -> str:
    """The session to open first: the one the desktop side was last using."""
    desktop_sid = read_session_id()
    if desktop_sid:
        return desktop_sid
    phone_sid = read_phone_session_id()
    sessions = list_sessions()
    for item in sessions:
        if item.get("id") != phone_sid:
            return str(item["id"])
    return str(sessions[0]["id"]) if sessions else ""


def summarize_message(msg: dict) -> dict | None:
    """Flatten one message for phone reading: real text plus a compact tool line."""
    info = msg.get("info") or {}
    texts: list[str] = []
    tools: list[str] = []
    files: list[str] = []
    for part in msg.get("parts") or []:
        if part.get("synthetic"):
            continue
        kind = part.get("type")
        if kind == "text":
            body = (part.get("text") or "").strip()
            if body:
                texts.append(body)
        elif kind == "tool":
            state = part.get("state") or {}
            label = str(part.get("tool") or "tool")
            title = str(state.get("title") or state.get("status") or "").strip()
            tools.append(f"{label}{(' · ' + title) if title else ''}")
        elif kind == "file":
            files.append(str(part.get("filename") or part.get("url") or "文件"))
    body = "\n\n".join(texts).strip()
    if files:
        body = (body + "\n" if body else "") + " ".join(f"[附件] {f}" for f in files)
    if not body and not tools:
        return None
    if len(body) > MSG_CHAR_CAP:
        body = body[:MSG_CHAR_CAP] + "\n…（本条已截断）"
    return {
        "role": info.get("role") or "?",
        "time": (info.get("time") or {}).get("created"),
        "text": body,
        "tools": tools[:8],
        "tool_count": len(tools),
    }


def load_session_history(sid: str, limit: int = 40, force: bool = False) -> dict:
    """Transcript of any MiMo session via `mimo export` (cached, newest tail)."""
    if not re.fullmatch(r"ses_[A-Za-z0-9]+", sid or ""):
        raise RuntimeError(f"非法会话 id：{sid!r}")
    now = time.time()
    cached = _HISTORY_CACHE.get(sid)
    if not force and cached and now - cached[0] < HISTORY_TTL:
        data = cached[1]
    else:
        raw = run_mimo_cli(["export", sid])
        start = raw.find("{")
        if start < 0:
            raise RuntimeError(f"export {sid} 未返回 JSON：{raw.strip()[:200]}")
        payload = json.loads(raw[start:])
        meta = payload.get("info") or {}
        stamp = meta.get("time") or {}
        data = {
            "session": {
                "id": meta.get("id") or sid,
                "title": meta.get("title") or "(无标题)",
                "directory": meta.get("directory") or "",
                "updated": stamp.get("updated"),
            },
            "messages": [
                row
                for row in (summarize_message(m) for m in (payload.get("messages") or []))
                if row
            ],
        }
        _HISTORY_CACHE[sid] = (now, data)
    limit = max(1, min(int(limit or 40), 200))
    messages = data["messages"]
    return {
        "session": data["session"],
        "total": len(messages),
        "shown": min(limit, len(messages)),
        "messages": messages[-limit:],
    }


def _safe_path(rel: str) -> Path:
    """Resolve a phone-supplied path inside the workspace; reject escapes."""
    root = WORKDIR.resolve()
    rel = (rel or "").replace("\\", "/").strip("/")
    target = (root / rel).resolve() if rel else root
    if target != root and root not in target.parents:
        raise RuntimeError("路径越界：仅允许访问工作目录内")
    return target


def fs_list(rel: str = "") -> dict:
    p = _safe_path(rel)
    if not p.is_dir():
        raise RuntimeError("不是目录：" + (rel or "/"))
    entries = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            st = child.stat()
        except Exception:
            continue
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "size": st.st_size,
            "mtime": int(st.st_mtime * 1000),
        })
    return {"path": rel or "", "root": str(WORKDIR), "entries": entries}


def fs_read(rel: str, limit: int = 60000) -> dict:
    p = _safe_path(rel)
    if not p.is_file():
        raise RuntimeError("不是文件：" + (rel or ""))
    size = p.stat().st_size
    cap = max(200, min(int(limit or 60000), 200000))
    raw = p.read_bytes()[:cap]
    return {
        "path": rel,
        "size": size,
        "truncated": size > len(raw),
        "content": raw.decode("utf-8", "replace"),
    }


AUTH_TOKEN_FILE = _HOME / "auth_token.txt"
AUTH_FAILS: dict[str, list] = {}
AUTH_FAIL_LIMIT = 10
AUTH_FAIL_WINDOW = 180.0


def get_auth_token() -> str:
    """Token for non-loopback clients. Generated once; overridable by env."""
    env_tok = (os.environ.get("PHONE_REMOTE_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    try:
        tok = AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except Exception:
        pass
    tok = secrets.token_hex(20)
    try:
        ensure_dirs()
        AUTH_TOKEN_FILE.write_text(tok, encoding="utf-8")
        try:
            os.chmod(AUTH_TOKEN_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    return tok


def client_key(client_ip: str, headers: Any) -> str:
    """Identity used for rate limiting.

    Tunnel traffic (cloudflared etc.) all arrives from 127.0.0.1, so the real
    client must come from the forwarded header — but only trust that header when
    the peer is genuinely local, otherwise anyone could spoof it to dodge the
    lockout.
    """
    ip = str(client_ip or "")
    if ip in ("127.0.0.1", "::1", "localhost"):
        for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
            v = (headers.get(h) or "").split(",")[0].strip()
            if v:
                return v
    return ip


def auth_locked(ip: str) -> bool:
    now = time.time()
    fails = [t for t in AUTH_FAILS.get(ip, []) if now - t < AUTH_FAIL_WINDOW]
    AUTH_FAILS[ip] = fails
    return len(fails) >= AUTH_FAIL_LIMIT


def auth_fail(ip: str) -> None:
    AUTH_FAILS.setdefault(ip, []).append(time.time())


def client_is_local(client_ip: str, headers: Any) -> bool:
    """Loopback counts as local ONLY when the request is genuinely local.

    Tunnels (cloudflared etc.) connect from 127.0.0.1 too, so a proxied request
    must NOT inherit the loopback exemption — fail closed: any forwarded /
    foreign Host is treated as public and needs a token.
    """
    if str(client_ip or "") not in ("127.0.0.1", "::1", "localhost"):
        return False
    for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP", "Forwarded"):
        if (headers.get(h) or "").strip():
            return False
    host = (headers.get("Host") or "").split(":")[0].strip().lower()
    if host and host not in ("127.0.0.1", "localhost", "::1"):
        return False
    return True


def check_auth(client_ip: str, headers: Any, qs: dict) -> bool:
    """Local machine is trusted; everything else needs a bearer/session token.

    A correct token always works — lockout only stops repeated wrong guesses,
    otherwise a single misconfigured client can lock out its own owner.
    """
    ip = str(client_ip or "")
    if client_is_local(ip, headers):
        return True
    key = client_key(ip, headers)
    supplied = ""
    auth = headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = (headers.get("X-Auth-Token") or "").strip()
    if not supplied:
        supplied = ((qs.get("token") or [""])[0]).strip()
    expected = get_auth_token()
    if supplied and hmac.compare_digest(supplied, expected):
        AUTH_FAILS.pop(key, None)
        return True
    if supplied and session_token_ok(supplied):
        AUTH_FAILS.pop(key, None)
        return True
    if auth_locked(key):
        return False
    auth_fail(key)
    return False


PAIR_FILE = _HOME / "pair_code.txt"
PAIR_USED_FILE = _HOME / "pair_code.used"
PAIR_TTL = 600.0


def new_pair_code() -> str:
    """Short human-typable code to install the long token on a phone."""
    code = "%06d" % secrets.randbelow(1000000)
    try:
        ensure_dirs()
        PAIR_FILE.write_text("%s|%s" % (code, time.time() + PAIR_TTL), encoding="utf-8")
    except Exception:
        pass
    return code


def peek_pair_code() -> str:
    """Return the live pairing code without minting a new one."""
    try:
        raw = PAIR_FILE.read_text(encoding="utf-8").strip()
        code, exp = raw.split("|", 1)
        if time.time() <= float(exp):
            return code
    except Exception:
        pass
    return ""


DEVICES_FILE = _HOME / "devices.json"
SESSIONS_FILE = _HOME / "sessions.json"
SESSION_TTL = 30 * 24 * 3600.0


def issue_device() -> dict:
    """One-time pairing installs a device credential (never used as an API token)."""
    device_id = secrets.token_hex(8)
    device_secret = secrets.token_hex(24)
    devices = read_json(DEVICES_FILE, {})
    devices[device_id] = {
        "secret_hash": hashlib.sha256(device_secret.encode("utf-8")).hexdigest(),
        "created": now_iso(),
        "last_seen": now_iso(),
    }
    write_json(DEVICES_FILE, devices)
    return {"device_id": device_id, "device_secret": device_secret}


def mint_session(device_id: str, device_secret: str) -> dict:
    """Per-launch random token; the device's previous token is rotated out."""
    devices = read_json(DEVICES_FILE, {})
    rec = devices.get(device_id or "")
    if not rec:
        raise RuntimeError("设备未配对，请重新输入配对码")
    digest = hashlib.sha256((device_secret or "").encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, str(rec.get("secret_hash") or "")):
        raise RuntimeError("设备凭据不正确")
    # Store only a hash: a leaked sessions.json must not yield usable tokens.
    token = secrets.token_hex(24)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now_ts = time.time()
    sessions = {
        k: v for k, v in read_json(SESSIONS_FILE, {}).items()
        if len(k) == 64 and float(v.get("expires") or 0) > now_ts
    }
    # Keep a few recent tokens per device: minting must stay per-launch random,
    # but invalidating the previous one instantly races with concurrent refreshes.
    mine = [(k, v) for k, v in sessions.items() if v.get("device_id") == device_id]
    mine.sort(key=lambda kv: float(kv[1].get("expires") or 0))
    while len(mine) >= 4:
        stale, _ = mine.pop(0)
        sessions.pop(stale, None)
    sessions[token_hash] = {
        "device_id": device_id,
        "expires": now_ts + SESSION_TTL,
        "created": now_iso(),
    }
    write_json(SESSIONS_FILE, sessions)
    rec["last_seen"] = now_iso()
    devices[device_id] = rec
    write_json(DEVICES_FILE, devices)
    return {"token": token, "expires_in": int(SESSION_TTL)}


def session_token_ok(supplied: str) -> bool:
    if not supplied:
        return False
    token_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    rec = read_json(SESSIONS_FILE, {}).get(token_hash)
    if not rec:
        return False
    return time.time() <= float(rec.get("expires") or 0)


BRIDGE_DIR = Path(__file__).resolve().parent
QR_DIR = _HOME / "qr"
APP_FILE = WORKDIR / "index.html"


def find_qrtool() -> tuple[str, str]:
    """Locate qr.js and the cwd node should run in (repo layout or legacy)."""
    for js, cwd in (
        (BRIDGE_DIR / "qr.js", BRIDGE_DIR),
        (_HOME / "qrtool" / "qr.js", _HOME / "qrtool"),
    ):
        if js.is_file():
            return str(js), str(cwd)
    raise RuntimeError("未找到 qr.js：请确认仓库完整（bridge/qr.js），并在 bridge 目录执行 npm install")


def tailscale_ip() -> str:
    """Tailscale IPv4 if the client is up — reachable at home AND outside."""
    exe = shutil_which("tailscale") or shutil_which("tailscale.exe")
    if not exe:
        return ""
    try:
        proc = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=4)
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("100."):
                return line.split()[0]
    except Exception:
        pass
    return ""


TUNNEL_FILE = _HOME / "tunnel.json"


def find_cloudflared() -> str:
    """Locate cloudflared: env override, bundled, then PATH."""
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    for cand in (
        os.environ.get("PHONE_REMOTE_CLOUDFLARED") or "",
        str(_HOME / "tunnel-bin" / name),
        str(BRIDGE_DIR / "node_modules" / ".bin" / ("cloudflared.cmd" if os.name == "nt" else "cloudflared")),
    ):
        if cand and Path(cand).exists():
            return cand
    return shutil_which("cloudflared") or shutil_which("cloudflared.exe") or ""


def tunnel_status() -> dict:
    info = read_json(TUNNEL_FILE, {})
    pid = int(info.get("pid") or 0)
    alive = pid > 0 and pid_alive(pid)
    return {
        "running": alive,
        "url": str(info.get("url") or "") if alive else "",
        "pid": pid if alive else 0,
        "cloudflared": bool(find_cloudflared()),
    }


def ensure_cloudflared() -> str:
    """Find cloudflared, or install it on first use (npm, then direct download).

    Keeps the repo binary-free: a fresh clone just works when the user runs
    `tunnel on` - nothing to pre-install.
    """
    exe = find_cloudflared()
    if exe:
        return exe
    ensure_dirs()
    npm = shutil_which("npm") or shutil_which("npm.cmd")
    if npm:
        try:
            subprocess.run(
                [npm, "install", "cloudflared", "--no-fund", "--no-audit"],
                cwd=str(BRIDGE_DIR),
                capture_output=True,
                timeout=600,
            )
        except Exception:
            pass
        exe = find_cloudflared()
        if exe:
            return exe
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    url = (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        if os.name == "nt"
        else "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    )
    dest = _HOME / "tunnel-bin" / name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as f:
            f.write(resp.read())
        if os.name != "nt":
            os.chmod(dest, 0o755)
        return str(dest)
    except Exception as e:
        raise RuntimeError(
            f"未找到 cloudflared，自动下载也失败：{e}。可手动安装后重试：npm i -g cloudflared 或 winget install cloudflared"
        )


def start_tunnel() -> dict:
    """Cloudflare quick tunnel -> a public https URL (needs a token to use)."""
    exe = ensure_cloudflared()
    st = tunnel_status()
    if st["running"]:
        return st
    ensure_dirs()
    port = current_port()
    log_path = _HOME / "tunnel.log"
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    url = ""
    deadline = time.time() + 40
    with log_path.open("a", encoding="utf-8") as logf:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            logf.write(line)
            logf.flush()
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                url = m.group(0)
                break
    if not url:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("隧道建立失败（可能被代理/VPN 掐断），详见 " + str(log_path))
    write_json(TUNNEL_FILE, {"url": url, "pid": proc.pid, "started": now_iso()})
    return {"running": True, "url": url, "pid": proc.pid, "cloudflared": True}


def stop_tunnel() -> dict:
    info = read_json(TUNNEL_FILE, {})
    pid = int(info.get("pid") or 0)
    if pid > 0:
        try:
            if os.name == "nt":
                os.system(f"taskkill /PID {pid} /F >NUL 2>&1")
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    try:
        TUNNEL_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return {"running": False, "url": ""}


def pair_url(code: str, port: int | None = None) -> str:
    port = int(port or current_port())
    tun = tunnel_status()
    if tun.get("running") and tun.get("url"):
        base = str(tun["url"]).rstrip("/")
    else:
        ts = tailscale_ip()
        if ts:
            base = f"http://{ts}:{port}"
        else:
            urls = lan_urls(port)
            base = next((u for u in urls if not u.startswith("http://127.")), (urls[0] if urls else f"http://127.0.0.1:{port}"))
    return f"{base}/app#pair={code}"


def render_pair_qr(port: int | None = None) -> dict:
    """Reuse or mint a pairing code, then render its QR (URL carries the code)."""
    code = peek_pair_code() or new_pair_code()
    url = pair_url(code, port)
    QR_DIR.mkdir(parents=True, exist_ok=True)
    out = QR_DIR / "pair.png"
    node = shutil_which("node") or "node"
    js, cwd = find_qrtool()
    # cwd holds qr.js and node_modules, so require('qrcode') resolves there
    proc = subprocess.run(
        [node, js, url, str(out)],
        capture_output=True,
        timeout=30,
        cwd=cwd,
    )
    if proc.returncode != 0 or not out.exists():
        err = ANSI_RE.sub("", (proc.stderr or b"").decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or ""))
        raise RuntimeError("二维码生成失败：" + (err.strip()[:200] or "unknown"))
    return {"code": code, "url": url, "png": str(out)}


def redeem_pair_code(supplied: str) -> dict:
    """Consume a pairing code (one-time) and issue a fresh device credential."""
    supplied = (supplied or "").strip()
    try:
        raw = PAIR_FILE.read_text(encoding="utf-8").strip()
        code, exp = raw.split("|", 1)
    except Exception:
        # No live code: tell the user *why* (already used vs expired).
        try:
            used = PAIR_USED_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            used = ""
        if supplied and used and hmac.compare_digest(supplied, used):
            raise RuntimeError("该配对码已使用过（一码一用），请重新获取一个新码")
        raise RuntimeError("配对码无效或已过期，请重新获取")
    if time.time() > float(exp):
        raise RuntimeError("配对码已过期（10 分钟内有效），请重新获取")
    if not (supplied and hmac.compare_digest(supplied, code)):
        raise RuntimeError("配对码不正确，请核对或重新获取")
    try:
        PAIR_FILE.unlink(missing_ok=True)
        PAIR_USED_FILE.write_text(code, encoding="utf-8")
    except Exception:
        pass
    return issue_device()


def mark_processing(item_id: str) -> None:
    board = load_board()
    for m in board.get("messages") or []:
        if m.get("id") == item_id:
            m["status"] = "processing"
            m["updated_at"] = now_iso()
            break
    write_board(board)


def process_pending_once() -> dict:
    """Process all current inbox items serially via mimo run."""
    ensure_dirs()
    items = list_pending()
    results = []
    for item in items:
        item_id = item.get("id") or ""
        text = (item.get("text") or "").strip()
        if not item_id or not text:
            continue
        mark_processing(item_id)
        ok, reply = run_mimo_agent(text)
        try:
            complete_item(item_id, "done" if ok else "error", reply)
            results.append({"id": item_id, "ok": ok, "text": text[:80], "reply_preview": reply[:120]})
        except Exception as e:
            results.append({"id": item_id, "ok": False, "error": str(e)})
    return {"processed": len(results), "results": results, "pending_count": count_pending()}


def _worker_loop() -> None:
    global _WORKER_STARTED
    try:
        # small delay so HTTP response returns first
        time.sleep(0.2)
        if auto_enabled():
            process_pending_once()
    except Exception as e:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{now_iso()} worker_error {e}\n")
        except Exception:
            pass
    finally:
        with WORKER_LOCK:
            _WORKER_STARTED = False


def kick_worker() -> None:
    """Start background auto-processor if enabled and not already running."""
    global _WORKER_STARTED
    if not auto_enabled():
        return
    with WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
    threading.Thread(target=_worker_loop, name="phone-remote-worker", daemon=True).start()


def enqueue(text: str, source: str = "phone") -> dict:
    ensure_dirs()
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")
    if source not in ("phone", "desktop"):
        source = "phone"
    item = {
        "id": new_id("req"),
        "role": "user",
        "source": source,
        "text": text,
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    write_json(INBOX / f"{item['id']}.json", item)
    upsert_board_message(item)
    return item


def list_pending() -> list[dict]:
    ensure_dirs()
    items = []
    for p in sorted(INBOX.glob("*.json")):
        data = read_json(p, None)
        if isinstance(data, dict) and data.get("text"):
            items.append(data)
    return items


def complete_item(item_id: str, status: str, text: str) -> dict:
    ensure_dirs()
    item_id = (item_id or "").strip()
    status = "done" if status not in ("done", "error") else status
    text = text or ""
    inbox_path = INBOX / f"{item_id}.json"
    req = read_json(inbox_path, None)
    if not isinstance(req, dict):
        # allow completing from board-only ids
        board = load_board()
        req = next((m for m in board.get("messages", []) if m.get("id") == item_id), None)
        if not isinstance(req, dict):
            raise FileNotFoundError(f"inbox item not found: {item_id}")

    req["status"] = "done" if status == "done" else "done"  # request closed
    req["updated_at"] = now_iso()
    if inbox_path.exists():
        out = OUTBOX / inbox_path.name
        write_json(out, {**req, "status": "done", "completed_at": now_iso()})
        inbox_path.unlink(missing_ok=True)

    reply = {
        "id": f"{item_id}__reply",
        "parent_id": item_id,
        "role": "assistant",
        "source": "mimo",
        "text": text if text.strip() else ("（无输出）" if status == "done" else "处理失败"),
        "status": status,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    # mark original user message done as well
    upsert_board_message({
        "id": item_id,
        "role": "user",
        "source": req.get("source", "phone"),
        "text": req.get("text", ""),
        "status": "done",
        "created_at": req.get("created_at"),
        "updated_at": now_iso(),
    })
    upsert_board_message(reply)
    return reply


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        # Windows: signal 0 may not work the same; try OpenProcess via ctypes
        if os.name == "nt":
            try:
                import ctypes

                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
            except Exception:
                return False
        return False


def read_pid() -> int:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return -1


def current_port() -> int:
    # Prefer port recorded by a live server
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{DEFAULT_PORT}/api/status", method="GET")
        with urllib.request.urlopen(req, timeout=0.4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return int(data.get("port") or DEFAULT_PORT)
    except Exception:
        pass
    pid = read_pid()
    meta = read_json(_HOME / "server_meta.json", {})
    if pid_alive(pid) and isinstance(meta, dict) and meta.get("port"):
        try:
            return int(meta["port"])
        except Exception:
            return DEFAULT_PORT
    return DEFAULT_PORT


def http_json(url: str, payload: dict | None = None, timeout: float = 3.0) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


class Handler(BaseHTTPRequestHandler):
    server_version = "PhoneRemote/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{now_iso()} {self.address_string()} {fmt % args}\n")
        except Exception:
            pass

    def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, code: int, data: Any) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _qs(self) -> dict:
        if "?" not in self.path:
            return {}
        return parse_qs(self.path.split("?", 1)[1])

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        qs = self._qs()
        if path in ("/app", "/app/index.html"):
            # Static app shell is public: the QR's #pair= code is the credential.
            if APP_FILE.is_file():
                self._send(200, APP_FILE.read_bytes(), "text/html; charset=utf-8")
            else:
                self._json(404, {"ok": False, "error": "未找到 index.html"})
            return
        if path == "/pair":
            if not client_is_local(self.client_address[0], self.headers):
                self._json(403, {"ok": False, "error": "配对页只能在电脑本机打开"})
                return
            try:
                info = render_pair_qr(int(self.server.server_address[1]))
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            html = (
                "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<title>MiMo 配对</title><style>"
                "body{margin:0;background:#0A0C10;color:#EDF0F5;font:15px/1.5 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
                "display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh}"
                ".card{background:#141A24;border:1px solid #232A38;border-radius:18px;padding:24px 22px;text-align:center;max-width:420px}"
                "h1{font-size:18px;margin:0 0 6px}p{color:#8B93A7;font-size:13px;margin:0 0 16px}"
                "img{width:280px;height:280px;border-radius:12px;background:#fff;padding:10px}"
                ".code{font-size:34px;letter-spacing:8px;font-weight:800;color:#FF6A00;margin:16px 0 4px}"
                ".url{font-size:11px;color:#8B93A7;word-break:break-all;margin-top:10px}"
                "</style></head><body><div class='card'>"
                "<h1>手机扫码即可连接</h1>"
                "<p>用手机相机扫描下方二维码 → 自动打开 App 并配对</p>"
                f"<img src='/api/pair/qr.png?ts={int(time.time())}' alt='pair qr'>"
                f"<div class='code'>{info['code']}</div>"
                "<p style='margin:0'>配对码 10 分钟内有效 · 一码一用</p>"
                f"<div class='url'>{info['url']}</div>"
                "</div></body></html>"
            )
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/pair/qr.png":
            if not client_is_local(self.client_address[0], self.headers):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                info = render_pair_qr(int(self.server.server_address[1]))
                data = Path(info["png"]).read_bytes()
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            self._send(200, data, "image/png")
            return
        if not check_auth(self.client_address[0], self.headers, qs):
            self._json(401, {"ok": False, "error": "unauthorized", "hint": "需要令牌：Authorization: Bearer <token> 或 ?token="})
            return
        if path in ("/", "/index.html"):
            self._send(200, HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/history":
            sid = (self._qs().get("sid") or [""])[0]
            page = HISTORY_PAGE.replace("__SID__", sid)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/fs/list":
            try:
                data = fs_list((self._qs().get("path") or [""])[0])
                data["ok"] = True
                self._json(200, data)
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
            return
        if path == "/api/fs/read":
            try:
                data = fs_read((self._qs().get("path") or [""])[0])
                data["ok"] = True
                self._json(200, data)
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
            return
        if path == "/api/tasks":
            try:
                items = list_chat_tasks(int((self._qs().get("limit") or ["30"])[0] or 30))
            except Exception as e:
                self._json(500, {"error": str(e), "tasks": []})
                return
            self._json(200, {"ok": True, "tasks": items})
            return
        if path == "/api/chat/status":
            task_id = (self._qs().get("task_id") or [""])[0]
            task = get_chat_task(task_id)
            if not task:
                self._json(404, {"ok": False, "error": "任务不存在"})
                return
            task["ok"] = True
            self._json(200, task)
            return
        if path == "/api/models":
            try:
                items = list_available_models(force=bool(self._qs().get("force")))
            except Exception as e:
                self._json(500, {"error": str(e), "models": [current_model()]})
                return
            self._json(200, {
                "models": items,
                "current": current_model(),
                "default": DEFAULT_MODEL,
            })
            return
        if path == "/api/sessions":
            try:
                items = list_sessions(force=bool(self._qs().get("force")))
            except Exception as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {
                "sessions": items,
                "default": default_history_session(),
                "current": read_session_id(),
            })
            return
        if path == "/api/history":
            qs = self._qs()
            sid = (qs.get("sid") or [""])[0] or read_session_id() or default_history_session()
            if not sid:
                self._json(404, {"error": "没有可读取的会话"})
                return
            try:
                limit = int((qs.get("limit") or ["40"])[0] or 40)
            except ValueError:
                limit = 40
            try:
                data = load_session_history(sid, limit=limit, force=bool(qs.get("force")))
                data["ok"] = True
                self._json(200, data)
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e), "sid": sid})
            return
        if path == "/api/board":
            board = load_board()
            board["pending_count"] = count_pending()
            board["urls"] = lan_urls(int(self.server.server_address[1]))
            board["server"] = "online"
            board["port"] = int(self.server.server_address[1])
            board["auto"] = auto_enabled()
            board["session_id"] = read_session_id()
            self._json(200, board)
            return
        if path == "/api/status":
            self._json(200, {
                "server": "online",
                "port": int(self.server.server_address[1]),
                "home": str(_HOME),
                "pending_count": count_pending(),
                "urls": lan_urls(int(self.server.server_address[1])),
                "pid": os.getpid(),
                "auto": auto_enabled(),
                "session_id": read_session_id(),
                "phone_session_id": read_phone_session_id(),
                "model": current_model(),
                "auth": "on",
                "token_file": str(AUTH_TOKEN_FILE),
                "tailscale": tailscale_ip(),
                "tunnel": tunnel_status(),
                "pair_code": (peek_pair_code() if client_is_local(self.client_address[0], self.headers) else ""),
                "mimo": " ".join(resolve_mimo()),
                "workdir": str(WORKDIR),
            })
            return
        if path == "/api/pending":
            self._json(200, {"pending": list_pending(), "count": count_pending()})
            return
        self._json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        if path == "/api/pair":
            key = client_key(self.client_address[0], self.headers)
            if auth_locked(key):
                self._json(429, {"ok": False, "error": "尝试次数过多，请稍后再试"})
                return
            try:
                device = redeem_pair_code(str(body.get("code") or ""))
                AUTH_FAILS.pop(key, None)
                self._json(200, {"ok": True, "device_id": device["device_id"], "device_secret": device["device_secret"]})
            except Exception as e:
                auth_fail(key)
                self._json(400, {"ok": False, "error": str(e)})
            return
        if path == "/api/session":
            key = client_key(self.client_address[0], self.headers)
            if auth_locked(key):
                self._json(429, {"ok": False, "error": "尝试次数过多，请稍后再试"})
                return
            try:
                sess = mint_session(str(body.get("device_id") or ""), str(body.get("device_secret") or ""))
                AUTH_FAILS.pop(key, None)
                self._json(200, {"ok": True, "token": sess["token"], "expires_in": sess["expires_in"]})
            except Exception as e:
                auth_fail(key)
                self._json(401, {"ok": False, "error": str(e)})
            return
        if path == "/api/pair/code":
            if not client_is_local(self.client_address[0], self.headers):
                self._json(403, {"ok": False, "error": "只能在电脑本机获取配对码"})
                return
            try:
                code = new_pair_code() if bool(body.get("refresh")) else (peek_pair_code() or new_pair_code())
                self._json(200, {"ok": True, "code": code, "ttl": int(PAIR_TTL)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        if path == "/api/login":
            key = client_key(self.client_address[0], self.headers)
            supplied = str(body.get("token") or "").strip()
            expected = get_auth_token()
            if supplied and hmac.compare_digest(supplied, expected):
                AUTH_FAILS.pop(key, None)
                self._json(200, {"ok": True})
                return
            if auth_locked(key):
                self._json(429, {"ok": False, "error": "尝试次数过多，请稍后再试"})
                return
            auth_fail(key)
            self._json(401, {"ok": False, "error": "令牌不正确"})
            return
        if not check_auth(self.client_address[0], self.headers, self._qs()):
            self._json(401, {"ok": False, "error": "unauthorized", "hint": "需要令牌：Authorization: Bearer <token>"})
            return
        if path == "/api/send":
            try:
                item = enqueue(str(body.get("text") or ""), str(body.get("source") or "phone"))
                kick_worker()
                self._json(200, {
                    "ok": True,
                    "item": item,
                    "pending_count": count_pending(),
                    "auto": auto_enabled(),
                })
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
            return
        if path == "/api/chat":
            try:
                task_id = start_chat(
                    str(body.get("sid") or ""),
                    str(body.get("text") or ""),
                    force=bool(body.get("force")),
                )
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            task = get_chat_task(task_id) or {}
            self._json(200, {
                "ok": True,
                "task_id": task_id,
                "status": task.get("status") or "running",
                "risk": task.get("risk") or "",
            })
            return
        if path == "/api/chat/cancel":
            try:
                task = cancel_chat(str(body.get("task_id") or ""))
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            self._json(200, {"ok": True, "task": task})
            return
        if path in ("/api/chat/approve", "/api/chat/reject"):
            try:
                fn = approve_chat if path.endswith("approve") else reject_chat
                task = fn(str(body.get("task_id") or ""))
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            self._json(200, {"ok": True, "task": task})
            return
        if path == "/api/model":
            try:
                model = set_model(str(body.get("model") or ""))
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e), "current": current_model()})
                return
            self._json(200, {"ok": True, "model": model})
            return
        if path == "/api/complete":
            try:
                reply = complete_item(
                    str(body.get("id") or ""),
                    str(body.get("status") or "done"),
                    str(body.get("text") or ""),
                )
                self._json(200, {"ok": True, "reply": reply, "pending_count": count_pending()})
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
            return
        self._json(404, {"error": "not found", "path": path})


def start_server(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> int:
    ensure_dirs()
    reconcile_chat_tasks()
    pid = read_pid()
    if pid_alive(pid):
        print(f"already_running pid={pid}")
        print("urls:")
        for u in lan_urls(current_port() if pid_alive(pid) else port):
            print(u)
        return 0

    httpd = ThreadingHTTPServer((host, port), Handler)
    bound_port = httpd.server_address[1]
    write_json(_HOME / "server_meta.json", {"port": bound_port, "host": host, "pid": os.getpid()})
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    # refresh board metadata
    board = load_board()
    write_board(board)

    print(f"started pid={os.getpid()} port={bound_port} host={host}")
    print("urls:")
    for u in lan_urls(bound_port):
        print(u)
    print(f"home={_HOME}")
    sys.stdout.flush()

    def _stop(signum, frame):  # noqa: ARG001
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        sys.exit(0)

    if os.name != "nt":
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


def stop_server() -> int:
    pid = read_pid()
    if not pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print("not_running")
        return 0
    try:
        if os.name == "nt":
            os.system(f"taskkill /PID {pid} /F >NUL 2>&1")
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        print(f"stop_failed pid={pid} error={e}")
        return 1
    time.sleep(0.3)
    PID_FILE.unlink(missing_ok=True)
    print(f"stopped pid={pid}")
    return 0


def cmd_status() -> int:
    ensure_dirs()
    pid = read_pid()
    alive = pid_alive(pid)
    port = current_port()
    pending = count_pending()
    print(f"running={str(alive).lower()} pid={pid if alive else '-'} port={port} pending={pending} auto={str(auto_enabled()).lower()}")
    print(f"home={_HOME}")
    print(f"session_id={read_session_id() or '-'}")
    print(f"phone_session_id={read_phone_session_id() or '-'}")
    print(f"model={current_model()}")
    print(f"mimo={' '.join(resolve_mimo())}")
    print(f"workdir={WORKDIR}")
    print("urls:")
    for u in lan_urls(port):
        print(u)
    if alive:
        try:
            st = http_json(f"http://127.0.0.1:{port}/api/status", timeout=1.0)
            print(f"server_ok={bool(st)}")
        except Exception as e:
            print(f"server_ok=false error={e}")
            return 1
    return 0


def cmd_pending() -> int:
    ensure_dirs()
    items = list_pending()
    print(json.dumps({"count": len(items), "pending": items}, ensure_ascii=False, indent=2))
    return 0


def cmd_complete(item_id: str, status: str, text: str) -> int:
    reply = complete_item(item_id, status, text)
    print(json.dumps({"ok": True, "reply": reply, "pending_count": count_pending()}, ensure_ascii=False, indent=2))
    return 0


def cmd_board() -> int:
    ensure_dirs()
    board = load_board()
    board["pending_count"] = count_pending()
    board["urls"] = lan_urls(current_port())
    print(json.dumps(board, ensure_ascii=False, indent=2))
    return 0


def cmd_clear_done() -> int:
    ensure_dirs()
    board = load_board()
    messages = [m for m in board.get("messages", []) if m.get("status") not in ("done", "error") or m.get("role") == "assistant"]
    # keep assistants + non-terminal; drop completed user/assistant pairs older than... actually keep all assistant replies and drop nothing by default except user done?
    # Spec: clear completed pairs to reduce clutter — keep last 50 messages
    board["messages"] = messages[-200:]
    write_board(board)
    print(json.dumps({"ok": True, "kept": len(board["messages"])}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiMo phone-remote bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="start LAN HTTP server")
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_start.add_argument("--host", type=str, default=DEFAULT_HOST)

    sub.add_parser("stop", help="stop server")
    sub.add_parser("status", help="server status + URLs")
    sub.add_parser("url", help="print primary LAN URL")
    sub.add_parser("pending", help="list pending inbox items as JSON")
    sub.add_parser("board", help="print board JSON")
    sub.add_parser("clear-done", help="trim board history")
    sub.add_parser("sessions", help="list recent MiMo sessions (for the history page)")
    sub.add_parser("pair", help="print a 6-digit pairing code for the phone app")
    sub.add_parser("qr", help="generate the pairing QR image and print its path")
    sub.add_parser("token", help="print the long access token (keep it private)")
    p_tun = sub.add_parser("tunnel", help="cloudflared public tunnel for out-of-home access")
    p_tun.add_argument("action", choices=["on", "off", "status"])
    p_hist = sub.add_parser("history", help="print a MiMo session transcript as JSON")
    p_hist.add_argument("--session", default="", help="session id (default: newest non-phone session)")
    p_hist.add_argument("--limit", type=int, default=40, help="how many trailing messages to show")
    p_hist.add_argument("--force", action="store_true", help="bypass the 20s cache")
    sub.add_parser("process", help="process pending inbox via mimo run (manual)")
    p_auto = sub.add_parser("auto", help="enable/disable auto-processing")
    p_auto.add_argument("state", choices=["on", "off", "status"])

    p_done = sub.add_parser("complete", help="mark request done and write reply to board")
    p_done.add_argument("--id", required=True)
    p_done.add_argument("--status", default="done", choices=["done", "error"])
    p_done.add_argument("--text", default="")
    p_done.add_argument("--file", default="", help="read reply text from file")

    args = parser.parse_args(argv)
    ensure_dirs()

    if args.cmd == "start":
        return start_server(port=args.port, host=args.host)
    if args.cmd == "stop":
        return stop_server()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "url":
        port = current_port()
        urls = lan_urls(port)
        print(urls[0] if urls else f"http://127.0.0.1:{port}")
        for u in urls:
            if u != (urls[0] if urls else None):
                print(u)
        return 0
    if args.cmd == "pending":
        return cmd_pending()
    if args.cmd == "board":
        return cmd_board()
    if args.cmd == "clear-done":
        return cmd_clear_done()
    if args.cmd == "pair":
        code = new_pair_code()
        print(f"配对码：{code}  （{int(PAIR_TTL)} 秒内有效，在手机 App 登录页输入 6 位数字即可）")
        return 0
    if args.cmd == "qr":
        try:
            info = render_pair_qr()
        except Exception as e:
            print("二维码生成失败：" + str(e))
            return 1
        print(f"配对码：{info['code']}")
        print(f"链接：{info['url']}")
        print(f"二维码图片：{info['png']}")
        return 0
    if args.cmd == "tunnel":
        if args.action == "on":
            try:
                st = start_tunnel()
            except Exception as e:
                print("隧道启动失败：" + str(e))
                return 1
            print(f"公网地址：{st['url']}")
            print("提示：公网链接同样需要配对码/令牌；用完执行 tunnel off 关闭。")
            return 0
        if args.action == "off":
            st = stop_tunnel()
            print("已关闭" if not st.get("running") else "关闭失败")
            return 0
        print(json.dumps(tunnel_status(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "token":
        print(get_auth_token())
        return 0
    if args.cmd == "sessions":
        try:
            items = list_sessions(force=True)
        except Exception as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            return 1
        print(json.dumps({
            "default": default_history_session(),
            "phone_session": read_session_id(),
            "sessions": items,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "history":
        sid = args.session or default_history_session()
        try:
            data = load_session_history(sid, limit=args.limit, force=args.force)
        except Exception as e:
            print(json.dumps({"error": str(e), "sid": sid}, ensure_ascii=False))
            return 1
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "process":
        result = process_pending_once()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "auto":
        if args.state == "status":
            print(f"auto={'on' if auto_enabled() else 'off'}")
            return 0
        set_auto(args.state == "on")
        print(f"auto={args.state}")
        return 0
    if args.cmd == "complete":
        text = args.text
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8")
        return cmd_complete(args.id, args.status, text)
    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
