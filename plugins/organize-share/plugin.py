# -*- coding: utf-8 -*-
"""TgtoDrive 工具箱插件：整理后自动分享

TgtoDrive 影视整理完成后，扫描 123 云盘整理目标目录树，识别带 {tmdb} 的标题文件夹，
为「新增」的剧/电影生成 123 云盘分享链接，并发送到指定 Telegram 群。
仅新增标题会建链发群；首次运行只记录基线不发群，避免历史刷屏。

凭据取自同容器的 /app/db/user.env（ENV_123_CLIENT_ID / ENV_123_CLIENT_SECRET /
ENV_TG_BOT_TOKEN / ENV_DIY_LINK_PWD / ENV_123_ORGANIZE_TARGET_PID）。
状态保存在 context["data_dir"]/state.json，更新插件后保留、卸载时删除。

使用：实用工具 → 我的插件 → 导入插件 → 选择本文件 → 确认信任 → 打开 → 填参数 → 运行。
定时扫描：我的插件 → 定时执行 → 选本插件 → 模式=scan → cron 例如 */3 * * * *。
"""

import os
import re
import json
import time
import traceback
import urllib.request
from pathlib import Path


PLUGIN_MANIFEST = {
    "schema_version": 1,
    "id": "organize-share",
    "name": "整理后自动分享",
    "version": "1.0.0",
    "description": (
        "TgtoDrive 整理完成后，自动为整理好的剧/电影生成 123 云盘分享链接并发送到 Telegram 群。"
        "扫描 123 整理目标目录树，识别带 {tmdb} 的标题文件夹，仅「新增」标题才建链发群；"
        "首次运行只记录基线（不发群），避免历史刷屏。支持快速/全量扫描、回填补发、试运行、获取群 chat_id。"
        "可通过开关开启「同步发送到社区/频道」，把分享同时发到你自己的 Telegram 社区或频道。"
    ),
    "author": "wistar",
    "entry": "plugin.py",
    "timeout": 3600,
    "inputs": [
        {
            "name": "chat_id",
            "label": "目标 TG 群 chat_id",
            "type": "text",
            "required": False,
            "default": "",
            "placeholder": "-1001234567890",
            "help": "分享发送到的 Telegram 群 ID。留空则只建链不发送。先自行把 @OracleTgto123_Bot 拉进群并在群里发消息，再用「获取群ID」模式取得。",
        },
        {
            "name": "mode",
            "label": "运行模式",
            "type": "select",
            "default": "scan",
            "options": [
                {"label": "扫描并分享（快速）", "value": "scan"},
                {"label": "全量扫描并分享", "value": "full_scan"},
                {"label": "回填基线（只记录不发群）", "value": "backfill"},
                {"label": "试运行（只列出不建链）", "value": "dry_run"},
                {"label": "获取群 chat_id", "value": "get_chat_id"},
            ],
            "help": "scan=只扫近年阈值的新标题；full_scan=扫全部历史；backfill=补发已存在但未发过的；dry_run=只列出不操作；get_chat_id=列出 bot 看到的群。",
        },
        {
            "name": "share_pwd",
            "label": "分享提取码",
            "type": "text",
            "required": False,
            "default": "",
            "placeholder": "留空则用系统默认提取码",
            "help": "留空则沿用 user.env 的 ENV_DIY_LINK_PWD。",
        },
        {
            "name": "share_expire",
            "label": "分享有效期(天)",
            "type": "number",
            "required": False,
            "default": 0,
            "min": 0,
            "max": 30,
            "step": 1,
            "help": "0=永久；123 仅支持 0/1/7/30。",
        },
        {
            "name": "target_pid",
            "label": "整理目标根目录 ID",
            "type": "text",
            "required": False,
            "default": "",
            "placeholder": "留空则用系统默认整理目标",
            "help": "123 云盘上整理输出根目录的文件 ID。留空沿用 ENV_123_ORGANIZE_TARGET_PID。",
        },
        {
            "name": "year_threshold",
            "label": "快速扫描年份阈值",
            "type": "number",
            "required": False,
            "default": 2024,
            "min": 1900,
            "max": 2100,
            "step": 1,
            "help": "scan 模式只扫描该年份及之后的标题文件夹（加速）。",
        },
        {
            "name": "full_scan_every",
            "label": "每 N 次快速扫描做一次全量",
            "type": "number",
            "required": False,
            "default": 20,
            "min": 1,
            "max": 1000,
            "step": 1,
        },
        {
            "name": "media_include",
            "label": "只处理媒体分类(每行一个正则)",
            "type": "textarea",
            "required": False,
            "default": "",
            "placeholder": "电影\n剧集\n动漫",
            "help": "留空=全部。只处理根媒体分类名匹配其中任一正则的（如 电影/剧集/动漫）。",
        },
        {
            "name": "media_exclude",
            "label": "排除媒体分类(每行一个正则)",
            "type": "textarea",
            "required": False,
            "default": "",
            "placeholder": "演唱会",
        },
        {
            "name": "post_enabled",
            "label": "发送消息到 TG 群",
            "type": "switch",
            "required": False,
            "default": True,
        },
        {
            "name": "community_enabled",
            "label": "同步发送到社区/频道",
            "type": "switch",
            "required": False,
            "default": False,
            "help": "关闭则只发 TG 群（或不发）。开启后，分享链接会同时发送到「社区/频道 chat_id」对应的 Telegram 社区或频道；需把机器人拉进该社区/频道并赋予发消息权限。",
        },
        {
            "name": "community_chat_id",
            "label": "社区/频道 chat_id",
            "type": "text",
            "required": False,
            "default": "",
            "placeholder": "-1001234567890",
            "help": "开启「同步发送到社区」后生效。Telegram 社区/频道的 chat_id（通常为 -100 开头的负数）。",
        },
        {
            "name": "template",
            "label": "消息模板",
            "type": "textarea",
            "required": False,
            "default": "🎬 《{title}》{year_block}\n📂 分类：{category}\n🔗 分享链接：{url}\n🔑 提取码：{pwd}",
            "help": "变量：{title}标题 {year}年份 {year_block}(年份) {category}分类 {url}链接 {pwd}提取码 {name}原文件夹名。",
        },
    ],
    "ui": {
        "width": "wide",
        "layout": "stack",
        "components": [
            {"type": "alert", "text": "首次使用：把机器人 @OracleTgto123_Bot 拉进目标群并在群里发消息，用「获取群ID」模式拿到 chat_id 填入上方。再在「我的插件→定时执行」里配 cron（如 */3 * * * *）即可自动扫描。", "style": {"tone": "warning"}},
            {"type": "heading", "text": "运行与发送"},
            {"type": "field", "name": "mode"},
            {"type": "field", "name": "chat_id"},
            {"type": "field", "name": "share_pwd"},
            {"type": "field", "name": "share_expire"},
            {"type": "heading", "text": "扫描范围"},
            {"type": "field", "name": "target_pid"},
            {"type": "field", "name": "year_threshold"},
            {"type": "field", "name": "full_scan_every"},
            {"type": "heading", "text": "媒体过滤与消息"},
            {"type": "field", "name": "media_include"},
            {"type": "field", "name": "media_exclude"},
            {"type": "field", "name": "post_enabled"},
            {"type": "field", "name": "community_enabled"},
            {"type": "field", "name": "community_chat_id"},
            {"type": "field", "name": "template"},
        ],
    },
}


