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
import html as _html
import json
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


PLUGIN_MANIFEST = {
    "schema_version": 1,
    "id": "organize-share",
    "name": "整理后自动分享",
    "version": "1.0.3",
    "description": (
        "TgtoDrive 整理完成后，自动为整理好的剧/电影生成 123 云盘分享链接并发送到 Telegram 群。"
        "扫描 123 整理目标目录树，识别带 {tmdb} 的标题文件夹，仅「新增」标题才建链发群；"
        "首次运行只记录基线（不发群），避免历史刷屏。支持快速/全量扫描、回填补发、试运行、获取群 chat_id。"
        "可通过开关把每个新分享发布到 pan1.me 资源社区（Xiuno 论坛主题帖，带标题去重）。"
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
            "label": "发布到 pan1.me 资源社区",
            "type": "switch",
            "required": False,
            "default": False,
            "help": "关闭则只发 TG 群（或不发）。开启后，每个新分享会在 pan1.me 论坛发一个主题帖，凭据自动读 user.env 的 ENV_123PANFX_COOKIE / ENV_123PANFX_BASE_URL / ENV_FORUM_UID。",
        },
        {
            "name": "forum_fid",
            "label": "社区版块",
            "type": "text",
            "required": False,
            "default": "auto",
            "placeholder": "auto 或版块ID，如 48",
            "help": "auto=按分类自动选（电影→2、剧集→48、动漫→37、综艺→52、纪录片/儿童/演唱会→45资源互助）；或填固定版块ID：2电影 48电视剧 37动漫 52综艺 59短剧 56-4K原盘 45资源互助。",
        },
        {
            "name": "forum_link_field",
            "label": "分享链接放置方式",
            "type": "select",
            "default": "http_url",
            "options": [
                {"label": "回复可见（http_url，推荐）", "value": "http_url"},
                {"label": "登录可见（vod_url）", "value": "vod_url"},
                {"label": "直接写进正文（所有人可见）", "value": "message"},
            ],
            "help": "分享链接放在论坛帖子的哪个字段。回复/登录可见会隐藏链接，需用户回复或登录后才显示。",
        },
        {
            "name": "forum_dedup",
            "label": "发帖前去重",
            "type": "switch",
            "required": False,
            "default": True,
            "help": "发帖前拉取「我的主题」，若已存在同题帖（含 TgtoDrive 自带发帖）则跳过，避免重复刷帖。",
        },
        {
            "name": "rich_message",
            "label": "富文本分享卡片",
            "type": "switch",
            "required": False,
            "default": True,
            "help": "开启后自动解析文件夹内视频文件（画质/编码/音频/体积/集数）并按 {tmdb} id 抓取 TMDB 资料（评分/简介/播出日期/标签/海报），以「海报+详情」卡片发到群里；失败自动回退纯文本。关闭则使用下方自定义模板。",
        },
        {
            "name": "template",
            "label": "消息模板",
            "type": "textarea",
            "required": False,
            "default": "🎬 《{title}》{year_block}\n📂 分类：{category}\n🔗 分享链接：{url}\n🔑 提取码：{pwd}",
            "help": "仅富文本关闭时生效。变量：{title} {year} {year_block} {category} {url} {pwd} {name}，以及 {quality}画质 {video}视频 {audio}音频 {size}体积 {episodes}集数 {vote}评分 {date}播出日期 {overview}简介 {genres}标签。",
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
            {"type": "field", "name": "forum_fid"},
            {"type": "field", "name": "forum_link_field"},
            {"type": "field", "name": "forum_dedup"},
            {"type": "field", "name": "rich_message"},
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

# pan1.me 资源社区（Xiuno 论坛）发帖：分类 -> 版块 fid
FORUM_CATEGORY_FID = {
    "电影": "2", "剧集": "48", "电视剧": "48", "动漫": "37",
    "综艺": "52", "短剧": "59", "纪录片": "45",
    "儿童节目": "45", "演唱会": "45",
}
FORUM_FID_NAMES = {
    "2": "电影", "48": "电视剧", "37": "动漫", "52": "综艺", "59": "短剧",
    "56": "4K原盘", "45": "资源互助", "40": "音频", "43": "反馈", "51": "电子书",
}


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
    nxt = 0
    pages = 0
    while True:
        payload = {"parentFileId": pid, "limit": 100}
        if nxt:
            payload["next"] = nxt
        res = c.fs_list(payload, base_url=WEB_BASE)
        data = res.get("data") or {}
        info = data.get("InfoList") or []
        if not info:
            break
        for it in info:
            if it.get("trashed"):
                continue
            items.append(it)
        n = data.get("Next")
        if n in (None, "", "-1", 0):
            break
        nxt = n
        pages += 1
        if pages > 200 or len(items) > 50000:
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


def _build_message(title_info, url, pwd, template, extra=None):
    title, year, tmdb = _parse_title(title_info["name"])
    cat = _derive_category(title_info["path"])
    year_block = "(%s)" % year if year else ""
    if not template or "{url}" not in template:
        template = DEFAULT_TEMPLATE
    kwargs = {
        "title": title, "year": year, "year_block": year_block,
        "category": cat, "url": url, "pwd": pwd, "name": title_info["name"],
    }
    kwargs.update(extra or {})
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return DEFAULT_TEMPLATE.format(**kwargs)


# ---------------------- 富文本分享卡片（v1.0.3） ----------------------
VIDEO_EXT_RE = re.compile(
    r"\.(mkv|mp4|ts|m2ts|mts|avi|wmv|rmvb|mov|iso|mpg|mpeg|vob|flv|webm)$", re.I)
EP_RE = re.compile(r"S\d{1,2}\.?[Ee](\d{1,4})")
EP_CN_RE = re.compile(r"第(\d{1,4})集")
QUALITY_RES = [
    ("8K", re.compile(r"4320p|8K", re.I)),
    ("4K", re.compile(r"2160p|4K\b", re.I)),
    ("1080p", re.compile(r"1080[pi]", re.I)),
    ("720p", re.compile(r"720p", re.I)),
    ("480p", re.compile(r"480p", re.I)),
]
SOURCE_RE = re.compile(r"WEB-?DL|WEB-?Rip|BluRay|BDMV|Remux|HDTV|DVD|WEB", re.I)
CODEC_RE = re.compile(r"H\.?26[56]|x26[45]|HEVC|AV1|VC-?1", re.I)
HDR_RE = re.compile(r"HDR10\+|HDR|HLG|DoVi|Dolby[ .]?Vision|SDR", re.I)
AUDIO_RE = re.compile(
    r"TrueHD[ .]?Atmos|DTS-?X|DTS-?HD(?:[ .]?MA)?|DTS|E-?AC-?3|DDP|DD\+|AC-?3|FLAC|AAC|LPCM|MP3", re.I)
CHAN_RE = re.compile(r"(?<!\d)([2568]\.[01])(?!\d)")


def _is_movie_cat(cat):
    return "电影" in (cat or "") or "院线" in (cat or "")


def _media_info(c, title_info):
    """遍历标题文件夹（最深 2 层），解析视频文件名得到画质/视频/音频/体积/集数。"""
    names = []
    all_size = 0
    video_size = 0

    def walk(pid, depth):
        nonlocal all_size, video_size
        if depth > 1:
            return
        try:
            items = _list_dir(c, pid)
        except Exception:
            return
        for it in items:
            nm = it.get("FileName", "")
            if it.get("Type") == 2:
                sz = int(it.get("Size") or 0)
                all_size += sz
                if VIDEO_EXT_RE.search(nm):
                    names.append(nm)
                    video_size += sz
            elif it.get("Type") == 1:
                walk(it.get("FileId"), depth + 1)

    walk(title_info["file_id"], 0)
    blob = " ".join(names)
    if not names:
        return None
    info = {}
    size = video_size or all_size
    if size > 0:
        info["size_gb"] = "%.2f" % (size / 1024 ** 3)
    for label, rex in QUALITY_RES:
        if rex.search(blob):
            info["quality"] = label
            break
    src = SOURCE_RE.search(blob)
    codec = CODEC_RE.search(blob)
    hdr = HDR_RE.search(blob)
    video = " ".join(x for x in [src and src.group(0), codec and codec.group(0), hdr and hdr.group(0)] if x)
    if video:
        info["video"] = video
    aud = AUDIO_RE.search(blob)
    if aud:
        chan = CHAN_RE.search(blob)
        a = aud.group(0)
        if "Atmos" in blob and "Atmos" not in a:
            a += " Atmos"
        info["audio"] = (a + (" " + chan.group(1) if chan else ""))[:24]
    cat = _derive_category(title_info["path"])
    if not _is_movie_cat(cat):
        eps = [int(m.group(1)) for m in (EP_RE.search(n) for n in names) if m]
        if not eps:
            eps = [int(m.group(1)) for m in (EP_CN_RE.search(n) for n in names) if m]
        if eps:
            info["episodes"] = "更新至 %d 集" % max(eps)
        else:
            info["episodes"] = "共 %d 个视频" % len(names)
    return info


def _tmdb_info(env, title_info, category):
    """按文件夹名里的 {tmdb-xxxx} id 抓 TMDB 中文资料；返回 None 表示不可用。"""
    key = env.get("ENV_TMDB_API_KEY")
    if not key:
        return None
    tm = TMDB_RE.search(title_info["name"])
    if not tm:
        return None
    m = re.search(r"(\d+)", tm.group(0))
    if not m:
        return None
    tid = m.group(1)
    base = (env.get("ENV_TMDB_API_BASE_URL") or "https://api.themoviedb.org/3").rstrip("/")
    if "/3" not in base:
        base = base + "/3"
    img = (env.get("ENV_TMDB_IMAGE_BASE_URL") or "https://image.tmdb.org/t/p").rstrip("/")
    order = ["movie", "tv"] if _is_movie_cat(category) else ["tv", "movie"]
    for kind in order:
        url = "%s/%s/%s?language=zh-CN&api_key=%s" % (base, kind, tid, key)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            return None
        except Exception:
            return None
        if not d or d.get("success") is False:
            continue
        poster = d.get("poster_path") or ""
        return {
            "kind": kind,
            "name": d.get("title") or d.get("name") or "",
            "overview": (d.get("overview") or "").strip(),
            "vote": d.get("vote_average") or 0,
            "date": d.get("release_date") or d.get("first_air_date") or "",
            "genres": [g.get("name") for g in (d.get("genres") or []) if g.get("name")],
            "poster_url": (img + "/w500" + poster) if poster else "",
        }
    return None


def _bot_send_photo(token, chat_id, photo, caption_html):
    """sendPhoto + HTML caption；失败抛异常由调用方回退。"""
    url = "https://api.telegram.org/bot%s/sendPhoto" % token
    data = json.dumps({
        "chat_id": chat_id, "photo": photo,
        "caption": caption_html[:1024], "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _rich_parts(title_info, url, pwd, media, tinfo):
    """构造富文本消息。返回 (纯文本, HTML卡片文案或None)。"""
    title, year, _ = _parse_title(title_info["name"])
    cat = _derive_category(title_info["path"])
    name_line = "%s (%s)" % (title, year) if year else title
    lines = ["🎬 名称：" + name_line]
    if cat:
        lines.append("📂 类型：" + cat)
    if media:
        if media.get("quality"):
            lines.append("🖥 画质：" + media["quality"])
        if media.get("video"):
            lines.append("🎥 视频：" + media["video"])
        if media.get("audio"):
            lines.append("🎧 音频：" + media["audio"])
        if media.get("size_gb"):
            lines.append("📦 大小：%s GB" % media["size_gb"])
        if media.get("episodes"):
            lines.append("🎞 集数：%s" % media["episodes"])
    if tinfo:
        try:
            if tinfo.get("vote"):
                lines.append("⭐ TMDB评分：%.1f/10" % float(tinfo["vote"]))
        except (TypeError, ValueError):
            pass
        if tinfo.get("date"):
            lines.append("📅 播出日期：" + tinfo["date"])
        gs = tinfo.get("genres") or []
        if gs:
            lines.append("🏷 标签：" + " ".join("#" + g for g in gs[:4]))
    overview = (tinfo or {}).get("overview") or ""
    if overview:
        short = overview[:120] + ("…" if len(overview) > 120 else "")
        lines.append("")
        lines.append("📝 简介：")
        lines.append(short)
    lines.append("")
    lines.append("🔗 链接：" + (url or ""))
    if pwd:
        lines.append("🔑 提取码：" + pwd)
    plain = "\n".join(lines)

    html_cap = None
    if tinfo and tinfo.get("poster_url"):
        h = ["🎬 <b>%s</b>" % _html.escape(name_line)]
        if cat:
            h.append("📂 类型：%s" % _html.escape(cat))
        if media:
            if media.get("quality"):
                h.append("🖥 画质：%s" % _html.escape(media["quality"]))
            if media.get("video"):
                h.append("🎥 视频：%s" % _html.escape(media["video"]))
            if media.get("audio"):
                h.append("🎧 音频：%s" % _html.escape(media["audio"]))
            if media.get("size_gb"):
                h.append("📦 大小：%s GB" % media["size_gb"])
            if media.get("episodes"):
                h.append("🎞 集数：%s" % _html.escape(media["episodes"]))
        if tinfo:
            try:
                if tinfo.get("vote"):
                    h.append("⭐ TMDB评分：%.1f/10" % float(tinfo["vote"]))
            except (TypeError, ValueError):
                pass
            if tinfo.get("date"):
                h.append("📅 播出日期：%s" % _html.escape(tinfo["date"]))
            gs = tinfo.get("genres") or []
            if gs:
                h.append("🏷 标签：%s" % " ".join("#" + _html.escape(g) for g in gs[:4]))
        if overview:
            short = overview[:110] + ("…" if len(overview) > 110 else "")
            h.append("")
            h.append("📝 简介：")
            h.append("<blockquote>%s</blockquote>" % _html.escape(short))
        h.append("")
        h.append('🔗 <a href="%s">点击保存到 123 云盘</a>' % _html.escape(url or ""))
        if pwd:
            h.append("🔑 提取码：<code>%s</code>" % _html.escape(pwd))
        html_cap = "\n".join(h)
    return plain, html_cap


# ---------------------- pan1.me 资源社区发帖 ----------------------
FORUM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _forum_cfg(env):
    base = (env.get("ENV_123PANFX_BASE_URL") or "https://pan1.me").rstrip("/")
    return base, env.get("ENV_123PANFX_COOKIE", ""), env.get("ENV_FORUM_UID", "")


def _forum_new_session(cookie):
    s = requests.Session()
    s.headers.update({"User-Agent": FORUM_UA})
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            s.cookies.set(k.strip(), v.strip(), domain="pan1.me")
    return s


def _balanced(text, op, cl, start):
    depth = 0
    instr = False
    esc = False
    for j in range(start, len(text)):
        c = text[j]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            instr = not instr
            continue
        if instr:
            continue
        if c == op:
            depth += 1
        elif c == cl:
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


def _forum_parse_tags(html):
    """从发帖表单解析 tagcatelist/tagcatemap -> {fid: [ {isforce, tags:[(tagid,name)]} ]}。
    tagcatelist 与 tagcatemap 是同一数据的两种形态，按标签集合去重避免重复分类。"""
    by_fid = {}
    seen_sig = {}
    for key, op, cl in (('"tagcatemap"', "{", "}"), ('"tagcatelist"', "[", "]")):
        idx = 0
        while True:
            i = html.find(key, idx)
            if i < 0:
                break
            start = html.find(op, i)
            if start < 0:
                idx = i + 1
                continue
            end = _balanced(html, op, cl, start)
            if end < 0:
                idx = i + 1
                continue
            chunk = html[start:end]
            idx = end
            if len(chunk) < 20:
                continue
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            entries = obj.values() if isinstance(obj, dict) else obj
            for e in entries:
                if not isinstance(e, dict):
                    continue
                fid = str(e.get("fid") or "")
                tags = [(str(t.get("tagid")), t.get("name") or "")
                        for t in (e.get("taglist") or [])
                        if isinstance(t, dict) and t.get("tagid")]
                if fid and tags:
                    sig = frozenset(t[0] for t in tags)
                    seen = seen_sig.setdefault(fid, set())
                    if sig in seen:
                        continue
                    seen.add(sig)
                    by_fid.setdefault(fid, []).append(
                        {"isforce": str(e.get("isforce")) == "1", "tags": tags})
    return by_fid


def _forum_pick_tags(tags_by_fid, fid, name_hint):
    """每个强制标签分类选一个 tagid：优先名称命中 name_hint，其次「其他/其它」，再第一个。"""
    picked = []
    hint = (name_hint or "").lower()
    for cat in tags_by_fid.get(str(fid), []):
        if not cat.get("isforce"):
            continue
        tags = cat["tags"]
        choice = None
        for tid, name in tags:
            if name and name.lower() in hint:
                choice = tid
                break
        if not choice:
            for tid, name in tags:
                if name in ("其他", "其它", "Other", "other"):
                    choice = tid
                    break
        if not choice and tags:
            choice = tags[0][0]
        if choice:
            picked.append(choice)
    uniq = []
    for x in picked:
        if x not in uniq:
            uniq.append(x)
    return uniq


def _forum_my_titles(session, base, uid):
    """拉取「我的主题」-> [(tid, title)]，用于去重与发帖后取 tid（排除 user-thread 导航链接）。"""
    out = []
    if not uid:
        return out
    try:
        html = session.get(base + "/?user-thread-%s.htm" % uid, timeout=30).text
    except Exception as e:
        print("社区：拉取我的主题失败 %s" % e)
        return out
    for href, tid, text in re.findall(
            r'<a[^>]+href="([?/]thread-(\d+)\.htm[^"]*)"[^>]*>(.*?)</a>',
            html, re.S | re.I):
        if "#comments" in href:
            continue
        t = re.sub(r"<[^>]+>", "", text).strip()
        if t:
            out.append((tid, t))
    return out


def _forum_already_posted(titles, core_title):
    if not core_title or len(core_title) < 2:
        return False
    low = core_title.lower()
    return any(low in (t[1] if isinstance(t, (list, tuple)) else str(t)).lower()
               for t in titles)


def _category_to_fid(category, fixed):
    fixed = (fixed or "").strip()
    if fixed and fixed.lower() != "auto":
        return fixed
    for key, fid in FORUM_CATEGORY_FID.items():
        if key in (category or ""):
            return fid
    return "45"


def _forum_subject(title_info):
    title, year, _ = _parse_title(title_info["name"])
    s = "%s (%s)" % (title, year) if year else title
    return s.strip()[:100]


def _forum_message(title_info, pwd, cat, media=None, tinfo=None):
    title, year, _ = _parse_title(title_info["name"])
    head = "《%s》%s" % (title, " (%s)" % year if year else "")
    lines = [head]
    if cat:
        lines.append("分类：%s" % cat)
    if media:
        spec = " ".join(x for x in [media.get("quality"), media.get("video"), media.get("audio")] if x)
        if spec:
            lines.append("规格：%s" % spec)
        if media.get("size_gb"):
            lines.append("体积：%s GB" % media["size_gb"])
        if media.get("episodes"):
            lines.append("集数：%s" % media["episodes"])
    if tinfo:
        try:
            if tinfo.get("vote"):
                lines.append("TMDB评分：%.1f/10" % float(tinfo["vote"]))
        except (TypeError, ValueError):
            pass
        if tinfo.get("date"):
            lines.append("播出日期：%s" % tinfo["date"])
        overview = (tinfo.get("overview") or "").strip()
        if overview:
            lines.append("简介：%s%s" % (overview[:150], "…" if len(overview) > 150 else ""))
    if pwd:
        lines.append("提取码：%s" % pwd)
    return "\n".join(lines)


def _forum_publish(session, base, fid, subject, message, link_field, link, name_hint):
    """发主题帖：先 GET 表单刷新会话并解析标签，选好各强制分类的 tagid，再 POST。
    注意：pan1.me 发帖成功不跳转到新主题（返回 200 表单页），成功与否由 _forum_find 查我的主题确认。"""
    form = session.get(base + "/?thread-create-0.htm", timeout=30)
    tags_by_fid = _forum_parse_tags(form.text)
    tagids = _forum_pick_tags(tags_by_fid, fid, name_hint)
    data = {
        "doctype": "1", "quotepid": "0", "fid": str(fid),
        "subject": subject, "message": message,
        "vod_url": "", "http_url": "",
    }
    if link_field == "message":
        data["message"] = (message + "\n" + (link or "")).strip()
    elif link_field in ("http_url", "vod_url") and link:
        data[link_field] = link
    if tagids:
        data["tagid[]"] = tagids
    r = session.post(base + "/?thread-create.htm", data=data,
                     headers={"Referer": base + "/?thread-create-0.htm"},
                     timeout=45, allow_redirects=True)
    return {
        "http": r.status_code, "tagids": tagids,
        "resp_len": len(r.text), "final_url": r.url,
        "snippet": re.sub(r"\s+", " ", r.text)[:200],
    }


def _forum_find(session, base, uid, subject):
    """发帖后查我的主题，按标题匹配取 (tid, url)；找不到返回 None（即未发成功）。"""
    key = (subject or "").strip().lower()
    if not key:
        return None
    for tid, title in _forum_my_titles(session, base, uid):
        tl = title.strip().lower()
        if tl == key or key in tl or tl in key:
            return tid, base + "/?thread-%s.htm" % tid
    return None


def _get_chat_id(token):
    url = "https://api.telegram.org/bot%s/getUpdates" % token
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        code = getattr(e, "code", None)
        if code == 409 or "409" in str(e):
            return {
                "error": "getUpdates 被占用（HTTP 409）",
                "hint": "该 bot 的更新正被 TgtoDrive 主程序的 getUpdates 长轮询占用"
                        "（同一 bot 只允许一个 getUpdates 连接），插件无法用 getUpdates 直接读群 ID。"
                        "请改用外部方式：从目标群转发任意一条消息给 @userinfobot 或 @getidsbot，"
                        "它回复的数字 ID（形如 -100xxxxxxxxxx）即为群 chat_id，填到「目标 TG 群 chat_id」并保存。",
                "groups": [],
            }
        return {"error": "getUpdates 失败: %s" % e}
    groups = []
    for u in res.get("result", []):
        msg = (u.get("message") or u.get("channel_post") or u.get("edited_message")
               or u.get("edited_channel_post") or {})
        ch = msg.get("chat") or {}
        fwd = msg.get("forward_from_chat") or {}
        if fwd:
            cid, ctype, ctitle = fwd.get("id"), fwd.get("type"), fwd.get("title")
        else:
            cid, ctype, ctitle = ch.get("id"), ch.get("type"), ch.get("title")
        if cid and ctype in ("group", "supergroup", "channel"):
            groups.append({"chat_id": cid, "title": ctitle, "type": ctype})
    uniq = {}
    for g in groups:
        uniq[str(g["chat_id"])] = g
    groups = list(uniq.values())
    if not groups:
        return {
            "hint": "未在最近更新中读到群聊（更新可能已被主程序消费，或群里还没有「命令 / @真提及 / 转发」类消息）。"
                    "更可靠的方式：从目标群转发任意消息给 @userinfobot 或 @getidsbot，"
                    "把返回的数字 ID（形如 -100xxxxxxxxxx）填到「目标 TG 群 chat_id」并保存。",
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
    forum_fid = (params.get("forum_fid") or "auto").strip()
    forum_link_field = (params.get("forum_link_field") or "http_url").strip()
    forum_dedup = bool(params.get("forum_dedup", True))
    template = params.get("template") or DEFAULT_TEMPLATE
    rich_enabled = bool(params.get("rich_message", True))
    include = _lines(params.get("media_include"))
    exclude = _lines(params.get("media_exclude"))
    token = env.get("ENV_TG_BOT_TOKEN")

    forum_base, forum_cookie, forum_uid = _forum_cfg(env)
    forum_session = None
    forum_titles = []
    if community_enabled:
        if requests is None:
            print("community_enabled=true 但容器缺少 requests 库，跳过社区发帖")
            community_enabled = False
        elif not forum_cookie:
            print("community_enabled=true 但 user.env 缺少 ENV_123PANFX_COOKIE，本次跳过社区发帖")
            community_enabled = False
        else:
            forum_session = _forum_new_session(forum_cookie)
            if forum_dedup:
                forum_titles = _forum_my_titles(forum_session, forum_base, forum_uid)
                print("社区去重：已拉取我的论坛主题 %d 个" % len(forum_titles))

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
        media = None
        tinfo = None
        if rich_enabled:
            try:
                media = _media_info(client, t)
            except Exception as e:
                print("媒体信息解析失败 %s: %s" % (t.get("name"), e))
            try:
                tinfo = _tmdb_info(env, t, _derive_category(t["path"]))
            except Exception as e:
                print("TMDB 获取失败 %s: %s" % (t.get("name"), e))
            if media or tinfo:
                plain_msg, html_cap = _rich_parts(t, url, diy_pwd, media, tinfo)
                msg = plain_msg
        posted = False
        if post_enabled and chat_id:
            try:
                sent_photo = False
                if rich_enabled and tinfo and tinfo.get("poster_url"):
                    try:
                        _, cap = _rich_parts(t, url, diy_pwd, media, tinfo)
                        _bot_send_photo(token, chat_id, tinfo["poster_url"], cap)
                        sent_photo = True
                    except Exception as pe:
                        print("海报卡片发送失败，回退文本 %s: %s" % (t.get("name"), pe))
                if not sent_photo:
                    _bot_send(token, chat_id, msg)
                posted = True
            except Exception as e:
                print("发送失败(群) %s: %s" % (t.get("name"), e))
        elif not chat_id:
            print("未配置 chat_id，仅建链未发送: %s" % t["name"])
        elif not post_enabled:
            print("post_enabled=false，仅建链未发送: %s" % t["name"])
        community_posted = False
        community_info = "未发社区"
        if community_enabled:
            title_core = _parse_title(t["name"])[0]
            if forum_dedup and _forum_already_posted(forum_titles, title_core):
                print("社区去重：已存在同题帖，跳过 %s" % t["name"])
                community_info = "去重跳过"
                forum_titles.append(("", t["name"]))
            else:
                try:
                    cat = _derive_category(t["path"])
                    fid = _category_to_fid(cat, forum_fid)
                    subject = _forum_subject(t)
                    fmsg = _forum_message(t, diy_pwd, cat, media, tinfo)
                    pub = _forum_publish(forum_session, forum_base, fid, subject,
                                         fmsg, forum_link_field, url, t["name"])
                    found = _forum_find(forum_session, forum_base, forum_uid, subject)
                    if found:
                        tid, turl = found
                        community_posted = True
                        community_info = "已发社区(%s) #%s" % (
                            FORUM_FID_NAMES.get(str(fid), "fid=%s" % fid), tid)
                        forum_titles.append((tid, subject))
                        print("社区发帖成功 %s -> %s (tags=%s)" % (t["name"], turl, pub.get("tagids")))
                    else:
                        community_info = "发帖未确认"
                        print("社区发帖未确认 %s | final=%s | %s" % (
                            t["name"], pub.get("final_url"), pub.get("snippet")))
                except Exception as e:
                    community_info = "发帖异常"
                    print("社区发帖异常 %s: %s" % (t["name"], e))
        state[fid] = {
            "name": t["name"], "path": t["path"], "share_key": key,
            "share_url": url, "posted": posted,
            "community_posted": community_posted, "community_info": community_info,
            "ts": int(time.time()),
        }
        processed += 1
        results.append({"name": t["name"], "url": url, "posted": posted,
                        "community_posted": community_posted,
                        "community_info": community_info})

    meta["cycle"] = cycle
    if do_full:
        meta["last_full_scan"] = int(time.time())
    state["__meta__"] = meta
    _save_state(state_path, state)

    sent = sum(1 for r in results if r["posted"])
    sent_comm = sum(1 for r in results if r.get("community_posted"))
    skipped_comm = sum(1 for r in results if r.get("community_info") == "去重跳过")
    print("本次处理 %d 个，发群 %d 个，社区发帖 %d 个（去重跳过 %d）" % (
        processed, sent, sent_comm, skipped_comm))
    rows = [[
        r["name"], r["url"] or "",
        ("已发群" if r["posted"] else "未发群") + "/" + (
            r.get("community_info") or ("已发社区" if r.get("community_posted") else "未发社区")),
    ] for r in results[:50]]
    return {
        "新增标题": len(new),
        "本次处理": processed,
        "发送成功(群)": sent,
        "社区发帖成功": sent_comm,
        "社区去重跳过": skipped_comm,
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
