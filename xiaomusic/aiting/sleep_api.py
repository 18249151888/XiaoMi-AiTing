# -*- coding: utf-8 -*-
"""小米爱听 —— 睡眠模式（渐弱定时关闭）。

场景：听着电台或音乐入睡，音量在设定时间内从当前音量线性衰减到 0，
到点后静音并停止播放。

挂载路径前缀：/api/aiting/sleep
"""
import asyncio
import time

from fastapi import APIRouter, Body, Depends, Query

from xiaomusic.api.dependencies import (
    log,
    verification,
    xiaomusic as xm,
)

from .aiting_api import read_volume, write_volume

router = APIRouter()

# {did: {...}}
_timers = {}
TICK_SEC = 20          # 衰减步长（秒）
MIN_MINUTES = 1
MAX_MINUTES = 480
DEFAULT_FALLBACK_VOLUME = 30   # 读不到当前音量时的兜底起始音量（取小值，避免突然变响）


class SleepTimer:
    def __init__(self, did, minutes, start_volume):
        self.did = did
        self.minutes = minutes
        self.total = minutes * 60
        self.start_volume = start_volume
        self.started_at = time.time()
        self.task = None
        self.cancelled = False
        self.finished = False

    @property
    def end_ts(self):
        return self.started_at + self.total

    def remain(self):
        return max(0, int(self.end_ts - time.time()))

    def target_volume(self):
        """按时间线性衰减的目标音量"""
        elapsed = time.time() - self.started_at
        if elapsed >= self.total:
            return 0
        ratio = 1 - (elapsed / self.total)
        return max(0, int(round(self.start_volume * ratio)))

    def to_dict(self):
        return {
            "did": self.did,
            "active": not (self.cancelled or self.finished),
            "minutes": self.minutes,
            "start_volume": self.start_volume,
            "current_volume": self.target_volume(),
            "remain": self.remain(),
            "remain_text": f"{self.remain() // 60}分{self.remain() % 60}秒",
            "started_at": int(self.started_at),
            "end_at": int(self.end_ts),
            "finished": self.finished,
        }


async def _run(t: SleepTimer):
    """渐弱主循环"""
    log.info(f"睡眠模式启动：did={t.did} 时长={t.minutes}分 起始音量={t.start_volume}")
    last = None
    try:
        while not t.cancelled:
            if time.time() >= t.end_ts:
                break
            v = t.target_volume()
            if v != last:
                try:
                    await write_volume(t.did, v)
                    last = v
                except Exception as e:
                    log.warning(f"睡眠模式调音量失败 vol={v}: {e}")
            await asyncio.sleep(TICK_SEC)

        if t.cancelled:
            return

        # 到点：静音 + 停止
        try:
            await write_volume(t.did, 0)
        except Exception as e:
            log.warning(f"睡眠模式静音失败: {e}")
        await asyncio.sleep(1)
        try:
            await xm.stop(t.did)
        except Exception as e:
            log.warning(f"睡眠模式停止播放失败: {e}")
        t.finished = True
        log.info(f"睡眠模式结束：did={t.did} 已静音并停止播放")
    except asyncio.CancelledError:
        log.info(f"睡眠模式任务被取消：did={t.did}")
    finally:
        if t.finished or t.cancelled:
            cur = _timers.get(t.did)
            if cur is t:
                pass  # 保留状态供前端查询，由下次启动覆盖


async def _cancel(did, restore=False):
    t = _timers.get(did)
    if not t:
        return False
    t.cancelled = True
    if t.task and not t.task.done():
        t.task.cancel()
    if restore:
        try:
            await write_volume(did, t.start_volume)
        except Exception as e:
            log.warning(f"恢复音量失败: {e}")
    return True