# ---------------------- 常量与路径 ----------------------
ENV_PATH = "/app/db/user.env"
WEB_BASE = "https://www.123pan.cn/b"
DEFAULT_TEMPLATE = "🎬 《{title}》{year_block}\n📂 分类：{category}\n🔗 分享链接：{url}\n🔑 提取码：{pwd}"

TMDB_RE = re.compile(r"\{tmdb[a-z-]*?-?\d+\}", re.I)
YEAR_RE = re.compile(r"\((\d{4})\)")
CAT_RE = re.compile(r"剧集|电影|综艺|动漫|纪录片|短剧|合集|资料|演唱会|儿童节目")


# ---------------------- 工具函数 ----------------------
def _load_env():
    env = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                if line.startswith("ENV_") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v
    except OSError:
        pass
    return env


def _lines(value):
    if not value:
        return None
    out = []
    for s in str(value).splitlines():
        s = s.strip()
        if s:
            out.append(s)
    return out or None


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(path, state):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))


_client = None


def _get_client(env):
    global _client
    if _client is None:
        from p123client import P123Client
        cid = env["ENV_123_CLIENT_ID"]
        secret = env["ENV_123_CLIENT_SECRET"]
        _client = P123Client(passport=cid, password=secret)
        print("123 云盘登录成功")
    return _client


