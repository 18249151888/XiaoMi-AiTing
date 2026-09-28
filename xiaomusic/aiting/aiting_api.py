# -*- coding: utf-8 -*-
"""小米爱听 —— 后台增强 API（设备、播放、系统、配置）。

挂载路径前缀：/api/aiting
"""
import os
import platform
import shutil
import time

from fastapi import APIRouter, Body, Depends, Query

from xiaomusic.api.dependencies import (
    config,
    log,
    verification,
    xiaomusic as xm,
)

from . import __app_name__, __app_name_en__, __version__
from .radio_api import get_radio

router = APIRouter()

_START_TS = time.time()

# ---------------------------------------------------------------- 音量工具
#
# 说明：小爱音箱的音量只存在音箱端，xiaomusic 本地不缓存。前端若拿不到真实音量，
# 滑块就会停在默认值上，用户一拖就发送「绝对位置」把音量顶到高位（突然炸响）。
# 这里做一层带 TTL 的缓存，读多写少，既保证前端能拿到真值，又不至于每秒打小米服务器。
_VOL_TTL = 5.0
_vol_cache = {}          # did -> (读取时间戳, 音量)

# volume_up / volume_down 每次加减的幅度（前端不传 step 时用这个）
VOL_STEP = 1


async def read_volume(did, ttl=_VOL_TTL):
    """读取音箱音量（带 TTL 缓存）。读取失败时返回上一次已知值，可能为 None。"""
    now = time.time()
    hit = _vol_cache.get(did)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    try:
        v = int(await xm.get_volume(did))
    except Exception as e:
        log.warning(f"读取音量失败 did={did}: {e}")
        return hit[1] if hit else None
    _vol_cache[did] = (now, v)
    return v


async def write_volume(did, volume):
    """写入音量（0-100 钳位）并同步缓存。"""
    volume = max(0, min(100, int(volume)))
    await xm.set_volume(did, volume)
    _vol_cache[did] = (time.time(), volume)
    return volume


def _cur_music(did):
    try:
        return (getattr(xm.devices[did].device, "cur_music", "") or "").strip()
    except Exception:
        return ""


def _music_url(name):
    """从音乐库取曲目地址（本地文件返回路径，网络曲目/电台返回 http 地址）。"""
    try:
        return (xm._music_library.get_all_music() or {}).get(name, "") or ""
    except Exception:
        return ""


def _is_radio(name):
    try:
        return name in (xm._music_library.get_all_radio() or {})
    except Exception:
        return False


def _mark_playing(did, name):
    """重推流后把设备标记为「播放中」。

    电台是直播流没有固定时长，必须手动置位，否则前端会显示成已停止。
    """
    try:
        dev = xm.devices[did]
        if name:
            dev.device.cur_music = name
        dev._playing = True
        dev._start_time = time.time()
        dev._paused_time = 0
        if _is_radio(name):
            dev._duration = 0
    except Exception as e:
        log.warning(f"标记播放状态失败 did={did}: {e}")


# ---------------------------------------------------------------- 概览

@router.get("/status")
async def status(_=Depends(verification)):
    """总体状态：登录、设备、播放、库统计"""
    try:
        need_login = await xm.need_login()
    except Exception:
        need_login = None
    devices = await _device_list()
    radio = get_radio()
    try:
        music = xm.get_music_list()
        n_songs = sum(len(v) for k, v in music.items()) if isinstance(music, dict) else 0
        n_lists = len(music) if isinstance(music, dict) else 0
    except Exception:
        n_songs, n_lists = 0, 0
    return {
        "ret": "OK",
        "app": {"name": __app_name__, "name_en": __app_name_en__, "version": __version__},
        "need_login": need_login,
        "devices": devices,
        "radio": radio.meta(),
        "music": {"songs": n_songs, "playlists": n_lists},
        "uptime": int(time.time() - _START_TS),
    }


# ---------------------------------------------------------------- 设备

