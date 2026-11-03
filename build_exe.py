import PyInstaller.__main__
import os
import shutil
import pyrender
import rasterio

# Clean previous build
if os.path.exists('dist'):
    shutil.rmtree('dist')
if os.path.exists('build'):
    shutil.rmtree('build')

# Get pyrender shader path
pyrender_path = os.path.dirname(pyrender.__file__)
shaders_path = os.path.join(pyrender_path, 'shaders')
if not os.path.exists(shaders_path):
    print(f"Warning: pyrender shaders not found at {shaders_path}")
else:
    print(f"Found pyrender shaders at: {shaders_path}")

def _detect_conda_base():
    candidates = [
        r'c:\programdata\miniconda3',
        r'c:\programdata\miniconda',
        r'c:\miniconda3',
        r'c:\miniconda',
        os.environ.get('CONDA_PREFIX', ''),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return r'c:\programdata\miniconda3'

conda_base = _detect_conda_base()
conda_lib_bin = os.path.join(conda_base, 'Library', 'bin')

proj_dll = os.path.join(conda_lib_bin, 'proj_9_3.dll')
libcurl_dll = os.path.join(conda_lib_bin, 'libcurl.dll')

proj_data_source = os.path.join(conda_base, 'Library', 'share', 'proj')

rasterio_path = os.path.dirname(rasterio.__file__)
rasterio_proj_data_source = os.path.join(rasterio_path, 'proj_data')

# Define arguments
args = [
    'gui_app.py',
    '--name=ObjOrthoRender v1.0.3',
    '--onefile',
    '--windowed',
    '--clean',
    '--hidden-import=rasterio',
    '--hidden-import=rasterio.sample',
    '--hidden-import=rasterio.vrt',
    '--hidden-import=rasterio._features',
    '--hidden-import=pyrender',
    '--hidden-import=trimesh',
    '--hidden-import=PIL',
    '--hidden-import=numpy',
    '--hidden-import=scipy.spatial.transform._rotation_groups',
    f'--add-data={shaders_path};pyrender/shaders',
    f'--add-data={rasterio_proj_data_source};rasterio/proj_data',
    f'--add-binary={proj_dll};.',
    f'--add-binary={libcurl_dll};.',
    f'--add-data={proj_data_source};Library/share/proj',
    f'--add-data={proj_data_source};proj_data_share',
    '--exclude-module=matplotlib',
    '--exclude-module=pandas',
    '--exclude-module=IPython',
    '--exclude-module=notebook',
    '--exclude-module=zmq',
    '--exclude-module=test',
    '--exclude-module=unittest',
    '--exclude-module=fiona',
]

# Run PyInstaller
print("Starting build process...")
PyInstaller.__main__.run(args)
print("Build finished. Check 'dist' folder.")