def _list_dir(c, pid):
    items = []
    last = None
    while True:
        payload = {"parentFileId": pid, "limit": 100}
        if last is not None:
            payload["lastFileId"] = last
        res = c.fs_list(payload, base_url=WEB_BASE)
        data = res.get("data") or {}
        info = data.get("InfoList") or []
        if not info:
            break
        for it in info:
            if it.get("trashed"):
                continue
            items.append(it)
        lf = data.get("lastFileId")
        if lf in (None, "-1"):
            break
        last = lf
        if len(items) > 50000:
            break
    return items


def _is_title(name):
    return "{tmdb" in name.lower()


def _media_pass(name, include, exclude):
    if exclude and any(re.search(x, name) for x in exclude):
        return False
    if include and not any(re.search(x, name) for x in include):
        return False
    return True


def _find_year_containers(c, pid, path, year_threshold, out, depth=0, max_depth=3):
    if depth > max_depth:
        return
    try:
        items = _list_dir(c, pid)
    except Exception as e:
        print("列出目录 %s 失败: %s" % (pid, e))
        return
    for it in items:
        if it.get("Type") != 1:
            continue
        name = it.get("FileName", "")
        fid = it.get("FileId")
        if not fid:
            continue
        if _is_title(name):
            continue
        m = re.fullmatch(r"(\d{4})", name)
        if m:
            year = int(m.group(1))
            if year >= year_threshold:
                out.append({"year": year, "file_id": fid, "path": path + [name]})
            continue
        _find_year_containers(c, fid, path + [name], year_threshold, out, depth + 1, max_depth)


def _collect_titles_from_container(c, pid, path, out):
    try:
        items = _list_dir(c, pid)
    except Exception as e:
        print("列出目录 %s 失败: %s" % (pid, e))
        return
    for it in items:
        if it.get("Type") != 1:
            continue
        name = it.get("FileName", "")
        fid = it.get("FileId")
        if not fid:
            continue
        if _is_title(name):
            out.append({"file_id": fid, "name": name, "path": path + [name]})


def _collect_titles_fast(c, target_pid, year_threshold, include, exclude):
    out = []
    try:
        roots = _list_dir(c, target_pid)
    except Exception as e:
        print("读取媒体根目录失败: %s" % e)
        return out
    for r in roots:
        if r.get("Type") != 1:
            continue
        rname = r.get("FileName", "")
        if not _media_pass(rname, include, exclude):
            continue
        rid = r.get("FileId")
        if not rid:
            continue
        containers = []
        _find_year_containers(c, rid, [rname], year_threshold, containers)
        for con in containers:
            _collect_titles_from_container(c, con["file_id"], con["path"], out)
    return out


def _collect_titles_full(c, pid, path, out, depth=0, max_depth=5, include=None, exclude=None):
    if depth > max_depth:
        return
    try:
        items = _list_dir(c, pid)
    except Exception as e:
        print("列出目录 %s 失败: %s" % (pid, e))
        return
    for it in items:
        if it.get("Type") != 1:
            continue
        name = it.get("FileName", "")
        fid = it.get("FileId")
        if not fid:
            continue
        if _is_title(name):
            out.append({"file_id": fid, "name": name, "path": path + [name]})
            continue
        if not _media_pass(name, include, exclude):
            continue
        _collect_titles_full(c, fid, path + [name], out, depth + 1, max_depth, include, exclude)


def _parse_title(name):
    m = YEAR_RE.search(name)
    year = m.group(1) if m else ""
    title = name[: m.start()].strip() if m else name
    tm = TMDB_RE.search(name)
    tmdb = tm.group(0) if tm else ""
    return title, year, tmdb


def _derive_category(path):
    candidates = [seg for seg in path[:-1] if not re.fullmatch(r"\d{4}", seg)]
    for seg in reversed(candidates):
        if CAT_RE.search(seg):
            return seg
    return candidates[-1] if candidates else ""


def _make_share(c, title_info, diy_pwd, expire):
    fid = title_info["file_id"]
    name = title_info["name"]
    payload = {
        "fileIDList": str(fid),
        "shareExpire": expire,
        "shareName": name[:30],
        "sharePwd": diy_pwd,
    }
    res = c.share_create(payload, base_url=WEB_BASE)
    if res.get("code") not in (0, 200):
        raise RuntimeError("share_create 失败: %s" % res)
    data = res.get("data") or {}
    key = data.get("ShareKey")
    url = "https://www.123pan.cn/s/%s" % key if key else None
    return key, url


