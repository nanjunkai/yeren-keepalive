#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
野人视频动态补漏器（含语音转文字）
====================================
背景：免 Cookie 公开接口 `/lgt/post/open/api/user/post` 只返回文字动态（group_post_*），
      完全不含视频帖（sns/article 类型）。视频必须走
      `POST /user_center/open/api/content/v2/get_by_uid`，而它要求 hexin-v 动态签名
      （与 Cookie v 同值、5~10 分钟过期、依赖浏览器指纹），纯 HTTP 复现不了。

做法：用真浏览器（Playwright + Chrome）打开博主主页，**截获页面自身发出的
      get_by_uid 响应**——签名由页面自己生成，我们只取结果，完全绕开逆向。

产出：标题 + 网页链接 + 时长 + 点赞数，**并用 faster-whisper 把口播转成文字**，
      一起推送飞书群。

用法：
    FEISHU_HOOK=<webhook> python watch_video.py             # 正常增量推送（含转写）
    python watch_video.py --dry                             # 只打印，不推送
    python watch_video.py --no-asr                          # 不转写，只推标题+链接
    ASR_MODEL=small python watch_video.py                    # 换更小更快的模型
    MAX_ASR_PER_RUN=3 python watch_video.py                  # 限制单轮转写条数
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone

USER_CODE = "yyktkkw6cx88m6b2056ca"
PAGE_URL = f"https://t.10jqka.com.cn/lgt/user_page/?user_code={USER_CODE}"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
CN = timezone(timedelta(hours=8))

# ---- 转写配置 ----
ASR_MODEL = os.environ.get("ASR_MODEL", "medium")
ASR_PROMPT = ("以下是A股散户博主的口播内容，涉及股票、板块、指数、仓位、涨停、"
              "复盘、题材、龙头、主力、缩量、放量、抄底、回撤等股市术语。")
MAX_ASR_PER_RUN = int(os.environ.get("MAX_ASR_PER_RUN", "3"))


def load_hook():
    """飞书 webhook：优先环境变量，其次本地文件。
    注意：仓库是 public，webhook 绝不能写死在代码里（否则谁都能往群里发消息）。
    GitHub Actions 里用 Secret FEISHU_HOOK；本地跑放在 state/hook.txt（不要提交）。"""
    h = (os.environ.get("FEISHU_HOOK") or "").strip()
    if h:
        return h
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "hook.txt")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


ARGS = [
    "--no-sandbox", "--disable-gpu",
    # ↓ 关键：不抹掉这两个开关，页面会被识别成自动化环境，签名模块不启动 → 全站 403
    "--disable-blink-features=AutomationControlled",
    "--no-first-run", "--no-default-browser-check",
]


# ---------------------------------------------------------------- 抓取
def fetch_contents(timeout_ms=60000, settle_ms=9000):
    """渲染博主主页并截获 get_by_uid 响应，返回条目列表"""
    from playwright.sync_api import sync_playwright

    responses = []
    with sync_playwright() as p:
        launch_kw = dict(headless=True, args=ARGS,
                         ignore_default_args=["--enable-automation"])
        if os.environ.get("PW_CHANNEL", "chrome") != "none":
            launch_kw["channel"] = os.environ.get("PW_CHANNEL", "chrome")
        browser = p.chromium.launch(**launch_kw)
        ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                  viewport={"width": 414, "height": 900})
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        page.on("response",
                lambda r: responses.append(r) if "get_by_uid" in r.url else None)
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(settle_ms)          # 等接口返回 + 首屏渲染
        payloads = []
        for r in responses:                        # 必须在 page 存活时解析
            try:
                payloads.append(r.json())
            except Exception:                      # noqa: BLE001
                pass
        browser.close()

    items = []
    for d in payloads:
        items.extend(((d.get("data") or {}).get("contents")) or [])
    return items


# ---------------------------------------------------------------- 归一化
def normalize(x):
    info = x.get("info") or {}
    video = x.get("video") or {}
    stat = x.get("stat") or {}
    ctime = int(info.get("ctime") or 0)
    ts = ctime / 1000 if ctime else 0
    dur = int((video.get("duration") or 0) / 1000)
    # client_url 形如 client.html?action=..^vid=..^vUrl=https://.../xxx.mp4
    cu = video.get("client_url") or ""
    mp4 = cu.split("vUrl=")[1].split("^")[0] if "vUrl=" in cu else ""
    return {
        "id": str(info.get("id") or ""),
        "ts": ts,
        "time": datetime.fromtimestamp(ts, CN).strftime("%m-%d %H:%M") if ts else "",
        "is_video": bool(video.get("vid")),
        "title": ((x.get("title") or {}).get("content") or "").strip(),
        "abstract": ((x.get("abstract") or {}).get("content") or "").strip(),
        "url": info.get("pc_jump_url") or info.get("jump_url") or "",
        "duration": f"{dur // 60}:{dur % 60:02d}" if dur else "",
        "like": stat.get("like_num"),
        "comment": stat.get("comment_num"),
        "cover": video.get("cover") or "",
        "mp4": mp4,
    }


# ---------------------------------------------------------------- 转写
_ASR = None


def _asr_model():
    global _ASR
    if _ASR is None:
        from faster_whisper import WhisperModel
        # CPU + int8 量化：GitHub runner 是 2 核，这是性价比最高的组合
        _ASR = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8")
    return _ASR


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 16)
    return os.path.getsize(dest)


