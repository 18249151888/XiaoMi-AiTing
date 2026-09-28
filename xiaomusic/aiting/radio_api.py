# -*- coding: utf-8 -*-
"""小米爱听 —— 网络收音机 API。

挂载路径前缀：/api/aiting/radio
"""
import asyncio
import os
import time

from fastapi import APIRouter, Body, Depends, Query

from xiaomusic.api.dependencies import (
    config,
    log,
    verification,
    xiaomusic as xm,
)

from .radio import RadioService
from .store import JsonStore
from . import normalize

router = APIRouter()

# ---------------------------------------------------------------- 单例

_HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARY = os.path.join(_HERE, "data", "radio_library.json")


def _conf_dir():
    try:
        p = config.conf_path
        if not os.path.isabs(p):
            p = os.path.join("/app", p)
        return p
    except Exception:
        return "/app/conf"


_service = None
_service_lock = asyncio.Lock()


def get_radio() -> RadioService:
    global _service
    if _service is None:
        store = JsonStore(os.path.join(_conf_dir(), "radio_user.json"),
                          {"favorites": [], "custom": [], "recent": [], "counts": {}})
        _service = RadioService(LIBRARY, store, log)
    return _service


# ---------------------------------------------------------------- 浏览

@router.get("/meta")
def radio_meta(_=Depends(verification)):
    """电台库概况"""
    r = get_radio()
    return {"ret": "OK", **r.meta()}


@router.get("/tree")
def radio_tree(_=Depends(verification)):
    """分类树：板块 -> 地区 -> 类型"""
    return {"ret": "OK", "tree": get_radio().tree()}


@router.get("/list")
def radio_list(
    board: str = Query(None),
    region: str = Query(None),
    category: str = Query(None),
    keyword: str = Query(None),
    fav: bool = Query(False),
    source: str = Query(None),
    page: int = Query(1),
    limit: int = Query(100),
    sort: str = Query("hot"),
    _=Depends(verification),
):
    """电台列表（支持多条件筛选）"""
    return {"ret": "OK", **get_radio().list(
        board=board, region=region, category=category, keyword=keyword,
        fav_only=fav, source=source, page=page, limit=limit, sort=sort)}


@router.get("/recent")
def radio_recent(limit: int = Query(30), _=Depends(verification)):
    """最近播放的电台"""
    return {"ret": "OK", "items": get_radio().recent(limit)}


@router.get("/get")
def radio_get(id: str = Query(...), _=Depends(verification)):
    """单个电台详情"""
    st = get_radio().get(id)
    if not st:
        return {"ret": "NotExist"}
    return {"ret": "OK", "station": st}


# ---------------------------------------------------------------- 收藏 / 自定义

@router.post("/fav")
def radio_fav(data: dict = Body(...), _=Depends(verification)):
    """切换收藏状态"""
    sid = data.get("id")
    if not sid:
        return {"ret": "Error", "msg": "缺少 id"}
    state = get_radio().toggle_favorite(sid)
    if state is None:
        return {"ret": "NotExist"}
    return {"ret": "OK", "fav": state}


@router.post("/custom")
def radio_add_custom(data: dict = Body(...), _=Depends(verification)):
    """新增自定义电台"""
    try:
        st = get_radio().add_custom(
            name=data.get("name", ""),
            url=data.get("url", ""),
            board=data.get("board") or "我的电台",
            region=data.get("region") or "自定义",
            category=data.get("category") or "自定义",
            tags=data.get("tags", ""),
        )
        return {"ret": "OK", "station": st}
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}


@router.put("/custom/{sid}")
def radio_update_custom(sid: str, data: dict = Body(...), _=Depends(verification)):
    """修改自定义电台"""
    try:
        st = get_radio().update_custom(sid, **data)
        return {"ret": "OK", "station": st}
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}


@router.delete("/custom/{sid}")
def radio_delete_custom(sid: str, _=Depends(verification)):
    """删除自定义电台"""
    try:
        get_radio().delete_custom(sid)
        return {"ret": "OK"}
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}


# ---------------------------------------------------------------- 连通性