async def _device_list():
    out = []
    try:
        devs = getattr(xm, "devices", {}) or {}
        for did, player in devs.items():
            d = getattr(player, "device", None)
            item = {
                "did": did,
                "device_id": getattr(d, "device_id", ""),
                "hardware": getattr(d, "hardware", ""),
                "name": getattr(d, "name", "") or did,
                "play_type": getattr(d, "play_type", None),
                "cur_music": getattr(d, "cur_music", ""),
                "cur_playlist": getattr(d, "cur_playlist", ""),
            }
            try:
                item["is_playing"] = player.isplaying()
            except Exception:
                item["is_playing"] = False
            try:
                offset, duration = player.get_offset_duration()
                item["offset"], item["duration"] = offset, duration
            except Exception:
                item["offset"], item["duration"] = 0, 0
            # 真实音量：前端滑块必须据此同步，否则用户一拖就跳到高位
            item["volume"] = await read_volume(did)
            out.append(item)
    except Exception as e:
        log.warning(f"获取设备列表失败: {e}")
    return out


@router.get("/devices")
async def devices(_=Depends(verification)):
    """音箱设备列表"""
    return {"ret": "OK", "devices": await _device_list()}


@router.get("/player/now")
async def player_now(did: str = Query(...), _=Depends(verification)):
    """当前播放详情"""
    if not xm.did_exist(did):
        return {"ret": "DidNotExist"}
    try:
        offset, duration = xm.get_offset_duration(did)
    except Exception:
        offset, duration = 0, 0
    return {
        "ret": "OK",
        "did": did,
        "is_playing": xm.isplaying(did),
        "cur_music": xm.playingmusic(did),
        "cur_playlist": xm.get_cur_play_list(did),
        "offset": offset,
        "duration": duration,
        "volume": await read_volume(did),
    }


@router.post("/player/command")
async def player_command(data: dict = Body(...), _=Depends(verification)):
    """通用播放控制。

    指令：play / stop / next / prev / volume / volume_up / volume_down /
          play_type / position
    """
    did = data.get("did") or ""
    cmd = (data.get("cmd") or "").lower()
    if not xm.did_exist(did):
        return {"ret": "DidNotExist"}
    try:
        if cmd == "play":
            name = (data.get("name") or "").strip()
            url = (data.get("url") or "").strip()
            if name:
                await xm.do_play(did, name, data.get("searchkey") or "")
            elif url:
                await xm.devices[did].group_player_play(url)
                _mark_playing(did, "")
            else:
                # 恢复播放：绝不推空 URL（空 URL 会让音箱回落到默认音量，听感上就是突然炸响）
                cur = _cur_music(did)
                if not cur:
                    return {"ret": "Error", "msg": "没有可恢复播放的曲目"}
                u = _music_url(cur)
                if u.startswith(("http://", "https://")):
                    await xm.devices[did].group_player_play(u)
                    _mark_playing(did, cur)
                else:
                    await xm.do_play(did, cur, "")
        elif cmd == "stop":
            await xm.stop(did)
        elif cmd == "next":
            await xm.play_next(did)
        elif cmd == "prev":
            await xm.play_prev(did)
        elif cmd == "volume":
            await write_volume(did, data.get("volume") or 0)
        elif cmd == "volume_up":
            cur = await read_volume(did)
            await write_volume(did, (cur if cur is not None else 0)
                               + int(data.get("step") or VOL_STEP))
        elif cmd == "volume_down":
            cur = await read_volume(did)
            await write_volume(did, (cur if cur is not None else 0)
                               - int(data.get("step") or VOL_STEP))
        elif cmd == "play_type":
            await xm.set_play_type(did, int(data.get("play_type") or 0))
        elif cmd == "position":
            await xm.devices[did].set_position(int(data.get("position") or 0))
        else:
            return {"ret": "Error", "msg": f"未知指令 {cmd}"}
    except Exception as e:
        log.error(f"播放控制失败 cmd={cmd}: {e}")
        return {"ret": "Error", "msg": str(e)}
    return {"ret": "OK", "volume": _vol_cache.get(did, (0, None))[1]}