def _bot_send(token, chat_id, text):
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    data = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _build_message(title_info, url, pwd, template):
    title, year, tmdb = _parse_title(title_info["name"])
    cat = _derive_category(title_info["path"])
    year_block = "(%s)" % year if year else ""
    if not template or "{url}" not in template:
        template = DEFAULT_TEMPLATE
    try:
        return template.format(
            title=title, year=year, year_block=year_block,
            category=cat, url=url, pwd=pwd, name=title_info["name"],
        )
    except (KeyError, IndexError):
        return DEFAULT_TEMPLATE.format(
            title=title, year=year, year_block=year_block,
            category=cat, url=url, pwd=pwd, name=title_info["name"],
        )


def _get_chat_id(token):
    url = "https://api.telegram.org/bot%s/getUpdates" % token
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"error": "getUpdates 失败: %s" % e}
    groups = []
    for u in res.get("result", []):
        ch = u.get("message", {}).get("chat", {})
        if ch.get("type") in ("group", "supergroup"):
            groups.append({"chat_id": ch["id"], "title": ch.get("title"), "type": ch.get("type")})
    if not groups:
        return {
            "hint": "未在最近更新中找到群聊。请确认 @OracleTgto123_Bot 已在群内，且群内有新消息，再运行本模式。",
            "groups": [],
        }
    return {
        "hint": "复制下面的 chat_id 填入「目标 TG 群 chat_id」，并保存为插件设置。",
        "groups": groups,
        "ui": {
            "layout": "stack",
            "components": [
                {
                    "type": "table",
                    "label": "bot 可见的群",
                    "headers": ["chat_id", "标题", "类型"],
                    "rows": [[str(g["chat_id"]), g.get("title") or "", g.get("type") or ""] for g in groups],
                }
            ],
        },
    }


