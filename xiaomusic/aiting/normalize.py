# -*- coding: utf-8 -*-
"""小米爱听 —— 电台响度归一化代理。

【要解决的问题】
电台库里的电台来自完全不同的 CDN（蜻蜓FM 64k mp3、CNR HLS、RTHK HLS、
Akamai HLS 等），各台音源自身响度差异实测可达 6~7 dB，听感接近「音量翻倍」。
切台时音箱音量其实没变，但耳朵会觉得「突然大声了」。

【方案】
不再把原始流地址直接推给音箱，而是推本机代理地址：

    http://<host>:<port>/api/aiting/radio/stream/<sid>.mp3?t=<token>

代理用 ffmpeg 的 dynaudnorm 做「按 RMS 的动态响度归一化」——注意不是按峰值：
实测各台峰值只差 1 dB 左右，而 RMS 差 6~7 dB，所以峰值归一化解决不了问题。
归一化后再过 alimiter 限幅防止削波，最后以 mp3 推给音箱。
这样所有电台到达音箱时响度基本一致，切台不再忽大忽小。

【安全与健壮性】
- 该端点必须免鉴权（小爱音箱不会做 HTTP Basic 认证），因此用 token 做轻量保护，
  避免被当成公开代理滥用。密钥持久化在 conf 卷里 —— 见下方「流密钥」注释，
  容器重启换密钥会把正在收听的音箱打断。
- 端点「自愈」：归一化链路拿不到音频时，自动退化为「只转码不归一化」再试一次，
  保证电台永远能出声，最坏情况只是回到原来的响度不齐。
- 限制并发转码数，避免拖垮 NAS。
"""
import asyncio
import hashlib
import os
import secrets
import shutil
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

router = APIRouter()

# ---------------------------------------------------------------- 常量

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

# ---------------------------------------------------------------- 流密钥
#
# 推给音箱的流地址形如 .../radio/stream/<sid>.mp3?t=<token>，token 由这个密钥派生。
#
# 【为什么密钥必须持久化】
# 音箱拿到地址后会**一直用同一个地址拉流**，直到用户换台或停止，中途不会重新取址。
# 早期版本用进程级随机密钥，容器一重建密钥就变，于是：
#     音箱仍拿着旧地址 -> 每次请求都 403 -> 拿不到音频数据 -> 播放中断
# 表现就是「听着听着自己停了」，而日志里只有一串看不懂的 403。
# （真实案例：部署新版本重建容器，正好把用户正在听的电台打断。）
# 所以密钥落在 conf 卷里，重建容器后 token 保持不变。
_SECRET_FILE = "stream_secret"
_SECRET = None


def _load_secret():
    """读取（首次则生成）流密钥。任何异常都退化成临时密钥，绝不影响启动。"""
    try:
        # 延迟导入：radio_api 与本模块互相引用，模块级导入会撞上循环依赖。
        # _conf_dir 是「配置目录」的唯一来源，这里复用以免两处逻辑走偏。
        from .radio_api import _conf_dir
        path = os.path.join(_conf_dir(), _SECRET_FILE)
    except Exception:
        path = ""

    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = f.read().strip()
            if len(s) >= 32:
                return s
        except FileNotFoundError:
            pass
        except Exception as e:
            log().warning(f"读取流密钥失败，本次改用临时密钥: {e}")
            return secrets.token_hex(16)

        s = secrets.token_hex(16)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(s)
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            log().info(f"已生成并保存流密钥：{path}")
        except Exception as e:
            log().warning(f"保存流密钥失败，本次改用临时密钥: {e}")
        return s

    return secrets.token_hex(16)


def _secret():
    """延迟初始化密钥。

    不能在模块级直接取：那时 radio_api 还没加载完，拿不到 conf 目录。
    """
    global _SECRET
    if _SECRET is None:
        _SECRET = _load_secret()
    return _SECRET


# ---------------------------------------------------------------- 运行参数