def transcribe(mp4_url):
    """下载 mp4 → faster-whisper 转写（内部用 PyAV 解码，不需要外部 ffmpeg）"""
    tmp = tempfile.mkdtemp(prefix="yeren-asr-")
    try:
        p = os.path.join(tmp, "v.mp4")
        size = _download(mp4_url, p)
        segs, _info = _asr_model().transcribe(
            p, language="zh", initial_prompt=ASR_PROMPT,
            vad_filter=True, beam_size=5)
        text = "".join(s.text for s in segs).strip()
        print(f"      ↓ {size/1e6:.1f}MB → {len(text)} 字")
        return text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 推送
def push(text, hook):
    data = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    req = urllib.request.Request(hook, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def compose(v, body=""):
    title = v["title"] or v["abstract"] or "(无标题)"
    if len(title) > 160:
        title = title[:160] + "…"
    head = "🎬 野人发视频了" + ("（含文字稿）" if body else "")
    bits = [v["time"]] if v["time"] else []
    if v["duration"]:
        bits.append(f"时长 {v['duration']}")
    if v["like"] is not None:
        bits.append(f"赞 {v['like']}")
    parts = [head, " ｜ ".join(bits), "────", title]
    if body:
        parts += ["────", "【口播文字稿】", body]
    if v["url"]:
        parts += ["────", f"🔗 {v['url']}"]
    return "\n".join(parts)


# ---------------------------------------------------------------- 状态
def load_seen(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f).get("seen") or [])
    except Exception:                              # noqa: BLE001
        return set()


def save_seen(path, seen):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(seen),
                   "updated": datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0,
                    help="只推最近 N 小时内的（0=按 seen 状态增量）")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    ap.add_argument("--no-asr", action="store_true", help="不转写文字")
    ap.add_argument("--state", default=os.environ.get("STATE_FILE",
                                                      "state/seen_video.json"))
    a = ap.parse_args()

    hook = load_hook()
    if not hook and not a.dry:
        print("[!] 未配置飞书 webhook：请设置环境变量 FEISHU_HOOK，"
              "或把 webhook 写进 state/hook.txt", file=sys.stderr)
        return 2

    raw = fetch_contents()
    items = [normalize(x) for x in raw]
    items.sort(key=lambda v: v["ts"])
    print(f"[i] 抓到 {len(items)} 条，其中视频 {sum(1 for v in items if v['is_video'])} 条")

    seen = load_seen(a.state)
    if a.hours:
        cutoff = datetime.now(CN).timestamp() - a.hours * 3600
        todo = [v for v in items if v["is_video"] and v["ts"] >= cutoff]
        mode = f"最近 {a.hours} 小时"
    else:
        todo = [v for v in items if v["is_video"] and v["id"] not in seen]
        mode = "增量"
        print(f"[i] 已见 {len(seen)} 条，待推 {len(todo)} 条")

    do_asr = not a.no_asr
    # 单轮转写条数上限：防止一次补推 7 条把 job 拖超时
    if do_asr and len(todo) > MAX_ASR_PER_RUN:
        asr_ids = {v["id"] for v in todo[-MAX_ASR_PER_RUN:]}   # 取最新的几条
        print(f"[i] 本轮转写上限 {MAX_ASR_PER_RUN} 条，"
              f"其余只推标题+链接（下轮不再重复转写已见条目）")
    else:
        asr_ids = {v["id"] for v in todo} if do_asr else set()

    if a.dry:
        print(f"[i] {mode}：将推送 {len(todo)} 条，其中转写 {len(asr_ids)} 条")
        for v in todo:
            mark = "ASR" if v["id"] in asr_ids else "   "
            print("-" * 60)
            print(f"[{mark}] {compose(v, '(这里会是识别出的文字稿)' if v['id'] in asr_ids else '')}")
        return 0

    ok, done = 0, 0
    for v in todo:
        body = ""
        if v["id"] in asr_ids and v["mp4"]:
            print(f"[i] 转写 {v['id']} {v['title'][:24]}")
            try:
                t0 = time.time()
                body = transcribe(v["mp4"])
                print(f"      耗时 {time.time()-t0:.1f}s")
                done += 1
            except Exception as e:                 # noqa: BLE001
                print(f"[!] 转写失败 {v['id']}: {str(e)[:160]}", file=sys.stderr)
        elif asr_ids and not v["mp4"]:
            print(f"[!] {v['id']} 无 mp4 直链，跳过转写", file=sys.stderr)
        try:
            st = push(compose(v, body), hook)
            print(f"[+] {v['time']} {v['title'][:28]} -> HTTP {st}")
            ok += 1
        except Exception as e:                     # noqa: BLE001
            print(f"[!] 推送失败 {v['id']}: {e}", file=sys.stderr)
        time.sleep(1.0)

    if not a.hours:                                # 记录所有已见视频，防重复
        new_ids = {v["id"] for v in items if v["is_video"]} - seen
        if new_ids:
            seen.update(new_ids)
            save_seen(a.state, seen)
            print(f"[i] 状态更新：+{len(new_ids)} 条")
    print(f"[i] 完成：成功 {ok}/{len(todo)}，转写 {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
