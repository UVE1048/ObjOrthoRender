import os
import glob
import numpy as np
import trimesh
import pyrender
import xml.etree.ElementTree as ET
from PIL import Image
import rasterio
from rasterio.merge import merge as rasterio_merge
from rasterio.windows import Window
import argparse
from collections import defaultdict
import concurrent.futures
import threading
import queue
import traceback
import gc
import sys
import time
import multiprocessing
from scipy import ndimage

# Ensure PROJ_LIB is set correctly for bundled environment (Crucial for workers)
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    try:
        candidates = [
            os.path.join(base_path, 'rasterio', 'proj_data'),
            os.path.join(base_path, 'Library', 'share', 'proj'),
            os.path.join(base_path, 'proj_data_share'),
            os.path.join(base_path, 'osgeo', 'data', 'proj'),
            os.path.join(base_path, 'pyproj', 'proj_dir', 'share', 'proj'),
        ]
        import sqlite3

        def _proj_db_minor(d):
            try:
                db = os.path.join(d, 'proj.db')
                if not os.path.exists(db):
                    return -1
                con = sqlite3.connect(db)
                cur = con.cursor()
                rows = dict(cur.execute('select key,value from metadata').fetchall())
                con.close()
                return int(rows.get('DATABASE.LAYOUT.VERSION.MINOR', '-1'))
            except Exception:
                return -1

        best_dir = None
        best_minor = -1
        for d in candidates:
            m = _proj_db_minor(d)
            if m > best_minor:
                best_minor = m
                best_dir = d
    except Exception:
        best_dir = None
    # Priority 1: Library/share/proj
    p1 = os.path.join(base_path, 'Library', 'share', 'proj')
    # Priority 2: proj_data_share
    p2 = os.path.join(base_path, 'proj_data_share')
    
    proj_lib_path = None
    if best_dir and os.path.exists(os.path.join(best_dir, 'proj.db')):
        proj_lib_path = best_dir
    elif os.path.exists(os.path.join(p1, 'proj.db')):
        proj_lib_path = p1
    elif os.path.exists(os.path.join(p2, 'proj.db')):
        proj_lib_path = p2
    
    if proj_lib_path:
        os.environ['PROJ_LIB'] = proj_lib_path
        os.environ['PROJ_DATA'] = proj_lib_path
        # print(f"Worker PROJ_LIB set to: {proj_lib_path}")
    try:
        dll_dirs = [
            base_path,
            os.path.join(base_path, 'Library', 'bin'),
            os.path.join(base_path, 'lib'),
        ]
        existing = os.environ.get('PATH', '')
        prepend = [d for d in dll_dirs if os.path.isdir(d)]
        os.environ['PATH'] = os.pathsep.join(prepend + [existing])
        for d in prepend:
            try:
                os.add_dll_directory(d)
            except Exception:
                pass
    except Exception:
        pass

# Constants
DEFAULT_GSD = 0.05  # 5 cm per pixel, adjust as needed

def _in_container():
    if os.environ.get("IN_DOCKER") == "1":
        return True
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")

def _is_main_process():
    try:
        return multiprocessing.current_process().name == "MainProcess"
    except Exception:
        return True