MAX_CONCURRENT = 4        # 同时最多几个转码进程
HARD_TIMEOUT = 25.0       # 单次尝试等首个音频块的硬上限（秒）
POLL_SLICE = 3.0          # 轮询首个音频块的切片（秒）
TOTAL_DEADLINE = 28.0     # 从收到请求到拿到首个音频块的总时限（秒）
ATTEMPTS_PER_MODE = 2     # 每种模式最多试几次（见下方"为什么要重试"）

# 为什么要重试：
#   实测发现部分 NAS（多网卡 + 多条等优先级默认路由 + 空策略路由表）会把 IPv4
#   出站流量轮询分摊到多个网关，其中有些网关根本不通。表现是「同一条地址，
#   有的连接通、有的超时」——因为源端口不同，哈希到的网关就不同。
#   每重试一次会换一个源端口，就有机会走到能通的网关。
#   IPv6 通常只有一条路由，不受影响。
CONNECT_TIMEOUT_US = 10000000   # 单次 TCP 连接超时（微秒），让坏路径尽快失败

# 归一化参数（可在后台调整，见 radio.py 的 settings）
TARGET_RMS = 0.125        # 目标 RMS 幅度，约 -18 dBFS
MAX_GAIN = 10.0           # dynaudnorm 最大增益倍数（约 +20 dB）
PEAK = 0.95               # 目标峰值
LIMIT = 0.95              # 限幅器上限
OUT_BITRATE = "128k"
OUT_SAMPLE_RATE = "44100"

# 部分 CDN 会按 UA 拦截，带一个常见浏览器 UA 更稳
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_active = set()           # 当前活跃的转码进程

_log = None


def log():
    """延迟获取 xiaomusic 的 logger（避免导入期依赖未初始化）。"""
    global _log
    if _log is None:
        try:
            from xiaomusic.api.dependencies import log as L
            _log = L
        except Exception:
            import logging
            _log = logging.getLogger("xiaomusic.aiting.normalize")
    return _log


# ---------------------------------------------------------------- 配置

def get_config():
    """读取归一化配置（默认开启）。"""
    cfg = {"enabled": True, "target_rms": TARGET_RMS, "max_gain": MAX_GAIN}
    try:
        from .radio_api import get_radio
        s = get_radio().get_settings()
        cfg["enabled"] = bool(s.get("normalize", True))
        cfg["target_rms"] = float(s.get("normalize_rms", TARGET_RMS))
        cfg["max_gain"] = float(s.get("normalize_max_gain", MAX_GAIN))
    except Exception:
        pass
    cfg["target_rms"] = max(0.01, min(0.5, cfg["target_rms"]))
    cfg["max_gain"] = max(1.0, min(30.0, cfg["max_gain"]))
    return cfg


def available():
    """ffmpeg 是否可用"""
    return bool(shutil.which("ffmpeg") or FFMPEG)


def _base_url():
    """本机对音箱可达的基地址，形如 http://192.168.1.100:8091

    取自配置里的 hostname（用户在「系统与设置」里填的 NAS 局域网地址），
    这里不写死任何地址 —— 每个用户的网络环境都不一样。
    """
    try:
        from xiaomusic.api.dependencies import config
        host = (getattr(config, "hostname", "") or "").strip().rstrip("/")
        if host and not host.startswith(("http://", "https://")):
            host = "http://" + host
        if not host:
            return ""
        port = getattr(config, "public_port", 0) or getattr(config, "port", 0)
        if ":" in host.split("//", 1)[-1]:
            return host          # hostname 里已经带了端口
        return f"{host}:{port}" if port else host
    except Exception:
        return ""


def token_for(sid):
    return hashlib.md5((str(sid) + _secret()).encode("utf-8")).hexdigest()[:16]


def build_stream_url(sid):
    """构造推给音箱的归一化流地址"""
    base = _base_url()
    if not base:
        return ""
    return f"{base}/api/aiting/radio/stream/{sid}.mp3?t={token_for(sid)}"