@router.post("/check")
async def radio_check(data: dict = Body(default={}), _=Depends(verification)):
    """检查电台连通性。传 id 检查单个；传 all=true 批量检查当前筛选结果。"""
    r = get_radio()
    sid = (data or {}).get("id")
    if sid:
        try:
            return {"ret": "OK", "result": r.check(sid)}
        except Exception as e:
            return {"ret": "Error", "msg": str(e)}

    # 批量：限制数量，避免长时间占用
    board = (data or {}).get("board")
    region = (data or {}).get("region")
    category = (data or {}).get("category")
    limit = int((data or {}).get("limit") or 60)
    items = r.list(board=board, region=region, category=category,
                   limit=limit)["items"]
    results = await asyncio.gather(*[asyncio.to_thread(r.check, s["id"]) for s in items])
    ok = sum(1 for x in results if x.get("ok"))
    return {"ret": "OK", "checked": len(results), "ok": ok, "results": results}


# ---------------------------------------------------------------- 在线更新

@router.get("/online")
def radio_online(keyword: str = Query(""), country: str = Query("CN"),
                 limit: int = Query(60), _=Depends(verification)):
    """从在线电台库拉取候选（仅预览，不落库）"""
    try:
        raw = get_radio().fetch_online(keyword=keyword, country=country, limit=limit)
        out = []
        for s in raw:
            url = (s.get("url_resolved") or s.get("url") or "").strip()
            if not url:
                continue
            out.append({
                "name": (s.get("name") or "").strip()[:60],
                "url": url,
                "tags": (s.get("tags") or "")[:80],
                "country": s.get("countrycode") or "",
                "votes": s.get("votes") or 0,
            })
        return {"ret": "OK", "items": out}
    except Exception as e:
        return {"ret": "Error", "msg": str(e)}


# ---------------------------------------------------------------- 播放

async def _mark_device_playing(did, name, playlist="网络电台"):
    """让前端播放状态与电台同步"""
    try:
        dev = xm.devices.get(did)
        if dev is None:
            return
        dev.device.cur_music = name
        dev.device.cur_playlist = playlist
        # 电台是直播流，无固定时长；置为播放中，避免进度条逻辑误判
        dev._playing = True
        dev._start_time = time.time()
        dev._paused_time = 0
        dev._duration = 0
    except Exception as e:
        log.warning(f"同步电台播放状态失败: {e}")


def _setup_radio_queue(did, stations, index):
    """把电台列表注册为「网络电台」歌单，使上一首/下一首可用。

    Args:
        stations: [{id, name, url}, ...] 播放队列
        index: 当前播放项下标
    """
    try:
        lib = xm._music_library
        names = []
        for s in stations:
            nm = s.get("name")
            raw = s.get("url")
            if not nm or not raw:
                continue
            # 注册时就用「归一化代理地址」，这样上一首/下一首也能保持响度一致
            url, _ = normalize.play_url_for(s.get("id"), raw)
            lib.all_music[nm] = url
            lib._all_radio[nm] = url       # 标记为电台：不做时长探测
            names.append(nm)
        if not names:
            return
        lib.music_list["网络电台"] = names

        dev = xm.devices.get(did)
        if dev is None:
            return
        dev._play_list = list(names)
        dev.device.cur_playlist = "网络电台"
        cur = stations[index].get("name")
        if cur in names:
            dev.device.playlist2music["网络电台"] = cur
        log.info(f"电台播放队列已建立：{len(names)} 个，当前 {cur}")
    except Exception as e:
        log.warning(f"建立电台播放队列失败: {e}")