# ---------------------- 入口 ----------------------
def run(params, context):
    env = _load_env()
    data_dir = Path(context["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "state.json"

    chat_id = (params.get("chat_id") or "").strip()
    mode = params.get("mode") or "scan"
    diy_pwd = (params.get("share_pwd") or "").strip() or env.get("ENV_DIY_LINK_PWD", "")
    try:
        expire = int(params.get("share_expire", 0) or 0)
    except (TypeError, ValueError):
        expire = 0
    if expire not in (0, 1, 7, 30):
        print("警告：分享有效期 %s 不被 123 支持(仅 0/1/7/30)，已改为永久(0)" % expire)
        expire = 0
    target_pid = params.get("target_pid") or env.get("ENV_123_ORGANIZE_TARGET_PID")
    if target_pid:
        target_pid = int(str(target_pid).strip())
    try:
        year_threshold = int(params.get("year_threshold", 2024) or 2024)
    except (TypeError, ValueError):
        year_threshold = 2024
    try:
        full_every = int(params.get("full_scan_every", 20) or 20)
    except (TypeError, ValueError):
        full_every = 20
    post_enabled = bool(params.get("post_enabled", True))
    community_enabled = bool(params.get("community_enabled", False))
    community_chat_id = (params.get("community_chat_id") or "").strip()
    template = params.get("template") or DEFAULT_TEMPLATE
    include = _lines(params.get("media_include"))
    exclude = _lines(params.get("media_exclude"))
    token = env.get("ENV_TG_BOT_TOKEN")

    if mode == "get_chat_id":
        if not token:
            return {"error": "user.env 缺少 ENV_TG_BOT_TOKEN"}
        return _get_chat_id(token)

    if not target_pid:
        raise ValueError("未配置整理目标根目录 ID（target_pid 或 ENV_123_ORGANIZE_TARGET_PID）")
    if not env.get("ENV_123_CLIENT_ID") or not env.get("ENV_123_CLIENT_SECRET"):
        raise ValueError("user.env 缺少 123 云盘凭据（ENV_123_CLIENT_ID / ENV_123_CLIENT_SECRET）")

    client = _get_client(env)
    state = _load_state(state_path)
    m = state.get("__meta__")
    meta = m if isinstance(m, dict) else {}
    first_run = not state
    cycle = meta.get("cycle", 0) + 1
    force_full = mode == "full_scan"
    do_full = force_full or first_run or (cycle % full_every == 0)

    if do_full:
        print("执行全量扫描")
        out = []
        _collect_titles_full(client, target_pid, [], out, include=include, exclude=exclude)
    else:
        print("执行快速扫描（年份 >= %s）" % year_threshold)
        out = _collect_titles_fast(client, target_pid, year_threshold, include, exclude)
    print("扫描到标题文件夹 %d 个" % len(out))

    if first_run:
        new_state = {}
        for t in out:
            new_state[str(t["file_id"])] = {
                "name": t["name"], "path": t["path"],
                "share_key": None, "share_url": None,
                "posted": False, "seeded": True, "ts": int(time.time()),
            }
        new_state["__meta__"] = {"cycle": cycle, "last_full_scan": int(time.time())}
        _save_state(state_path, new_state)
        print("首次运行：已记录基线 %d 个标题（不建链不发送），后续仅处理新增" % len(out))
        return {
            "首次运行": True,
            "已记录基线": len(out),
            "说明": "已把现有标题记为基线，不会发群。之后每次运行只处理新增标题。",
        }

    new = [t for t in out if str(t["file_id"]) not in state]
    todo = list(new)
    if mode == "backfill":
        for t in out:
            st = state.get(str(t["file_id"]))
            if st and not st.get("posted"):
                todo.append(t)
    seen = set()
    todo = [t for t in todo if not (t["file_id"] in seen or seen.add(t["file_id"]))]
    print("新增标题 %d 个，本次待处理 %d 个" % (len(new), len(todo)))

    if mode == "dry_run":
        samples = [
            {"name": t["name"], "category": _derive_category(t["path"]), "path": "/".join(t["path"])}
            for t in todo[:50]
        ]
        return {
            "新增待处理": len(todo),
            "样本(前50)": samples,
            "ui": {
                "layout": "stack",
                "components": [
                    {"type": "alert", "text": "试运行：仅列出，未建链未发送。", "style": {"tone": "muted"}},
                    {
                        "type": "table",
                        "label": "待处理标题",
                        "headers": ["标题文件夹", "分类", "路径"],
                        "rows": [[s["name"], s["category"], s["path"]] for s in samples],
                    },
                ],
            },
        }

    processed = 0
    results = []
    for t in todo:
        fid = str(t["file_id"])
        try:
            key, url = _make_share(client, t, diy_pwd, expire)
        except Exception as e:
            print("建链失败 %s: %s" % (t.get("name"), e))
            traceback.print_exc()
            continue
        msg = _build_message(t, url, diy_pwd, template)
        posted = False
        if post_enabled and chat_id:
            try:
                _bot_send(token, chat_id, msg)
                posted = True
            except Exception as e:
                print("发送失败(群) %s: %s" % (t.get("name"), e))
        elif not chat_id:
            print("未配置 chat_id，仅建链未发送: %s" % t["name"])
        elif not post_enabled:
            print("post_enabled=false，仅建链未发送: %s" % t["name"])
        community_posted = False
        if community_enabled and community_chat_id:
            try:
                _bot_send(token, community_chat_id, msg)
                community_posted = True
            except Exception as e:
                print("发送失败(社区) %s: %s" % (t.get("name"), e))
        elif community_enabled and not community_chat_id:
            print("community_enabled=true 但未配置 community_chat_id，跳过社区发送")
        state[fid] = {
            "name": t["name"], "path": t["path"], "share_key": key,
            "share_url": url, "posted": posted,
            "community_posted": community_posted, "ts": int(time.time()),
        }
        processed += 1
        results.append({"name": t["name"], "url": url, "posted": posted, "community_posted": community_posted})

    meta["cycle"] = cycle
    if do_full:
        meta["last_full_scan"] = int(time.time())
    state["__meta__"] = meta
    _save_state(state_path, state)

    sent = sum(1 for r in results if r["posted"])
    sent_comm = sum(1 for r in results if r.get("community_posted"))
    print("本次处理 %d 个，发群 %d 个，发社区 %d 个" % (processed, sent, sent_comm))
    rows = [[
        r["name"], r["url"] or "",
        ("已发群" if r["posted"] else "未发群") + ("/已发社区" if r.get("community_posted") else "/未发社区"),
    ] for r in results[:50]]
    return {
        "新增标题": len(new),
        "本次处理": processed,
        "发送成功(群)": sent,
        "发送成功(社区)": sent_comm,
        "ui": {
            "layout": "stack",
            "components": [
                {
                    "type": "table",
                    "label": "本次处理结果",
                    "headers": ["标题", "分享链接", "状态"],
                    "rows": rows,
                }
            ],
        },
    }
