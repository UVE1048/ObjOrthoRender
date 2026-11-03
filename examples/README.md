# 样例数据

一份**完全合成**的最小模型，用于验证安装是否正常，不需要准备任何自有数据。

```bash
python render_orthophotos.py --input examples/sample --output out --gsd 0.5
```

## 文件说明

| 文件 | 内容 |
| :--- | :--- |
| `metadata.xml` | `SRS=EPSG:3857`，`SRSOrigin=0,0,0` |
| `model.obj` | 4 个轴对齐盒子：1 块 120×120 地面 + 3 栋不同高度的"楼" |
| `model.mtl` | 单材质，贴图指向 `diffuse.png` |
| `diffuse.png` | 64×64 棋盘格，带对角线和边框 |

## 为什么用棋盘格贴图

棋盘格能直接暴露两类常见问题：

- **UV 的 V 轴翻转** —— 对角线方向会整体镜像
- **UV 缩放错误** —— 格子数会与预期不符（正常情况下每个面 4×4 格）

比纯色贴图更容易定位问题。

## 期望输出

`out/merged_all.tif`，RGB 三波段，240×240 像素 @ 0.5 m GSD（即 120 m 见方），`EPSG:3857`，原点 `0,0`。

地面占据大部分区域，三栋"楼"在正射视角下是三个实心矩形——**它们的高度差异在正射投影里不可见**，这正是正射影像的定义。

### 如果输出是全白

日志里会有一条：

```text
[WARN] Textured rendering failed; falling back to untextured white material ...
       reason: ArgumentError: ... glGenTextures ...
```

说明贴图上传失败、回退成了白色材质。几何与地理参考仍然正确，只是没有纹理。多见于 macOS + Python 3.13 的 pyrender / PyOpenGL 兼容问题——换 Python 3.12 或改用 Docker 镜像即可。

## 说明

这份数据是程序生成的几何体，与任何真实地点无关，可以自由使用与修改。