@router.post("/play")
async def radio_play(data: dict = Body(...), _=Depends(verification)):
    """播放电台：把电台流地址推送给小爱音箱。

    可选传入 queue（当前列表的电台 id 数组），用于支持上一首/下一首。
    响度均衡开启时推送的是本机归一化代理地址，而不是电台原始地址。
    传 dry_run=true 时只解析地址、不推给音箱（用于自检/排障）。
    """
    did = data.get("did") or ""
    sid = data.get("id")
    r = get_radio()
    st = r.get(sid) if sid else None
    if st is None and data.get("url"):
        st = {"id": sid or "", "name": data.get("name") or "自定义电台",
              "url": data["url"]}
    if st is None:
        return {"ret": "NotExist"}
    if not xm.did_exist(did):
        return {"ret": "DidNotExist"}

    raw = st["url"]
    play_url, mode = normalize.play_url_for(st.get("id") or sid, raw)
    log.info(f"小米爱听·播放电台 [{st.get('name')}] 响度均衡={mode} -> {play_url}")

    if data.get("dry_run"):
        return {"ret": "OK", "dry_run": True, "name": st.get("name"),
                "url": play_url, "raw_url": raw, "normalize": mode}

    # 播前连通性探测：音源被限流/临时故障时直接给出提示，
    # 而不是把死地址推给音箱、让用户「点了没反应」。
    if not data.get("skip_probe"):
        ok, detail = await normalize.probe_source(raw)
        if not ok:
            log.warning(f"电台音源不可达 [{st.get('name')}]: {detail}")
            return {"ret": "Unreachable", "name": st.get("name"),
                    "msg": f"该电台音源当前连不上（{detail}）。"
                           f"可能是音源临时故障或被限流，请稍后重试或换个台。"}

    try:
        await xm.play_url(did, play_url)
    except Exception as e:
        log.error(f"电台播放失败: {e}")
        return {"ret": "Error", "msg": str(e)}

    if sid:
        r.mark_played(sid)

    # 建立播放队列（支持上一首/下一首）
    queue_ids = data.get("queue") or []
    stations, index = [], 0
    if queue_ids:
        for i, qid in enumerate(queue_ids):
            q = r.get(qid)
            if q:
                if qid == sid:
                    index = len(stations)
                stations.append({"id": q["id"], "name": q["name"],
                                 "url": q["url"]})
    if not stations:
        stations, index = [{"id": st.get("id") or sid or "",
                            "name": st.get("name"), "url": raw}], 0
    _setup_radio_queue(did, stations, index)

    await _mark_device_playing(did, st.get("name") or "网络电台")
    return {"ret": "OK", "name": st.get("name"), "url": play_url,
            "normalize": mode, "queue_len": len(stations)}


@router.post("/stop")
async def radio_stop(data: dict = Body(...), _=Depends(verification)):
    """停止播放"""
    did = data.get("did") or ""
    if not xm.did_exist(did):
        return {"ret": "DidNotExist"}
    try:
        await xm.stop(did)
    except Exception:
        try:
            await xm.devices[did].stop()
        except Exception as e:
            return {"ret": "Error", "msg": str(e)}
    return {"ret": "OK"}


# ---------------------------------------------------------------- 响度均衡

@router.get("/normalize")
def normalize_get(_=Depends(verification)):
    """查询响度均衡设置与运行状态"""
    r = get_radio()
    s = r.get_settings()
    return {"ret": "OK", **normalize.status(),
            "settings": {"normalize": bool(s.get("normalize", True)),
                         "normalize_rms": s.get("normalize_rms"),
                         "normalize_max_gain": s.get("normalize_max_gain")}}


@router.post("/normalize")
def normalize_set(data: dict = Body(...), _=Depends(verification)):
    """开启/关闭响度均衡，或调整目标响度。

    参数：enabled(bool) / target_rms(0.01-0.5) / max_gain(1-30)
    """
    r = get_radio()
    kw = {}
    if data.get("enabled") is not None:
        kw["normalize"] = bool(data["enabled"])
    if data.get("target_rms") is not None:
        kw["normalize_rms"] = max(0.01, min(0.5, float(data["target_rms"])))
    if data.get("max_gain") is not None:
        kw["normalize_max_gain"] = max(1.0, min(30.0, float(data["max_gain"])))
    if kw:
        r.set_settings(**kw)
    s = r.get_settings()
    log.info(f"响度均衡设置已更新: {kw}")
    return {"ret": "OK", **normalize.status(),
            "settings": {"normalize": bool(s.get("normalize", True)),
                         "normalize_rms": s.get("normalize_rms"),
                         "normalize_max_gain": s.get("normalize_max_gain")}}

