# -*- mode: python -*-
# vim: ft=python

import os
import re

from PyInstaller.utils.hooks import collect_data_files

def _resolve_root_dir():
    env_root = os.environ.get('X_ANYLABELING_ROOT')
    if env_root:
        return os.path.abspath(env_root)

    spec_path = globals().get('SPEC')
    if isinstance(spec_path, str) and spec_path:
        return os.path.abspath(os.path.join(os.path.dirname(spec_path), '..', '..', '..'))

    return os.path.abspath(os.getcwd())

ROOT_DIR = _resolve_root_dir()

def _p(*parts):
    return os.path.join(ROOT_DIR, *parts)

def _load_version():
    app_info_path = _p('anylabeling', 'app_info.py')
    with open(app_info_path, 'r', encoding='utf-8') as f:
        content = f.read()
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Failed to read __version__ from: {app_info_path}")
    return match.group(1)

__version__ = _load_version()

a = Analysis(
    [_p('anylabeling', 'app.py')],
    pathex=[_p('anylabeling')],
    binaries=[],
    datas=[
        (_p('anylabeling', 'configs', 'auto_labeling', '*.yaml'), 'anylabeling/configs/auto_labeling'),
        (_p('anylabeling', 'configs', '*.yaml'), 'anylabeling/configs'),
        (_p('anylabeling', 'views', 'labeling', 'widgets', 'auto_labeling', 'auto_labeling.ui'), 'anylabeling/views/labeling/widgets/auto_labeling'),
        (_p('anylabeling', 'services', 'auto_labeling', 'configs', 'bert', '*'), 'anylabeling/services/auto_labeling/configs/bert'),
        (_p('anylabeling', 'services', 'auto_labeling', 'configs', 'clip', '*'), 'anylabeling/services/auto_labeling/configs/clip'),
        (_p('anylabeling', 'services', 'auto_labeling', 'configs', 'ppocr', '*'), 'anylabeling/services/auto_labeling/configs/ppocr'),
        (_p('anylabeling', 'services', 'auto_labeling', 'configs', 'ram', '*'), 'anylabeling/services/auto_labeling/configs/ram')
    ] + collect_data_files('matplotlib'),
    hiddenimports=[
        'matplotlib',
        'matplotlib.backends.backend_agg',
        'matplotlib.font_manager',
        'matplotlib.mathtext',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name=f'X-AnyLabeling-{__version__}-macOS',
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon=_p('anylabeling', 'resources', 'images', 'icon.icns'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=f'X-AnyLabeling-{__version__}-macOS',
)
app = BUNDLE(
    coll,
    name=f'X-AnyLabeling-v{__version__}-macOS.app',
    icon=_p('anylabeling', 'resources', 'images', 'icon.icns'),
    bundle_identifier='io.github.zhkuo24.x-anylabeling',
    info_plist={'NSHighResolutionCapable': 'True'},
)