def _format_duration(seconds):
    try:
        seconds = int(round(float(seconds)))
    except Exception:
        seconds = 0
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def _fill_holes_inpaint_geotiff(path, small_max_area=4000, large_iters=200):
    with rasterio.open(path) as src:
        img = src.read()
        mask = src.dataset_mask()

    if img.ndim != 3 or img.shape[0] < 3:
        return {"filled_pixels": 0, "holes": 0, "components": 0}

    valid = mask > 0
    filled_valid = ndimage.binary_fill_holes(valid)
    holes = filled_valid & (~valid)
    holes_count = int(holes.sum())
    if holes_count <= 0:
        return {"filled_pixels": 0, "holes": 0, "components": 0}

    labels, n = ndimage.label(holes)
    if n <= 0:
        return {"filled_pixels": 0, "holes": holes_count, "components": 0}

    objects = ndimage.find_objects(labels)
    h, w = valid.shape
    out = img.astype(np.float32, copy=True)
    use_diffuse = holes_count <= 20000000

    for idx, slc in enumerate(objects, start=1):
        if slc is None:
            continue
        comp = labels[slc] == idx
        area = int(comp.sum())
        if area <= 0:
            continue

        r0, r1 = slc[0].start, slc[0].stop
        c0, c1 = slc[1].start, slc[1].stop
        pad = 2
        rr0 = max(0, r0 - pad)
        rr1 = min(h, r1 + pad)
        cc0 = max(0, c0 - pad)
        cc1 = min(w, c1 + pad)
        box = (slice(rr0, rr1), slice(cc0, cc1))

        sub_valid = valid[box]
        sub_invalid = ~sub_valid
        sub_hole = labels[box] == idx

        if not np.any(sub_hole):
            continue

        _, inds = ndimage.distance_transform_edt(sub_invalid.astype(np.uint8), return_indices=True)
        ir = inds[0]
        ic = inds[1]

        for b in range(min(3, out.shape[0])):
            sub = out[b, box[0], box[1]]
            sub[sub_hole] = sub[ir[sub_hole], ic[sub_hole]]
            out[b, box[0], box[1]] = sub

        if use_diffuse and area > small_max_area and large_iters > 0:
            iters = int(min(large_iters, max(20, (np.sqrt(area) * 2.5))))
            kernel = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.float32) / 8.0
            for _ in range(iters):
                for b in range(min(3, out.shape[0])):
                    sub = out[b, box[0], box[1]]
                    blurred = ndimage.convolve(sub, kernel, mode="nearest")
                    sub[sub_hole] = blurred[sub_hole]
                    out[b, box[0], box[1]] = sub

    out_u8 = np.clip(out[:3], 0.0, 255.0).astype(np.uint8, copy=False)
    valid2 = valid | holes
    mask_out = (valid2.astype(np.uint8) * 255)

    with rasterio.open(path, "r+") as dst:
        dst.write(out_u8)
        try:
            dst.write_mask(mask_out)
        except Exception:
            pass

    return {"filled_pixels": holes_count, "holes": holes_count, "components": int(n)}

