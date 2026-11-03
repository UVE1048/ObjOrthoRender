# ObjOrthoRender v1.0.3 使用说明（Docker / CLI / GUI）

本工具用于将一批带纹理的 OBJ 模型渲染为带地理参考的 GeoTIFF 正射影像，并支持合并输出。

## 1. 导入镜像（离线 tar 包，可选）

如果你拿到的是离线镜像包（例如 `obj-ortho-cli_1.0.3.tar`），在目标机器上导入：

```bash
docker load -i obj-ortho-cli_1.0.3.tar
```


## 2. CLI 运行和使用（Docker）

Docker CLI 默认只需要挂载输入目录到容器内 `/data`，输出会写入输入目录下的 `MergedDOM/`。

### 2.1 运行方式（推荐）

参数说明（可选）：

*   `--input`：输入文件夹（容器内路径；递归查找 `.obj`）。不填默认 `/data`
*   `--metadata`：可选，`metadata.xml` 文件路径（容器内路径）。不填默认使用 `--input/metadata.xml`
*   `--output`：可选，输出文件夹（容器内路径）。不填默认输出到 `--input/MergedDOM`
*   `--gsd`：分辨率（米/像素），默认 `0.05`
*   `--workers`：并行进程数，默认 `4`
*   `--merge-mode`：合并模式，默认 `all`
    *   `all`：合并全部为 `MergedDOM/merged_all.tif`，并删除单张 TIFF（仅保留合并结果）
    *   `subfolder`：按输出子文件夹合并，并删除单张 TIFF（仅保留合并结果）
    *   `none`：不合并，保留全部单张 TIFF
*   `--fill-holes`：填洞模式（基于 mask 的后处理），默认 `on`
    *   `off`：关闭填洞
    *   `on`：默认强度（推荐）
    *   `fast`：更快（轻量填洞）
    *   `strong`：更强（更适合大面积水面洞，但更耗时）

Windows（PowerShell）示例（默认参数即可运行）：

```powershell
docker run --rm -it -v "D:\path\to\input:/data" obj-ortho-cli:1.0.3
```

Linux/macOS（bash）示例：

```bash
docker run --rm -it -v /host/input:/data obj-ortho-cli:1.0.3
```

### 2.2 常用示例

挂载一个“根目录”，但指定其中某个子目录作为输入（并指定 metadata.xml 路径）：

```powershell
docker run --rm -it -v "G:\tmp\models:/data" obj-ortho-cli:1.0.3 --input "/data/ProjectA/Mesh" --metadata "/data/ProjectA/metadata.xml" --output "/data/ProjectA/MergedDOM"
```

合并所有（默认行为）：

```powershell
docker run --rm -it -v "D:\path\to\input:/data" obj-ortho-cli:1.0.3 --merge-mode all
```

按子文件夹合并：

```powershell
docker run --rm -it -v "D:\path\to\input:/data" obj-ortho-cli:1.0.3 --merge-mode subfolder
```

不合并，仅输出每个 OBJ 对应的单张 TIFF：

```powershell
docker run --rm -it -v "D:\path\to\input:/data" obj-ortho-cli:1.0.3 --merge-mode none
```

提高并行度（例如 8）：

```powershell
docker run --rm -it -v "D:\path\to\input:/data" obj-ortho-cli:1.0.3 --workers 8
```

关闭填洞：

```powershell
docker run --rm -it -v "D:\path\to\input:/data" obj-ortho-cli:1.0.3 --fill-holes off
```

强化填洞（适合大面积水面洞）：

```powershell
docker run --rm -it -v "D:\path\to\input:/data" obj-ortho-cli:1.0.3 --fill-holes strong
```

### 2.3 进度显示

CLI 会显示渲染与合并的进度条。

## 3. 输入数据格式

输入目录必须包含：

1. `metadata.xml`：定义坐标系与原点（用于 GeoTIFF 地理参考）

```xml
<Metadata>
    <SRS>EPSG:3857</SRS>
    <SRSOrigin>0,0,0</SRSOrigin>
</Metadata>
```

2. OBJ 模型与纹理：

*   `.obj` 可位于任意子目录
*   `.mtl` 与贴图文件（png/jpg 等）需能通过 OBJ/MTL 中的相对路径被正确读取

## 5. 输出产物与目录结构

默认输出目录：

```text
<INPUT_DIR>/MergedDOM
```


## 6. 注意事项

*   渲染依赖 OpenGL。Docker 版本镜像内置 Xvfb + Mesa 软件 OpenGL，可在无显卡/无显示环境运行，但速度可能慢于 GPU 环境。
*   `--merge-mode all` 在数据量非常大时会生成超大 GeoTIFF，可能消耗较多磁盘空间；合并阶段优先使用 GDAL（更快）。

## 7. GUI 参数说明（目录选择）

GUI 中主要需要选择/填写三项路径：

*   输入文件夹：选择包含 OBJ 模型的目录（OBJ 可以在子目录中，程序会递归查找）
*   metadata.xml（可选）：不填则默认使用“输入文件夹下的 metadata.xml”；填写后会优先使用该路径
*   输出文件夹：选择保存结果的目录（GUI 模式下不会默认写到输入目录的 `MergedDOM`，以此处选择为准）
