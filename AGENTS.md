# AGENTS.md

面向 AI 编码代理（Cursor / Codex / Copilot / Claude Code 等）的仓库说明。人类读者请先看 [README](README.md)。

## 这是什么

把带纹理的 OBJ 三维模型渲染成带地理参考的 GeoTIFF 正射影像（DOM）。纯 Python，OpenGL 离屏渲染。

## 关键文件

- `render_orthophotos.py` — 渲染核心 + CLI 入口。所有渲染、合并、填洞逻辑都在这里。改动前先读 `main()` 的参数处理与 `_in_container()`。
- `gui_app.py` — Tkinter 界面，只做参数收集与子进程调度，不含算法。
- `build_exe.py` / `release_build.py` — PyInstaller 打包，处理 PROJ 数据与 DLL 收集。
- `Dockerfile` — 无头运行镜像，内置 Xvfb + Mesa。

## 命令

```bash
pip install -r requirements.txt
python render_orthophotos.py --input <dir> --output <dir> --gsd 0.05 --workers 4 --merge-mode all
python gui_app.py
docker build -t obj-ortho-cli:1.0.3 .
```

## 约定

- 输出为 RGB 三波段、无 alpha，空白区域为黑色。不要改成 RGBA —— GIS 端透明叠加会导致边界异常。
- 坐标系只从 `metadata.xml` 读，不要在代码里硬编码 EPSG。
- 新增 CLI 参数必须同时加进 `gui_app.py` 的界面，否则 GUI 用户无法使用。
- `--input` 为必填，不要恢复成某个本地默认路径。

## 已知坑

- `pyrender` 在没有 OpenGL 3.3+ 的环境会失败。Docker 路线靠 Xvfb + Mesa 规避。
- Linux 上 `osgeo.gdal` 只在装了 `gdal-bin libgdal-dev` 时可用，不可用时回退到 `rasterio.merge`。
- Windows 下 GDAL 的 wheel 难装，`requirements.txt` 里已用环境标记排除。