class ProgressPrinter:
    def __init__(self, total, prefix):
        self.total = max(0, int(total))
        self.prefix = str(prefix)
        self._last_print = -1
        self._last_len = 0
        self._last_current = 0
        self._last_detail = ""
        self._start = time.perf_counter()
        self._force = os.environ.get("FORCE_PROGRESS") == "1"
        self._disable = os.environ.get("NO_PROGRESS") == "1"
        self._tty = (sys.stdout.isatty() or self._force) and not self._disable
        self._step = max(1, self.total // 50) if self.total > 0 else 1

    def _bar(self, current, width=30):
        if self.total <= 0:
            return ("-" * width), 0
        current = max(0, min(self.total, int(current)))
        ratio = current / float(self.total)
        filled = int(round(ratio * width))
        filled = max(0, min(width, filled))
        bar = ("#" * filled) + ("-" * (width - filled))
        percent = int(round(ratio * 100))
        percent = max(0, min(100, percent))
        return bar, percent

    def update(self, current, detail=""):
        if self._disable:
            return
        current = max(0, int(current))
        bar, percent = self._bar(current, width=22)
        detail = (detail or "").strip()
        elapsed = time.perf_counter() - self._start
        if self.total > 0 and current > 0:
            rate = elapsed / float(current)
            eta = max(0.0, (self.total - current) * rate)
        else:
            eta = 0.0
        line = f"{self.prefix} [{bar.replace('#', '█').replace('-', '░')}] {percent:3d}% {current}/{self.total} { _format_duration(elapsed) } ETA { _format_duration(eta) }"
        if detail:
            line = f"{line} {detail}"
        self._last_current = current
        self._last_detail = detail

        if self._tty:
            pad = ""
            if self._last_len > len(line):
                pad = " " * (self._last_len - len(line))
            sys.stdout.write("\r" + line + pad)
            sys.stdout.flush()
            self._last_len = max(self._last_len, len(line))
            return

        if current == self.total or current == 0 or (current - self._last_print) >= self._step:
            print(line, flush=True)
            self._last_print = current

    def log(self, message):
        if message is None:
            return
        message = str(message)
        if self._disable or not self._tty:
            print(message, flush=True)
            return
        sys.stdout.write("\r" + (" " * self._last_len) + "\r")
        sys.stdout.flush()
        print(message, flush=True)
        self.update(self._last_current, self._last_detail)

    def finish(self):
        if self._disable:
            return
        if self._tty:
            sys.stdout.write("\r" + (" " * self._last_len) + "\r\n")
            sys.stdout.flush()

def _safe_unlink(path):
    try:
        os.remove(path)
        return True
    except Exception:
        return False

def _prune_empty_dirs(root_dir):
    try:
        if not root_dir or not os.path.isdir(root_dir):
            return
        for current_root, dirnames, filenames in os.walk(root_dir, topdown=False):
            if current_root == root_dir:
                continue
            if filenames:
                continue
            if dirnames:
                continue
            try:
                os.rmdir(current_root)
            except Exception:
                pass
    except Exception:
        return

class AsyncTileWriter:
    def __init__(self, dst):
        self.dst = dst
        self.queue = queue.Queue(maxsize=4) # Buffer a few tiles
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.error = None
        self.thread.start()

    def _worker(self):
        while True:
            item = self.queue.get()
            if item is None:
                break
            try:
                kind, data, window = item
                if kind == "data":
                    self.dst.write(data, window=window)
                elif kind == "mask":
                    self.dst.write_mask(data, window=window)
                else:
                    raise ValueError(f"Unknown write kind: {kind}")
            except Exception as e:
                self.error = e
            finally:
                self.queue.task_done()

    def write(self, tile_data, window):
        if self.error:
            raise self.error
        self.queue.put(("data", tile_data, window))

    def write_mask(self, mask, window):
        if self.error:
            raise self.error
        self.queue.put(("mask", mask, window))

    def close(self):
        self.queue.put(None)
        self.thread.join()
        if self.error:
            raise self.error

def load_metadata(xml_path):
    """
    Parses the metadata.xml file to extract SRS and Origin.
    """
    if not os.path.exists(xml_path):
        raise FileNotFoundError(f"Metadata file not found: {xml_path}")
    
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    srs_text = root.find('SRS').text
    origin_text = root.find('SRSOrigin').text
    
    # Parse origin "x,y,z"
    origin = [float(x) for x in origin_text.split(',')]
    
    return {
        'srs': srs_text,
        'origin': origin # [x, y, z]
    }

def get_mesh_bounds(mesh):
    """
    Returns the min and max bounds of the mesh.
    """
    return mesh.bounds # [[minx, miny, minz], [maxx, maxy, maxz]]

def _resize_image_to_limit(image, max_dim):
    if image is None:
        return None, False
    if isinstance(image, np.ndarray):
        if image.ndim == 2:
            image = Image.fromarray(image, mode='L')
        elif image.ndim == 3 and image.shape[2] == 3:
            image = Image.fromarray(image, mode='RGB')
        elif image.ndim == 3 and image.shape[2] == 4:
            image = Image.fromarray(image, mode='RGBA')
        else:
            return image, False
    if not hasattr(image, 'size'):
        return image, False
    w, h = image.size
    if w <= max_dim and h <= max_dim:
        return image, False
    new_w = min(w, max_dim)
    new_h = min(h, max_dim)
    try:
        resample = Image.Resampling.LANCZOS
    except Exception:
        resample = Image.LANCZOS
    resized = image.resize((new_w, new_h), resample)
    return resized, True

def _apply_texture_resize(mesh, max_dim):
    try:
        if isinstance(mesh, trimesh.Scene):
            did_any = False
            for geom in mesh.geometry.values():
                did_any = _apply_texture_resize(geom, max_dim=max_dim) or did_any
            return did_any
        visual = getattr(mesh, "visual", None)
        material = getattr(visual, "material", None)
        if material is None:
            return False
        if hasattr(material, "materials"):
            did_any = False
            for sub in getattr(material, "materials", []) or []:
                if not hasattr(sub, "image"):
                    continue
                image = getattr(sub, "image", None)
                resized, did = _resize_image_to_limit(image, max_dim)
                if did:
                    sub.image = resized
                    did_any = True
            return did_any
        if not hasattr(material, "image"):
            return False
        image = getattr(material, "image", None)
        resized, did = _resize_image_to_limit(image, max_dim)
        if did:
            material.image = resized
        return did
    except Exception:
        return False

def _get_first_texture_image(mesh):
    try:
        if isinstance(mesh, trimesh.Scene):
            for geom in mesh.geometry.values():
                img = _get_first_texture_image(geom)
                if img is not None:
                    return img
            return None
        visual = getattr(mesh, "visual", None)
        material = getattr(visual, "material", None)
        if material is None:
            return None
        if hasattr(material, "materials"):
            for sub in getattr(material, "materials", []) or []:
                img = getattr(sub, "image", None)
                if img is not None:
                    return img
            return None
        return getattr(material, "image", None)
    except Exception:
        return None

def _iter_trimesh_geometries(mesh):
    if isinstance(mesh, trimesh.Scene):
        for geom in mesh.geometry.values():
            if isinstance(geom, trimesh.Trimesh):
                yield geom
        return
    if isinstance(mesh, trimesh.Trimesh):
        yield mesh

def render_and_save_tiled(mesh, output_path, gsd, srs_code, global_origin, tile_size=2048):
    """
    Renders the mesh in tiles and writes directly to a GeoTIFF to avoid memory/GL limits.
    """
    bounds = mesh.bounds
    min_x, min_y, min_z = bounds[0]
    max_x, max_y, max_z = bounds[1]
    
    width_m = max_x - min_x
    height_m = max_y - min_y
    
    full_width = int(np.ceil(width_m / gsd))
    full_height = int(np.ceil(height_m / gsd))
    
    # Ensure at least 1 pixel
    full_width = max(1, full_width)
    full_height = max(1, full_height)
    
    original_image = _get_first_texture_image(mesh)
    
    def _build_scene(use_textures: bool):
        # Default ambient light
        scene = pyrender.Scene(bg_color=[0, 0, 0, 0], ambient_light=[1.0, 1.0, 1.0])
        
        # Standard directional light
        dl = pyrender.DirectionalLight(color=[1.0, 1.0, 1.0], intensity=1.0)
        scene.add(dl, pose=np.eye(4))
        
        if use_textures:
            for geom in _iter_trimesh_geometries(mesh):
                pr_mesh = pyrender.Mesh.from_trimesh(geom)
                if isinstance(pr_mesh, (list, tuple)):
                    for pm in pr_mesh:
                        scene.add(pm)
                else:
                    scene.add(pr_mesh)
        else:
            material = pyrender.MetallicRoughnessMaterial(
                baseColorFactor=[1.0, 1.0, 1.0, 1.0],
                metallicFactor=0.0,
                roughnessFactor=1.0,
            )
            for geom in _iter_trimesh_geometries(mesh):
                mesh_no_tex = geom.copy()
                try:
                    vc = np.full((len(mesh_no_tex.vertices), 4), 255, dtype=np.uint8)
                    mesh_no_tex.visual = trimesh.visual.ColorVisuals(mesh_no_tex, vertex_colors=vc)
                except Exception:
                    pass
                pr_mesh = pyrender.Mesh.from_trimesh(mesh_no_tex, material=material, smooth=False)
                if isinstance(pr_mesh, (list, tuple)):
                    for pm in pr_mesh:
                        scene.add(pm)
                else:
                    scene.add(pr_mesh)
        return scene

    scene = None
    use_textures = False
    for max_dim in (16384, 8192, 4096, 2048, 1024):
        if original_image is not None:
            _apply_texture_resize(mesh, max_dim=max_dim)
        try:
            scene = _build_scene(use_textures=True)
            test_camera = pyrender.OrthographicCamera(xmag=1.0, ymag=1.0, znear=0.1, zfar=20000.0)
            test_pose = np.eye(4)
            test_pose[0, 3] = (min_x + max_x) / 2.0
            test_pose[1, 3] = (min_y + max_y) / 2.0
            test_pose[2, 3] = max_z + 1000.0
            test_node = scene.add(test_camera, pose=test_pose)
            # Use small test size
            r = pyrender.OffscreenRenderer(64, 64)
            r.render(scene, flags=pyrender.RenderFlags.RGBA)
            r.delete()
            scene.remove_node(test_node)
            use_textures = True
            break
        except Exception:
            try:
                r.delete()
            except Exception:
                pass
            try:
                scene.clear()
            except Exception:
                pass
            scene = None
            use_textures = False
            continue
    if scene is None:
        scene = _build_scene(use_textures=False)
    
    # GeoTIFF Profile
    transform = rasterio.Affine(
        gsd, 0.0, min_x + global_origin[0],
        0.0, -gsd, max_y + global_origin[1]
    )
    
    profile = {
        'driver': 'GTiff',
        'dtype': 'uint8',
        'count': 3,
        'width': full_width,
        'height': full_height,
        'crs': srs_code,
        'transform': transform,
        'compress': 'lzw',
        'tiled': True,
        'blockxsize': 256,
        'blockysize': 256,
        'bigtiff': 'IF_NEEDED'
    }
    
    with rasterio.open(output_path, 'w', **profile) as dst:
        async_writer = AsyncTileWriter(dst)
        renderer = None
        current_renderer_size = (0, 0)
        
        # Loop over tiles
        for row in range(0, full_height, tile_size):
            for col in range(0, full_width, tile_size):
                # Current tile dimensions
                w = min(tile_size, full_width - col)
                h = min(tile_size, full_height - row)
                
                # Reuse or create renderer
                if renderer is None or current_renderer_size != (w, h):
                    if renderer is not None:
                        renderer.delete()
                    try:
                        renderer = pyrender.OffscreenRenderer(w, h)
                        current_renderer_size = (w, h)
                    except Exception as e:
                        raise RuntimeError(f"Failed to create renderer of size {w}x{h}: {e}") from e
                
                # Tile World Bounds (Local to mesh)
                tile_tl_x = min_x + col * gsd
                tile_tl_y = max_y - row * gsd
                
                # Camera Setup
                extent_x = w * gsd
                extent_y = h * gsd
                
                xmag = extent_x / 2.0
                ymag = extent_y / 2.0
                
                center_x = tile_tl_x + xmag
                center_y = tile_tl_y - ymag
                camera_z = max_z + 1000.0
                
                camera = pyrender.OrthographicCamera(xmag=xmag, ymag=ymag, znear=0.1, zfar=20000.0)
                camera_pose = np.eye(4)
                camera_pose[0, 3] = center_x
                camera_pose[1, 3] = center_y
                camera_pose[2, 3] = camera_z
                
                cam_node = scene.add(camera, pose=camera_pose)
                
                try:
                    color, depth = renderer.render(scene, flags=pyrender.RenderFlags.RGBA)
                    scene.remove_node(cam_node)
                except Exception as e:
                    try:
                        scene.remove_node(cam_node)
                    except Exception:
                        pass
                    if use_textures:
                        use_textures = False
                        scene = _build_scene(use_textures=False)
                        cam_node = scene.add(camera, pose=camera_pose)
                        color, depth = renderer.render(scene, flags=pyrender.RenderFlags.RGBA)
                        scene.remove_node(cam_node)
                    else:
                        raise e
                
                # Use RGB only
                if color.shape[2] == 4:
                    tile_rgb = color[:, :, :3]
                    tile_mask = color[:, :, 3]
                    alpha = tile_mask.astype(np.float32)
                    nz = alpha > 0.0
                    if np.any(nz):
                        rgb_f = tile_rgb.astype(np.float32)
                        rgb_f[nz] = np.clip((rgb_f[nz] * 255.0) / alpha[nz, None], 0.0, 255.0)
                        tile_rgb = rgb_f.astype(np.uint8)
                    valid = alpha >= 8.0
                    tile_mask = (valid.astype(np.uint8) * 255)
                else:
                    tile_rgb = color
                    tile_mask = None
                
                # Async Write
                tile_data = np.moveaxis(tile_rgb, 2, 0)
                window = Window(col, row, w, h)
                async_writer.write(tile_data, window)
                if tile_mask is not None:
                    async_writer.write_mask(tile_mask, window)
                
                # Cleanup
                del tile_rgb, tile_data, tile_mask, color, depth
                gc.collect()

        # Finish writing all tiles
        async_writer.close()
        if renderer is not None:
            renderer.delete()

    # Clean up scene
    scene.clear()
    del scene

def process_single_file(obj_path, input_dir, output_base_dir, gsd, srs_code, global_origin):
    """
    Process a single OBJ file: Render and Save GeoTIFF.
    """
    try:
        # Determine output path maintaining structure
        rel_path = os.path.relpath(os.path.dirname(obj_path), input_dir)
        target_dir = os.path.join(output_base_dir, rel_path)
        os.makedirs(target_dir, exist_ok=True)
        
        output_filename = os.path.splitext(os.path.basename(obj_path))[0] + '.tif'
        output_path = os.path.join(target_dir, output_filename)
        
        # Load mesh
        # Try to use pyfastobj for faster parsing if available
        mesh = None
        try:
            import pyfastobj
            fo = pyfastobj.FastObj(obj_path)
            # Convert fast-obj to trimesh (simplified)
            # Note: pyfastobj might not be fully compatible with trimesh Scene structure easily,
            # so we use trimesh as a reliable fallback/base but with optimized settings.
            # If pyfastobj is truly needed, we'd need to map its attributes.
            # For now, we'll stick to trimesh.load with process=False as it's very efficient.
            raise ImportError("Fallback to trimesh for better material support")
        except ImportError:
            resolver = None
            try:
                resolver = trimesh.resolvers.FilePathResolver(os.path.dirname(obj_path))
            except Exception:
                resolver = None
            mesh = trimesh.load(obj_path, process=False, resolver=resolver)
        
        # Render and Save (Tiled)
        render_and_save_tiled(mesh, output_path, gsd, srs_code, global_origin)
        
        # Cleanup to free memory immediately
        del mesh
        gc.collect()
        
        return target_dir, output_path, None
        
    except Exception as e:
        msg = str(e)
        if not msg:
            msg = repr(e)
        return None, None, msg

def merge_tiffs(tiff_files, output_path):
    """
    Merges multiple TIFF files into one using GDAL VRT for high performance.
    """
    if not tiff_files:
        return
    
    # Remove existing file if it exists
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError as e:
            raise
        
    try:
        from osgeo import gdal
        
        # 1. Build VRT (Virtual Dataset) - Fast, Metadata only
        vrt_options = gdal.BuildVRTOptions(resampleAlg='nearest', addAlpha=False)
        # Create an in-memory VRT or temporary file VRT. 
        # Using in-memory (/vsimem/) is cleaner but might use RAM if not careful.
        # Since we just pass file paths, VRT is small.
        vrt_path = output_path.replace('.tif', '.vrt')
        
        ds = gdal.BuildVRT(vrt_path, tiff_files, options=vrt_options)
        
        if ds is None:
            raise Exception("gdal.BuildVRT failed")
            
        # 2. Translate VRT to GeoTIFF - This performs the actual merge/mosaic
        # Use multithreading and compression
        creation_options = ["COMPRESS=LZW", "TILED=YES", "NUM_THREADS=ALL_CPUS", "BIGTIFF=IF_NEEDED"]
        translate_options = gdal.TranslateOptions(format="GTiff", creationOptions=creation_options)
        
        gdal.Translate(output_path, ds, options=translate_options)
        
        # Cleanup
        del ds # Close dataset
        gc.collect()
        if os.path.exists(vrt_path):
            os.remove(vrt_path)
        
    except ImportError:
        # Fallback to Rasterio
        src_files_to_mosaic = []
        for fp in tiff_files:
            src = rasterio.open(fp)
            src_files_to_mosaic.append(src)
            
        mosaic, out_trans = rasterio_merge(src_files_to_mosaic, masked=True)
        
        out_meta = src_files_to_mosaic[0].meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_trans,
            "count": 3, 
            "compress": "lzw"
        })
        
        with rasterio.open(output_path, "w", **out_meta) as dest:
            if hasattr(mosaic, "filled") and hasattr(mosaic, "mask"):
                filled = mosaic.filled(0)
                dest.write(filled)
                try:
                    valid = ~np.any(mosaic.mask, axis=0)
                    dest.write_mask((valid.astype(np.uint8) * 255))
                except Exception:
                    pass
            else:
                dest.write(mosaic)
            
        for src in src_files_to_mosaic:
            src.close()
    except Exception as e:
        raise

