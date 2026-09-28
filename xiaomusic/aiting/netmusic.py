# -*- coding: utf-8 -*-
"""网络搜歌服务。

基于网易云音乐公开 web 接口搜索歌曲，并通过 ``/song/media/outer/url`` 的
302 跳转拿到 CDN mp3 直接下载到 NAS 音乐库。

设计约束：
  - 只使用 Python 标准库（urllib），不引入新依赖，与 radio.py 保持一致；
  - 下载目标固定为音乐库根目录下的「网络歌曲」文件夹；
  - 落盘后把文件属主/权限对齐到 NAS 上现有音乐文件（uid=1000 / gid=1001），
    这样用户在 NAS 文件管理器（File Station）里能直接看到和播放。
"""
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SEARCH_URL = "https://music.163.com/api/search/get/web"
OUTER_URL = "https://music.163.com/song/media/outer/url?id={sid}.mp3"

# 对齐 NAS 上现有音乐文件的属主（nas:Users = 1000:1001）
NAS_UID = 1000
NAS_GID = 1001

# 可下载/可识别的音频扩展名（与 xiaomusic.const.SUPPORT_MUSIC_TYPE 对齐）
AUDIO_EXT = (".mp3", ".flac", ".wav", ".ape", ".ogg", ".m4a", ".wma")

MIN_AUDIO_BYTES = 200 * 1024     # 小于 200KB 视为占位/失败
MAX_AUDIO_BYTES = 100 * 1024 * 1024  # 单文件硬上限 100MB，防爆


def _http(url, data=None, timeout=20, extra_headers=None):
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    if extra_headers:
        headers.update(extra_headers)
    if data is not None and not isinstance(data, bytes):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def search(keyword, limit=30):
    """搜索歌曲，返回 [{id, name, artist, album, duration}]。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    limit = max(1, min(int(limit or 30), 50))
    data = {"s": keyword, "type": 1, "offset": 0, "total": "true", "limit": limit}
    with _http(SEARCH_URL, data=data, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    songs = (j.get("result") or {}).get("songs") or []
    out = []
    for s in songs:
        artists = "/".join(a.get("name", "") for a in s.get("artists", []))
        out.append({
            "id": s.get("id"),
            "name": (s.get("name") or "").strip(),
            "artist": artists,
            "album": (s.get("album") or {}).get("name", ""),
            "duration": int(s.get("duration") or 0) // 1000,  # ms -> s
        })
    return out


def _sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\r\n]+', " ", (name or "")).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120] or "未知歌曲"


def _fix_perms(path, is_dir=False):
    try:
        os.chmod(path, 0o755 if is_dir else 0o644)
        os.chown(path, NAS_UID, NAS_GID)
    except Exception:
        # 非 root 运行或文件系统不支持 chown 时静默跳过
        pass


def dest_music_name(name, artist):
    """生成保存用文件名（不含扩展名）。"""
    title = f"{name} - {artist}" if artist else name
    return _sanitize(title)


def download(sid, name, artist, dest_dir, log=None):
    """下载单曲到 dest_dir。

    Returns:
        (abs_path, existed): 文件路径与是否已存在（秒下）。
    Raises:
        Exception: 网络失败 / 无版权 / 文件过小时抛出。
    """
    sid = str(sid or "").strip()
    if not sid:
        raise ValueError("缺少歌曲 id")
    os.makedirs(dest_dir, exist_ok=True)
    _fix_perms(dest_dir, is_dir=True)

    title = dest_music_name(name, artist)
    final_path = os.path.join(dest_dir, title + ".mp3")
    if os.path.exists(final_path) and os.path.getsize(final_path) > MIN_AUDIO_BYTES:
        return final_path, True

    outer = OUTER_URL.format(sid=sid)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".part")
    downloaded = 0
    ctype = ""
    try:
        with os.fdopen(tmp_fd, "wb") as fout:
            # urlopen 默认跟随 302，直接落到 CDN mp3
            with _http(outer, timeout=30) as resp:
                ctype = resp.headers.get("Content-Type") or ""
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fout.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > MAX_AUDIO_BYTES:
                        raise RuntimeError("文件过大，已中止")
        if downloaded < MIN_AUDIO_BYTES:
            raise RuntimeError(
                f"下载内容过小（{downloaded} 字节），该歌曲可能为 VIP / 无版权")
        if "text/html" in ctype.lower():
            raise RuntimeError("返回了网页而非音频，该歌曲可能无版权")
        os.replace(tmp_path, final_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    _fix_perms(final_path)
    if log:
        log.info(f"[网络搜歌] 已保存《{title}》 {downloaded // 1024} KB -> {final_path}")
    return final_path, False