# ---------------------------------------------------------------- 音乐库

@router.get("/music/playlists")
def music_playlists(_=Depends(verification)):
    """所有歌单及数量"""
    try:
        data = xm.get_music_list()
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}
    if isinstance(data, dict):
        items = [{"name": k, "count": len(v)} for k, v in data.items()]
        items.sort(key=lambda x: -x["count"])
        return {"ret": "OK", "playlists": items}
    return {"ret": "OK", "playlists": []}


@router.get("/music/list")
def music_list(playlist: str = Query(...), page: int = Query(1),
               limit: int = Query(200), keyword: str = Query(""),
               _=Depends(verification)):
    """某个歌单的歌曲"""
    try:
        data = xm.get_music_list()
        songs = list(data.get(playlist, []))
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}
    if keyword:
        kw = keyword.lower()
        songs = [s for s in songs if kw in s.lower()]
    total = len(songs)
    start = (max(1, page) - 1) * limit
    return {"ret": "OK", "total": total, "items": songs[start:start + limit]}


@router.get("/music/search")
def music_search(keyword: str = Query(...), limit: int = Query(60),
                 _=Depends(verification)):
    """全局搜索本地音乐"""
    try:
        res = xm.searchmusic(keyword)
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}
    if isinstance(res, list):
        return {"ret": "OK", "items": res[:limit]}
    return {"ret": "OK", "items": res}


@router.post("/music/refresh")
async def music_refresh(_=Depends(verification)):
    """重新扫描音乐目录"""
    try:
        await xm.gen_music_list()
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}
    return {"ret": "OK"}


# ---------------------------------------------------------------- 系统

@router.get("/system/info")
def system_info(_=Depends(verification)):
    """运行环境信息"""
    try:
        usage = shutil.disk_usage("/app/music")
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except Exception:
        disk = None
    return {
        "ret": "OK",
        "app": {"name": __app_name__, "version": __version__},
        "python": platform.python_version(),
        "platform": platform.platform(),
        "hostname": config.hostname,
        "port": config.port,
        "music_path": config.music_path,
        "conf_path": config.conf_path,
        "disk": disk,
        "uptime": int(time.time() - _START_TS),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/system/logs")
def system_logs(lines: int = Query(200), _=Depends(verification)):
    """读取运行日志尾部"""
    path = getattr(config, "log_file", "xiaomusic.log.txt")
    if not os.path.isabs(path):
        path = os.path.join("/app", path)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, max(4096, lines * 260))
            f.seek(size - block)
            data = f.read().decode("utf-8", "replace")
        return {"ret": "OK", "lines": data.splitlines()[-lines:]}
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}


# ---------------------------------------------------------------- 配置

@router.get("/config")
def get_config(_=Depends(verification)):
    """读取当前配置（隐藏敏感字段）"""
    try:
        data = xm.getconfig()
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}
    if isinstance(data, dict):
        safe = dict(data)
        if safe.get("password"):
            safe["password"] = "******"
        if safe.get("cookie"):
            safe["cookie"] = "******"
        return {"ret": "OK", "config": safe}
    return {"ret": "OK", "config": {}}


@router.post("/config")
async def save_config(data: dict = Body(...), _=Depends(verification)):
    """保存配置（会触发重新初始化）"""
    try:
        await xm.saveconfig(data)
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}
    return {"ret": "OK"}


@router.post("/system/reinit")
async def system_reinit(_=Depends(verification)):
    """重新登录并初始化设备"""
    try:
        await xm.reinit()
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}
    return {"ret": "OK"}


@router.get("/ping")
def ping():
    """健康检查（无需认证）"""
    return {"ret": "OK", "app": __app_name_en__, "version": __version__}