def play_url_for(sid, raw_url):
    """决定推给音箱的播放地址（同步，不做网络探测）。

    端点自身会「自愈」降级，所以这里不需要预检，直接给代理地址即可。

    Returns:
        (url, mode)   mode: "normalized" | "raw"
    """
    cfg = get_config()
    if not cfg["enabled"] or not available():
        return raw_url, "raw"
    if not sid or not raw_url:
        return raw_url, "raw"
    proxy = build_stream_url(sid)
    return (proxy, "normalized") if proxy else (raw_url, "raw")


async def probe_source(url, timeout=3.5, attempts=2):
    """快速探测音源主机能否建立 TCP 连接（会重试，理由见「为什么要重试」）。

    音源被限流、或本机 IPv4 多网关走错路时，如果直接把地址推给音箱，
    用户会「点了没反应、也没提示」。先探一下就能给出明确提示。

    Returns:
        (ok, detail)   ok=False 时 detail 是失败原因
    """
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception as e:
        return True, f"parse skip: {e}"
    host = p.hostname
    if not host:
        return True, "no host"
    port = p.port or (443 if p.scheme == "https" else 80)

    last = ""
    for i in range(max(1, attempts)):
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout)
            try:
                w.close()
                await asyncio.wait_for(w.wait_closed(), timeout=1)
            except Exception:
                pass
            if i:
                log().info(f"音源探测第 {i + 1} 次才成功 {host}:{port}")
            return True, "ok"
        except asyncio.TimeoutError:
            last = f"连接 {host}:{port} 超时"
        except Exception as e:
            last = f"连接 {host}:{port} 失败: {e}"
    return False, last


# ---------------------------------------------------------------- ffmpeg

def ffmpeg_cmd(src, cfg=None, normalize=True):
    cfg = cfg or get_config()
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        # 直播流默认要探测 5 秒才出第一帧，压到 2 秒，起播更快
        "-analyzeduration", "2000000", "-probesize", "2000000",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        # 单次连接超时，让"走错网关"的路径尽快失败，好让上层重试换条路
        "-timeout", str(CONNECT_TIMEOUT_US),
        "-user_agent", USER_AGENT,
        "-i", src,
        "-vn",
    ]
    if normalize:
        af = ("dynaudnorm=f=250:g=15:p={p}:m={m}:r={r},"
              "alimiter=limit={l}").format(
            p=PEAK, m=cfg["max_gain"], r=cfg["target_rms"], l=LIMIT)
        cmd += ["-af", af]
    cmd += [
        "-ar", OUT_SAMPLE_RATE, "-ac", "2",
        "-c:a", "libmp3lame", "-b:a", OUT_BITRATE,
        "-f", "mp3", "-",
    ]
    return cmd


def _kill(proc):
    try:
        proc.kill()
    except Exception:
        pass


async def _spawn(src, normalize):
    return await asyncio.create_subprocess_exec(
        *ffmpeg_cmd(src, normalize=normalize),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )


async def _reap(proc):
    _kill(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except Exception:
        pass


async def _err_text(proc, n=300):
    try:
        if proc.stderr:
            data = await asyncio.wait_for(proc.stderr.read(1200), timeout=2)
            return data.decode("utf-8", "replace").strip()[:n]
    except Exception:
        pass
    return ""


async def _read_slice(proc, timeout):
    """读一块音频。返回 (数据, 是否超时)"""
    try:
        return await asyncio.wait_for(proc.stdout.read(16384), timeout=timeout), False
    except asyncio.TimeoutError:
        return b"", True


async def _open_stream(src, normalize, deadline):
    """起一个转码进程并等首个音频块（单次尝试）。

    注意：只有「ffmpeg 明确退出」才算失败。单纯读超时不算失败——那通常只是音源
    起播慢，此时应该继续等，而不是白白重来一遍（重来也连不上同一个音源）。

    Args:
        deadline: 绝对时间戳（time.time()），超过就放弃；由调用方做多路重试

    Returns:
        (proc, first_chunk, err_text)  失败时 proc 为 None
    """
    try:
        proc = await _spawn(src, normalize)
    except Exception as e:
        return None, b"", f"spawn failed: {e}"
    _active.add(proc)

    while True:
        left = min(HARD_TIMEOUT, deadline - time.time())
        if left <= 0:
            break
        chunk, _ = await _read_slice(proc, min(POLL_SLICE, left))
        if chunk:
            return proc, chunk, ""
        if proc.returncode is not None:
            break                      # 进程已退出，肯定是失败了

    err = await _err_text(proc)
    _active.discard(proc)
    await _reap(proc)
    return None, b"", err


# ---------------------------------------------------------------- 拉流端点
#
# 注意：此端点故意不加 Depends(verification) —— 小爱音箱无法做 HTTP Basic 认证。
# 用 token 查询参数做轻量保护。

@router.get("/stream/{sid}")
async def stream(sid: str, request: Request, t: str = Query("")):
    sid = sid[:-4] if sid.endswith(".mp3") else sid

    if t != token_for(sid):
        raise HTTPException(status_code=403, detail="bad token")

    try:
        from .radio_api import get_radio
        st = get_radio().get(sid)
    except Exception:
        st = None
    if not st:
        raise HTTPException(status_code=404, detail="station not found")
    src = (st.get("url") or "").strip()
    if not src:
        raise HTTPException(status_code=404, detail="station url empty")
    if not available():
        raise HTTPException(status_code=502, detail="ffmpeg not available")
    if len(_active) >= MAX_CONCURRENT:
        raise HTTPException(status_code=503, detail="too many streams")

    name = st.get("name") or sid
    deadline = time.time() + TOTAL_DEADLINE

    proc = first = None
    err = ""
    mode = "normalized"

    # 1) 先试「归一化」链路；失败可重试（换源端口 -> 可能换到能通的网关）
    for _ in range(ATTEMPTS_PER_MODE):
        proc, first, err = await _open_stream(src, True, deadline)
        if proc is not None or time.time() >= deadline:
            break

    # 2) 仍拿不到音频 -> 降级为「只转码不归一化」，保证有声音
    if proc is None:
        log().warning(f"响度归一化失败 [{name}] err={err}")
        mode = "raw-transcode"
        for _ in range(ATTEMPTS_PER_MODE):
            proc, first, err = await _open_stream(src, False, deadline)
            if proc is not None or time.time() >= deadline:
                break

    if proc is None:
        log().error(f"电台拉流彻底失败 [{name}] err={err}")
        raise HTTPException(status_code=502, detail="stream unavailable")

    log().info(f"响度归一化拉流 [{name}] mode={mode} <- {src}")

    async def gen():
        try:
            chunk = first
            while chunk:
                if await request.is_disconnected():
                    break
                yield chunk
                chunk = await proc.stdout.read(16384)
        finally:
            _active.discard(proc)
            await _reap(proc)
            log().info(f"响度归一化结束 [{name}]")

    # icy-name 只是给播放器显示台名用的。HTTP 头值必须是可见 ASCII，
    # 而中文台名 encode("ascii","ignore") 后可能只剩空白（例如
    # "郑州人民广播电台 音乐广播" -> " "）。空白字符串是真值，会绕过 or 兜底，
    # 导致 h11 抛 LocalProtocolError: Illegal header value b' '，
    # 连接在发出任何响应前就被关闭 —— 表现为「这个台怎么点都没反应」。
    # 所以必须 strip 后再兜底。
    icy_name = name.encode("ascii", "ignore").decode().strip() or "radio"

    return StreamingResponse(
        gen(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "icy-name": icy_name},
    )


def status():
    """给后台看的运行状态"""
    cfg = get_config()
    return {
        "enabled": cfg["enabled"],
        "target_rms": cfg["target_rms"],
        "max_gain": cfg["max_gain"],
        "active": len(_active),
        "max_concurrent": MAX_CONCURRENT,
        "ffmpeg_ok": available(),
    }