@router.post("/start")
async def sleep_start(data: dict = Body(...), _=Depends(verification)):
    """开启睡眠模式。

    参数：did, minutes（默认 30）, volume（可选，不传则用当前音量）
    """
    did = data.get("did") or ""
    if not xm.did_exist(did):
        return {"ret": "DidNotExist"}

    try:
        minutes = int(data.get("minutes") or 30)
    except Exception:
        minutes = 30
    minutes = max(MIN_MINUTES, min(MAX_MINUTES, minutes))

    # 起始音量：优先用传入值；否则读设备当前音量；读不到则用兜底值
    vol = data.get("volume")
    if vol is None:
        vol = await read_volume(did)      # 带缓存，读失败时返回上次已知值
    try:
        vol = int(vol)
    except Exception:
        vol = 0
    if vol <= 0:
        # 完全读不到音量（常见于音箱离线）时才用兜底值。
        # 取小值，避免「本来是小声，一开始渐弱反而变响」。
        vol = int(data.get("fallback_volume") or DEFAULT_FALLBACK_VOLUME)
    vol = max(0, min(100, vol))

    # 覆盖旧任务
    await _cancel(did)

    t = SleepTimer(did, minutes, vol)
    t.task = asyncio.create_task(_run(t))
    _timers[did] = t
    return {"ret": "OK", "timer": t.to_dict()}


@router.post("/rebase")
async def sleep_rebase(data: dict = Body(...), _=Depends(verification)):
    """手动调整音量后，让渐弱从新音量重新开始（保留剩余时间）。

    不这么做的话，用户手动调完音量，20 秒内会被定时器按旧基准拉回去，
    听感上就是「音量不受控」。

    参数：did, volume（可选，不传则读当前音量）
    """
    did = data.get("did") or ""
    t = _timers.get(did)
    if not t or t.cancelled or t.finished:
        return {"ret": "NotFound"}

    vol = data.get("volume")
    if vol is None:
        vol = await read_volume(did)
    try:
        vol = int(vol)
    except Exception:
        vol = 0
    if vol <= 0:
        vol = t.start_volume
    vol = max(0, min(100, vol))

    remain = t.remain()
    if remain <= 0:
        return {"ret": "NotFound"}

    # 重置基准：把「剩余时长」当作新的总时长，从新音量重新线性衰减，
    # 结束时刻保持不变。
    t.start_volume = vol
    t.total = remain
    t.started_at = time.time()
    log.info(f"睡眠模式基准已重置：did={did} 新起始音量={vol} 剩余={remain}s")
    return {"ret": "OK", "timer": t.to_dict()}


@router.post("/cancel")
async def sleep_cancel(data: dict = Body(default={}), _=Depends(verification)):
    """取消睡眠模式。restore=true 时恢复原始音量。"""
    did = (data or {}).get("did") or ""
    restore = bool((data or {}).get("restore"))
    ok = await _cancel(did, restore=restore)
    if not ok:
        return {"ret": "NotFound"}
    return {"ret": "OK"}


@router.get("/status")
def sleep_status(did: str = Query(...), _=Depends(verification)):
    """查询睡眠模式状态。

    注意：定时器「跑完」和「被取消」都必须报 active=false。
    早期版本只判断了 cancelled，导致跑完后前端一直显示
    「睡眠模式进行中：剩余 0分0秒」，永远不消失。

    另外「结束」和「取消」都是**一次性事件**：报一次之后就把定时器清掉。
    否则状态会一直挂在内存里，而前端的防重复标记（S._sleepDone）是内存变量、
    刷新即失效 —— 结果就是用户每次刷新页面都会再弹一次「睡眠模式已结束」。
    """
    t = _timers.get(did)
    if not t:
        return {"ret": "OK", "active": False}
    if t.cancelled:
        _timers.pop(did, None)
        return {"ret": "OK", "active": False, "cancelled": True}
    if t.finished:
        _timers.pop(did, None)
        return {"ret": "OK", "active": False, "finished": True,
                "timer": t.to_dict()}
    return {"ret": "OK", "active": True, "timer": t.to_dict()}


@router.get("/list")
def sleep_list(_=Depends(verification)):
    """所有设备上仍在运行的睡眠定时器（已结束/已取消的不列出）"""
    return {"ret": "OK", "items": [t.to_dict() for t in _timers.values()
                                   if not t.cancelled and not t.finished]}
