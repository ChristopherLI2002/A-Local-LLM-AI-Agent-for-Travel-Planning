# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for A Local LLM AI Agent for Travel Planning.
# Build:  python -m PyInstaller --noconfirm launch.spec

from PyInstaller.utils.hooks import collect_all, collect_submodules
import certifi

block_cipher = None

datas = [(certifi.where(), "certifi")]
binaries = []
hiddenimports = [
    "travel_agent",
    "travel_agent.agent",
    "travel_agent.attraction_images",
    "travel_agent.airline_names",
    "travel_agent.browser_tools",
    "travel_agent.cli",
    "travel_agent.config",
    "travel_agent.destination_guides",
    "travel_agent.gui",
    "travel_agent.itinerary_parse",
    "travel_agent.llm_select",
    "travel_agent.ollama_lifecycle",
    "travel_agent.places",
    "travel_agent.planner_query",
    "travel_agent.pricing",
    "travel_agent.regions",
    "travel_agent.trip_urls",
    "PIL",
    "PIL.Image",
    "PIL.ImageTk",
    "dotenv",
    "certifi",
    "httpx",
    "httpcore",
    "ollama",
    "rich",
]

for pkg in ("playwright", "ollama"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += collect_submodules("travel_agent")

a = Analysis(
    ["launch.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalLLMTravelAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI app — no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LocalLLMTravelAgent",
)
