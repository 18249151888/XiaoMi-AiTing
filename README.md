# 小米爱听 XiaoMi-AiTing

在开源 [xiaomusic](https://github.com/hanxi/xiaomusic) 之上二次开发的小爱音箱本地播放增强面板，面向 NAS 部署。

## 功能

- 🎛️ **播放控制台**：设备选择、音量、播放模式、播放状态实时显示
- 📻 **网络收音机**：内置 1600+ 中文电台，支持收藏、自定义电台、连通性检测、响度均衡
- 🔍 **网络搜歌**：全网搜歌，一键下载到 NAS 音乐库「网络歌曲」文件夹，自动加入本地歌单
- 🎵 **本地音乐**：按 NAS 文件夹自动生成歌单
- 🌙 **睡眠模式**：音量渐弱定时关闭

## 网络搜歌

基于网易云音乐公开搜索接口搜索歌曲，通过 `outer/url` 跳转拿到 CDN mp3 后直接下载到音乐库。

- 搜索接口：`GET /api/aiting/netmusic/search?keyword=歌名/歌手&limit=30`
- 下载接口：`POST /api/aiting/netmusic/download`，body `{id, name, artist, play_after?, did?}`
- 已下载列表：`GET /api/aiting/netmusic/local`
- 下载目录：音乐库根目录下的 **`网络歌曲`** 文件夹，落盘后自动重新扫描音乐库，立即出现在「本地音乐 → 网络歌曲」歌单
- VIP / 无版权歌曲会返回失败提示，换个版本或翻唱即可

## 目录结构

```
aiting_main.py            程序入口（注入爱听增强路由）
xiaomusic/aiting/
  boot.py                 路由注入（不修改上游源码）
  aiting_api.py           设备/播放/音乐库/系统增强 API
  radio.py / radio_api.py 网络收音机
  netmusic.py / netmusic_api.py  网络搜歌与下载
  sleep_api.py            睡眠模式
  normalize.py            响度归一化拉流
  web/                    前端面板（原生 JS，无构建）
```

## 部署

Docker 运行，挂载：

- `/app/conf`  ← 配置目录
- `/app/music` ← NAS 音乐库（下载的网络歌曲存放在其下的 `网络歌曲/`）

镜像环境：Python 3.12 + FastAPI，仅依赖标准库实现搜歌下载，无额外三方包。