def main():
    total_start = time.perf_counter()
    in_container = _in_container()
    default_input = "/data" if in_container else None
    default_output = None
    default_workers = 4 if in_container else os.cpu_count()
    default_merge_mode = "all"

    parser = argparse.ArgumentParser(description="Render OBJ models to Orthophoto GeoTIFFs")
    parser.add_argument('--input', default=default_input, help='Input directory containing OBJ files (recursive)')
    parser.add_argument('--metadata', default=None, help='Optional metadata.xml path. If set, uses this metadata instead of <input>/metadata.xml')
    parser.add_argument('--output', default=default_output, help='Output directory for generated GeoTIFFs')
    parser.add_argument('--gsd', type=float, default=0.05, help='Ground Sample Distance (meters per pixel)')
    parser.add_argument('--merge-mode', choices=['all', 'subfolder', 'none'], default=default_merge_mode, help='Merge strategy: all, subfolder, none')
    parser.add_argument('--workers', type=int, default=default_workers, help='Number of worker processes')
    parser.add_argument('--subdir', action='append', help='Only process OBJs under these subdirectories (relative to input). Can be specified multiple times.')
    parser.add_argument(
        '--fill-holes',
        nargs='?',
        const='on',
        default='on',
        choices=['off', 'on', 'fast', 'strong'],
        help='Hole filling preset: off/on/fast/strong (default: on).'
    )
    args = parser.parse_args()
    if not args.input:
        parser.error("--input is required: point it at the directory containing your .obj files")
    merge_mode = args.merge_mode
    keep_merged_only = merge_mode != "none"
    fill_mode = args.fill_holes
    do_fill = fill_mode != 'off'
    if fill_mode == 'fast':
        fill_small_max_area = 2000
        fill_large_iters = 80
    elif fill_mode == 'strong':
        fill_small_max_area = 8000
        fill_large_iters = 400
    else:
        fill_small_max_area = 4000
        fill_large_iters = 200
    
    input_dir = args.input
    output_base_dir = args.output or os.path.join(input_dir, "MergedDOM")
    metadata_path = args.metadata or os.path.join(input_dir, "metadata.xml")
    os.makedirs(output_base_dir, exist_ok=True)

    print(f"Input: {input_dir}", flush=True)
    print(f"Metadata: {metadata_path}", flush=True)
    print(f"Output: {output_base_dir}", flush=True)
    print(f"GSD: {args.gsd}", flush=True)
    print(f"Workers: {args.workers}", flush=True)
    print(f"MergeMode: {merge_mode}", flush=True)
    print(f"KeepMergedOnly: {bool(keep_merged_only)}", flush=True)
    print(f"FillHoles: {fill_mode}", flush=True)
    
    if not os.path.exists(metadata_path):
        print(f"Error: metadata.xml not found: {metadata_path}", flush=True)
        return

    # Load Metadata
    try:
        metadata = load_metadata(metadata_path)
        print(f"Loaded metadata. SRS: {metadata['srs']}, Origin: {metadata['origin']}", flush=True)
    except Exception as e:
        print(f"Failed to load metadata: {e}", flush=True)
        return
        
    global_origin = metadata['origin']
    srs_code = metadata['srs'] 
    
    # Search for OBJ files
    obj_files = glob.glob(os.path.join(input_dir, '**', '*.obj'), recursive=True)
    total_files = len(obj_files)
    print(f"Found {total_files} OBJ files.", flush=True)
    if args.subdir:
        prefixes = [os.path.normpath(os.path.join(input_dir, d)) for d in args.subdir]
        obj_files = [p for p in obj_files if any(os.path.normpath(p).startswith(pref) for pref in prefixes)]
        total_files = len(obj_files)
        print(f"Filtered to {total_files} OBJ files under: {args.subdir}", flush=True)
    if total_files == 0:
        print("No OBJ files found. Nothing to do.", flush=True)
        return
    
    # Track files for merging: { directory_path: [list_of_tif_paths] }
    generated_files_by_dir = defaultdict(list)
    ok_count = 0
    fail_count = 0
    render_start = time.perf_counter()
    
    # Processing
    print(f"Processing with {args.workers} workers...", flush=True)
    use_sequential = getattr(sys, 'frozen', False) or args.workers <= 1
    render_progress = ProgressPrinter(total_files, "Render")
    render_progress.update(0)
    if use_sequential:
        for i, obj_path in enumerate(obj_files, start=1):
            target_dir, output_path, error = process_single_file(
                obj_path,
                input_dir,
                output_base_dir,
                args.gsd,
                srs_code,
                global_origin
            )
            if target_dir and output_path:
                generated_files_by_dir[target_dir].append(output_path)
                ok_count += 1
                render_progress.update(i, os.path.basename(obj_path))
            else:
                fail_count += 1
                render_progress.update(i, os.path.basename(obj_path))
                render_progress.log(f"[{i}/{total_files}] Failed: {os.path.basename(obj_path)} -> {error}")
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {}
            for obj_path in obj_files:
                future = executor.submit(
                    process_single_file,
                    obj_path,
                    input_dir,
                    output_base_dir,
                    args.gsd,
                    srs_code,
                    global_origin
                )
                futures[future] = obj_path

            completed = 0
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                obj_path = futures[future]
                try:
                    target_dir, output_path, error = future.result()
                except Exception as e:
                    fail_count += 1
                    render_progress.update(completed, os.path.basename(obj_path))
                    render_progress.log(f"[{completed}/{total_files}] Failed: {os.path.basename(obj_path)} -> {e}")
                    continue
                if target_dir and output_path:
                    generated_files_by_dir[target_dir].append(output_path)
                    ok_count += 1
                    render_progress.update(completed, os.path.basename(obj_path))
                else:
                    fail_count += 1
                    render_progress.update(completed, os.path.basename(obj_path))
                    render_progress.log(f"[{completed}/{total_files}] Failed: {os.path.basename(obj_path)} -> {error}")
    render_progress.finish()
    render_elapsed = time.perf_counter() - render_start

    # Merging step
    merge_elapsed = 0.0
    merged_outputs = []
    removed_tiles_total = 0
    if merge_mode != "none":
        merge_start = time.perf_counter()
        print("\nStarting merge process...", flush=True)
        merge_tasks = []
        if merge_mode == "subfolder":
            for dir_path, tiff_files in generated_files_by_dir.items():
                if len(tiff_files) <= 0:
                    continue
                dir_name = os.path.basename(dir_path) or "merged"
                parent_dir = os.path.dirname(dir_path)
                merged_output_path = os.path.join(parent_dir, f"{dir_name}.tif")
                merge_tasks.append((dir_name, tiff_files, merged_output_path, list(tiff_files)))
        elif merge_mode == "all":
            all_tiffs = []
            for tiffs in generated_files_by_dir.values():
                all_tiffs.extend(tiffs)
            if len(all_tiffs) > 0:
                merged_output_path = os.path.join(output_base_dir, "merged_all.tif")
                merge_tasks.append(("merged_all", all_tiffs, merged_output_path, list(all_tiffs)))
        else:
            merge_tasks = []

        total_merges = len(merge_tasks)
        merge_progress = ProgressPrinter(total_merges, "Merge")
        merge_progress.update(0)
        for i, (task_name, tiff_files, merged_output_path, cleanup_files) in enumerate(merge_tasks, start=1):
            try:
                merge_progress.update(i - 1, f"{task_name} ({len(tiff_files)} tiles)")
                merge_tiffs(tiff_files, merged_output_path)
                merge_progress.update(i, os.path.basename(merged_output_path))
                merged_outputs.append(merged_output_path)
                if keep_merged_only:
                    removed = 0
                    for fp in cleanup_files:
                        if fp != merged_output_path and _safe_unlink(fp):
                            removed += 1
                    removed_tiles_total += removed
            except Exception as e:
                merge_progress.log(f"[{i}/{total_merges}] Failed merge for {task_name}: {e}")
                merge_progress.update(i, f"{task_name} failed")
        merge_progress.finish()
        if keep_merged_only:
            _prune_empty_dirs(output_base_dir)
        merge_elapsed = time.perf_counter() - merge_start

    fill_elapsed = 0.0
    fill_files = 0
    fill_pixels = 0
    if do_fill:
        fill_targets = merged_outputs if merge_mode != "none" else [p for tiffs in generated_files_by_dir.values() for p in tiffs]
        if fill_targets:
            fill_start = time.perf_counter()
            fill_progress = ProgressPrinter(len(fill_targets), "Fill")
            fill_progress.update(0)
            for i, fp in enumerate(fill_targets, start=1):
                try:
                    stats = _fill_holes_inpaint_geotiff(fp, small_max_area=fill_small_max_area, large_iters=fill_large_iters)
                    fill_files += 1
                    fill_pixels += int(stats.get("filled_pixels", 0) or 0)
                    fill_progress.update(i, os.path.basename(fp))
                except Exception as e:
                    fill_progress.log(f"[{i}/{len(fill_targets)}] Fill failed: {fp} -> {e}")
                    fill_progress.update(i, os.path.basename(fp))
            fill_progress.finish()
            fill_elapsed = time.perf_counter() - fill_start

    total_elapsed = time.perf_counter() - total_start
    tiles_total = sum(len(v) for v in generated_files_by_dir.values())
    print("", flush=True)
    print("Summary", flush=True)
    print(f"- Version: 1.0.3", flush=True)
    print(f"- Input: {input_dir}", flush=True)
    print(f"- Output: {output_base_dir}", flush=True)
    print(f"- MergeMode: {merge_mode}", flush=True)
    print(f"- Total OBJ: {total_files} | Success: {ok_count} | Failed: {fail_count}", flush=True)
    print(f"- Tiles Generated: {tiles_total} | Tiles Removed: {removed_tiles_total}", flush=True)
    if merged_outputs:
        if merge_mode == "all":
            print(f"- Merged Output: {merged_outputs[0]}", flush=True)
        else:
            print(f"- Merged Outputs: {len(merged_outputs)}", flush=True)
    print(f"- Time Render: {_format_duration(render_elapsed)}", flush=True)
    if merge_mode != "none":
        print(f"- Time Merge: {_format_duration(merge_elapsed)}", flush=True)
    if do_fill:
        print(f"- Fill Holes: mode={fill_mode}, files={fill_files}, pixels={fill_pixels}, time={_format_duration(fill_elapsed)}", flush=True)
    print(f"- Time Total: {_format_duration(total_elapsed)}", flush=True)


if __name__ == "__main__":
    main()
