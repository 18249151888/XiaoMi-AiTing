# -*- coding: utf-8 -*-
"""小米爱听 —— 集成引导。

在不修改上游源码的前提下，把「小米爱听」的路由注入到 xiaomusic 的
FastAPI 应用中。原理：包装 `xiaomusic.api.routers.register_routers`，
在其注册完原有路由后，追加我们自己的路由。
"""
import os
import sys

_INSTALLED = False


def install():
    """把小米爱听的路由挂载到上游应用（幂等）。"""
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import xiaomusic.api.routers as R
    except Exception as e:  # pragma: no cover
        print(f"[小米爱听] 无法导入上游路由模块: {e}", file=sys.stderr)
        return False

    _orig = R.register_routers

    def _patched(app):
        _orig(app)
        try:
            from xiaomusic.aiting.radio_api import router as radio_router
            from xiaomusic.aiting.aiting_api import router as aiting_router
            from xiaomusic.aiting.sleep_api import router as sleep_router
            from xiaomusic.aiting.normalize import router as stream_router
            from xiaomusic.aiting.netmusic_api import router as netmusic_router

            app.include_router(radio_router, prefix="/api/aiting/radio",
                               tags=["网络收音机"])
            # 响度归一化拉流端点（免鉴权，供音箱直接拉流）
            app.include_router(stream_router, prefix="/api/aiting/radio",
                               tags=["响度均衡"])
            app.include_router(sleep_router, prefix="/api/aiting/sleep",
                               tags=["睡眠模式"])
            app.include_router(aiting_router, prefix="/api/aiting",
                               tags=["小米爱听"])
            # 网络搜歌：搜索歌曲并下载到 NAS 音乐库「网络歌曲」文件夹
            app.include_router(netmusic_router, prefix="/api/aiting/netmusic",
                               tags=["网络搜歌"])
            print("[小米爱听] 路由注册完成：/api/aiting/**", file=sys.stderr)
        except Exception as e:
            import traceback
            print(f"[小米爱听] 路由注册失败: {e}", file=sys.stderr)
            traceback.print_exc()

    R.register_routers = _patched

    # app.py 内部是 `from xiaomusic.api.routers import register_routers` 运行时导入，
    # 因此补丁生效；这里再兜底一次，兼容不同上游版本。
    try:
        import xiaomusic.api.app as A
        if hasattr(A, "register_routers"):
            A.register_routers = _patched
    except Exception:
        pass

    _INSTALLED = True
    return True


def ensure_default_config(conf_dir="/app/conf"):
    """首次运行时生成最小配置模板，便于新用户直接改或走网页配置。"""
    try:
        os.makedirs(conf_dir, exist_ok=True)
        path = os.path.join(conf_dir, "setting.json")
        if os.path.exists(path):
            return False
        import json
        tpl = {
            "account": "",
            "password": "",
            "mi_did": "",
            "hostname": "",
            "port": int(os.environ.get("XIAOMUSIC_PORT", "8091")),
            "public_port": int(os.environ.get("XIAOMUSIC_PORT", "8091")),
            "music_path": "music",
            "conf_path": "conf",
            "disable_httpauth": True,
            "music_path_depth": 10,
            "exclude_dirs": "@eaDir,tmp",
            "get_duration_type": "ffprobe",
            "ffmpeg_location": "./ffmpeg/bin",
            "enable_pull_ask": True,
            "recently_added_playlist_len": 50,
            "search_music_count": 100,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tpl, f, ensure_ascii=False, indent=2)
        print(f"[小米爱听] 已生成初始配置模板 {path}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[小米爱听] 生成配置模板失败: {e}", file=sys.stderr)
        return False
