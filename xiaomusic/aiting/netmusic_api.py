# -*- coding: utf-8 -*-
"""小米爱听 —— 网络搜歌 API。

挂载路径前缀：/api/aiting/netmusic

功能：
  - GET  /search   网络搜索歌曲（网易云公开库，仅预览，不下载）
  - POST /download 下载选中歌曲到 NAS 音乐库「网络歌曲」文件夹，并刷新音乐库
  - GET  /local    列出已下载到「网络歌曲」文件夹的歌曲
"""
import asyncio
import os

from fastapi import APIRouter, Body, Depends, Query

from xiaomusic.api.dependencies import (
    config,
    log,
    verification,
    xiaomusic as xm,
)

from . import netmusic

router = APIRouter()

# 下载到音乐库里的文件夹名（与用户约定一致）
DOWNLOAD_DIRNAME = "网络歌曲"


def _music_dir():
    p = getattr(config, "music_path", "music")
    if not os.path.isabs(p):
        p = os.path.join("/app", p)
    return p


def _dest_dir():
    return os.path.join(_music_dir(), DOWNLOAD_DIRNAME)


@router.get("/search")
def netmusic_search(keyword: str = Query(...), limit: int = Query(30),
                    _=Depends(verification)):
    """网络搜索歌曲（仅返回结果列表，不落盘）。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return {"ret": "Error", "msg": "请输入搜索关键词"}
    try:
        items = netmusic.search(keyword, limit=limit)
    except Exception as e:
        log.warning(f"[网络搜歌] 搜索失败: {e}")
        return {"ret": "Error", "msg": f"搜索失败：{e}"}
    return {"ret": "OK", "keyword": keyword, "count": len(items), "items": items}


@router.post("/download")
async def netmusic_download(data: dict = Body(...), _=Depends(verification)):
    """下载一首歌曲到 NAS 音乐库「网络歌曲」文件夹。

    body: {id, name, artist, play_after?: bool, did?: str}
    下载完成后自动重新扫描音乐库，新歌会立刻出现在「本地音乐」的
    「网络歌曲」歌单里；play_after=true 时紧接着推给指定音箱播放。
    """
    sid = data.get("id")
    name = (data.get("name") or "").strip()
    artist = (data.get("artist") or "").strip()
    if not sid or not name:
        return {"ret": "Error", "msg": "缺少歌曲 id 或名称"}

    dest = _dest_dir()
    try:
        path, existed = await asyncio.to_thread(
            netmusic.download, sid, name, artist, dest, log)
    except Exception as e:
        log.warning(f"[网络搜歌] 下载失败 {name}: {e}")
        return {"ret": "Error", "msg": str(e)}

    # 刷新音乐库，让新歌立刻进入「网络歌曲」歌单
    try:
        await xm.gen_music_list()
    except Exception as e:
        log.warning(f"[网络搜歌] 刷新音乐库失败: {e}")

    song_title = os.path.splitext(os.path.basename(path))[0]
    result = {
        "ret": "OK",
        "existed": existed,
        "song": song_title,
        "folder": DOWNLOAD_DIRNAME,
    }

    # 可选：下载完成后立即播放
    if data.get("play_after"):
        did = data.get("did") or ""
        if did and xm.did_exist(did):
            try:
                await xm.do_play(did, song_title, "")
                result["played"] = True
            except Exception as e:
                result["played"] = False
                result["play_msg"] = str(e)
    return result


@router.get("/local")
def netmusic_local(_=Depends(verification)):
    """列出已下载到「网络歌曲」文件夹的歌曲。"""
    dest = _dest_dir()
    try:
        names = []
        if os.path.isdir(dest):
            for f in sorted(os.listdir(dest)):
                if f.startswith("."):
                    continue
                if f.lower().endswith(netmusic.AUDIO_EXT):
                    names.append(os.path.splitext(f)[0])
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}
    return {"ret": "OK", "folder": DOWNLOAD_DIRNAME, "count": len(names),
            "items": names}
