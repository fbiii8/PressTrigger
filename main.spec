# -*- mode: python ; coding: utf-8 -*-
# 打包命令： pyinstaller main.spec
#
# v2.2 打包配置：
# * 输入模拟全部由 ctypes 完成，不依赖 pywin32。
# * 隐藏导入只保留确实会被动态加载的模块。
# * 版本资源来自 version_info.txt。
import sys

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('switch.mp3', '.'), ('icon.ico', '.')],
    hiddenimports=[
        'pystray._win32',          # 托盘后端按平台动态导入
        'PIL.Image',
        'PIL.ImageDraw',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 只排除确定用不到的重型标准库/第三方包。
        # 注意：不要排除 urllib/http/email/xml —— playsound3 顶层会 import urllib，
        # PIL/pkg_resources 也会间接触达，排掉会让提示音静默失效。
        'pytest', 'unittest', 'pydoc', 'doctest',
        'sqlite3', 'distutils', 'setuptools', 'pip',
        'numpy', 'olefile',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PressTrigger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # UPX 压缩会被部分杀软/反作弊判定为可疑
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                 # 无 CMD 黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
    version='version_info.txt',    # exe 属性显示 PressTrigger 2.2.0
    uac_admin=True,                # 清单里直接要求管理员权限，启动即提权
)
