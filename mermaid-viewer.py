#!/usr/bin/env python3
"""
mermaid-viewer.py — Mermaid Studio v5 (PySide6)
===============================================
Two-panel Mermaid diagram editor:
  Left  : syntax-highlighted code editor with multiple file tabs
  Right : live preview rendered by Mermaid.js in a QWebEngineView

v5 highlights
-------------
  * Preview: drag to pan, wheel to pan, Ctrl+wheel to zoom at the cursor,
    Fit / 100% buttons, per-tab view state kept across re-renders
  * Errors: line-numbered Mermaid parse errors are underlined in the editor
    and the last good diagram stays on screen
  * Editor: line numbers, find/replace, auto-indent, autocomplete, snippets,
    comment toggle (Ctrl+/)
  * Session restore + autosave; external-change detection (live reload)
  * Export: PNG (1x/2x/4x, transparent option), SVG, vector PDF, standalone
    self-contained HTML, Markdown, PowerPoint shapes, "export all tabs"
  * Project folder explorer and clickable source diagnostics
  * Preview edits to flowchart / state / class nodes are written back into source
  * Mermaid 10.9.8 or 11.x, downloaded once and cached for offline use
  * Outline panel, diff vs saved file, outline-to-diagram generator,
    theme JSON import/export

Run:  pip install -r requirements.txt   then   python mermaid-viewer.py
Build: .\\build_exe.ps1  (PyInstaller)
"""

from __future__ import annotations

import base64
import difflib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QBuffer, QFileSystemWatcher, QIODevice, QMarginsF, QMimeData, QObject,
    QSortFilterProxyModel, QRect, QRectF, QRegularExpression, QSettings, QSize, QSizeF,
    QStandardPaths,
    Qt, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QFont, QIcon, QImage, QImageWriter,
    QKeySequence, QPageSize, QPainter, QPalette, QPdfWriter, QPen, QPixmap,
    QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFormat,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QColorDialog, QCompleter, QDialog,
    QDockWidget, QFileDialog, QFileSystemModel, QFontComboBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QStatusBar, QStyle,
    QTabWidget, QTreeView,
    QTableWidget, QTableWidgetItem, QTextEdit, QToolBar, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QStringListModel

APP_TITLE = "Mermaid Studio"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_MERMAID_VERSION = "10.9.8"
# Pinned builds offered in the Mermaid menu (label shown to the user).
MERMAID_VERSIONS = {
    "10.9.8": "Mermaid 10.9.8 (stable)",
    "11.17.2": "Mermaid 11.17.2 (adds architecture, packet, kanban)",
}
DIAGRAM_EXTS = {".mmd", ".mm", ".txt"}
MAX_RECENT_THEMES = 5
MAX_OPEN_BYTES = 5 * 1024 * 1024
WARN_OPEN_BYTES = 1024 * 1024
MAX_PNG_PIXELS = 16000
AUTOSAVE_MS = 20000


def mermaid_cdn_url(version: str) -> str:
    return f"https://cdn.jsdelivr.net/npm/mermaid@{version}/dist/mermaid.min.js"


def cache_dir() -> Path:
    """Writable per-user folder for downloaded Mermaid bundles and session data."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    d = Path(os.environ.get("MERMAID_STUDIO_DATA") or Path(base or Path.home()) / "MermaidStudio")
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_local_bundle(version: str) -> Path | None:
    """Locate a local mermaid.min.js for `version` (cache, app folder, legacy name)."""
    names = [cache_dir() / f"mermaid-{version}.min.js",
             APP_DIR / f"mermaid-{version}.min.js",
             APP_DIR / "vendor" / f"mermaid-{version}.min.js"]
    if version == DEFAULT_MERMAID_VERSION:
        names.append(APP_DIR / "mermaid.min.js")
    for p in names:
        try:
            if p.is_file() and p.stat().st_size > 100_000:
                return p
        except OSError:
            continue
    return None


def version_major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except ValueError:
        return 10

# --------------------------------------------------------------------------
# Defaults / themes / presets
# --------------------------------------------------------------------------
DEFAULT_TEXT = """\
%% Mermaid Studio — edit me; the preview updates live.
flowchart TD
    A[Start] --> B{Decision}
    B -->|Yes| C[Do the thing]
    B -->|No| D[Do nothing]
    C --> E([Finish])
    D --> E

    classDef good fill:#2d6a4f,stroke:#74c69d,color:#fff
    class C good
"""

DEFAULT_COLORS = {
    "background": "#ffffff",
    "primaryColor": "#eef2ff",
    "primaryTextColor": "#111827",
    "primaryBorderColor": "#6366f1",
    "lineColor": "#64748b",
    "nodeFill": "#e0e7ff",
    "nodeBorder": "#4f46e5",
    "mainLabel": "#1e293b",
}

THEMES = {
    "Studio (default)": dict(DEFAULT_COLORS),
    "Light minimal":    {"background": "#ffffff", "primaryColor": "#ffffff",
                          "primaryTextColor": "#333333", "primaryBorderColor": "#9e9e9e",
                          "lineColor": "#666666", "nodeFill": "#ffffff",
                          "nodeBorder": "#9e9e9e", "mainLabel": "#333333"},
    "Forest":           {"background": "#f0fdf4", "primaryColor": "#dcfce7",
                          "primaryTextColor": "#14532d", "primaryBorderColor": "#16a34a",
                          "lineColor": "#15803d", "nodeFill": "#dcfce7",
                          "nodeBorder": "#16a34a", "mainLabel": "#14532d"},
    "Ocean":            {"background": "#ecfeff", "primaryColor": "#cffafe",
                          "primaryTextColor": "#164e63", "primaryBorderColor": "#0891b2",
                          "lineColor": "#0e7490", "nodeFill": "#cffafe",
                          "nodeBorder": "#0891b2", "mainLabel": "#164e63"},
    "Sunset":           {"background": "#fff7ed", "primaryColor": "#ffedd5",
                          "primaryTextColor": "#7c2d12", "primaryBorderColor": "#ea580c",
                          "lineColor": "#c2410c", "nodeFill": "#ffedd5",
                          "nodeBorder": "#ea580c", "mainLabel": "#7c2d12"},
    "Rose":             {"background": "#fff1f2", "primaryColor": "#ffe4e6",
                          "primaryTextColor": "#881337", "primaryBorderColor": "#e11d48",
                          "lineColor": "#be123c", "nodeFill": "#ffe4e6",
                          "nodeBorder": "#e11d48", "mainLabel": "#881337"},
    "Midnight":         {"background": "#0f172a", "primaryColor": "#1e293b",
                          "primaryTextColor": "#e2e8f0", "primaryBorderColor": "#64748b",
                          "lineColor": "#94a3b8", "nodeFill": "#1e293b",
                          "nodeBorder": "#64748b", "mainLabel": "#f1f5f9"},
    "Blueprint":        {"background": "#0c4a6e", "primaryColor": "#075985",
                          "primaryTextColor": "#e0f2fe", "primaryBorderColor": "#7dd3fc",
                          "lineColor": "#bae6fd", "nodeFill": "#075985",
                          "nodeBorder": "#7dd3fc", "mainLabel": "#f0f9ff"},
}

COLOR_FIELDS = [
    ("background",         "Background"),
    ("primaryColor",       "Node color"),
    ("primaryTextColor",   "Node text"),
    ("primaryBorderColor", "Node border"),
    ("lineColor",          "Edges / arrows"),
    ("nodeFill",           "Fill (classDef)"),
    ("nodeBorder",         "Border (classDef)"),
    ("mainLabel",          "Title / label"),
]
COLOR_LABELS = dict(COLOR_FIELDS)

PRESETS = {
    "Flowchart": """flowchart TD
    A[Project start] --> B[Gather requirements]
    B --> C[Design]
    C --> D{Approved?}
    D -->|Yes| E[Build]
    D -->|No| C
    E --> F[Test]
    F --> G[Ship]
    classDef step fill:#e0e7ff,stroke:#4f46e5
    classDef go fill:#dcfce7,stroke:#16a34a
    class B,C,E,F step
    class G go
""",
    "Sequence": """sequenceDiagram
    participant U as User
    participant A as App
    participant S as Server
    U->>A: Click button
    A->>S: POST /api/data
    S-->>A: 200 OK
    A-->>U: Show result
    Note right of A: All async
""",
    "Class": """classDiagram
    class Shape {
        +String name
        +float area()
        +float perimeter()
    }
    class Circle {
        +float radius
        +float area()
    }
    class Square {
        +float side
        +float area()
    }
    Shape <|-- Circle
    Shape <|-- Square
    Shape ..> Color : uses
""",
    "State": """stateDiagram-v2
    [*] --> Idle
    Idle --> Loading: request()
    Loading --> Active: success
    Loading --> Error: failure
    Error --> Loading: retry()
    Active --> Idle: close()
    Active --> [*]
""",
    "Entity-relationship": """erDiagram
    CUSTOMER ||--o{ ORDER : places
    ORDER ||--|{ LINE-ITEM : contains
    PRODUCT ||--o{ LINE-ITEM : consists
    CUSTOMER {
        string name
        string email PK
    }
    ORDER {
        int id PK
        date placed_on
        decimal total
    }
    PRODUCT {
        int id PK
        string name
        decimal price
    }
""",
    "Pie chart": """pie showData
    title Share by team
    "Frontend" : 42
    "Backend" : 31
    "Data" : 18
    "Ops" : 9
""",
    "Gantt": """gantt
    title Product roadmap
    dateFormat  YYYY-MM-DD
    section Platform
    Foundations      :a1, 2026-11-02, 3w
    API v2           :a2, after a1, 4w
    section Clients
    Web app          :b1, 2026-12-01, 5w
    Mobile app       :b2, after b1, 4w
    section Launch
    Beta             :c1, after b2, 2w
    GA               :milestone, after c1, 0d
""",
    "Mindmap": """mindmap
    root((Project))
        Scope
            MVP features
            Non-goals
        Team
            Design
            Engineering
        Delivery
            Milestones
            Risks
""",
    "User journey": """journey
    title Online order
    section Discover
        Browse catalog: 5: Customer
        Compare products: 4: Customer
    section Purchase
        Add item to cart: 5: Customer
        Complete payment: 3: Customer, Payment service
    section Fulfillment
        Track delivery: 4: Customer
        Receive order: 5: Customer
""",
    "Quadrant chart": """quadrantChart
    title Feature prioritization
    x-axis Low effort --> High effort
    y-axis Low impact --> High impact
    quadrant-1 Major projects
    quadrant-2 Quick wins
    quadrant-3 Fill-ins
    quadrant-4 Reconsider
    Search: [0.25, 0.8]
    Dark mode: [0.35, 0.6]
    Analytics: [0.75, 0.7]
    Animation: [0.8, 0.25]
""",
    "Requirement diagram": """requirementDiagram
    requirement checkout_req {
        id: 1
        text: Orders must be payable online.
        risk: high
        verifymethod: test
    }
    element checkout_service {
        type: service
    }
    requirement payment_req {
        id: 2
        text: Card payments must be authorized.
        risk: low
        verifymethod: test
    }
    checkout_service - satisfies -> payment_req
    checkout_req - contains -> payment_req
""",
    "Git graph": """gitGraph
    commit id: "Initial"
    branch feature
    commit id: "Feature work"
    checkout main
    commit id: "Release prep"
    merge feature id: "Merge feature"
    commit id: "Release"
""",
    "C4 context": """C4Context
    title Online shop - system context
    Person(customer, "Customer", "Browses and buys products")
    System(shop, "Online Shop", "Manages catalog and orders")
    System_Ext(payments, "Payment Provider", "Processes card payments")
    System_Ext(delivery, "Delivery Service", "Ships customer orders")
    Rel(customer, shop, "Places orders")
    Rel(shop, payments, "Requests payment")
    Rel(shop, delivery, "Books shipment")
""",
    "C4 container": """C4Container
    title Online shop - containers
    Person(customer, "Customer")
    System_Boundary(shop, "Online Shop") {
        Container(web, "Web App", "React", "Customer storefront")
        Container(api, "Shop API", "Python", "Catalog and order logic")
        ContainerDb(db, "Shop Database", "PostgreSQL", "Products and orders")
    }
    Rel(customer, web, "Browses and orders")
    Rel(web, api, "Calls")
    Rel(api, db, "Reads and writes")
""",
    "C4 component": """C4Component
    title Shop API - components
    Container_Boundary(api, "Shop API") {
        Component(controller, "Order Controller", "FastAPI", "Handles requests")
        Component(service, "Order Service", "Python", "Applies order rules")
        Component(repository, "Order Repository", "SQLAlchemy", "Stores orders")
    }
    ContainerDb(db, "Shop Database", "PostgreSQL")
    Rel(controller, service, "Calls")
    Rel(service, repository, "Loads and saves")
    Rel(repository, db, "Reads and writes")
""",
    "C4 dynamic": """C4Dynamic
    title Place an order
    Person(customer, "Customer")
    System_Boundary(shop, "Online Shop") {
        Container(web, "Web App", "React")
        Container(api, "Shop API", "Python")
        ContainerDb(db, "Shop Database", "PostgreSQL")
    }
    RelIndex(1, customer, web, "Submits cart")
    RelIndex(2, web, api, "Creates order")
    RelIndex(3, api, db, "Saves order")
    RelIndex(4, api, web, "Returns confirmation")
""",
    "C4 deployment": """C4Deployment
    title Online shop - deployment
    Deployment_Node(device, "Customer device", "Laptop or phone") {
        Container(browser, "Web Browser", "Chrome, Firefox, Safari")
    }
    Deployment_Node(cloud, "Cloud platform", "Managed hosting") {
        Deployment_Node(app_node, "Application host", "Linux") {
            Container(api, "Shop API", "Python")
        }
        Deployment_Node(data_node, "Data host", "Managed database") {
            ContainerDb(db, "Shop Database", "PostgreSQL")
        }
    }
    Rel(browser, api, "HTTPS")
    Rel(api, db, "SQL")
""",
    "Timeline": """timeline
    title Product launch
    section Discovery
        2025 Q1 : Customer research
        2025 Q2 : Prototype
    section Delivery
        2025 Q3 : Public beta
        2025 Q4 : General availability
""",
    "Sankey diagram": """sankey-beta
    Website,Product page,70
    Website,Help center,30
    Product page,Add to cart,45
    Product page,Leave,25
    Add to cart,Purchase,35
    Add to cart,Abandoned cart,10
""",
    "XY chart": """xychart-beta
    title "Monthly orders"
    x-axis [Jan, Feb, Mar, Apr, May, Jun]
    y-axis "Orders" 0 --> 100
    bar [25, 38, 42, 57, 72, 86]
    line [20, 32, 48, 55, 68, 91]
""",
    "Block diagram": """block-beta
    columns 3
    block:clients
        Web
        Mobile
    end
    api["Shop API"]
    database[("Orders DB")]
    clients --> api
    api --> database
""",
}

# Samples that need Mermaid 11+ (not available in the 10.x bundle).
PRESETS_V11 = {
    "Kanban": """\
kanban
  todo[Todo]
    t1[Write the README]
    t2[Add unit tests]
  doing[In progress]
    t3[Build the export pipeline]@{ assigned: 'dave', priority: 'High' }
  done[Done]
    t4[Ship v1]
""",
    "Architecture": """\
architecture-beta
    group api(cloud)[API]

    service db(database)[Database] in api
    service disk(disk)[Storage] in api
    service server(server)[Server] in api

    db:L -- R:server
    disk:T -- B:server
""",
    "Packet": """\
packet-beta
    0-15: "Source Port"
    16-31: "Destination Port"
    32-63: "Sequence Number"
    64-95: "Acknowledgment Number"
    96-99: "Data Offset"
    100-105: "Reserved"
    106-111: "Flags"
    112-127: "Window"
""",
}

SHORTCUTS = [
    ("Ctrl+Enter",    "Render diagram now"),
    ("Ctrl+O",        "Open a .mmd file (opens in a new tab)"),
    ("Ctrl+Shift+O",  "Open a project folder"),
    ("Ctrl+S",        "Save file (Save As if untitled)"),
    ("Ctrl+Shift+S",  "Save file as…"),
    ("Ctrl+N",        "New tab"),
    ("Ctrl+W",        "Close tab (last tab resets instead)"),
    ("Drag (preview)", "Pan the diagram"),
    ("Wheel (preview)", "Pan the diagram"),
    ("Ctrl+Wheel",    "Zoom preview at the mouse pointer"),
    ("Double-click (preview)", "Fit diagram to window"),
    ("Ctrl+Plus / Ctrl+Minus", "Zoom in / out"),
    ("Ctrl+0 / Ctrl+1", "Fit to window / actual size (100%)"),
    ("Ctrl+F / Ctrl+H", "Find / find and replace"),
    ("F3 / Shift+F3", "Find next / previous"),
    ("Ctrl+/",        "Toggle %% comment on the selected lines"),
    ("Ctrl+Space",    "Autocomplete"),
    ("Tab / Shift+Tab", "Indent / unindent"),
    ("F8",            "Jump to the error line"),
    ("Ctrl+Shift+C",  "Copy image + SVG to clipboard"),
    ("Ctrl+Shift+L",  "Toggle dark / light UI"),
    ("Drag & drop",   "Drop .mmd / .mm / .txt files to open them"),
    ("F1",            "Show this shortcut list"),
]


# --------------------------------------------------------------------------
# HTML wrapper for Mermaid.js
# --------------------------------------------------------------------------
def build_html(script_src: str) -> str:
    """Preview page. `script_src` is a file:// URI or CDN URL for mermaid.min.js."""
    js = f"<script src='{script_src}'></script>"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
  html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; }}
  body {{ background: #ffffff; transition: background-color .12s;
         user-select: none; cursor: grab; }}
  body.dragging {{ cursor: grabbing; }}
  #wrap {{ position: absolute; left: 0; top: 0; transform-origin: 0 0;
          will-change: transform; }}
  #wrap svg {{ display: block; max-width: none !important; }}
  .errbox {{ font-family: Consolas, "Courier New", monospace; font-size: 13px;
             color: #b91c1c; background: #fee2e2; border: 1px solid #fca5a5;
             border-radius: 8px; padding: 12px 16px; white-space: pre-wrap;
             width: 560px; }}
  .mm-sel {{ filter: drop-shadow(0 0 3px #2563eb) drop-shadow(0 0 1px #2563eb); }}
  #wrap svg * {{ cursor: pointer; }}
  #proppanel {{ position: fixed; right: 10px; bottom: 10px; z-index: 20; display: none;
               width: 210px; padding: 8px 10px; font: 12px "Segoe UI", sans-serif;
               color: #111; background: #fffffff2; border: 1px solid #94a3b8;
               border-radius: 8px; box-shadow: 0 2px 10px #0004; cursor: default; }}
  #proppanel .pp_head {{ display: flex; justify-content: space-between; margin-bottom: 6px;
               overflow: hidden; }}
  #proppanel .pp_head b {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  #pp_close {{ cursor: pointer; padding: 0 4px; }}
  #proppanel label {{ display: flex; justify-content: space-between; align-items: center;
               margin: 3px 0; }}
  #proppanel input[type=number] {{ width: 60px; }}
  #proppanel input[type=range] {{ width: 90px; }}
  #proppanel .pp_btns {{ display: flex; gap: 6px; margin-top: 6px; }}
  #proppanel button {{ flex: 1; font-size: 11px; }}
  #errbanner {{ position: fixed; left: 8px; right: 8px; top: 8px; z-index: 10;
               display: none; font-family: Consolas, "Courier New", monospace;
               font-size: 12px; color: #7f1d1d; background: #fee2e2e6;
               border: 1px solid #fca5a5; border-radius: 6px; padding: 6px 10px;
               white-space: pre-wrap; cursor: default; user-select: text;
               max-height: 40vh; overflow: auto; }}
</style>
</head>
<body>
<div id="wrap"></div>
<div id="errbanner" title="Click to dismiss"></div>
<div id="proppanel">
  <div class="pp_head"><b id="pp_title"></b><span id="pp_close" title="Deselect (Esc)">&#x2715;</span></div>
  <label>Fill <input type="color" id="pp_fill"></label>
  <label>Stroke <input type="color" id="pp_stroke"></label>
  <label>Stroke width <input type="number" id="pp_sw" min="0" max="20" step="0.5"></label>
  <label>Text color <input type="color" id="pp_text"></label>
  <label>Font size <input type="number" id="pp_fs" min="4" max="96" step="1"></label>
  <label>Opacity <input type="range" id="pp_op" min="0" max="1" step="0.05"></label>
  <div class="pp_btns"><button id="pp_reset">Reset element</button><button id="pp_resetall">Reset all</button></div>
</div>
<script>
  var __wrap = document.getElementById('wrap');
  var __banner = document.getElementById('errbanner');
  window.__seq = 0;
  window.__lastBg = '#ffffff';
  window.__hasDiagram = false;
  var NS = 'http://www.w3.org/2000/svg';
  var view = {{ zoom: 1, x: 0, y: 0, fit: true }};

  function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }}

  // ---- Qt bridge -------------------------------------------------------
  function bridge_call(name) {{
    var b = window.bridge;
    if (b && typeof b[name] === 'function') {{
      b[name].apply(b, Array.prototype.slice.call(arguments, 1));
    }}
  }}
  var __chanReady = false, __winLoaded = false, __readySent = false;
  function maybe_ready() {{
    if (__chanReady && __winLoaded && !__readySent) {{
      __readySent = true;
      bridge_call('ready');
    }}
  }}
  window.addEventListener('load', function () {{ __winLoaded = true; maybe_ready(); }});
  if (typeof QWebChannel !== 'undefined' && window.qt && qt.webChannelTransport) {{
    new QWebChannel(qt.webChannelTransport, function (ch) {{
      window.bridge = ch.objects.bridge;
      __chanReady = true;
      maybe_ready();
    }});
  }}

  // ---- pan / zoom ------------------------------------------------------
  function apply_view() {{
    __wrap.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.zoom + ')';
  }}
  function report_view(user) {{
    bridge_call('viewState', view.zoom, view.x, view.y, view.fit, !!user);
  }}
  function content_size() {{
    return {{ w: __wrap.offsetWidth, h: __wrap.offsetHeight }};
  }}
  function fit_view(user) {{
    var c = content_size();
    if (!c.w || !c.h) {{ apply_view(); return; }}
    var vw = window.innerWidth, vh = window.innerHeight, pad = 24;
    var z = Math.min((vw - pad * 2) / c.w, (vh - pad * 2) / c.h, 2);
    z = Math.max(z, 0.05);
    view.zoom = z;
    view.x = (vw - c.w * z) / 2;
    view.y = (vh - c.h * z) / 2;
    view.fit = true;
    apply_view();
    report_view(user);
  }}
  function actual_size(user) {{
    var c = content_size();
    view.zoom = 1;
    view.x = (window.innerWidth - c.w) / 2;
    view.y = (window.innerHeight - c.h) / 2;
    view.fit = false;
    apply_view();
    report_view(user);
  }}
  function zoom_at(factor, cx, cy) {{
    var nz = Math.max(0.05, Math.min(8, view.zoom * factor));
    var k = nz / view.zoom;
    view.x = cx - (cx - view.x) * k;
    view.y = cy - (cy - view.y) * k;
    view.zoom = nz;
    view.fit = false;
    apply_view();
    report_view(true);
  }}
  function zoom_by(factor) {{ zoom_at(factor, window.innerWidth / 2, window.innerHeight / 2); }}
  function set_view(z, x, y, fit) {{
    view = {{ zoom: z, x: x, y: y, fit: !!fit }};
    if (view.fit) fit_view(false); else apply_view();
  }}

  var drag = null;
  document.addEventListener('mousedown', function (e) {{
    if (e.button !== 0) return;
    drag = {{ x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false }};
  }});
  window.addEventListener('mousemove', function (e) {{
    if (!drag) return;
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
    drag.moved = true;
    document.body.classList.add('dragging');
    view.x = drag.vx + dx;
    view.y = drag.vy + dy;
    view.fit = false;
    apply_view();
  }});
  window.addEventListener('mouseup', function () {{
    if (drag && drag.moved) {{ report_view(true); window.__dragEnd = Date.now(); }}
    drag = null;
    document.body.classList.remove('dragging');
  }});
  document.addEventListener('wheel', function (e) {{
    e.preventDefault();
    if (e.ctrlKey) {{
      zoom_at(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY);
    }} else {{
      view.x -= e.deltaX;
      view.y -= e.deltaY;
      view.fit = false;
      apply_view();
      report_view(true);
    }}
  }}, {{ passive: false }});
  document.addEventListener('dblclick', function (e) {{
    if (e.target.closest && e.target.closest('#proppanel')) return;
    fit_view(true);
  }});

  // ---- element selection & property editing ----------------------------
  // Overrides are keyed by the element's child-index path inside the SVG so
  // they survive re-renders as long as the diagram structure is unchanged.
  var __overrides = {{}};
  var __selEl = null, __selKey = null;
  var SHAPES = 'rect,path,polygon,circle,ellipse,line,polyline';
  var PICK = 'g.node, g.cluster, g.edgeLabel, g.actor, g.label, g.classGroup, '
           + '.actor, .messageText, .messageLine0, .messageLine1, '
           + '.flowchart-link, .edgePath, g.slice, .task, .section';

  function cur_svg() {{ return __wrap.querySelector('svg'); }}
  function key_of(el) {{
    var s = cur_svg(), parts = [];
    while (el && el !== s) {{
      var p = el.parentNode;
      parts.unshift(Array.prototype.indexOf.call(p.children, el));
      el = p;
    }}
    return parts.join('/');
  }}
  function el_of(key) {{
    var el = cur_svg();
    if (!el) return null;
    if (key === '') return null;
    var parts = key.split('/');
    for (var i = 0; i < parts.length; i++) {{
      el = el.children[parseInt(parts[i], 10)];
      if (!el) return null;
    }}
    return el;
  }}
  function shapes_of(el) {{
    var list = el.matches(SHAPES) ? [el] : Array.prototype.slice.call(el.querySelectorAll(SHAPES));
    return list.filter(function (x) {{ return !x.closest('foreignObject, defs, marker'); }});
  }}
  function source_id_of(el) {{
    var node = el && el.closest('g.node');
    var m = node && /^(?:flowchart|state|classId)-(.+)-\\d+$/.exec(node.id || '');
    return m ? m[1] : '';
  }}
  function texts_of(el) {{
    var tag = el.tagName.toLowerCase();
    if (tag === 'text' || tag === 'tspan') return {{ svg: [el], html: [] }};
    return {{
      svg: Array.prototype.slice.call(el.querySelectorAll('text, tspan')),
      html: Array.prototype.slice.call(el.querySelectorAll('foreignObject span, foreignObject p, foreignObject div'))
    }};
  }}
  function to_hex(c) {{
    var m = /rgba?\\(\\s*(\\d+)[,\\s]+(\\d+)[,\\s]+(\\d+)/.exec(c || '');
    if (!m) return null;
    return '#' + [m[1], m[2], m[3]].map(function (n) {{
      return ('0' + parseInt(n, 10).toString(16)).slice(-2);
    }}).join('');
  }}
  function apply_props(el, p) {{
    var sh = shapes_of(el), tx = texts_of(el);
    sh.forEach(function (x) {{
      if (p.fill && getComputedStyle(x).fill !== 'none') x.style.setProperty('fill', p.fill, 'important');
      if (p.stroke) x.style.setProperty('stroke', p.stroke, 'important');
      if (p.strokeWidth) x.style.setProperty('stroke-width', p.strokeWidth + 'px', 'important');
      if (p.opacity !== undefined && p.opacity !== '') x.style.setProperty('opacity', p.opacity, 'important');
    }});
    tx.svg.forEach(function (x) {{
      if (p.textColor) x.style.setProperty('fill', p.textColor, 'important');
      if (p.fontSize) x.style.setProperty('font-size', p.fontSize + 'px', 'important');
    }});
    tx.html.forEach(function (x) {{
      if (p.textColor) x.style.setProperty('color', p.textColor, 'important');
      if (p.fontSize) x.style.setProperty('font-size', p.fontSize + 'px', 'important');
    }});
  }}
  function clear_props(el) {{
    var all = [el].concat(Array.prototype.slice.call(el.querySelectorAll('*')));
    all.forEach(function (x) {{
      ['fill', 'stroke', 'stroke-width', 'opacity', 'font-size', 'color'].forEach(function (k) {{
        if (x.style) x.style.removeProperty(k);
      }});
    }});
  }}
  function reapply_overrides() {{
    Object.keys(__overrides).forEach(function (k) {{
      var el = el_of(k);
      if (el) apply_props(el, __overrides[k]);
    }});
    if (__selKey !== null) {{
      var e2 = el_of(__selKey);
      if (e2) {{ __selEl = e2; e2.classList.add('mm-sel'); }} else deselect();
    }}
  }}
  function strip_selection(root) {{
    root.querySelectorAll('.mm-sel').forEach(function (x) {{ x.classList.remove('mm-sel'); }});
  }}

  var __panel = document.getElementById('proppanel');
  function pf(id) {{ return document.getElementById(id); }}
  function deselect() {{
    if (__selEl) __selEl.classList.remove('mm-sel');
    __selEl = null; __selKey = null;
    __panel.style.display = 'none';
  }}
  function select_el(el) {{
    if (__selEl) __selEl.classList.remove('mm-sel');
    __selEl = el;
    __selKey = key_of(el);
    el.classList.add('mm-sel');
    var sh = shapes_of(el), tx = texts_of(el);
    var s0 = sh[0] ? getComputedStyle(sh[0]) : null;
    var t0 = tx.svg[0] ? getComputedStyle(tx.svg[0]).fill
           : (tx.html[0] ? getComputedStyle(tx.html[0]).color : null);
    var fs = tx.svg[0] ? getComputedStyle(tx.svg[0]).fontSize
           : (tx.html[0] ? getComputedStyle(tx.html[0]).fontSize : '');
    var sourceId = source_id_of(el);
    pf('pp_title').textContent = (el.id ? el.id : el.tagName.toLowerCase()
        + (el.getAttribute('class') ? '.' + el.getAttribute('class').split(/\\s+/)[0] : ''))
        + (sourceId ? '' : ' (preview only)');
    pf('pp_fill').value = (s0 && to_hex(s0.fill)) || '#ffffff';
    pf('pp_stroke').value = (s0 && to_hex(s0.stroke)) || '#000000';
    pf('pp_sw').value = s0 ? (parseFloat(s0.strokeWidth) || 0) : 1;
    pf('pp_text').value = to_hex(t0) || '#000000';
    pf('pp_fs').value = parseFloat(fs) || 14;
    pf('pp_op').value = s0 ? (parseFloat(s0.opacity) || 1) : 1;
    __panel.style.display = 'block';
  }}
  function panel_changed(prop, val) {{
    if (!__selEl) return;
    var o = __overrides[__selKey] || (__overrides[__selKey] = {{}});
    o[prop] = val;
    apply_props(__selEl, o);
    var id = source_id_of(__selEl);
    if (id) {{
      window.__sourceEdited = window.__sourceEdited || {{}};
      window.__sourceEdited[id] = true;
      window.__styleSendTimers = window.__styleSendTimers || {{}};
      clearTimeout(window.__styleSendTimers[id + ':' + prop]);
      window.__styleSendTimers[id + ':' + prop] = setTimeout(function () {{
        bridge_call('previewStyleChanged', id, prop, String(val));
        delete window.__styleSendTimers[id + ':' + prop];
      }}, 350);
    }}
  }}
  function cancel_style_sends(id) {{
    var timers = window.__styleSendTimers || {{}};
    Object.keys(timers).forEach(function (key) {{
      if (key.indexOf(id + ':') === 0) {{
        clearTimeout(timers[key]);
        delete timers[key];
      }}
    }});
  }}
  [['pp_fill', 'fill'], ['pp_stroke', 'stroke'], ['pp_sw', 'strokeWidth'],
   ['pp_text', 'textColor'], ['pp_fs', 'fontSize'], ['pp_op', 'opacity']].forEach(function (p) {{
    pf(p[0]).addEventListener('input', function () {{ panel_changed(p[1], this.value); }});
  }});
  pf('pp_reset').addEventListener('click', function () {{
    if (!__selEl) return;
    var id = source_id_of(__selEl);
    delete __overrides[__selKey];
    clear_props(__selEl);
    if (id) {{
      cancel_style_sends(id);
      delete (window.__sourceEdited || {{}})[id];
      bridge_call('previewStyleChanged', id, 'reset', '');
    }}
    select_el(__selEl);
  }});
  pf('pp_resetall').addEventListener('click', function () {{
    Object.keys(window.__sourceEdited || {{}}).forEach(function (id) {{
      cancel_style_sends(id);
      bridge_call('previewStyleChanged', id, 'reset', '');
    }});
    window.__sourceEdited = {{}};
    __overrides = {{}};
    var s = cur_svg();
    if (s) s.querySelectorAll('[style]').forEach(function (x) {{
      ['fill', 'stroke', 'stroke-width', 'opacity', 'font-size', 'color'].forEach(function (k) {{
        x.style.removeProperty(k);
      }});
    }});
    if (__selEl) select_el(__selEl);
  }});
  pf('pp_close').addEventListener('click', deselect);
  ['mousedown', 'mouseup', 'wheel', 'click', 'keydown'].forEach(function (ev) {{
    __panel.addEventListener(ev, function (e) {{ e.stopPropagation(); }}, {{ passive: true }});
  }});
  document.addEventListener('keydown', function (e) {{
    if (e.key === 'Escape') deselect();
  }});
  document.addEventListener('click', function (e) {{
    if (Date.now() - (window.__dragEnd || 0) < 80) return;
    var s = cur_svg(), t = e.target;
    if (!s || !(t instanceof Element) || t === s || !s.contains(t) || t.closest('defs, style, marker')) {{
      deselect();
      return;
    }}
    var el = t.closest(PICK);
    select_el(el && s.contains(el) ? el : t);
  }});
  window.addEventListener('resize', function () {{ if (view.fit) fit_view(false); }});
  __banner.addEventListener('click', function () {{ __banner.style.display = 'none'; }});

  // ---- errors ----------------------------------------------------------
  function show_error(msg) {{
    window.__seq++;
    window.__hasDiagram = false;
    deselect();
    __wrap.innerHTML = '<div class="errbox">Mermaid error<br>' + esc(msg) + '</div>';
    __banner.style.display = 'none';
    view.fit = true;
    fit_view(false);
  }}
  function show_banner(msg) {{
    __banner.textContent = msg;
    __banner.style.display = 'block';
  }}
  function drop_stray(id) {{
    // Mermaid leaves its own error SVG behind in <body> after a failed render.
    [id, 'd' + id].forEach(function (x) {{
      var el = document.getElementById(x);
      if (el && !__wrap.contains(el)) el.remove();
    }});
  }}
  function normalise_svg() {{
    var s = __wrap.querySelector('svg');
    if (!s) return;
    var vb = s.viewBox && s.viewBox.baseVal;
    var r = s.getBoundingClientRect();
    var w = (vb && vb.width) || r.width;
    var h = (vb && vb.height) || r.height;
    s.setAttribute('width', w);
    s.setAttribute('height', h);
    s.style.maxWidth = 'none';
    s.style.overflow = 'visible';
  }}

  async function mermaid_render(code, opts) {{
    const s = ++window.__seq;
    const id = 'mm' + s;
    try {{
      let text = String(code).replace(/^\\uFEFF/, '');
      text = text.trim().length === 0 ? '%% empty diagram' : text;
      const bg = (opts && opts.themeVariables && opts.themeVariables.background) || '#ffffff';
      window.__lastBg = bg;
      document.body.style.background = bg;
      const key = JSON.stringify(opts || {{}});
      if (key !== window.__optsKey) {{
        mermaid.initialize({{ startOnLoad: false, securityLevel: 'loose', ...opts }});
        window.__optsKey = key;
      }}
      const {{ svg, bindFunctions }} = await mermaid.render(id, text);
      if (window.__seq !== s) return;          // stale response
      __wrap.innerHTML = svg;
      drop_stray(id);
      __banner.style.display = 'none';
      window.__hasDiagram = true;
      normalise_svg();
      reapply_overrides();
      if (typeof bindFunctions === 'function') {{
        try {{ bindFunctions(__wrap); }} catch (e) {{}}
      }}
      if (view.fit) fit_view(false); else apply_view();
      bridge_call('renderDone', s, true, '', 0);
    }} catch (err) {{
      drop_stray(id);
      if (window.__seq !== s) return;
      const msg = err && err.message ? err.message : String(err);
      const m = /line\\s+(\\d+)/i.exec(msg);
      const line = m ? parseInt(m[1], 10) : 0;
      if (window.__hasDiagram) show_banner(msg); else show_error(msg);
      bridge_call('renderDone', s, false, msg, line);
    }}
  }}

  // Browser-rendered PNG (matches the preview exactly). The result goes back
  // through the bridge because runJavaScript does not await promises.
  function export_png(token, scale, transparent, maxpx) {{
    var s = document.querySelector('#wrap svg');
    if (!s) {{ bridge_call('pngDone', token, 'ERR:no diagram'); return; }}
    var vb = s.viewBox && s.viewBox.baseVal;
    var w = (vb && vb.width) || parseFloat(s.getAttribute('width')) || 0;
    var h = (vb && vb.height) || parseFloat(s.getAttribute('height')) || 0;
    if (!w || !h) {{ bridge_call('pngDone', token, 'ERR:empty diagram'); return; }}
    if (maxpx) scale = Math.min(scale, maxpx / Math.max(w, h));
    var clone = s.cloneNode(true);
    strip_selection(clone);
    clone.setAttribute('xmlns', NS);
    clone.setAttribute('width', w);
    clone.setAttribute('height', h);
    clone.style.maxWidth = '';
    var xml = new XMLSerializer().serializeToString(clone);
    var img = new Image();
    img.onload = function () {{
      try {{
        var c = document.createElement('canvas');
        c.width = Math.max(1, Math.round(w * scale));
        c.height = Math.max(1, Math.round(h * scale));
        var ctx = c.getContext('2d');
        if (!transparent) {{
          ctx.fillStyle = window.__lastBg || '#ffffff';
          ctx.fillRect(0, 0, c.width, c.height);
        }}
        ctx.drawImage(img, 0, 0, c.width, c.height);
        bridge_call('pngDone', token, c.toDataURL('image/png'));
      }} catch (e) {{
        bridge_call('pngDone', token, 'ERR:' + e.message);
      }}
    }};
    img.onerror = function () {{ bridge_call('pngDone', token, 'ERR:image load failed'); }};
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
  }}

  // Export-ready SVG: bake computed fill/stroke into attributes (mermaid's
  // CSS uses var(--c-*) which QSvgRenderer cannot resolve), drop <style>
  // blocks, and ensure a background rect.
  function export_svg() {{
    var s = document.querySelector('#wrap svg');
    if (!s) return '';
    var clone = s.cloneNode(true);
    strip_selection(clone);
    var live = s.querySelectorAll('*');
    var cl = clone.querySelectorAll('*');
    var NS = 'http://www.w3.org/2000/svg';
    var foPairs = [];
    for (var i = 0; i < live.length; i++) {{
      var cs = getComputedStyle(live[i]);
      var e = cl[i];
      var tag = live[i].tagName.toLowerCase();
      if (tag === 'foreignobject') foPairs.push([live[i], e]);
      if (tag === 'text' || tag === 'tspan') {{
        e.setAttribute('font-family', cs.fontFamily.replace(/"/g, "'"));
        e.setAttribute('font-size', cs.fontSize);
        e.setAttribute('font-weight', cs.fontWeight);
        if (cs.fontStyle && cs.fontStyle !== 'normal') e.setAttribute('font-style', cs.fontStyle);
        if (cs.textAnchor && cs.textAnchor !== 'start') e.setAttribute('text-anchor', cs.textAnchor);
        // QSvgRenderer mishandles em units on x/y/dy.
        ['x', 'y', 'dx', 'dy'].forEach(function (a) {{
          var v = e.getAttribute(a);
          if (v && /em$/.test(v)) e.setAttribute(a, parseFloat(v) * parseFloat(cs.fontSize));
        }});
      }}
      var f = cs.fill;
      if (f && f !== 'none' && f !== 'transparent') e.setAttribute('fill', f);
      var st = cs.stroke;
      if (st && st !== 'none' && st !== 'transparent') {{
        e.setAttribute('stroke', st);
        if (cs.strokeWidth) e.setAttribute('stroke-width', cs.strokeWidth);
      }}
    }}
    // QSvgRenderer ignores <foreignObject> (HTML labels), so swap each one
    // for an equivalent <text> built from the label's computed style.
    var svgRootInv = s.getScreenCTM().inverse();
    foPairs.forEach(function (p) {{
      var fo = p[0], target = p[1];
      var src = fo.querySelector('span, p, div') || fo;
      var lcs = getComputedStyle(src);
      var raw = (fo.innerText || fo.textContent || '').replace(/\\u00a0/g, ' ');
      var lines = raw.split(/\\r?\\n/).map(function (t) {{ return t.trim(); }})
                     .filter(function (t) {{ return t.length; }});
      if (!lines.length) {{ target.remove(); return; }}
      var fw = parseFloat(fo.getAttribute('width')) || fo.getBoundingClientRect().width;
      var fh = parseFloat(fo.getAttribute('height')) || fo.getBoundingClientRect().height;
      var fs = parseFloat(lcs.fontSize) || 16;
      var lh = fs * 1.2;
      var y0 = fh / 2 - (lines.length - 1) * lh / 2;
      var m = svgRootInv.multiply(fo.getScreenCTM());
      var g = document.createElementNS(NS, 'g');
      g.setAttribute('transform', 'matrix(' + [m.a, m.b, m.c, m.d, m.e, m.f].join(',')
                     + ') translate(' + (parseFloat(fo.getAttribute('x')) || 0)
                     + ',' + (parseFloat(fo.getAttribute('y')) || 0) + ')');
      // One <text> per line: QSvgRenderer mis-positions <tspan y=...>.
      lines.forEach(function (ln, k) {{
        var t = document.createElementNS(NS, 'text');
        t.setAttribute('x', fw / 2);
        t.setAttribute('y', y0 + k * lh + fs * 0.35);
        t.setAttribute('text-anchor', 'middle');
        t.setAttribute('font-family', lcs.fontFamily.replace(/"/g, "'"));
        t.setAttribute('font-size', fs + 'px');
        t.setAttribute('font-weight', lcs.fontWeight);
        if (lcs.fontStyle && lcs.fontStyle !== 'normal') t.setAttribute('font-style', lcs.fontStyle);
        t.setAttribute('fill', lcs.color);
        t.setAttribute('stroke', 'none');
        t.textContent = ln;
        g.appendChild(t);
      }});
      target.parentNode.removeChild(target);
      clone.appendChild(g);
    }});
    clone.querySelectorAll('style').forEach(function (x) {{ x.remove(); }});
    var vb = s.viewBox && s.viewBox.baseVal;
    var w = (vb && vb.width) || parseFloat(s.getAttribute('width')) || 0;
    var h = (vb && vb.height) || parseFloat(s.getAttribute('height')) || 0;
    if (w && h) {{
      var rect = clone.querySelector('rect#canvas');
      if (!rect) {{
        rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        clone.insertBefore(rect, clone.firstChild);
      }}
      rect.setAttribute('x', vb ? vb.x : 0);
      rect.setAttribute('y', vb ? vb.y : 0);
      rect.setAttribute('width', w);
      rect.setAttribute('height', h);
      rect.setAttribute('fill', window.__lastBg || '#ffffff');
      clone.setAttribute('width', w);
      clone.setAttribute('height', h);
    }}
    clone.style.maxWidth = '';
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    return new XMLSerializer().serializeToString(clone);
  }}

  // Native-shape export for PowerPoint: returns JSON describing every visible
  // shape / label in the diagram, in SVG user units relative to the viewBox.
  function export_shapes() {{
    var s = cur_svg();
    if (!s || !s.getScreenCTM()) return '';
    var vb = s.viewBox && s.viewBox.baseVal;
    var ox = vb ? vb.x : 0, oy = vb ? vb.y : 0;
    var W = (vb && vb.width) || parseFloat(s.getAttribute('width')) || 0;
    var H = (vb && vb.height) || parseFloat(s.getAttribute('height')) || 0;
    if (!W || !H) return '';
    var inv = s.getScreenCTM().inverse();
    var out = [], handled = new Set(), order = new Map(), oi = 0;
    s.querySelectorAll('*').forEach(function (el) {{ order.set(el, oi++); }});
    function mat(el) {{ return inv.multiply(el.getScreenCTM()); }}
    function sc(m) {{ return Math.sqrt(m.a * m.a + m.b * m.b) || 1; }}
    function tp(m, x, y) {{
      var p = s.createSVGPoint(); p.x = x; p.y = y;
      p = p.matrixTransform(m);
      return [p.x - ox, p.y - oy];
    }}
    function box(el) {{
      var b = el.getBBox(), m = mat(el);
      var c = [tp(m, b.x, b.y), tp(m, b.x + b.width, b.y),
               tp(m, b.x, b.y + b.height), tp(m, b.x + b.width, b.y + b.height)];
      var xs = c.map(function (p) {{ return p[0]; }}), ys = c.map(function (p) {{ return p[1]; }});
      var x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys);
      return {{ x: x0, y: y0, w: Math.max.apply(null, xs) - x0, h: Math.max.apply(null, ys) - y0 }};
    }}
    function col(c) {{
      var m = /rgba?\\(([^)]+)\\)/.exec(c || '');
      if (!m) return null;
      var p = m[1].split(/[,\\/ ]+/).filter(Boolean).map(parseFloat);
      var a = p.length > 3 ? p[3] : 1;
      if (!(a > 0)) return null;
      var hex = [0, 1, 2].map(function (i) {{
        return ('0' + Math.round(p[i]).toString(16)).slice(-2);
      }}).join('');
      return {{ c: '#' + hex, a: a }};
    }}
    function lines_of(str) {{
      return String(str || '').replace(/\\u00a0/g, ' ').split(/\\r?\\n/)
        .map(function (t) {{ return t.trim(); }}).filter(function (t) {{ return t.length; }});
    }}
    function text_info(root) {{
      var fo = root.tagName.toLowerCase() === 'foreignobject' ? root : root.querySelector('foreignObject');
      var ls, cs, el;
      if (fo) {{
        el = fo.querySelector('span, p, div') || fo;
        cs = getComputedStyle(el);
        ls = lines_of(fo.innerText || fo.textContent);
        var c1 = col(cs.color), fz = parseFloat(cs.fontSize) * sc(mat(fo));
      }} else {{
        var ts = root.tagName.toLowerCase() === 'text' ? [root] : Array.prototype.slice.call(root.querySelectorAll('text'));
        if (!ts.length) return null;
        ls = [];
        ts.forEach(function (t) {{
          var sp = t.querySelectorAll('tspan');
          if (sp.length > 1) sp.forEach(function (x) {{ ls = ls.concat(lines_of(x.textContent)); }});
          else ls = ls.concat(lines_of(t.textContent));
        }});
        el = ts[0];
        cs = getComputedStyle(el);
        var c1 = col(cs.fill), fz = parseFloat(cs.fontSize) * sc(mat(el));
      }}
      if (!ls.length) return null;
      var fam = (cs.fontFamily || '').split(',')[0].replace(/["']/g, '').trim();
      return {{ lines: ls, c: c1 ? c1.c : '#000000', size: fz || 14, family: fam || 'Arial',
               bold: parseInt(cs.fontWeight, 10) >= 600, italic: cs.fontStyle === 'italic' }};
    }}
    function paint(el, it, m) {{
      var cs = getComputedStyle(el), op = parseFloat(cs.opacity);
      if (isNaN(op)) op = 1;
      var f = col(cs.fill);
      if (f) {{ f.a *= op * (parseFloat(cs.fillOpacity) || 1); }}
      var st = col(cs.stroke), sw = parseFloat(cs.strokeWidth) * sc(m);
      if (st && sw > 0) {{
        st.a *= op * (isNaN(parseFloat(cs.strokeOpacity)) ? 1 : parseFloat(cs.strokeOpacity));
        st.w = sw;
        st.dash = cs.strokeDasharray && cs.strokeDasharray !== 'none';
      }} else st = null;
      it.fill = f; it.stroke = st;
      var me = el.getAttribute('marker-end') || (cs.markerEnd !== 'none' ? cs.markerEnd : '');
      var ms = el.getAttribute('marker-start') || (cs.markerStart !== 'none' ? cs.markerStart : '');
      it.tail = !!(me && me !== 'none'); it.head = !!(ms && ms !== 'none');
    }}
    function sample(el, m) {{
      var len = el.getTotalLength(), n = Math.max(8, Math.min(120, Math.ceil(len / 10))), pts = [];
      for (var i = 0; i <= n; i++) {{
        var p = el.getPointAtLength(len * i / n);
        pts.push(tp(m, p.x, p.y));
      }}
      return pts;
    }}
    function shape_item(el) {{
      var tag = el.tagName.toLowerCase(), m = mat(el), it = {{}};
      var b = box(el);
      it.x = b.x; it.y = b.y; it.w = b.w; it.h = b.h; it.r = 0; it.text = null;
      paint(el, it, m);
      if (tag === 'rect') {{
        it.k = 'rect';
        it.r = Math.max(parseFloat(el.getAttribute('rx')) || 0, parseFloat(el.getAttribute('ry')) || 0) * sc(m);
      }} else if (tag === 'circle' || tag === 'ellipse') {{
        it.k = 'oval';
      }} else if (tag === 'polygon' || tag === 'polyline') {{
        it.k = 'poly';
        it.closed = tag === 'polygon';
        it.pts = [];
        for (var i = 0; i < el.points.numberOfItems; i++) {{
          var p = el.points.getItem(i);
          it.pts.push(tp(m, p.x, p.y));
        }}
      }} else if (tag === 'line') {{
        it.k = 'poly'; it.closed = false;
        it.pts = [tp(m, el.x1.baseVal.value, el.y1.baseVal.value), tp(m, el.x2.baseVal.value, el.y2.baseVal.value)];
      }} else if (tag === 'path') {{
        it.k = 'poly';
        it.closed = !!it.fill;
        it.pts = sample(el, m);
      }} else return null;
      if (!it.fill && !it.stroke) return null;
      if ((it.k === 'rect' || it.k === 'oval') && (it.w < 0.5 || it.h < 0.5)) return null;
      if (it.k === 'poly' && it.pts.length < 2) return null;
      return it;
    }}
    function skipped(el) {{
      return !!el.closest('defs, marker, style, symbol, clipPath, mask, pattern');
    }}

    // Flowchart-style nodes: one shape that carries its own label.
    s.querySelectorAll('g.node').forEach(function (g) {{
      if (skipped(g)) return;
      var labels = g.querySelectorAll('foreignObject, text');
      if (labels.length > 1) return;
      var cands = Array.prototype.slice.call(g.querySelectorAll('rect, polygon, circle, ellipse, path'))
        .filter(function (x) {{ return !x.closest('foreignObject, g.label'); }});
      if (!cands.length) return;
      var it = null;
      try {{ it = shape_item(cands[0]); }} catch (e) {{}}
      if (!it) return;
      it.text = text_info(g);
      it.node = true;
      it.o = order.get(cands[0]);
      out.push(it);
      handled.add(cands[0]);
      g.querySelectorAll('foreignObject, text').forEach(function (x) {{ handled.add(x); }});
    }});

    // Everything else, in document (paint) order.
    var items = [];
    s.querySelectorAll('*').forEach(function (el) {{
      if (skipped(el) || el.closest('foreignObject') && el.tagName.toLowerCase() !== 'foreignobject') return;
      var tag = el.tagName.toLowerCase();
      var cs0 = getComputedStyle(el);
      if (cs0.display === 'none' || cs0.visibility === 'hidden') return;
      try {{
        if (tag === 'text' || tag === 'foreignobject') {{
          if (handled.has(el)) return;
          var ti = text_info(el);
          if (!ti) return;
          var b = box(el);
          items.push({{ k: 'text', o: order.get(el), x: b.x, y: b.y, w: b.w, h: b.h, text: ti, fill: null, stroke: null }});
        }} else if (!handled.has(el)) {{
          var it = shape_item(el);
          if (it) {{ it.o = order.get(el); items.push(it); }}
        }}
      }} catch (e) {{}}
    }});
    var all = out.concat(items);
    all.sort(function (a, b) {{ return a.o - b.o; }});
    return JSON.stringify({{ w: W, h: H, bg: window.__lastBg || '#ffffff', items: all }});
  }}
</script>
{js}
<script>
  if (typeof mermaid === 'undefined') {{
    show_error('The Mermaid library failed to load. Check your internet '
               + 'connection, or choose Mermaid > Download for offline use.');
  }}
</script>
</body>
</html>
"""


def build_standalone_html(code: str, bundle: str, title: str, colors: dict) -> str:
    """Build a single-file HTML diagram with its Mermaid runtime embedded."""
    safe_bundle = bundle.replace("</script", "<\\/script")
    safe_code = json.dumps(code).replace("<", "\\u003c")
    safe_title = html.escape(title)
    safe_colors = json.dumps(colors)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
  html, body {{ min-height: 100%; margin: 0; }}
  body {{ display: grid; place-items: center; padding: 24px; box-sizing: border-box;
          background: #f8fafc; font-family: system-ui, sans-serif; }}
  main {{ max-width: 100%; overflow: auto; background: white; padding: 16px;
          border-radius: 8px; box-shadow: 0 2px 12px #0002; }}
  svg {{ display: block; max-width: 100%; height: auto; }}
  #error {{ color: #b91c1c; white-space: pre-wrap; }}
</style>
<script>{safe_bundle}</script>
</head>
<body>
<main id="diagram"></main>
<script>
  const source = {safe_code};
  mermaid.initialize({{ startOnLoad: false, securityLevel: 'loose',
                       theme: 'base', themeVariables: {safe_colors} }});
  mermaid.render('mermaid-studio-diagram', source).then(function (result) {{
    document.getElementById('diagram').innerHTML = result.svg;
    if (typeof result.bindFunctions === 'function')
      result.bindFunctions(document.getElementById('diagram'));
  }}).catch(function (error) {{
    document.getElementById('diagram').innerHTML =
      '<pre id="error"></pre>';
    document.getElementById('error').textContent =
      error && error.message ? error.message : String(error);
  }});
</script>
</body>
</html>
"""


def js_str(s: str) -> str:
    """Quote a Python str as a JavaScript string literal."""
    return json.dumps(s)


def build_pptx(data: dict, path: Path) -> None:
    """Write the shape description from export_shapes() as native PowerPoint shapes."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.dml import MSO_LINE
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.oxml.xmlchemy import OxmlElement
    from pptx.util import Emu, Pt

    px = 9525                                   # EMU per CSS pixel
    margin = 24.0
    width, height = float(data["w"]), float(data["h"])
    k = min(1.0, 5000.0 / (max(width, height) + 2 * margin))   # PowerPoint caps slides at 56in

    def pos(v: float) -> int:
        return int(round((v + margin) * k * px))

    def size(v: float) -> int:
        return max(0, int(round(v * k * px)))

    def rgb(c: str) -> RGBColor:
        return RGBColor.from_string(c.lstrip("#").upper())

    def alpha(node, a: float) -> None:
        clr = node.find(".//" + qn("a:srgbClr")) if node is not None else None
        if clr is not None and a < 0.999:
            el = OxmlElement("a:alpha")
            el.set("val", str(int(max(0.0, a) * 100000)))
            clr.append(el)

    prs = Presentation()
    prs.slide_width = Emu(size(width + 2 * margin))
    prs.slide_height = Emu(size(height + 2 * margin))
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    bg = _hex_or_none(data.get("bg"))
    if bg:
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = rgb(bg)

    def style(shape, it: dict) -> None:
        fill, stroke = it.get("fill"), it.get("stroke")
        if fill:
            shape.fill.solid()
            shape.fill.fore_color.rgb = rgb(fill["c"])
            alpha(shape._element.spPr.find(qn("a:solidFill")), fill["a"])
        else:
            shape.fill.background()
        if stroke:
            shape.line.color.rgb = rgb(stroke["c"])
            shape.line.width = Emu(max(1, size(stroke["w"])))
            alpha(shape._element.spPr.find(qn("a:ln")), stroke["a"])
            if stroke.get("dash"):
                shape.line.dash_style = MSO_LINE.DASH
            ln = shape.line._get_or_add_ln()
            for flag, tag in (("head", "a:headEnd"), ("tail", "a:tailEnd")):
                if it.get(flag):
                    end = OxmlElement(tag)
                    end.set("type", "triangle")
                    ln.append(end)
        else:
            shape.line.fill.background()
        shape.shadow.inherit = False

    def fill_text(tf, t: dict, wrap: bool) -> None:
        tf.word_wrap = wrap
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        for i, line in enumerate(t["lines"]):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = PP_ALIGN.CENTER
            run = p.add_run()
            run.text = line
            f = run.font
            f.size = Pt(max(1.0, round(t["size"] * k * 0.75, 1)))
            f.bold = bool(t.get("bold"))
            f.italic = bool(t.get("italic"))
            f.name = t.get("family") or "Arial"
            f.color.rgb = rgb(t.get("c") or "#000000")

    def is_diamond(pts, x, y, w, h) -> bool:
        if len(pts) not in (4, 5) or w <= 0 or h <= 0:
            return False
        mids = [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)]
        return all(any(abs(px_ - mx) < w * 0.03 + 1 and abs(py_ - my) < h * 0.03 + 1
                       for px_, py_ in pts) for mx, my in mids)

    for it in data.get("items", []):
        kind = it["k"]
        x, y, w, h = it["x"], it["y"], it["w"], it["h"]
        if kind == "text":
            pad = w * 0.1 + 2
            box = slide.shapes.add_textbox(Emu(pos(x - pad / 2)), Emu(pos(y)),
                                           Emu(size(w + pad)), Emu(size(h)))
            fill_text(box.text_frame, it["text"], False)
            continue
        shape = None
        if kind == "rect":
            rounded = it.get("r", 0) > 0.5
            shape = slide.shapes.add_shape(
                MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE,
                Emu(pos(x)), Emu(pos(y)), Emu(size(w)), Emu(size(h)))
            if rounded and min(w, h) > 0:
                shape.adjustments[0] = min(0.5, it["r"] / min(w, h))
        elif kind == "oval":
            shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, Emu(pos(x)), Emu(pos(y)),
                                           Emu(size(w)), Emu(size(h)))
        elif kind == "poly":
            pts = it["pts"]
            if it.get("closed") and is_diamond(pts, x, y, w, h):
                shape = slide.shapes.add_shape(MSO_SHAPE.DIAMOND, Emu(pos(x)), Emu(pos(y)),
                                               Emu(size(w)), Emu(size(h)))
            else:
                ep = [(pos(a), pos(b)) for a, b in pts]
                fb = slide.shapes.build_freeform(ep[0][0], ep[0][1], scale=1.0)
                fb.add_line_segments(ep[1:], close=bool(it.get("closed")))
                shape = fb.convert_to_shape()
        if shape is None:
            continue
        style(shape, it)
        if it.get("text"):
            fill_text(shape.text_frame, it["text"], True)

    prs.save(str(path))


def _hex_or_none(c) -> str | None:
    c = (c or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        return c
    m = re.fullmatch(r"#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])", c)
    return "#" + "".join(ch * 2 for ch in m.groups()) if m else None


# --------------------------------------------------------------------------
# Page <-> Python bridge and bundle downloader
# --------------------------------------------------------------------------
class Bridge(QObject):
    """Receives callbacks from the preview page through QWebChannel."""

    pageReady = Signal()
    renderFinished = Signal(bool, str, int)          # ok, message, error line
    viewChanged = Signal(float, float, float, bool, bool)   # zoom, x, y, fit, user
    pngReady = Signal(int, str)                      # token, data URL or "ERR:..."
    styleChanged = Signal(str, str, str)             # source id, property, value

    @Slot()
    def ready(self) -> None:
        self.pageReady.emit()

    @Slot(int, bool, str, int)
    def renderDone(self, seq: int, ok: bool, message: str, line: int) -> None:
        self.renderFinished.emit(ok, message, line)

    @Slot(float, float, float, bool, bool)
    def viewState(self, zoom: float, x: float, y: float, fit: bool, user: bool) -> None:
        self.viewChanged.emit(zoom, x, y, fit, user)

    @Slot(int, str)
    def pngDone(self, token: int, data: str) -> None:
        self.pngReady.emit(token, data)

    @Slot(str, str, str)
    def previewStyleChanged(self, source_id: str, prop: str, value: str) -> None:
        self.styleChanged.emit(source_id, prop, value)


class BundleDownloader(QObject):
    """Downloads a pinned mermaid.min.js into the cache folder on a worker thread."""

    finished = Signal(str, bool, str)                # version, ok, message

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._active: set[str] = set()

    def busy(self, version: str) -> bool:
        return version in self._active

    def start(self, version: str) -> None:
        if version in self._active:
            return
        self._active.add(version)
        dest = cache_dir() / f"mermaid-{version}.min.js"
        threading.Thread(target=self._run, args=(version, dest), daemon=True).start()

    def _run(self, version: str, dest: Path) -> None:
        ok, msg = False, ""
        try:
            req = urllib.request.Request(
                mermaid_cdn_url(version), headers={"User-Agent": "MermaidStudio"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if len(data) < 100_000 or b"mermaid" not in data:
                raise ValueError("downloaded file does not look like Mermaid")
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            ok = True
        except Exception as e:                       # network, disk, validation
            msg = str(e)
        self._active.discard(version)
        self.finished.emit(version, ok, msg)


# --------------------------------------------------------------------------
# Pure helpers: outline parsing and text -> diagram generation
# --------------------------------------------------------------------------
OUTLINE_PATTERNS = [
    (re.compile(r"^\s*subgraph\s+(.+)$", re.I), "subgraph"),
    (re.compile(r"^\s*section\s+(.+)$", re.I), "section"),
    (re.compile(r"^\s*(?:participant|actor)\s+(.+)$", re.I), "participant"),
    (re.compile(r"^\s*class\s+(\w[\w.-]*)\s*(?:\{|$)", re.I), "class"),
    (re.compile(r"^\s*state\s+(.+?)\s*(?:\{|$)", re.I), "state"),
    (re.compile(r"^\s*(\w[\w-]*)\s*\{\s*$"), "entity"),
    (re.compile(r"^\s*branch\s+(.+)$", re.I), "branch"),
    (re.compile(r"^\s*(?:loop|alt|opt|par|critical|break|rect)\b\s*(.*)$", re.I), "block"),
    (re.compile(r"^\s*title\s*:?\s*(.+)$", re.I), "title"),
]


def parse_outline(text: str) -> list[tuple[int, int, str, str]]:
    """Return (line_no, depth, kind, label) entries describing the diagram structure."""
    items: list[tuple[int, int, str, str]] = []
    header_done = False
    for n, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("%%") or s == "---":
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        depth = indent // 4
        if not header_done:
            first = s.split()[0].lower()
            if first in KEYWORDS:
                items.append((n, 0, "diagram", s))
                header_done = True
                continue
        for rx, kind in OUTLINE_PATTERNS:
            m = rx.match(raw)
            if m:
                label = m.group(1).strip() if m.groups() and m.group(1) else kind
                items.append((n, depth, kind, label))
                break
    return items


def outline_to_mermaid(text: str, kind: str = "auto") -> str:
    """Rule-based conversion of plain text into Mermaid (no network, no AI model).

    flowchart: lines like `Start -> Validate input -> Valid? -> Save`
    sequence:  lines like `User -> Server: login request`
    mindmap:   indented bullet outline (first line is the root)
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    arrow = re.compile(r"\s*(?:--+>|->|→|=>)\s*")
    if kind == "auto":
        has_arrow = any(arrow.search(ln) for ln in lines)
        indented = any(ln[:1] in (" ", "\t", "-", "*") for ln in lines)
        if has_arrow and any(re.search(r"(?:->|→|=>)[^:]*:", ln) for ln in lines):
            kind = "sequence"
        elif has_arrow:
            kind = "flowchart"
        elif indented or len(lines) > 1:
            kind = "mindmap"
        else:
            kind = "flowchart"

    def clean(label: str) -> str:
        label = label.strip().strip("-*• ").strip()
        return label.replace('"', "#quot;")

    if kind == "mindmap":
        out = ["mindmap"]
        stack: list[int] = []
        for i, ln in enumerate(lines):
            expanded = ln.replace("\t", "    ")
            indent = len(expanded) - len(expanded.lstrip(" "))
            while stack and stack[-1] >= indent:
                stack.pop()
            level = len(stack)
            if i > 0 and level == 0:             # extra top-level lines become root children
                stack = [-1]
                level = 1
            stack.append(indent)
            label = re.sub(r'[()\[\]{}"]', "", clean(expanded)) or "node"
            pad = "  " * (level + 1)
            out.append(f"{pad}root(({label}))" if i == 0 else f"{pad}{label}")
        return "\n".join(out) + "\n"

    if kind == "sequence":
        out = ["sequenceDiagram"]
        seen: list[str] = []
        body: list[str] = []

        def pid(name: str) -> str:
            return re.sub(r"\W", "_", name) or "P"

        for ln in lines:
            m = re.match(r"^\s*(.+?)\s*(--+>|->|→|=>)\s*(.+?)\s*:\s*(.*)$", ln)
            if not m:
                continue
            a, arr, b, msg = clean(m.group(1)), m.group(2), clean(m.group(3)), clean(m.group(4))
            for who in (a, b):
                if who not in seen:
                    seen.append(who)
            body.append(f"    {pid(a)}{'-->>' if arr.startswith('--') else '->>'}{pid(b)}: {msg}")
        out += [f"    participant {pid(w)} as {w}" if pid(w) != w else f"    participant {w}"
                for w in seen]
        return "\n".join(out + body) + "\n"

    # flowchart
    ids: dict[str, str] = {}
    out = ["flowchart TD"]
    terminal = {"start", "end", "begin", "finish", "done", "stop"}

    def node(label: str) -> str:
        label = clean(label)
        if label in ids:
            return ids[label]
        nid = f"N{len(ids) + 1}"
        ids[label] = nid
        if label.endswith("?"):
            return f'{nid}{{"{label}"}}'
        if label.lower() in terminal:
            return f'{nid}(["{label}"])'
        return f'{nid}["{label}"]'

    for ln in lines:
        parts = [p for p in arrow.split(ln.strip()) if p.strip()]
        if parts:
            out.append("    " + " --> ".join(node(p) for p in parts))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Editor highlighting
# --------------------------------------------------------------------------
KEYWORDS = {
    "flowchart", "graph", "sequencediagram", "classdiagram", "statediagram-v2",
    "statediagram", "erdiagram", "pie", "gantt", "mindmap", "timeline",
    "journey", "gitgraph", "quadrantchart", "sankey-beta", "xychart-beta",
    "block-beta", "kanban", "requirementdiagram", "c4context", "c4container",
    "c4component", "c4dynamic", "c4deployment", "packet", "packet-beta",
    "architecture-beta",
}
BLOCK_WORDS = {
    "participant", "actor", "alt", "else", "opt", "loop", "par", "end", "note",
    "section", "title", "dateformat", "axisformat", "excludes", "includes",
    "classdef", "class", "style", "linkstyle", "click", "acctitle", "accdescr",
    "direction",
}
TOKEN_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_-]*)\b|"([^"]*)"|\'([^\']*)\'')


# --------------------------------------------------------------------------
# Style editing (chart-level init directive and element-level style/classDef)
# --------------------------------------------------------------------------
INIT_RX = re.compile(r"%%\{\s*(?:init|initialize)\s*:\s*(.*?)\s*\}%%", re.S)
CSS_BEGIN, CSS_END = "/*ms*/", "/*/ms*/"
CSS_BLOCK_RX = re.compile(re.escape(CSS_BEGIN) + r".*?" + re.escape(CSS_END), re.S)
THEME_KEYS = {"font_family": "fontFamily", "font_size": "fontSize",
              "fill": "primaryColor", "stroke": "primaryBorderColor",
              "color": "primaryTextColor"}
# kind -> (config section, {option: key}); spacing/margin options differ per diagram
SECTION_KEYS = {
    "flowchart": ("flowchart", {"padding": "padding", "margin": "diagramPadding",
                                "node_spacing": "nodeSpacing", "rank_spacing": "rankSpacing"}),
    "classdiagram": ("class", {"padding": "padding", "margin": "diagramPadding",
                               "node_spacing": "nodeSpacing", "rank_spacing": "rankSpacing"}),
    "statediagram": ("state", {"padding": "padding", "margin": "diagramPadding",
                               "node_spacing": "nodeSpacing", "rank_spacing": "rankSpacing"}),
    "erdiagram": ("er", {"padding": "entityPadding", "margin": "diagramPadding"}),
    "sequencediagram": ("sequence", {"padding": "boxMargin", "margin": "diagramMarginX",
                                     "node_spacing": "actorMargin",
                                     "rank_spacing": "messageMargin"}),
    "gantt": ("gantt", {"padding": "barGap", "margin": "topPadding"}),
}
ELEMENT_KINDS = {"flowchart", "classdiagram", "statediagram"}
_KIND_ALIASES = {"graph": "flowchart", "flowchart-elk": "flowchart",
                 "statediagram-v2": "statediagram", "classdiagram-v2": "classdiagram"}


def diagram_kind(text: str) -> str:
    """Lower-cased diagram keyword (graph/flowchart-elk/-v2 variants normalised)."""
    in_front = False
    for raw in text.splitlines():
        s = raw.strip()
        if s == "---":
            in_front = not in_front
            continue
        if in_front or not s or s.startswith("%%"):
            continue
        word = s.split()[0].lower()
        return _KIND_ALIASES.get(word, word)
    return ""


def _loads_loose(s: str) -> dict | None:
    for cand in (s, s.replace("'", '"')):
        try:
            v = json.loads(cand)
        except ValueError:
            continue
        return v if isinstance(v, dict) else None
    return None


def _deep_merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def _prune(d: dict) -> dict:
    for k in list(d):
        if isinstance(d[k], dict):
            _prune(d[k])
        if d[k] in ({}, "", None):
            del d[k]
    return d


def read_init(text: str) -> dict:
    m = INIT_RX.search(text)
    return (_loads_loose(m.group(1)) or {}) if m else {}


def _head_insert_pos(text: str) -> int:
    """Character offset where a new init directive goes (after any front matter)."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        pos = len(lines[0])
        for ln in lines[1:]:
            pos += len(ln)
            if ln.strip() == "---":
                return pos
    return 0


def write_init(text: str, cfg: dict) -> str:
    m = INIT_RX.search(text)
    editable = m is not None and _loads_loose(m.group(1)) is not None
    if not cfg:
        if not editable:
            return text
        end = m.end() + (1 if text[m.end():m.end() + 1] == "\n" else 0)
        return text[:m.start()] + text[end:]
    directive = "%%{init: " + json.dumps(cfg, separators=(",", ":")) + "}%%"
    if editable:
        return text[:m.start()] + directive + text[m.end():]
    pos = _head_insert_pos(text)
    return text[:pos] + directive + "\n" + text[pos:]


def _css_block(opts: dict) -> str:
    rules = []
    if opts.get("stroke_width") is not None:
        rules.append(".node rect,.node polygon,.node circle,.node ellipse,.node path,"
                     f".cluster rect{{stroke-width:{opts['stroke_width']}px}}")
    if opts.get("radius") is not None:
        rules.append(f".node rect,.cluster rect{{rx:{opts['radius']}px;ry:{opts['radius']}px}}")
    if opts.get("font_weight"):
        rules.append(".nodeLabel,.node text,.cluster-label span,.cluster-label text,"
                     f".label text{{font-weight:{opts['font_weight']}}}")
    return CSS_BEGIN + "".join(rules) + CSS_END if rules else ""


def chart_style_options(text: str) -> dict:
    """Best-effort reverse of apply_chart_style, used to prefill the dialog."""
    cfg = read_init(text)
    tv = cfg.get("themeVariables", {}) if isinstance(cfg.get("themeVariables"), dict) else {}
    out: dict = {}
    for opt, key in THEME_KEYS.items():
        if key in tv:
            out[opt] = tv[key]
    if isinstance(out.get("font_size"), str):
        m = re.match(r"\d+", out["font_size"])
        out["font_size"] = int(m.group()) if m else None
    sec = SECTION_KEYS.get(diagram_kind(text))
    if sec and isinstance(cfg.get(sec[0]), dict):
        for opt, key in sec[1].items():
            if key in cfg[sec[0]]:
                out[opt] = cfg[sec[0]][key]
    css = cfg.get("themeCSS", "")
    for pat, opt, conv in ((r"stroke-width:(\d+)px", "stroke_width", int),
                           (r"rx:(\d+)px", "radius", int),
                           (r"font-weight:(\w+)", "font_weight", str)):
        m = re.search(pat, css or "")
        if m:
            out[opt] = conv(m.group(1))
    return {k: v for k, v in out.items() if v is not None}


def apply_chart_style(text: str, opts: dict, clear: bool = False) -> str:
    """Merge chart-wide settings into the text's %%{init}%% directive.
    `opts` keys: font_family, font_size, font_weight, fill, stroke, stroke_width,
    color, radius, padding, margin, node_spacing, rank_spacing (absent = unchanged)."""
    cfg = read_init(text)
    sec = SECTION_KEYS.get(diagram_kind(text))
    if clear:
        tv = cfg.get("themeVariables")
        if isinstance(tv, dict):
            for key in THEME_KEYS.values():
                tv.pop(key, None)
        if sec and isinstance(cfg.get(sec[0]), dict):
            for key in sec[1].values():
                cfg[sec[0]].pop(key, None)
        if isinstance(cfg.get("themeCSS"), str):
            cfg["themeCSS"] = CSS_BLOCK_RX.sub("", cfg["themeCSS"])
        return write_init(text, _prune(cfg))
    add: dict = {}
    for opt, key in THEME_KEYS.items():
        if opts.get(opt) not in (None, ""):
            val = opts[opt]
            add.setdefault("themeVariables", {})[key] = f"{val}px" if opt == "font_size" else val
    if sec:
        for opt, key in sec[1].items():
            if opts.get(opt) is not None:
                add.setdefault(sec[0], {})[key] = opts[opt]
    _deep_merge(cfg, add)
    block = _css_block(opts)
    if block:
        old = CSS_BLOCK_RX.sub("", cfg.get("themeCSS", "")) if isinstance(cfg.get("themeCSS"), str) else ""
        cfg["themeCSS"] = old + block
    return write_init(text, _prune(cfg))


_SKIP_LINE = re.compile(
    r"^(?:%%|style\b|classDef\b|class\b|click\b|linkStyle\b|direction\b|end\b|note\b|"
    r"title\b|accTitle|accDescr|---|flowchart\b|graph\b|stateDiagram|classDiagram)", re.I)
_FLOW_SKIP = {"TD", "TB", "BT", "RL", "LR"}


def _strip_labels(s: str) -> str:
    s = re.sub(r'"[^"]*"', "", s)
    for _ in range(3):
        s = re.sub(r"\[[^\]]*\]|\([^()]*\)|\{[^{}]*\}", "", s)
    s = re.sub(r"\|[^|]*\|", "", s)
    s = re.sub(r"--\s+[^->=]+?\s*-->", "-->", s)
    return re.sub(r":::\w+", "", s)


def list_elements(text: str) -> list[str]:
    """Element ids that can be styled (flowchart/state/class diagrams) plus
    existing classDefs as 'classDef:NAME'."""
    kind = diagram_kind(text)
    if kind not in ELEMENT_KINDS:
        return []
    ids: list[str] = []
    classes: list[str] = []

    def add(i: str):
        if i and i not in ids and i not in _FLOW_SKIP:
            ids.append(i)

    for raw in text.splitlines():
        s = raw.strip()
        m = re.match(r"classDef\s+(\w[\w-]*)", s)
        if m:
            classes.append("classDef:" + m.group(1))
            continue
        if kind == "classdiagram":
            m = re.match(r"class\s+(\w+)", s)
            if m:
                add(m.group(1))
                continue
            m = re.match(r'(\w+)\s+(?:"[^"]*"\s+)?[<|*o.\-\\/>]+\s*(?:"[^"]*"\s+)?(\w+)', s)
            if m:
                add(m.group(1))
                add(m.group(2))
            elif re.match(r"\w+\s*:", s):
                add(re.match(r"(\w+)", s).group(1))
            continue
        if not s or _SKIP_LINE.match(s) and not (kind == "flowchart" and s.startswith("subgraph")):
            continue
        if kind == "flowchart":
            m = re.match(r"subgraph\s+(\w[\w-]*)", s)
            if m:
                add(m.group(1))
                continue
            for tok in re.findall(r"[A-Za-z_]\w*", _strip_labels(s)):
                add(tok)
        else:
            m = re.match(r'state\s+(?:"[^"]*"\s+as\s+)?(\w+)', s)
            if m:
                add(m.group(1))
                continue
            s = re.sub(r"<<.*?>>|\[\*\]", "", s.split(":")[0])
            for tok in re.findall(r"[A-Za-z_]\w*", s):
                add(tok)
    return ids + classes


def lint_diagram(text: str) -> list[tuple[int, str]]:
    """Find source-level styling references that cannot match a diagram element."""
    if diagram_kind(text) != "flowchart":
        return []
    elements = set(list_elements(text))
    class_defs: set[str] = set()
    lines = text.splitlines()
    for raw in lines:
        m = re.match(r"^\s*classDef\s+([\w-]+)", raw)
        if m:
            class_defs.add(m.group(1))

    warnings: list[tuple[int, str]] = []
    for line_no, raw in enumerate(lines, start=1):
        m = re.match(r"^\s*style\s+([\w-]+)\s+", raw)
        if m and m.group(1) not in elements:
            warnings.append((line_no, f"Style target '{m.group(1)}' is not defined in this diagram."))
            continue
        m = re.match(r"^\s*class\s+([\w,-]+)\s+([\w,-]+)\s*;?\s*$", raw)
        if m:
            for target in m.group(1).split(","):
                if target not in elements:
                    warnings.append((line_no, f"Class target '{target}' is not defined in this diagram."))
            for class_name in m.group(2).split(","):
                if class_name not in class_defs:
                    warnings.append((line_no, f"Class '{class_name}' has no matching classDef."))
    return warnings


def parse_props(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in re.split(r"[,;]", s):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _safe(v: str) -> str:
    return re.sub(r"[,;{}%\"']", "", v).strip()


def element_props(opts: dict) -> dict[str, str]:
    """Translate dialog options into CSS-like style properties."""
    p: dict[str, str] = {}
    if opts.get("fill"):
        p["fill"] = _safe(opts["fill"])
    if opts.get("stroke"):
        p["stroke"] = _safe(opts["stroke"])
    if opts.get("stroke_width") is not None:
        p["stroke-width"] = f"{opts['stroke_width']}px"
    if opts.get("color"):
        p["color"] = _safe(opts["color"])
    if opts.get("font_family"):
        p["font-family"] = _safe(opts["font_family"].split(",")[0])
    if opts.get("font_size") is not None:
        p["font-size"] = f"{opts['font_size']}px"
    if opts.get("font_weight"):
        p["font-weight"] = _safe(opts["font_weight"])
    if opts.get("opacity") is not None:
        p["opacity"] = _safe(str(opts["opacity"]))
    if opts.get("radius") is not None:
        p["rx"] = p["ry"] = f"{opts['radius']}px"
    if opts.get("padding") is not None:
        p["padding"] = f"{opts['padding']}px"
    return p


_PROP_TO_OPT = {"fill": "fill", "stroke": "stroke", "color": "color",
                "opacity": "opacity",
                "font-family": "font_family", "font-weight": "font_weight"}
_PX_PROPS = {"stroke-width": "stroke_width", "font-size": "font_size",
             "rx": "radius", "padding": "padding"}


def _style_line_rx(kind: str, eid: str) -> tuple[str, re.Pattern]:
    """(statement keyword, regex matching the line that styles `eid`)."""
    if eid.startswith("classDef:"):
        name = re.escape(eid.split(":", 1)[1])
        return "classDef", re.compile(rf"^[ \t]*classDef[ \t]+{name}[ \t]+(.*)$", re.M)
    name = re.escape(eid)
    if kind in ("flowchart", "classdiagram"):
        return "style", re.compile(rf"^[ \t]*style[ \t]+{name}[ \t]+(.*)$",         re.M)
    return "classDef", re.compile(rf"^[ \t]*classDef[ \t]+ms_{name}[ \t]+(.*)$", re.M)


def element_style_options(text: str, eid: str) -> dict:
    _, rx = _style_line_rx(diagram_kind(text), eid)
    m = rx.search(text)
    if not m:
        return {}
    out: dict = {}
    for k, v in parse_props(m.group(1)).items():
        if k in _PROP_TO_OPT:
            out[_PROP_TO_OPT[k]] = v
        elif k in _PX_PROPS and re.match(r"\d+", v):
            out[_PX_PROPS[k]] = int(re.match(r"\d+", v).group())
    return out


def apply_element_style(text: str, eid: str, opts: dict, clear: bool = False) -> str:
    """Set (or with clear=True remove) the style of one element. New values are
    merged over the element's existing properties."""
    kind = diagram_kind(text)
    keyword, rx = _style_line_rx(kind, eid)
    m = rx.search(text)
    props = parse_props(m.group(1)) if m else {}
    if clear:
        props = {}
    else:
        props.update(element_props(opts))
    via_class = keyword == "classDef" and not eid.startswith("classDef:")
    target = f"ms_{eid}" if via_class else eid.split(":", 1)[-1]
    css = ",".join(f"{k}:{v}" for k, v in props.items())
    new_line = f"{keyword} {target} {css}"
    if via_class:
        apply_kw = "cssClass" if kind == "classdiagram" else "class"
        who = f'"{eid}"' if kind == "classdiagram" else eid
        apply_rx = re.compile(
            rf'^[ \t]*{apply_kw}[ \t]+"?{re.escape(eid)}"?[ \t]+ms_{re.escape(eid)}[ \t]*;?[ \t]*$\n?', re.M)
        has_apply = apply_rx.search(text) is not None
    if m:
        if props:
            text = text[:m.start()] + new_line + text[m.end():]
        else:
            end = m.end() + (1 if text[m.end():m.end() + 1] == "\n" else 0)
            text = text[:m.start()] + text[end:]
    elif props:
        text = text.rstrip("\n") + "\n" + new_line + "\n"
    if via_class:
        if props and not has_apply:
            text = text.rstrip("\n") + f"\n{apply_kw} {who} ms_{eid}\n"
        elif not props and has_apply:
            text = apply_rx.sub("", text)
    return text


class MermaidHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self._fmt_comment = QTextCharFormat()
        self._fmt_comment.setForeground(QColor("#7a8a99"))
        self._fmt_comment.setFontItalic(True)
        self._fmt_keyword = QTextCharFormat()
        self._fmt_keyword.setForeground(QColor("#7c3aed"))
        self._fmt_keyword.setFontWeight(700)
        self._fmt_block = QTextCharFormat()
        self._fmt_block.setForeground(QColor("#2563eb"))
        self._fmt_block.setFontWeight(700)
        self._fmt_string = QTextCharFormat()
        self._fmt_string.setForeground(QColor("#15803d"))

    def highlightBlock(self, text: str) -> None:
        i = text.find("%%")
        if i >= 0:
            self.setFormat(i, len(text) - i, self._fmt_comment)
            text = text[:i]
        for m in TOKEN_RE.finditer(text):
            kw = m.group(1)
            if kw:
                word = kw.lower()
                if word in KEYWORDS and not text[:m.start()].strip():
                    fmt = self._fmt_keyword
                elif word in BLOCK_WORDS:
                    fmt = self._fmt_block
                else:
                    continue
            else:
                fmt = self._fmt_string
            self.setFormat(m.start(), m.end() - m.start(), fmt)


# --------------------------------------------------------------------------
COMPLETION_WORDS = sorted({
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram-v2",
    "erDiagram", "pie", "gantt", "mindmap", "timeline", "journey", "gitGraph",
    "quadrantChart", "sankey-beta", "xychart-beta", "block-beta", "kanban",
    "requirementDiagram", "C4Context", "C4Container", "C4Component",
    "C4Dynamic", "C4Deployment", "packet-beta", "architecture-beta",
    "participant", "actor", "alt", "else", "opt", "loop", "par", "and", "end",
    "Note", "section", "title", "dateFormat", "axisFormat", "excludes",
    "includes", "classDef", "class", "style", "linkStyle", "click", "accTitle",
    "accDescr", "direction", "subgraph", "autonumber", "activate",
    "deactivate", "rect", "critical", "break", "option", "branch", "checkout",
    "commit", "merge", "cherry-pick", "namespace", "state", "service", "group",
})

SNIPPETS = {
    "Flowchart skeleton": "flowchart TD\n    A[Start] --> B{Decision}\n    B -->|Yes| C[Done]\n    B -->|No| D[Retry]$0",
    "Subgraph": "subgraph Name\n    direction TB\n    $0\nend",
    "Style class (flowchart)": "classDef highlight fill:#fde68a,stroke:#b45309,color:#1f2937\nclass A highlight$0",
    "Click link (flowchart)": 'click A href "https://example.com" _blank$0',
    "Sequence: loop": "loop Every minute\n    A->>B: ping$0\nend",
    "Sequence: alt / else": "alt Success\n    A->>B: ok$0\nelse Failure\n    A->>B: error\nend",
    "Sequence: opt": "opt Optional step\n    A->>B: maybe$0\nend",
    "Sequence: par": "par Branch one\n    A->>B: one$0\nand Branch two\n    A->>C: two\nend",
    "Sequence: note": "Note over A,B: $0",
    "Class definition": "class Name {\n    +String field\n    +method() void\n}$0",
    "ER entity": "ENTITY {\n    string name\n    int id PK$0\n}",
    "State composite": "state Parent {\n    [*] --> Child\n    Child --> [*]$0\n}",
    "Gantt section": "section Name\n    Task one :t1, 2024-01-01, 7d$0",
    "Comment": "%% $0",
}

BLOCK_OPEN_RE = re.compile(
    r"^(subgraph|loop|alt|opt|par|critical|break|rect|else|and|option)\b", re.I)


# --------------------------------------------------------------------------
# Editor / view subclasses (drag-drop neutralized)
# --------------------------------------------------------------------------
class LineNumberArea(QWidget):
    def __init__(self, editor: EditorEdit):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:                          # noqa: N802
        return QSize(self._editor.line_number_width(), 0)

    def paintEvent(self, event) -> None:                  # noqa: N802
        self._editor.paint_line_numbers(event)


class EditorEdit(QPlainTextEdit):
    """Code editor: line numbers, error-line marking, auto-indent, completion,
    comment toggle and snippets. Refuses file-URL drops (the main window opens
    them as tabs) but still accepts normal text paste."""

    style_requested = Signal(str)            # word under the cursor

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._error_line = 0
        self._area = LineNumberArea(self)
        self._completer = QCompleter(self)
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer_model = QStringListModel(COMPLETION_WORDS, self._completer)
        self._completer.setModel(self._completer_model)
        self._completer.activated.connect(self._insert_completion)
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_area)
        self.cursorPositionChanged.connect(self._refresh_selections)
        self._update_margin()
        self._refresh_selections()

    # ---- gutter ---------------------------------------------------------
    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self, *_args) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._area.scroll(0, dy)
        else:
            self._area.update(0, rect.y(), self._area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event) -> None:                 # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._area)
        pal = self.palette()
        base = pal.color(QPalette.ColorRole.Base)
        dark = base.lightness() < 128
        painter.fillRect(event.rect(), base.lighter(118) if dark else base.darker(104))
        text = pal.color(QPalette.ColorRole.Text)
        dim = QColor(text.red(), text.green(), text.blue(), 120)
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        height = self.fontMetrics().height()
        current = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                if number + 1 == self._error_line:
                    painter.fillRect(0, top, self._area.width(), bottom - top,
                                     QColor(220, 38, 38, 150))
                    painter.setPen(QColor("#ffffff"))
                else:
                    painter.setPen(text if number == current else dim)
                painter.drawText(0, top, self._area.width() - 6, height,
                                 Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    # ---- error line / current line --------------------------------------
    def set_error_line(self, line: int) -> None:
        line = line if 0 < line <= self.blockCount() else 0
        if line != self._error_line:
            self._error_line = line
            self._refresh_selections()
            self._area.update()

    @property
    def error_line(self) -> int:
        return self._error_line

    def goto_line(self, line: int) -> None:
        block = self.document().findBlockByNumber(max(0, line - 1))
        if block.isValid():
            cur = QTextCursor(block)
            self.setTextCursor(cur)
            self.centerCursor()
            self.setFocus()

    def _refresh_selections(self) -> None:
        sels: list[QTextEdit.ExtraSelection] = []
        cur_sel = QTextEdit.ExtraSelection()
        cur_sel.format.setBackground(QColor(127, 127, 127, 36))
        cur_sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        cur_sel.cursor = self.textCursor()
        cur_sel.cursor.clearSelection()
        sels.append(cur_sel)
        if self._error_line:
            block = self.document().findBlockByNumber(self._error_line - 1)
            if block.isValid():
                err = QTextEdit.ExtraSelection()
                err.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
                err.format.setUnderlineColor(QColor("#ef4444"))
                err.format.setBackground(QColor(239, 68, 68, 50))
                err.cursor = QTextCursor(block)
                err.cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                sels.append(err)
        self.setExtraSelections(sels)
        self._area.update()

    # ---- line editing helpers -------------------------------------------
    def _selected_blocks(self) -> tuple[int, int]:
        cur = self.textCursor()
        doc = self.document()
        start = doc.findBlock(cur.selectionStart()).blockNumber()
        end_block = doc.findBlock(cur.selectionEnd())
        end = end_block.blockNumber()
        if cur.hasSelection() and cur.selectionEnd() == end_block.position() and end > start:
            end -= 1                          # selection ends at column 0: exclude that line
        return start, end

    def _edit_lines(self, fn: Callable[[str], str]) -> None:
        first, last = self._selected_blocks()
        editor = QTextCursor(self.document())
        editor.beginEditBlock()
        for n in range(first, last + 1):
            block = self.document().findBlockByNumber(n)
            new = fn(block.text())
            if new != block.text():
                c = QTextCursor(block)
                c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                c.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                               QTextCursor.MoveMode.KeepAnchor)
                c.insertText(new)
        editor.endEditBlock()

    def toggle_comment(self) -> None:
        first, last = self._selected_blocks()
        texts = [self.document().findBlockByNumber(n).text() for n in range(first, last + 1)]
        nonblank = [t for t in texts if t.strip()]
        if not nonblank:
            return
        if all(t.lstrip().startswith("%%") for t in nonblank):
            self._edit_lines(lambda t: re.sub(r"^(\s*)%% ?", r"\1", t, count=1) if t.strip() else t)
        else:
            col = min(len(t) - len(t.lstrip()) for t in nonblank)
            self._edit_lines(lambda t: t[:col] + "%% " + t[col:] if t.strip() else t)

    def indent_selection(self) -> None:
        cur = self.textCursor()
        if cur.hasSelection():
            self._edit_lines(lambda t: "    " + t if t.strip() else t)
        else:
            cur.insertText(" " * (4 - cur.positionInBlock() % 4))

    def unindent_selection(self) -> None:
        self._edit_lines(lambda t: re.sub(r"^( {1,4}|\t)", "", t))

    def insert_snippet(self, snippet: str) -> None:
        cur = self.textCursor()
        line = cur.block().text()
        indent = line[:len(line) - len(line.lstrip(" \t"))]
        body = ("\n" + indent).join(snippet.split("\n"))
        if line.strip() and not cur.hasSelection():
            cur.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            body = "\n" + indent + body
        marker = body.find("$0")
        body = body.replace("$0", "")
        start = cur.selectionStart()
        cur.insertText(body)
        self.setTextCursor(cur)
        if marker >= 0:
            c2 = self.textCursor()
            c2.setPosition(start + marker)
            self.setTextCursor(c2)
        self.setFocus()

    def _auto_indent_newline(self) -> None:
        cur = self.textCursor()
        text = cur.block().text()
        col = cur.positionInBlock()
        indent = text[:len(text) - len(text.lstrip(" \t"))][:col]
        before = text[:col].strip()
        if BLOCK_OPEN_RE.match(before) or before.endswith("{"):
            indent += "    "
        cur.insertText("\n" + indent)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    # ---- completion -----------------------------------------------------
    def _word_prefix(self) -> str:
        cur = self.textCursor()
        before = cur.block().text()[:cur.positionInBlock()]
        m = re.search(r"[A-Za-z_]\w*$", before)
        return m.group(0) if m else ""

    def _insert_completion(self, completion: str) -> None:
        prefix = self._word_prefix()
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.Left,
                         QTextCursor.MoveMode.KeepAnchor, len(prefix))
        cur.insertText(completion)
        self.setTextCursor(cur)

    def show_completions(self, force: bool = False) -> None:
        prefix = self._word_prefix()
        popup = self._completer.popup()
        if len(prefix) < 2 and not force:
            popup.hide()
            return
        if len(prefix) <= 2 or force:
            words = set(COMPLETION_WORDS) | set(re.findall(r"[A-Za-z_]\w{2,}", self.toPlainText()))
            self._completer_model.setStringList(sorted(words, key=str.lower))
        self._completer.setCompletionPrefix(prefix)
        count = self._completer.completionCount()
        if count == 0 or (count == 1 and
                          self._completer.currentCompletion().lower() == prefix.lower()):
            popup.hide()
            return
        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.translate(self.viewport().pos())
        rect.setWidth(popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width() + 16)
        self._completer.complete(rect)

    def contextMenuEvent(self, event) -> None:            # noqa: N802
        cur = self.cursorForPosition(event.pos())
        cur.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cur.selectedText()
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        menu.addAction("Style chart / element…",
                       lambda: self.style_requested.emit(word))
        menu.exec(event.globalPos())
        menu.deleteLater()

    def keyPressEvent(self, event) -> None:               # noqa: N802
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        popup = self._completer.popup()
        if popup.isVisible() and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                                         Qt.Key.Key_Escape, Qt.Key.Key_Tab,
                                         Qt.Key.Key_Backtab):
            event.ignore()
            return
        if ctrl and key == Qt.Key.Key_Space:
            self.show_completions(force=True)
            return
        if ctrl and key == Qt.Key.Key_Slash:
            self.toggle_comment()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not ctrl:
            self._auto_indent_newline()
            return
        if key == Qt.Key.Key_Tab and not ctrl:
            self.indent_selection()
            return
        if key == Qt.Key.Key_Backtab:
            self.unindent_selection()
            return
        super().keyPressEvent(event)
        text = event.text()
        if ctrl or not text or not (text[-1].isalnum() or text[-1] == "_"):
            popup.hide()
            return
        self.show_completions()

    def canInsertFromMimeData(self, source) -> bool:      # noqa: N802
        if source and source.hasUrls():
            urls = source.urls()
            if urls and all(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in DIAGRAM_EXTS
                            for u in urls):
                return False          # let the window-level drop handle it
        return super().canInsertFromMimeData(source)


class FindBar(QWidget):
    """Find / replace bar that works on whichever editor is current."""

    def __init__(self, get_editor: Callable[[], EditorEdit | None], parent: QWidget | None = None):
        super().__init__(parent)
        self._get_editor = get_editor
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(2)

        row1 = QHBoxLayout()
        self._find = QLineEdit()
        self._find.setPlaceholderText("Find")
        self._find.setClearButtonEnabled(True)
        self._case = QCheckBox("Aa")
        self._case.setToolTip("Match case")
        self._regex = QCheckBox(".*")
        self._regex.setToolTip("Regular expression")
        prev_btn = QPushButton("↑")
        prev_btn.setToolTip("Previous match (Shift+F3)")
        next_btn = QPushButton("↓")
        next_btn.setToolTip("Next match (F3)")
        close_btn = QPushButton("✕")
        close_btn.setToolTip("Close (Esc)")
        for b in (prev_btn, next_btn, close_btn):
            b.setFixedWidth(28)
        self._status = QLabel("")
        for w in (self._find, self._case, self._regex, prev_btn, next_btn,
                  self._status, close_btn):
            row1.addWidget(w)
        row1.setStretch(0, 1)
        outer.addLayout(row1)

        self._row2 = QWidget()
        row2 = QHBoxLayout(self._row2)
        row2.setContentsMargins(0, 0, 0, 0)
        self._repl = QLineEdit()
        self._repl.setPlaceholderText("Replace with")
        rep_btn = QPushButton("Replace")
        all_btn = QPushButton("Replace all")
        row2.addWidget(self._repl, 1)
        row2.addWidget(rep_btn)
        row2.addWidget(all_btn)
        outer.addWidget(self._row2)

        self._find.textChanged.connect(lambda _t: self.find_text(incremental=True))
        self._find.returnPressed.connect(self.find_text)
        self._case.toggled.connect(lambda _c: self.find_text(incremental=True))
        self._regex.toggled.connect(lambda _c: self.find_text(incremental=True))
        next_btn.clicked.connect(lambda: self.find_text())
        prev_btn.clicked.connect(lambda: self.find_text(backward=True))
        close_btn.clicked.connect(self.close_bar)
        rep_btn.clicked.connect(self.replace_one)
        all_btn.clicked.connect(self.replace_all)
        self.hide()

    def show_find(self, replace: bool = False) -> None:
        ed = self._get_editor()
        self.show()
        self._row2.setVisible(replace)
        if ed is not None:
            sel = ed.textCursor().selectedText()
            if sel and "\u2029" not in sel:
                self._find.setText(sel)
        self._find.setFocus()
        self._find.selectAll()

    def close_bar(self) -> None:
        self.hide()
        ed = self._get_editor()
        if ed is not None:
            ed.setFocus()

    def keyPressEvent(self, event) -> None:               # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        super().keyPressEvent(event)

    def _flags(self, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        if self._case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        return flags

    def _pattern(self) -> str | QRegularExpression | None:
        text = self._find.text()
        if not self._regex.isChecked():
            return text
        opt = (QRegularExpression.PatternOption.NoPatternOption if self._case.isChecked()
               else QRegularExpression.PatternOption.CaseInsensitiveOption)
        rx = QRegularExpression(text, opt)
        return rx if rx.isValid() else None

    def find_text(self, backward: bool = False, incremental: bool = False) -> bool:
        ed = self._get_editor()
        pat = self._pattern()
        if ed is None or not self._find.text():
            self._status.setText("")
            return False
        if pat is None:
            self._status.setText("Bad regex")
            return False
        if incremental:
            cur = ed.textCursor()
            cur.setPosition(cur.selectionStart())
            ed.setTextCursor(cur)
        found = ed.find(pat, self._flags(backward))
        if not found:                                       # wrap around
            cur = ed.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End if backward
                             else QTextCursor.MoveOperation.Start)
            ed.setTextCursor(cur)
            found = ed.find(pat, self._flags(backward))
        self._status.setText("" if found else "No matches")
        return found

    def _re_flags(self) -> int:
        return 0 if self._case.isChecked() else re.IGNORECASE

    def replace_one(self) -> None:
        ed = self._get_editor()
        if ed is None:
            return
        cur = ed.textCursor()
        if cur.hasSelection():
            sel = cur.selectedText()
            try:
                pat = self._find.text() if self._regex.isChecked() else re.escape(self._find.text())
                if re.fullmatch(pat, sel, self._re_flags()):
                    repl = self._repl.text()
                    if self._regex.isChecked():
                        repl = re.sub(pat, repl, sel, count=1, flags=self._re_flags())
                    cur.insertText(repl)
                    ed.setTextCursor(cur)
            except re.error:
                self._status.setText("Bad regex")
                return
        self.find_text()

    def replace_all(self) -> None:
        ed = self._get_editor()
        needle = self._find.text()
        if ed is None or not needle:
            return
        pat = needle if self._regex.isChecked() else re.escape(needle)
        repl = self._repl.text()
        try:
            if self._regex.isChecked():
                new, n = re.subn(pat, repl, ed.toPlainText(), flags=self._re_flags())
            else:
                new, n = re.subn(pat, lambda _m: repl, ed.toPlainText(), flags=self._re_flags())
        except re.error:
            self._status.setText("Bad regex")
            return
        if n:
            cur = ed.textCursor()
            cur.beginEditBlock()
            cur.select(QTextCursor.SelectionType.Document)
            cur.insertText(new)
            cur.endEditBlock()
        self._status.setText(f"{n} replaced")


class MermaidView(QWebEngineView):
    def __init__(self, profile: QWebEngineProfile, bridge: Bridge, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._page = QWebEnginePage(profile, self)
        self.setPage(self._page)
        self._channel = QWebChannel(self._page)
        self._channel.registerObject("bridge", bridge)
        self._page.setWebChannel(self._channel)
        s = self._page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

    def run_js(self, code: str, callback: Callable | None = None) -> None:
        if callback is None:
            self._page.runJavaScript(code)
        else:
            self._page.runJavaScript(code, callback)

    def svg_to_image(self, svg: str, scale: int = 2) -> QImage:
        """Fallback rasteriser: render an SVG string with QSvgRenderer."""
        if svg:
            try:
                renderer = QSvgRenderer(svg.encode("utf-8"))
                if renderer.isValid():
                    vb = renderer.viewBox()
                    w, h = int(vb.width()), int(vb.height())
                    if w < 10 or h < 10:
                        w, h = max(self.width(), 800), max(self.height(), 600)
                    img = QImage(w * scale, h * scale, QImage.Format.Format_ARGB32)
                    img.fill(Qt.GlobalColor.transparent)
                    p = QPainter(img)
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    renderer.render(p)
                    p.end()
                    if not img.isNull():
                        return img
            except Exception:
                pass
        pm = self.grab().toImage()
        return pm if pm else QImage()


# --------------------------------------------------------------------------
# Color chip bar
# --------------------------------------------------------------------------
class ColorChipBar(QWidget):
    def __init__(self, on_pick, on_reset, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        self._chips: dict[str, QLabel] = {}
        for key, label in COLOR_FIELDS:
            chip = QLabel()
            chip.setFixedSize(18, 18)
            chip.setToolTip(f"{label}\nclick to change")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.mousePressEvent = lambda _event, k=key: on_pick(k)
            self._chips[key] = chip
            lay.addWidget(chip)
        btn = QPushButton("Reset")
        btn.setFixedHeight(20)
        btn.setToolTip("Restore the Studio default colors")
        btn.clicked.connect(on_reset)
        lay.addWidget(btn)

    def set_colors(self, colors: dict):
        for key, chip in self._chips.items():
            c = colors.get(key, "#000000")
            chip.setStyleSheet(
                f"background:{c}; border:1px solid #555; border-radius:3px;")


# --------------------------------------------------------------------------
# Tab model
# --------------------------------------------------------------------------
@dataclass
class TabModel:
    editor: EditorEdit
    path: Path | None = None
    dirty: bool = False
    theme_name: str = "Studio (default)"     # "" means custom colors
    colors: dict = field(default_factory=lambda: dict(DEFAULT_COLORS))
    view: dict = field(default_factory=lambda: {"zoom": 1.0, "x": 0.0, "y": 0.0, "fit": True})
    highlighter: MermaidHighlighter | None = None
    recent_themes: list = field(default_factory=list)
    disk_mtime: int | None = None            # mtime_ns of our last read/write of `path`
    error: tuple[int, str] | None = None     # last Mermaid error (line, message)
    last_text: str = ""                      # text at the last real change (filters spurious signals)

    @property
    def display_name(self) -> str:
        return self.path.name if self.path else "untitled.mmd"


def make_app_icon() -> QIcon:
    """Draw the application icon (a tiny flowchart on a purple tile)."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#7c3aed"))
        r = size * 0.18
        p.drawRoundedRect(QRect(0, 0, size, size), r, r)
        pen = QPen(QColor("#ede9fe"))
        pen.setWidthF(max(1.0, size / 24))
        p.setPen(pen)
        p.setBrush(QColor("#ede9fe"))
        bw, bh = size * 0.34, size * 0.2
        top = QRect(int(size * 0.33), int(size * 0.12), int(bw), int(bh))
        left = QRect(int(size * 0.1), int(size * 0.66), int(bw), int(bh))
        right = QRect(int(size * 0.56), int(size * 0.66), int(bw), int(bh))
        p.drawLine(top.center().x(), top.bottom(), left.center().x(), left.top())
        p.drawLine(top.center().x(), top.bottom(), right.center().x(), right.top())
        for box in (top, left, right):
            p.drawRoundedRect(box, size * 0.04, size * 0.04)
        p.end()
        icon.addPixmap(pm)
    return icon


def read_text_file(path: Path) -> tuple[str, bool]:
    """Read a diagram file. Returns (text, lossy) where lossy means some bytes
    were not valid UTF-8 and were replaced."""
    size = path.stat().st_size
    if size > MAX_OPEN_BYTES:
        raise ValueError(f"File is too large to open ({size / 1_048_576:.1f} MB; "
                         f"limit {MAX_OPEN_BYTES // 1_048_576} MB).")
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True


def valid_colors(raw: object) -> dict:
    """Merge `raw` over the default colors, keeping only valid #rrggbb values."""
    out = dict(DEFAULT_COLORS)
    if isinstance(raw, dict):
        for key in DEFAULT_COLORS:
            v = raw.get(key)
            if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                out[key] = v
    return out


class ColorButton(QPushButton):
    """Button that shows and picks a #rrggbb color."""

    def __init__(self, color: str = "#ffffff", parent: QWidget | None = None):
        super().__init__(parent)
        self.value = color
        self.clicked.connect(self._pick)
        self.set_value(color)

    def set_value(self, color: str):
        if not QColor(color).isValid():
            color = "#ffffff"
        self.value = QColor(color).name()
        text_col = "#000" if QColor(self.value).lightness() > 128 else "#fff"
        self.setText(self.value)
        self.setStyleSheet(f"background:{self.value};color:{text_col};")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.value), self, "Pick color")
        if c.isValid():
            self.set_value(c.name())


class StyleDialog(QDialog):
    """Edit fonts, padding, margins, spacing, colors and borders for the whole
    chart (init directive) or one element (style / classDef)."""

    CHART = "Entire chart"
    NUMBERS = [
        ("padding", "Padding (px)", 0, 100),
        ("margin", "Margin / diagram padding (px)", 0, 200),
        ("node_spacing", "Node spacing (px)", 0, 300),
        ("rank_spacing", "Rank / message spacing (px)", 0, 300),
        ("font_size", "Font size (px)", 6, 96),
        ("stroke_width", "Stroke width (px)", 0, 20),
        ("radius", "Corner radius (px)", 0, 50),
    ]
    COLORS = [("fill", "Fill"), ("stroke", "Stroke / border"), ("color", "Text color")]
    WEIGHTS = ["normal", "bold", "300", "500", "600", "800"]
    CHART_ONLY = {"margin", "node_spacing", "rank_spacing"}

    def __init__(self, get_text: Callable[[], str], set_text: Callable[[str], None],
                 initial: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Style chart / element")
        self._get, self._set = get_text, set_text
        self._rows: dict[str, tuple[QCheckBox, QWidget]] = {}
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)

        self._scope = QComboBox()
        self._scope.setEditable(True)
        self._scope.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        form.addRow("Apply to", self._scope)
        self._hint = QLabel()
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        def add_row(key: str, label: str, widget: QWidget):
            chk = QCheckBox()
            widget.setEnabled(False)
            chk.toggled.connect(widget.setEnabled)
            box = QHBoxLayout()
            box.addWidget(chk)
            box.addWidget(widget, 1)
            form.addRow(label, box)
            self._rows[key] = (chk, widget)

        self._font = QFontComboBox()
        add_row("font_family", "Font family", self._font)
        self._weight = QComboBox()
        self._weight.addItems(self.WEIGHTS)
        add_row("font_weight", "Font weight", self._weight)
        for key, label, lo, hi in self.NUMBERS:
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(max(lo, min(hi, 8)))
            add_row(key, label, sp)
        for key, label in self.COLORS:
            add_row(key, label, ColorButton())

        self._status = QLabel()
        lay.addWidget(self._status)
        btns = QHBoxLayout()
        lay.addLayout(btns)
        apply_btn = QPushButton("Apply")
        apply_btn.setDefault(True)
        reset_btn = QPushButton("Remove styling")
        close_btn = QPushButton("Close")
        for b in (apply_btn, reset_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(close_btn)
        apply_btn.clicked.connect(self.apply)
        reset_btn.clicked.connect(lambda: self.apply(clear=True))
        close_btn.clicked.connect(self.accept)

        self._fill_scope(initial)
        self._scope.currentTextChanged.connect(self._load_scope)
        self._load_scope()

    # ---- helpers --------------------------------------------------------
    def _fill_scope(self, select: str = ""):
        text = self._get()
        self._scope.blockSignals(True)
        self._scope.clear()
        self._scope.addItem(self.CHART)
        self._scope.addItems(list_elements(text))
        i = self._scope.findText(select) if select else -1
        if select and i < 0 and select != self.CHART:
            self._scope.setCurrentIndex(-1)
            self._scope.setEditText(select)
        else:
            self._scope.setCurrentIndex(max(0, i))
        self._scope.blockSignals(False)

    def _is_chart(self) -> bool:
        return self._scope.currentText().strip() in ("", self.CHART)

    def _load_scope(self, *_):
        text = self._get()
        chart = self._is_chart()
        opts = chart_style_options(text) if chart else element_style_options(
            text, self._scope.currentText().strip())
        kind = diagram_kind(text)
        for key, (chk, widget) in self._rows.items():
            chk.setChecked(False)
            chk.setEnabled(chart or key not in self.CHART_ONLY)
            if key in opts:
                chk.setChecked(True)
                val = opts[key]
                if isinstance(widget, QSpinBox):
                    widget.setValue(int(val))
                elif isinstance(widget, ColorButton):
                    widget.set_value(str(val))
                elif isinstance(widget, QFontComboBox):
                    widget.setCurrentFont(QFont(str(val)))
                elif isinstance(widget, QComboBox):
                    widget.setCurrentText(str(val))
        if chart:
            known = kind in SECTION_KEYS
            self._hint.setText(
                f"Diagram: {kind or 'unknown'}. Chart settings are written to a "
                "%%{init}%% directive at the top of the text."
                + ("" if known else " Padding/margin/spacing have no effect for this diagram type."))
        else:
            self._hint.setText(
                "Element styling is written as a style / classDef line. Padding and "
                "corner radius only affect some shapes.")

    def _collect(self) -> dict:
        out: dict = {}
        for key, (chk, widget) in self._rows.items():
            if not chk.isChecked() or not chk.isEnabled():
                continue
            if isinstance(widget, QSpinBox):
                out[key] = widget.value()
            elif isinstance(widget, ColorButton):
                out[key] = widget.value
            elif isinstance(widget, QFontComboBox):
                out[key] = widget.currentFont().family()
            elif isinstance(widget, QComboBox):
                out[key] = widget.currentText()
        return out

    def apply(self, clear: bool | None = None):
        clear = bool(clear)
        text = self._get()
        opts = self._collect()
        if not clear and not opts:
            self._status.setText("Tick at least one attribute to apply.")
            return
        target = self._scope.currentText().strip()
        if self._is_chart():
            new = apply_chart_style(text, opts, clear)
        else:
            new = apply_element_style(text, target, opts, clear)
        if new != text:
            self._set(new)
        self._status.setText("Removed styling." if clear else "Applied.")
        self._fill_scope(target)
        self._load_scope()


# --------------------------------------------------------------------------
# Main window (single owner of all tab state)
# --------------------------------------------------------------------------
class MermaidStudio(QMainWindow):
    _pptx_copied = Signal(bool, str)
    def __init__(self, settings: QSettings | None = None, restore: bool = True):
        super().__init__()
        self._settings = settings or QSettings("MermaidStudio", "MermaidStudio")
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(make_app_icon())
        self.resize(1360, 860)

        self._dark = bool(self._settings.value("ui_dark", False, type=bool))
        recent = self._settings.value("recent", [], type=list)
        self._recent = [path for path in recent if isinstance(path, str)] \
            if isinstance(recent, list) else []
        export_dir = self._settings.value("export_dir", "", type=str)
        self._export_dir = export_dir if isinstance(export_dir, str) else ""
        version = str(self._settings.value("mermaid_version", DEFAULT_MERMAID_VERSION, type=str))
        self._mermaid_version = version if version in MERMAID_VERSIONS else DEFAULT_MERMAID_VERSION
        scale = self._settings.value("png_scale", 2, type=int)
        self._png_scale = scale if scale in (1, 2, 4) else 2
        self._png_transparent = bool(self._settings.value("png_transparent", False, type=bool))
        self._loaded = False
        self._load_gen = 0
        self._models: list[TabModel] = []
        self._render_waiters: list[tuple[int, Callable[[bool, str, int], None]]] = []
        self._waiter_id = 0
        self._png_pending: dict[int, tuple[int, bool, Callable[[QImage | None, str], None]]] = {}
        self._png_token = 0
        self._batch: dict | None = None
        self._session_dirty = False
        self._render_tab: TabModel | None = None            # window-level drag & drop

        # --- page <-> python bridge, web view, bundle downloader ---
        self._bridge = Bridge(self)
        self._bridge.pageReady.connect(self._on_page_ready)
        self._bridge.renderFinished.connect(self._on_rendered)
        self._bridge.viewChanged.connect(self._on_view_changed)
        self._bridge.pngReady.connect(self._on_png_ready)
        self._bridge.styleChanged.connect(self._on_preview_style_changed)
        self._pptx_copied.connect(self._on_pptx_copied)
        self._downloader = BundleDownloader(self)
        self._downloader.finished.connect(self._on_bundle_downloaded)
        self._view = MermaidView(QWebEngineProfile.defaultProfile(), self._bridge, self)
        self._view.setMinimumSize(320, 240)
        self._view.loadFinished.connect(self._on_page_loaded)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)

        # --- tabs + find bar ---
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self._tabs.tabCloseRequested.connect(self.close_tab)
        self._tabs.currentChanged.connect(self._on_tab_switch)
        self._tabs.tabBar().tabMoved.connect(self._on_tab_moved)
        self._find_bar = FindBar(lambda: self._cur.editor if self._cur else None)
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)
        left_lay.addWidget(self._tabs, 1)
        left_lay.addWidget(self._find_bar)

        # --- splitter ---
        sp = QSplitter(Qt.Orientation.Horizontal)
        sp.addWidget(left)
        sp.addWidget(self._view)
        sp.setSizes([560, 800])
        sp.setStretchFactor(0, 0)
        sp.setStretchFactor(1, 1)
        self.setCentralWidget(sp)
        self._splitter = sp

        self.setStatusBar(QStatusBar())
        self._err_label = QLabel()
        self._err_label.setStyleSheet("color:#dc2626;")
        self._err_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._err_label.hide()
        self._zoom_label = QLabel("100%")
        self.statusBar().addPermanentWidget(self._err_label, 1)
        self.statusBar().addPermanentWidget(self._zoom_label)

        # --- outline panel ---
        self._outline = QListWidget()
        self._outline.itemClicked.connect(self._on_outline_clicked)
        self._outline_dock = QDockWidget("Outline", self)
        self._outline_dock.setObjectName("outline")
        self._outline_dock.setWidget(self._outline)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._outline_dock)
        self._outline_dock.setVisible(
            bool(self._settings.value("outline_visible", False, type=bool)))
        self._outline_dock.visibilityChanged.connect(lambda _v: self._update_outline())

        self._diagnostics = QListWidget()
        self._diagnostics.itemClicked.connect(self._on_diagnostic_clicked)
        self._diagnostics_dock = QDockWidget("Diagnostics", self)
        self._diagnostics_dock.setObjectName("diagnostics")
        self._diagnostics_dock.setWidget(self._diagnostics)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._diagnostics_dock)
        self._diagnostics_dock.hide()

        self._workspace_model = QFileSystemModel(self)
        self._workspace_model.setNameFilters(
            [f"*{ext}" for ext in sorted(DIAGRAM_EXTS)]
            + [f"*{ext.upper()}" for ext in sorted(DIAGRAM_EXTS)])
        self._workspace_model.setNameFilterDisables(False)
        self._workspace_proxy = QSortFilterProxyModel(self)
        self._workspace_proxy.setSourceModel(self._workspace_model)
        self._workspace_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._workspace_proxy.setFilterKeyColumn(0)
        self._workspace_proxy.setRecursiveFilteringEnabled(True)
        self._workspace_search = QLineEdit()
        self._workspace_search.setPlaceholderText("Filter diagrams by filename")
        self._workspace_search.textChanged.connect(self._workspace_proxy.setFilterFixedString)
        self._workspace_view = QTreeView()
        self._workspace_view.setModel(self._workspace_proxy)
        self._workspace_view.setHeaderHidden(True)
        for column in range(1, self._workspace_model.columnCount()):
            self._workspace_view.setColumnHidden(column, True)
        self._workspace_view.doubleClicked.connect(self._open_workspace_index)
        workspace_contents = QWidget()
        workspace_layout = QVBoxLayout(workspace_contents)
        workspace_layout.setContentsMargins(2, 2, 2, 2)
        workspace_layout.addWidget(self._workspace_search)
        workspace_layout.addWidget(self._workspace_view)
        self._workspace_dock = QDockWidget("Project", self)
        self._workspace_dock.setObjectName("project")
        self._workspace_dock.setWidget(workspace_contents)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._workspace_dock)
        self._workspace_dock.hide()
        configured_workspace = self._settings.value("workspace_root", "", type=str)
        self._workspace_root = configured_workspace if isinstance(configured_workspace, str) else ""

        self._build_toolbars()
        self._build_menus()

        # --- timers ---
        self._timer = QTimer(self)             # debounced render
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.render_now)
        self._outline_timer = QTimer(self)
        self._outline_timer.setSingleShot(True)
        self._outline_timer.setInterval(400)
        self._outline_timer.timeout.connect(self._update_outline)
        self._diagnostics_timer = QTimer(self)
        self._diagnostics_timer.setSingleShot(True)
        self._diagnostics_timer.setInterval(350)
        self._diagnostics_timer.timeout.connect(self._update_diagnostics)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(AUTOSAVE_MS)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start()

        # --- preview, then tabs (restored session or a fresh tab) ---
        self._load_preview()
        if not (restore and self._restore_session()):
            self._add_tab(DEFAULT_TEXT)

        # --- restore persisted UI state ---
        self._restore_state()
        if self._workspace_root and Path(self._workspace_root).is_dir():
            self._set_workspace_root(Path(self._workspace_root), announce=False)
        self._apply_dark(self._dark)
        self._update_title()
        self._update_outline()
        self._update_diagnostics()
        self._sync_watcher()

    # ------------------------------------------------------------------ UI
    def _build_toolbars(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(icon, tip, slot, shortcut=None):
            a = QAction(self.style().standardIcon(icon), tip, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        SP = QStyle.StandardPixmap
        act(SP.SP_FileIcon, "Open .mmd file (new tab)  [or drag & drop]",
            self.open_file, "Ctrl+O")
        act(SP.SP_DialogSaveButton, "Save file", self.save_file, "Ctrl+S")
        act(SP.SP_DialogSaveButton, "Save as…", self.save_as, "Ctrl+Shift+S")
        tb.addSeparator()

        self._recent_menu = QMenu("Recent files", self)
        tb.addAction(self._recent_menu.menuAction())
        self._rebuild_recent_menu()

        tb.addSeparator()

        # per-tab theme preset + recent themes
        self._theme = QComboBox()
        self._theme.addItems(list(THEMES.keys()))
        self._theme.setToolTip("Theme preset for the current tab")
        self._theme.currentTextChanged.connect(self._on_theme)
        tb.addWidget(self._theme)
        self._recent_theme_menu = QMenu("Recent themes", self)
        tb.addAction(self._recent_theme_menu.menuAction())
        self._rebuild_recent_theme_menu()

        # per-tab color chips
        chip_tb = QToolBar("Colors")
        chip_tb.setMovable(False)
        self.addToolBar(chip_tb)
        self._chips = ColorChipBar(self._pick_color, self._reset_colors, self)
        chip_tb.addWidget(self._chips)

        tb.addSeparator()

        # presets
        pm = QMenu("Samples", self)
        for name in PRESETS:
            a = pm.addAction(name)
            a.triggered.connect(lambda _, n=name: self._apply_preset(n))
        pm11 = pm.addMenu("Needs Mermaid 11")
        for name in PRESETS_V11:
            a = pm11.addAction(name)
            a.triggered.connect(lambda _, n=name: self._apply_preset(n))
        tb.addAction(pm.menuAction())

        tb.addSeparator()

        # export (with remembered export folder and PNG options)
        im = QMenu("Export", self)
        im.addAction("PNG…", lambda: self.save_image("png"))
        im.addAction("SVG…", lambda: self.save_image("svg"))
        im.addAction("PDF…", lambda: self.save_image("pdf"))
        im.addAction("Standalone HTML…", self.export_standalone_html)
        im.addAction("PowerPoint shapes…", self.export_pptx)
        im.addAction("Markdown…", self.export_markdown)
        im.addAction("Export all tabs…", self.export_all_tabs)
        im.addSeparator()
        scale_menu = im.addMenu("PNG scale")
        group = QActionGroup(self)
        group.setExclusive(True)
        for s in (1, 2, 4):
            a = scale_menu.addAction(f"{s}x")
            a.setCheckable(True)
            a.setChecked(s == self._png_scale)
            group.addAction(a)
            a.triggered.connect(lambda _=False, v=s: self._set_png_scale(v))
        self._transparent_act = im.addAction("Transparent PNG background")
        self._transparent_act.setCheckable(True)
        self._transparent_act.setChecked(self._png_transparent)
        self._transparent_act.toggled.connect(self._set_png_transparent)
        im.addSeparator()
        self._export_folder_info = im.addAction("Export folder: (default)")
        self._export_folder_info.setEnabled(False)
        im.addAction("Choose export folder…", self.choose_export_folder)
        tb.addAction(im.menuAction())
        self._export_menu = im
        self._update_export_folder_info()

        # copy (incl. combined)
        cm = QMenu("Copy", self)
        cm.addAction("PNG image", self.copy_image)
        cm.addAction("SVG markup", self.copy_svg)
        cm.addAction("Image + SVG", self.copy_combined)
        cm.addAction("PowerPoint shapes", self.copy_pptx)
        cm.addAction("Markdown code block", self.copy_markdown)
        tb.addAction(cm.menuAction())
        act(SP.SP_DialogApplyButton,
            "Copy image + SVG to clipboard", self.copy_combined, "Ctrl+Shift+C")
        tb.addSeparator()

        # render + zoom
        act(SP.SP_ArrowForward, "Render", self.render_now, "Ctrl+Return")
        act(SP.SP_FileDialogDetailedView, "Style chart / element (Ctrl+Shift+Y)",
            self.open_style_dialog)
        act(SP.SP_TitleBarShadeButton, "Zoom in", lambda: self._zoom_by(1.25), "Ctrl++")
        act(SP.SP_TitleBarUnshadeButton, "Zoom out", lambda: self._zoom_by(1 / 1.25), "Ctrl+-")
        act(SP.SP_TitleBarMaxButton, "Fit to window", self._zoom_fit, "Ctrl+0")
        act(SP.SP_DesktopIcon, "Actual size (100%)", self._zoom_actual, "Ctrl+1")
        tb.addSeparator()

        # tabs + help + dark mode
        act(SP.SP_FileDialogNewFolder, "New tab", self.new_tab, "Ctrl+N")
        act(SP.SP_DialogCloseButton, "Close tab",
            lambda: self.close_tab(self._tabs.currentIndex()), "Ctrl+W")
        act(SP.SP_MessageBoxInformation, "Shortcuts (F1)", self.show_shortcuts, "F1")
        tb.addSeparator()
        act(SP.SP_BrowserReload, "Toggle dark / light UI", self.toggle_dark, "Ctrl+Shift+L")

    def _build_menus(self):
        mb = self.menuBar()

        fm = mb.addMenu("&File")
        fm.addAction("Open diagram…", self.open_file, QKeySequence("Ctrl+O"))
        fm.addAction("Open folder…", self.open_workspace, QKeySequence("Ctrl+Shift+O"))
        fm.addAction("Close folder", self.close_workspace)
        fm.addSeparator()
        fm.addAction("Save", self.save_file, QKeySequence("Ctrl+S"))
        fm.addAction("Save as…", self.save_as, QKeySequence("Ctrl+Shift+S"))

        em = mb.addMenu("&Edit")
        em.addAction("Find…", lambda: self._find_bar.show_find(False), QKeySequence("Ctrl+F"))
        em.addAction("Replace…", lambda: self._find_bar.show_find(True), QKeySequence("Ctrl+H"))
        em.addAction("Find next", lambda: self._find_bar.find_text(False), QKeySequence("F3"))
        em.addAction("Find previous", lambda: self._find_bar.find_text(True), QKeySequence("Shift+F3"))
        em.addSeparator()
        em.addAction("Toggle comment", self._toggle_comment, QKeySequence("Ctrl+/"))
        em.addAction("Go to error line", self._goto_error, QKeySequence("F8"))
        sm = em.addMenu("Insert snippet")
        for name, body in SNIPPETS.items():
            sm.addAction(name, lambda b=body: self._insert_snippet(b))

        vm = mb.addMenu("&View")
        self._outline_act = self._outline_dock.toggleViewAction()
        self._outline_act.setText("Outline panel")
        vm.addAction(self._outline_act)
        self._workspace_act = self._workspace_dock.toggleViewAction()
        self._workspace_act.setText("Project panel")
        vm.addAction(self._workspace_act)
        self._diagnostics_act = self._diagnostics_dock.toggleViewAction()
        self._diagnostics_act.setText("Diagnostics panel")
        vm.addAction(self._diagnostics_act)
        vm.addAction("Fit to window", self._zoom_fit)
        vm.addAction("Actual size", self._zoom_actual)

        mm = mb.addMenu("&Mermaid")
        vgroup = QActionGroup(self)
        vgroup.setExclusive(True)
        self._version_actions: dict[str, QAction] = {}
        for ver, label in MERMAID_VERSIONS.items():
            a = mm.addAction(label)
            a.setCheckable(True)
            a.setChecked(ver == self._mermaid_version)
            vgroup.addAction(a)
            self._version_actions[ver] = a
            a.triggered.connect(lambda _=False, v=ver: self._set_mermaid_version(v))
        mm.addSeparator()
        mm.addAction("Download selected version for offline use",
                     lambda: self._download_bundle(self._mermaid_version, True))

        tm = mb.addMenu("&Tools")
        tm.addAction("Style chart / element…", self.open_style_dialog, QKeySequence("Ctrl+Shift+Y"))
        tm.addSeparator()
        tm.addAction("Generate diagram from text…", self.generate_from_text)
        tm.addAction("Diff against saved file…", self.diff_against_saved)
        tm.addSeparator()
        tm.addAction("Export theme as JSON…", self.export_theme)
        tm.addAction("Import theme from JSON…", self.import_theme)
        tm.addAction("Export standalone HTML…", self.export_standalone_html)

    def _rebuild_recent_menu(self):
        self._recent_menu.clear()
        if not self._recent:
            a = self._recent_menu.addAction("(no recent files)")
            a.setEnabled(False)
            return
        for p in self._recent:
            a = self._recent_menu.addAction(Path(p).name)
            a.setToolTip(p)
            a.triggered.connect(lambda _, pp=p: self._open_recent(pp))
        self._recent_menu.addSeparator()
        self._recent_menu.addAction("Clear recent files", self._clear_recent)

    def _rebuild_recent_theme_menu(self):
        self._recent_theme_menu.clear()
        tab = self._cur
        names = tab.recent_themes if tab else []
        if not names:
            a = self._recent_theme_menu.addAction("(no recent themes for this tab)")
            a.setEnabled(False)
            return
        for name in names:
            a = self._recent_theme_menu.addAction(name)
            a.triggered.connect(lambda _, n=name: self._select_theme(n))
        self._recent_theme_menu.addSeparator()
        self._recent_theme_menu.addAction(
            "Clear this tab's recent themes", self._clear_recent_themes)

    def _update_export_folder_info(self):
        d = self._export_dir if self._export_dir and Path(self._export_dir).is_dir() else ""
        self._export_folder_info.setText(
            f"Export folder: {d}" if d else "Export folder: (default)")

    # ------------------------------------------------------------------ project workspace
    def open_workspace(self):
        start = self._workspace_root or str(self._default_export_dir())
        root = QFileDialog.getExistingDirectory(self, "Open project folder", start)
        if root:
            self._set_workspace_root(Path(root))

    def _set_workspace_root(self, root: Path, announce: bool = True):
        try:
            root = root.resolve(strict=True)
        except OSError as e:
            QMessageBox.critical(self, "Open folder failed", str(e))
            return
        if not root.is_dir():
            QMessageBox.critical(self, "Open folder failed", f"Not a folder:\n{root}")
            return
        self._workspace_root = str(root)
        self._settings.setValue("workspace_root", self._workspace_root)
        root_index = self._workspace_model.setRootPath(self._workspace_root)
        self._workspace_view.setRootIndex(self._workspace_proxy.mapFromSource(root_index))
        self._workspace_dock.setWindowTitle(f"Project — {root.name or root}")
        self._workspace_dock.show()
        if announce:
            self.statusBar().showMessage(f"Project folder: {root}", 4000)

    def close_workspace(self):
        self._workspace_root = ""
        self._settings.setValue("workspace_root", "")
        root_index = self._workspace_model.setRootPath("")
        self._workspace_view.setRootIndex(self._workspace_proxy.mapFromSource(root_index))
        self._workspace_dock.setWindowTitle("Project")
        self.statusBar().showMessage("Project folder closed", 2500)

    def _open_workspace_index(self, index):
        source_index = self._workspace_proxy.mapToSource(index)
        if not source_index.isValid() or self._workspace_model.isDir(source_index):
            return
        path = Path(self._workspace_model.filePath(source_index))
        if path.suffix.lower() in DIAGRAM_EXTS:
            self._open_path(path)

    # ------------------------------------------------------------------ tabs
    @property
    def _cur(self) -> TabModel | None:
        idx = self._tabs.currentIndex()
        if 0 <= idx < len(self._models):
            return self._models[idx]
        return None

    def _add_tab(self, text: str, path: Path | None = None) -> int:
        ed = EditorEdit()
        ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        ed.setFont(QFont("Consolas", 11))
        ed.blockSignals(True)
        ed.setPlainText(text)
        ed.blockSignals(False)
        ed.textChanged.connect(lambda: self._on_editor_text(ed))
        ed.style_requested.connect(self.open_style_dialog)

        idx = self._tabs.addTab(ed, path.name if path else "untitled.mmd")
        tab = TabModel(editor=ed, path=path, last_text=ed.toPlainText())
        tab.highlighter = MermaidHighlighter(ed.document())
        if path:
            tab.disk_mtime = self._mtime(path)
            self._tabs.setTabToolTip(idx, str(path))
        self._models.insert(idx, tab)
        self._sync_editor_style(ed)
        self._tabs.setCurrentIndex(idx)
        if self._tabs.currentIndex() == idx and self._tabs.count() == 1:
            self._on_tab_switch(idx)     # first tab: no currentChanged fired for it
        self._sync_watcher()
        self._session_dirty = True
        return idx

    @staticmethod
    def _mtime(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def new_tab(self):
        self._add_tab(DEFAULT_TEXT)
        self.statusBar().showMessage("New tab", 2000)

    def close_tab(self, idx: int):
        if idx < 0 or idx >= len(self._models):
            return
        tab = self._models[idx]
        if tab.dirty and not self._confirm_close_tab(tab):
            return
        if self._tabs.count() <= 1:
            self._reset_tab(idx)
            return
        self._remove_tab(idx)

    def _confirm_close_tab(self, tab: TabModel) -> bool:
        r = QMessageBox.question(
            self, "Unsaved changes", f"Close '{tab.display_name}' and discard its changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return r == QMessageBox.StandardButton.Yes

    def _remove_tab(self, idx: int):
        tab = self._models.pop(idx)          # keep list in sync before signal
        w = tab.editor
        tab.highlighter = None
        self._tabs.removeTab(idx)            # fires currentChanged
        w.deleteLater()
        self._sync_watcher()
        self._session_dirty = True

    def _reset_tab(self, idx: int):
        tab = self._models[idx]
        self._set_text(tab, DEFAULT_TEXT)
        tab.path = None
        tab.dirty = False
        tab.disk_mtime = None
        tab.theme_name = "Studio (default)"
        tab.colors = dict(DEFAULT_COLORS)
        tab.view = {"zoom": 1.0, "x": 0.0, "y": 0.0, "fit": True}
        self._tabs.setTabText(idx, tab.display_name)
        self._tabs.setTabToolTip(idx, "")
        self._on_tab_switch(idx)
        self._sync_watcher()
        self._session_dirty = True
        self.statusBar().showMessage("Tab reset", 2000)

    def _on_tab_moved(self, from_idx: int, to_idx: int):
        item = self._models.pop(from_idx)
        self._models.insert(to_idx, item)
        self._session_dirty = True

    def _on_tab_switch(self, idx: int):
        if idx < 0 or idx >= len(self._models):
            return
        tab = self._models[idx]
        # restore this tab's theme + colors + view
        self._theme.blockSignals(True)
        self._theme.setCurrentIndex(self._theme.findText(tab.theme_name)
                                    if tab.theme_name else -1)
        self._theme.blockSignals(False)
        self._chips.set_colors(tab.colors)
        self._rebuild_recent_theme_menu()
        self._sync_editor_style(tab.editor)
        self._show_error_state(tab)
        self._update_title()
        self._update_outline()
        self._update_diagnostics()
        self._session_dirty = True
        if self._loaded:
            self._push_view(tab)
            self.render_now()

    @staticmethod
    def _set_text(tab: TabModel, text: str):
        """Replace the editor text without emitting textChanged."""
        tab.editor.blockSignals(True)
        tab.editor.setPlainText(text)
        tab.editor.blockSignals(False)
        tab.last_text = tab.editor.toPlainText()

    def _on_editor_text(self, ed: EditorEdit):
        idx = self._tabs.indexOf(ed)
        if not 0 <= idx < len(self._models):
            return
        tab = self._models[idx]
        text = ed.toPlainText()
        if text == tab.last_text:
            return                           # formatting-only change, not an edit
        tab.last_text = text
        tab.dirty = True
        ed.set_error_line(0)
        self._update_title()
        if tab is self._cur:
            self._timer.setInterval(self._debounce_ms(ed))
            self._outline_timer.start()
            self._diagnostics_timer.start()
            self._timer.start()
        self._session_dirty = True

    @staticmethod
    def _debounce_ms(ed: QPlainTextEdit) -> int:
        """Short delay for small diagrams, longer for big ones."""
        return int(max(150, min(1200, 150 + ed.document().characterCount() / 20)))

    def _update_title(self):
        tab = self._cur
        if tab:
            name, mark = tab.display_name, (" *" if tab.dirty else "")
        else:
            name, mark = "untitled", ""
        self.setWindowTitle(f"{name}{mark} — {APP_TITLE}")

    # ------------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, e):              # noqa: N802
        if self._mime_has_diagram_files(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):               # noqa: N802
        if self._mime_has_diagram_files(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):                   # noqa: N802
        md = e.mimeData()
        if not self._mime_has_diagram_files(md):
            e.ignore()
            return
        opened = 0
        for u in md.urls():
            if not u.isLocalFile():
                continue
            p = Path(u.toLocalFile())
            if not p.is_file():
                continue
            self._open_path(p)
            opened += 1
        e.acceptProposedAction()
        if opened:
            self.statusBar().showMessage(
                f"Opened {opened} file{'s' if opened != 1 else ''}", 3000)

    @staticmethod
    def _mime_has_diagram_files(md: QMimeData) -> bool:
        if md is None or not md.hasUrls():
            return False
        for u in md.urls():
            if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in DIAGRAM_EXTS:
                return True
        return False

    # ------------------------------------------------------------------ files
    def open_file(self):
        start = self._export_dir or str(APP_DIR)
        path, _ = QFileDialog.getOpenFileName(
            self, "Open diagram", start,
            "Mermaid (*.mmd *.mm *.txt);;All files (*)")
        if path:
            self._open_path(Path(path))

    def _open_path(self, path: Path):
        for idx, tab in enumerate(self._models):
            if tab.path == path:
                self._tabs.setCurrentIndex(idx)
                return
        try:
            size = path.stat().st_size
            if size > WARN_OPEN_BYTES and size <= MAX_OPEN_BYTES:
                r = QMessageBox.question(
                    self, "Large file",
                    f"{path.name} is {size / 1_048_576:.1f} MB. Open it anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if r != QMessageBox.StandardButton.Yes:
                    return
            text, lossy = read_text_file(path)
        except Exception as e:
            QMessageBox.critical(self, "Open failed", str(e))
            return
        self._add_tab(text, path)
        self._add_recent(str(path))
        note = " (some bytes were not valid UTF-8 and were replaced)" if lossy else ""
        self.statusBar().showMessage(f"Opened {path}{note}", 6000 if lossy else 4000)

    def _open_recent(self, p: str):
        if Path(p).exists():
            self._open_path(Path(p))
        else:
            QMessageBox.warning(self, "Recent file", f"Not found:\n{p}")

    def _add_recent(self, p: str):
        self._recent = [x for x in self._recent if x != p]
        self._recent.insert(0, p)
        self._recent = self._recent[:10]
        self._settings.setValue("recent", self._recent)
        self._rebuild_recent_menu()

    def _clear_recent(self):
        self._recent = []
        self._settings.setValue("recent", self._recent)
        self._rebuild_recent_menu()

    def _confirm_discard(self) -> bool:
        tab = self._cur
        name = tab.display_name if tab else "this tab"
        r = QMessageBox.question(
            self, "Unsaved changes", f"'{name}' has unsaved changes. Discard them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return r == QMessageBox.StandardButton.Yes

    def save_file(self):
        tab = self._cur
        if tab is None:
            return
        if tab.path is None:
            self.save_as()
            return
        self._write_file(tab.path)

    def save_as(self):
        tab = self._cur
        if tab is None:
            return
        stem = tab.display_name
        if not stem.endswith((".mmd", ".mm", ".txt")):
            stem += ".mmd"
        start = tab.path.parent if tab.path else (
            Path(self._export_dir) if self._export_dir and Path(self._export_dir).is_dir()
            else APP_DIR)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagram", str(start / stem),
            "Mermaid (*.mmd);;All files (*)")
        if path:
            self._write_file(Path(path))

    def _write_file(self, path: Path):
        tab = self._cur
        if tab is None:
            return
        try:
            # Write to a temp file then replace, so a crash can't truncate the original.
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(tab.editor.toPlainText(), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        tab.path = path
        tab.dirty = False
        tab.disk_mtime = self._mtime(path)
        idx = self._tabs.indexOf(tab.editor)
        self._tabs.setTabText(idx, tab.display_name)
        self._tabs.setTabToolTip(idx, str(path))
        self._add_recent(str(path))
        self._update_title()
        self._sync_watcher()
        self._session_dirty = True
        self.statusBar().showMessage(f"Saved {path}", 4000)

    # ------------------------------------------------------------------ live reload
    def _sync_watcher(self):
        wanted = {str(t.path) for t in self._models if t.path}
        current = set(self._watcher.files())
        stale = list(current - wanted)
        if stale:
            self._watcher.removePaths(stale)
        new = [p for p in wanted - current if Path(p).exists()]
        if new:
            self._watcher.addPaths(new)

    def _on_file_changed(self, p: str):
        path = Path(p)
        tab = next((t for t in self._models if t.path == path), None)
        if tab is None:
            return
        # Editors often replace files atomically, which drops the watch.
        QTimer.singleShot(150, lambda: self._handle_external_change(tab, path))

    def _handle_external_change(self, tab: TabModel, path: Path):
        if tab not in self._models:
            return
        if str(path) not in self._watcher.files() and path.exists():
            self._watcher.addPath(str(path))
        mtime = self._mtime(path)
        if mtime is None or mtime == tab.disk_mtime:
            return                           # deleted, or our own save
        tab.disk_mtime = mtime
        if tab.dirty:
            r = QMessageBox.question(
                self, "File changed on disk",
                f"{path.name} changed on disk, but you have unsaved edits.\n"
                "Reload and discard your edits?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
        try:
            text, _lossy = read_text_file(path)
        except Exception as e:
            self.statusBar().showMessage(f"Reload failed: {e}", 5000)
            return
        self._set_text(tab, text)
        tab.dirty = False
        self._update_title()
        self._session_dirty = True
        if tab is self._cur:
            self.render_now()
            self._update_outline()
        self.statusBar().showMessage(f"Reloaded {path.name} from disk", 3000)

    def _apply_preset(self, name: str):
        tab = self._cur
        if tab is None:
            return
        needs11 = name in PRESETS_V11
        text = PRESETS_V11[name] if needs11 else PRESETS[name]
        if tab.dirty and not self._confirm_discard():
            return
        if needs11 and version_major(self._mermaid_version) < 11:
            r = QMessageBox.question(
                self, "Needs Mermaid 11",
                f"'{name}' requires Mermaid 11. Switch to Mermaid {list(MERMAID_VERSIONS)[-1]} now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r == QMessageBox.StandardButton.Yes:
                self._set_mermaid_version(list(MERMAID_VERSIONS)[-1])
        self._set_text(tab, text)
        tab.dirty = True
        tab.editor.set_error_line(0)
        self._update_title()
        self._update_outline()
        self.render_now()
        self._session_dirty = True
        self.statusBar().showMessage(f"Sample applied: {name}", 4000)

    # ------------------------------------------------------------------ colors (per tab)
    def _pick_color(self, key: str):
        tab = self._cur
        if tab is None:
            return
        cur = tab.colors.get(key, "#000000")
        c = QColorDialog.getColor(QColor(cur), self,
                                  f"Pick {COLOR_LABELS.get(key, key)}")
        if c.isValid():
            tab.colors[key] = c.name()
            tab.theme_name = ""              # now custom
            self._theme.blockSignals(True)
            self._theme.setCurrentIndex(-1)
            self._theme.blockSignals(False)
            self._chips.set_colors(tab.colors)
            self._timer.start()

    def _reset_colors(self):
        self._select_theme("Studio (default)")

    def _select_theme(self, name: str):
        tab = self._cur
        if tab is None or name not in THEMES:
            return
        tab.theme_name = name
        tab.colors = dict(THEMES[name])
        # per-tab recent-theme memory
        tab.recent_themes = [n for n in tab.recent_themes if n != name]
        tab.recent_themes.insert(0, name)
        tab.recent_themes = tab.recent_themes[:MAX_RECENT_THEMES]
        self._theme.blockSignals(True)
        self._theme.setCurrentIndex(self._theme.findText(name))
        self._theme.blockSignals(False)
        self._chips.set_colors(tab.colors)
        self._rebuild_recent_theme_menu()
        self._timer.start()

    def _on_theme(self, name: str):
        if name in THEMES:
            self._select_theme(name)

    def _clear_recent_themes(self):
        tab = self._cur
        if tab:
            tab.recent_themes = []
            self._rebuild_recent_theme_menu()

    # ------------------------------------------------------------------ preview page
    def _bundle_source(self) -> str:
        local = find_local_bundle(self._mermaid_version)
        return QUrl.fromLocalFile(str(local)).toString() if local \
            else mermaid_cdn_url(self._mermaid_version)

    def _load_preview(self):
        self._loaded = False
        self._load_gen += 1
        if find_local_bundle(self._mermaid_version) is None:
            self._download_bundle(self._mermaid_version, False)
        # A file (not setHtml) avoids setHtml's 2 MB limit and gives the page a real origin.
        page = cache_dir() / "preview.html"
        page.write_text(build_html(self._bundle_source()), encoding="utf-8")
        self._view.load(QUrl.fromLocalFile(str(page)))

    def _on_page_loaded(self, ok: bool):
        if not ok:
            return
        gen = self._load_gen
        # If the web channel never reports in, render anyway (without callbacks).
        QTimer.singleShot(3000, lambda: self._on_page_ready() if gen == self._load_gen else None)

    def _on_page_ready(self):
        if self._loaded:
            return
        self._loaded = True
        tab = self._cur
        if tab:
            self._push_view(tab)
        self.render_now()

    def _set_mermaid_version(self, version: str):
        if version not in MERMAID_VERSIONS:
            return
        for ver, act in self._version_actions.items():
            act.setChecked(ver == version)
        if version == self._mermaid_version and self._loaded:
            return
        self._mermaid_version = version
        self._settings.setValue("mermaid_version", version)
        self._load_preview()
        self.statusBar().showMessage(f"Using Mermaid {version}", 4000)

    def _download_bundle(self, version: str, announce: bool):
        if find_local_bundle(version):
            if announce:
                self.statusBar().showMessage(
                    f"Mermaid {version} is already available offline", 4000)
            return
        if self._downloader.busy(version):
            return
        self._downloader.start(version)
        self.statusBar().showMessage(f"Downloading Mermaid {version} for offline use…", 4000)

    def _on_bundle_downloaded(self, version: str, ok: bool, msg: str):
        if ok:
            self.statusBar().showMessage(f"Mermaid {version} saved for offline use", 5000)
        else:
            self.statusBar().showMessage(f"Could not download Mermaid {version}: {msg}", 8000)

    # ------------------------------------------------------------------ render
    def render_now(self):
        self._timer.stop()                   # absorb any pending debounced render
        self._diagnostics_timer.stop()
        self._update_diagnostics()
        tab = self._cur
        if tab is None or not self._loaded:
            return
        code = tab.editor.toPlainText()
        opts = {
            "theme": "base",
            "themeVariables": tab.colors,
            "flowchart": {"htmlLabels": True},
        }
        self._render_tab = tab
        js = f"mermaid_render({js_str(code)}, {json.dumps(opts)}); 'ok'"
        self._view.run_js(js, lambda _result: None)

    def _on_rendered(self, ok: bool, message: str, line: int):
        tab = getattr(self, "_render_tab", None) or self._cur
        if tab in self._models:
            tab.error = None if ok else (line, message)
            tab.editor.set_error_line(0 if ok else line)
            if tab is self._cur:
                self._show_error_state(tab)
        waiters, self._render_waiters = self._render_waiters, []
        for _wid, cb in waiters:
            cb(ok, message, line)

    def _on_preview_style_changed(self, source_id: str, prop: str, value: str):
        tab = self._cur
        if tab is None or source_id not in list_elements(tab.editor.toPlainText()):
            self.statusBar().showMessage(
                "This preview element has no matching editable Mermaid source ID.", 4000)
            return

        text = tab.editor.toPlainText()
        if prop == "reset":
            new_text = apply_element_style(text, source_id, {}, clear=True)
        else:
            option_by_property = {
                "fill": ("fill", "color"),
                "stroke": ("stroke", "color"),
                "strokeWidth": ("stroke_width", "number"),
                "textColor": ("color", "color"),
                "fontSize": ("font_size", "integer"),
                "opacity": ("opacity", "opacity"),
            }
            mapping = option_by_property.get(prop)
            if mapping is None:
                self.statusBar().showMessage(f"Unsupported preview style: {prop}", 4000)
                return
            option, value_kind = mapping
            if value_kind == "color":
                color = QColor(value)
                if not color.isValid():
                    self.statusBar().showMessage("Invalid preview color.", 4000)
                    return
                option_value = color.name()
            elif value_kind == "integer":
                try:
                    option_value = int(value)
                except ValueError:
                    self.statusBar().showMessage("Invalid preview font size.", 4000)
                    return
                if not 4 <= option_value <= 96:
                    self.statusBar().showMessage("Preview font size must be from 4 to 96.", 4000)
                    return
            else:
                try:
                    option_value = float(value)
                except ValueError:
                    self.statusBar().showMessage("Invalid preview style value.", 4000)
                    return
                if not 0 <= option_value <= (1 if value_kind == "opacity" else 20):
                    self.statusBar().showMessage("Preview style value is out of range.", 4000)
                    return
            new_text = apply_element_style(text, source_id, {option: option_value})

        if new_text == text:
            return
        cursor = tab.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(new_text)
        cursor.endEditBlock()
        self.statusBar().showMessage(f"Saved preview styling for {source_id} to Mermaid source.", 3000)

    def _show_error_state(self, tab: TabModel):
        tab.editor.set_error_line(tab.error[0] if tab.error else 0)
        if tab.error:
            first = tab.error[1].strip().splitlines()[0] if tab.error[1].strip() else "error"
            where = f"line {tab.error[0]}: " if tab.error[0] else ""
            self._err_label.setText(f"⚠ {where}{first}")
            self._err_label.setToolTip(tab.error[1])
            self._err_label.show()
        else:
            self._err_label.hide()

    def _goto_error(self):
        tab = self._cur
        if tab and tab.error and tab.error[0]:
            tab.editor.goto_line(tab.error[0])
            tab.editor.setFocus()
        else:
            self.statusBar().showMessage("No error with a line number", 2000)

    def _after_render(self, cb: Callable[[bool, str, int], None], timeout_ms: int = 15000):
        """Call `cb` once after the next render finishes (or with ok=False on timeout)."""
        self._waiter_id += 1
        wid = self._waiter_id
        self._render_waiters.append((wid, cb))

        def expire():
            for i, (w, f) in enumerate(self._render_waiters):
                if w == wid:
                    del self._render_waiters[i]
                    f(False, "Timed out waiting for the preview", 0)
                    return
        QTimer.singleShot(timeout_ms, expire)

    # ------------------------------------------------------------------ zoom / pan (per tab)
    def _push_view(self, tab: TabModel):
        v = tab.view
        self._view.run_js(
            f"set_view({float(v['zoom'])}, {float(v['x'])}, {float(v['y'])}, "
            f"{'true' if v['fit'] else 'false'});")

    def _on_view_changed(self, zoom: float, x: float, y: float, fit: bool, user: bool):
        self._zoom_label.setText(f"{zoom:.0%}" + (" (fit)" if fit else ""))
        tab = self._cur
        if tab and user:
            tab.view = {"zoom": zoom, "x": x, "y": y, "fit": fit}
            self._session_dirty = True

    def _zoom_by(self, factor: float):
        self._view.run_js(f"zoom_by({factor})")

    def _zoom_fit(self):
        self._view.run_js("fit_view(true)")

    def _zoom_actual(self):
        self._view.run_js("actual_size(true)")

    # ------------------------------------------------------------------ edit helpers
    def _toggle_comment(self):
        if self._cur:
            self._cur.editor.toggle_comment()

    def _insert_snippet(self, body: str):
        if self._cur:
            self._cur.editor.insert_snippet(body)
            self._cur.editor.setFocus()

    # ------------------------------------------------------------------ outline
    def _update_outline(self):
        if not self._outline_dock.isVisible():
            return
        tab = self._cur
        self._outline.clear()
        if tab is None:
            return
        for line, depth, kind, label in parse_outline(tab.editor.toPlainText()):
            item = QListWidgetItem(f"{'    ' * depth}{label}   [{kind}]")
            item.setData(Qt.ItemDataRole.UserRole, line)
            self._outline.addItem(item)

    def _on_outline_clicked(self, item: QListWidgetItem):
        tab = self._cur
        line = item.data(Qt.ItemDataRole.UserRole)
        if tab and isinstance(line, int):
            tab.editor.goto_line(line)
            tab.editor.setFocus()

    def _update_diagnostics(self):
        self._diagnostics.clear()
        tab = self._cur
        if tab is None:
            return
        warnings = lint_diagram(tab.editor.toPlainText())
        if not warnings:
            item = QListWidgetItem("No source-level style issues found.")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._diagnostics.addItem(item)
            return
        for line, message in warnings:
            item = QListWidgetItem(f"Warning · line {line}: {message}")
            item.setData(Qt.ItemDataRole.UserRole, line)
            self._diagnostics.addItem(item)

    def _on_diagnostic_clicked(self, item: QListWidgetItem):
        tab = self._cur
        line = item.data(Qt.ItemDataRole.UserRole)
        if tab and isinstance(line, int):
            tab.editor.goto_line(line)
            tab.editor.setFocus()

    # ------------------------------------------------------------------ tools: diff / generate / themes
    def diff_against_saved(self):
        tab = self._cur
        if tab is None:
            return
        if tab.path is None or not tab.path.exists():
            QMessageBox.information(self, "Diff", "This tab has no saved file to compare with.")
            return
        try:
            saved, _lossy = read_text_file(tab.path)
        except Exception as e:
            QMessageBox.critical(self, "Diff", str(e))
            return
        diff = list(difflib.unified_diff(
            saved.splitlines(), tab.editor.toPlainText().splitlines(),
            "saved", "current", lineterm=""))
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Changes in {tab.display_name}")
        dlg.resize(720, 480)
        lay = QVBoxLayout(dlg)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
        if not diff:
            view.setPlainText("No differences.")
        else:
            rows = []
            for ln in diff:
                color = ("#16a34a" if ln.startswith("+") and not ln.startswith("+++")
                         else "#dc2626" if ln.startswith("-") and not ln.startswith("---")
                         else "#7c3aed" if ln.startswith("@@") else "")
                style = f' style="color:{color}"' if color else ""
                rows.append(f"<div{style}>{html.escape(ln) or '&nbsp;'}</div>")
            view.setHtml("<pre style='margin:0'>" + "".join(rows) + "</pre>")
        lay.addWidget(view)
        dlg.exec()

    def open_style_dialog(self, word: str = ""):
        tab = self._cur
        if tab is None:
            return
        ed = tab.editor

        def set_text(new: str):
            cur = ed.textCursor()
            cur.beginEditBlock()               # one undo step
            cur.select(QTextCursor.SelectionType.Document)
            cur.insertText(new)
            cur.endEditBlock()
            self.render_now()

        word = word if isinstance(word, str) else ""
        StyleDialog(ed.toPlainText, set_text, word, self).exec()

    def generate_from_text(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Generate diagram from text")
        dlg.resize(720, 520)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "Rule-based converter (not AI). Flowchart: 'Start -> Check -> Done'. "
            "Sequence: 'User -> Server: login'. Mindmap: an indented outline."))
        kind = QComboBox()
        kind.addItems(["auto", "flowchart", "sequence", "mindmap"])
        lay.addWidget(kind)
        src = QPlainTextEdit()
        src.setPlaceholderText("Type or paste your text here…")
        out = QPlainTextEdit()
        out.setReadOnly(True)
        out.setFont(QFont("Consolas", 10))
        lay.addWidget(src, 1)
        lay.addWidget(out, 1)

        def refresh():
            out.setPlainText(outline_to_mermaid(src.toPlainText(), kind.currentText()))
        src.textChanged.connect(refresh)
        kind.currentTextChanged.connect(refresh)
        row = QHBoxLayout()
        row.addStretch(1)
        new_btn = QPushButton("Open in new tab")
        rep_btn = QPushButton("Replace current tab")
        cancel = QPushButton("Cancel")
        for b in (new_btn, rep_btn, cancel):
            row.addWidget(b)
        lay.addLayout(row)
        choice: list[str] = []
        new_btn.clicked.connect(lambda: (choice.append("new"), dlg.accept()))
        rep_btn.clicked.connect(lambda: (choice.append("replace"), dlg.accept()))
        cancel.clicked.connect(dlg.reject)
        if not dlg.exec() or not choice:
            return
        code = out.toPlainText()
        if not code.strip():
            return
        if choice[0] == "new" or self._cur is None:
            idx = self._add_tab(code)
            self._models[idx].dirty = True
        else:
            if self._cur.dirty and not self._confirm_discard():
                return
            self._cur.editor.setPlainText(code)
        self.render_now()

    def export_theme(self):
        tab = self._cur
        if tab is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export theme", str(self._default_export_dir() / "theme.json"),
            "Theme JSON (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(
                {"name": tab.theme_name or "custom", "colors": tab.colors}, indent=2),
                encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        self.statusBar().showMessage(f"Theme saved to {path}", 4000)

    def import_theme(self):
        tab = self._cur
        if tab is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import theme", str(self._default_export_dir()), "Theme JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            raw = data.get("colors", data) if isinstance(data, dict) else None
            if not isinstance(raw, dict) or not any(k in raw for k in DEFAULT_COLORS):
                raise ValueError("No recognised color keys in this file.")
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Import failed", str(e))
            return
        tab.colors = valid_colors(raw)
        tab.theme_name = ""
        self._theme.blockSignals(True)
        self._theme.setCurrentIndex(-1)
        self._theme.blockSignals(False)
        self._chips.set_colors(tab.colors)
        self._session_dirty = True
        self.render_now()
        self.statusBar().showMessage("Theme imported", 3000)

    # ------------------------------------------------------------------ export folder
    def choose_export_folder(self):
        start = (self._export_dir if self._export_dir and Path(self._export_dir).is_dir()
                 else str(APP_DIR))
        d = QFileDialog.getExistingDirectory(
            self, "Choose export folder", start)
        if not d:
            return
        self._export_dir = d
        self._settings.setValue("export_dir", d)
        self._update_export_folder_info()
        self.statusBar().showMessage(f"Export folder: {d}", 3000)

    def _default_export_dir(self) -> Path:
        if self._export_dir and Path(self._export_dir).is_dir():
            return Path(self._export_dir)
        tab = self._cur
        if tab and tab.path and tab.path.parent.is_dir():
            return tab.path.parent
        return APP_DIR

    # ------------------------------------------------------------------ export / copy
    def _set_png_scale(self, scale: int):
        self._png_scale = scale
        self._settings.setValue("png_scale", scale)

    def _set_png_transparent(self, on: bool):
        self._png_transparent = on
        self._settings.setValue("png_transparent", on)

    def _get_export_svg(self, callback):
        self._view.run_js("export_svg()", callback)

    def _request_png(self, scale: int, transparent: bool,
                     cb: Callable[[QImage | None, str], None]):
        """Ask the page for a browser-rendered PNG; fall back to QSvgRenderer."""
        self._png_token += 1
        token = self._png_token
        self._png_pending[token] = (scale, transparent, cb)
        self._view.run_js(f"export_png({token}, {scale}, "
                          f"{'true' if transparent else 'false'}, {MAX_PNG_PIXELS})")
        QTimer.singleShot(8000, lambda: self._png_failed(token, "timed out"))

    def _on_png_ready(self, token: int, data: str):
        if data.startswith("ERR:"):
            self._png_failed(token, data[4:])
            return
        entry = self._png_pending.pop(token, None)
        if entry is None:
            return
        raw = base64.b64decode(data.split(",", 1)[-1])
        img = QImage()
        img.loadFromData(raw, "PNG")
        entry[2](img if not img.isNull() else None, "" if not img.isNull() else "bad PNG data")

    def _png_failed(self, token: int, why: str):
        entry = self._png_pending.pop(token, None)
        if entry is None:
            return
        scale, _transparent, cb = entry

        def got(svg):
            if not svg:
                cb(None, why or "no diagram")
                return
            img = self._view.svg_to_image(svg, scale)
            cb(img if not img.isNull() else None, "" if not img.isNull() else why)
        self._view.run_js("export_svg()", got)

    def _save_raster(self, fmt: str, path: Path, img: QImage | None,
                     err: str) -> tuple[bool, str]:
        if img is None:
            return False, err or "No image to export yet."
        if fmt == "png":
            writer = QImageWriter(str(path), b"PNG")
            return (True, "") if writer.write(img) else (False, writer.errorString())
        return False, f"Unsupported raster format: {fmt}"

    @staticmethod
    def _save_vector_pdf(path: Path, svg: str) -> tuple[bool, str]:
        renderer = QSvgRenderer(svg.encode("utf-8"))
        if not renderer.isValid():
            return False, "The rendered SVG is invalid."
        bounds = renderer.viewBoxF()
        if bounds.width() <= 0 or bounds.height() <= 0:
            size = renderer.defaultSize()
            bounds = QRectF(0, 0, size.width(), size.height())
        if bounds.width() <= 0 or bounds.height() <= 0:
            return False, "The rendered diagram has no size."
        pdf = QPdfWriter(str(path))
        pdf.setPageSize(QPageSize(
            QSizeF(bounds.width() * 0.75, bounds.height() * 0.75), QPageSize.Unit.Point))
        pdf.setPageMargins(QMarginsF(0, 0, 0, 0))
        painter = QPainter(pdf)
        if not painter.isActive():
            return False, "Could not create the PDF file."
        renderer.render(painter, QRectF(painter.viewport()))
        painter.end()
        return True, ""

    def _write_export(self, fmt: str, path: Path, done: Callable[[bool, str], None]):
        if fmt in ("svg", "pdf"):
            def got(svg):
                if not svg:
                    done(False, "No rendered diagram to export yet.")
                    return
                if fmt == "pdf":
                    done(*self._save_vector_pdf(path, svg))
                    return
                try:
                    path.write_text(svg, encoding="utf-8")
                except OSError as e:
                    done(False, str(e))
                    return
                done(True, "")
            self._view.run_js("export_svg()", got)
        elif fmt == "html":
            local = find_local_bundle(self._mermaid_version)
            if local is None:
                done(False,
                     "The selected Mermaid bundle is not available locally. "
                     "Use Mermaid > Download selected version for offline use, then retry.")
                return
            tab = self._cur
            if tab is None:
                done(False, "No diagram is open.")
                return
            try:
                bundle = local.read_text(encoding="utf-8")
                document = build_standalone_html(
                    tab.editor.toPlainText(), bundle, tab.display_name, tab.colors)
                path.write_text(document, encoding="utf-8")
            except OSError as e:
                done(False, str(e))
                return
            done(True, "")
        else:
            scale = self._png_scale
            transparent = self._png_transparent
            self._request_png(
                scale, transparent,
                lambda img, err: done(*self._save_raster(fmt, path, img, err)))

    def export_standalone_html(self):
        tab = self._cur
        base = tab.path.stem if tab and tab.path else "diagram"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export standalone HTML",
            str(self._default_export_dir() / f"{base}.html"),
            "HTML document (*.html)")
        if not path:
            return

        def done(ok: bool, err: str):
            if ok:
                self.statusBar().showMessage(f"Saved standalone diagram to {path}", 4000)
            else:
                QMessageBox.critical(self, "HTML export failed", err)
        self._write_export("html", Path(path), done)

    def save_image(self, fmt: str = "png"):
        tab = self._cur
        base = tab.path.stem if tab and tab.path else "diagram"
        path, _ = QFileDialog.getSaveFileName(
            self, f"Save as {fmt.upper()}", str(self._default_export_dir() / f"{base}.{fmt}"),
            f"{fmt.upper()} (*.{fmt})")
        if not path:
            return

        def done(ok: bool, err: str):
            if ok:
                self.statusBar().showMessage(f"Saved {path}", 4000)
            else:
                QMessageBox.critical(self, "Export failed", err)
        self._write_export(fmt, Path(path), done)

    def export_pptx(self):
        tab = self._cur
        base = tab.path.stem if tab and tab.path else "diagram"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save as PowerPoint", str(self._default_export_dir() / f"{base}.pptx"),
            "PowerPoint (*.pptx)")
        if not path:
            return

        def got(raw):
            try:
                data = json.loads(raw) if raw else None
                if not data or not data.get("items"):
                    raise ValueError("No diagram shapes to export yet.")
                build_pptx(data, Path(path))
            except ImportError:
                QMessageBox.critical(self, "Export failed",
                                     "python-pptx is not installed.\n\npip install python-pptx")
                return
            except Exception as e:                      # noqa: BLE001
                QMessageBox.critical(self, "Export failed", str(e))
                return
            self.statusBar().showMessage(f"Saved {path}", 4000)
        self._view.run_js("export_shapes()", got)

    def _markdown_block(self) -> str:
        tab = self._cur
        code = tab.editor.toPlainText().rstrip("\n") if tab else ""
        return f"```mermaid\n{code}\n```\n"

    def export_markdown(self):
        tab = self._cur
        base = tab.path.stem if tab and tab.path else "diagram"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save as Markdown", str(self._default_export_dir() / f"{base}.md"),
            "Markdown (*.md)")
        if not path:
            return
        try:
            Path(path).write_text(self._markdown_block(), encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        self.statusBar().showMessage(f"Saved {path}", 4000)

    def copy_markdown(self):
        QApplication.clipboard().setText(self._markdown_block())
        self.statusBar().showMessage("Markdown block copied to clipboard", 3000)

    def export_all_tabs(self):
        if self._batch or not self._loaded or not self._models:
            return
        fmt, ok = QInputDialog.getItem(
            self, "Export all tabs", "Format:", ["png", "svg", "pdf", "md", "html"], 0, False)
        if not ok:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Export all tabs to…", str(self._default_export_dir()))
        if not folder:
            return
        self._batch = {"i": 0, "orig": self._tabs.currentIndex(), "folder": Path(folder),
                       "fmt": fmt, "ok": 0, "failed": [], "used": set()}
        self.setEnabled(False)
        self._batch_step()

    def _batch_step(self):
        b = self._batch
        if b is None:
            return
        if b["i"] >= len(self._models):
            self._batch_finish()
            return
        self._tabs.blockSignals(True)
        self._tabs.setCurrentIndex(b["i"])
        self._tabs.blockSignals(False)
        self._on_tab_switch(b["i"])           # also starts the render
        self._after_render(self._batch_rendered, 15000)

    def _batch_rendered(self, ok: bool, message: str, line: int):
        b = self._batch
        if b is None:
            return
        tab = self._models[b["i"]]
        stem = tab.path.stem if tab.path else f"diagram-{b['i'] + 1}"
        name, n = stem, 1
        while name in b["used"]:
            n += 1
            name = f"{stem}-{n}"
        b["used"].add(name)
        path = b["folder"] / f"{name}.{b['fmt']}"

        def done(success: bool, err: str):
            if success:
                b["ok"] += 1
            else:
                b["failed"].append(f"{tab.display_name}: {err}")
            b["i"] += 1
            self._batch_step()

        if not ok:
            done(False, message.strip().splitlines()[0] if message.strip() else "render failed")
        elif b["fmt"] == "md":
            try:
                path.write_text(f"```mermaid\n{tab.editor.toPlainText().rstrip(chr(10))}\n```\n",
                                encoding="utf-8")
                done(True, "")
            except OSError as e:
                done(False, str(e))
        else:
            self._write_export(b["fmt"], path, done)

    def _batch_finish(self):
        b = self._batch
        self._batch = None
        self.setEnabled(True)
        if b is None:
            return
        self._tabs.setCurrentIndex(b["orig"])
        self._on_tab_switch(b["orig"])
        msg = f"Exported {b['ok']} of {b['ok'] + len(b['failed'])} tabs to {b['folder']}"
        if b["failed"]:
            QMessageBox.warning(self, "Export all tabs", msg + "\n\n" + "\n".join(b["failed"]))
        else:
            self.statusBar().showMessage(msg, 6000)

    def copy_image(self):
        self._request_png(2, False, lambda img, err: self._copy_done("image", img, err))

    def copy_svg(self):
        self._get_export_svg(self._copy_svg_done)

    def copy_pptx(self):
        """Build the shapes in a temp .pptx, then have PowerPoint copy them natively."""
        if sys.platform != "win32":
            QMessageBox.information(self, "Copy", "Copying PowerPoint shapes needs Windows "
                                    "with Microsoft PowerPoint installed.")
            return

        def got(raw):
            try:
                data = json.loads(raw) if raw else None
                if not data or not data.get("items"):
                    raise ValueError("Nothing to copy yet.")
                tmp = Path(tempfile.mkdtemp(prefix="mermaid-pptx-")) / "diagram.pptx"
                build_pptx(data, tmp)
            except ImportError:
                QMessageBox.critical(self, "Copy failed",
                                     "python-pptx is not installed.\n\npip install python-pptx")
                return
            except Exception as e:                      # noqa: BLE001
                QMessageBox.critical(self, "Copy failed", str(e))
                return
            self.statusBar().showMessage("Asking PowerPoint to copy the shapes…")
            threading.Thread(target=self._pptx_copy_worker, args=(tmp,), daemon=True).start()
        self._view.run_js("export_shapes()", got)

    def _pptx_copy_worker(self, tmp: Path) -> None:
        try:
            import pythoncom
            import win32com.client as wc
        except ImportError:
            self._pptx_copied.emit(False, "pywin32 is not installed.\n\npip install pywin32")
            return
        pythoncom.CoInitialize()
        pres = None
        try:
            try:
                app = wc.GetActiveObject("PowerPoint.Application")
            except pythoncom.com_error:
                app = wc.Dispatch("PowerPoint.Application")
            # Needs a window: Copy fails on windowless presentations.
            pres = app.Presentations.Open(str(tmp), True, False, True)
            slide = pres.Slides(1)
            slide.Shapes.Range().Copy()
            self._pptx_copied.emit(True, "")
        except Exception as e:                          # noqa: BLE001
            self._pptx_copied.emit(False, f"PowerPoint could not copy the shapes:\n{e}")
        finally:
            try:
                if pres is not None:
                    pres.Saved = True
                    pres.Close()
            except Exception:                           # noqa: BLE001
                pass
            pythoncom.CoUninitialize()
            shutil.rmtree(tmp.parent, ignore_errors=True)

    def _on_pptx_copied(self, ok: bool, err: str):
        if ok:
            self.statusBar().showMessage(
                "Shapes copied. Paste into PowerPoint (Ctrl+V).", 5000)
        else:
            self.statusBar().clearMessage()
            QMessageBox.critical(self, "Copy failed", err)

    def copy_combined(self):
        self._request_png(2, False, lambda img, err: self._copy_done("combined", img, err))

    def _copy_svg_done(self, svg: str):
        if not svg:
            QMessageBox.warning(self, "Copy", "Nothing to copy yet.")
            return
        QApplication.clipboard().setText(svg)
        self.statusBar().showMessage("SVG copied to clipboard", 3000)

    def _copy_done(self, kind: str, img: QImage | None, err: str):
        if img is None:
            QMessageBox.warning(self, "Copy", err or "Nothing to copy yet.")
            return
        if kind == "image":
            QApplication.clipboard().setPixmap(QPixmap.fromImage(img))
            self.statusBar().showMessage("Image copied to clipboard", 3000)
            return

        def with_svg(svg: str):
            buffer = QBuffer()
            if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
                QMessageBox.critical(self, "Copy failed",
                                     "Could not prepare image data for the clipboard.")
                return
            writer = QImageWriter(buffer, b"PNG")
            if not writer.write(img):
                QMessageBox.critical(self, "Copy failed", writer.errorString())
                return
            mime = QMimeData()
            mime.setData("image/png", buffer.data().data())
            if svg:
                mime.setData("text/svg+xml", svg.encode("utf-8"))
                mime.setText(svg)
            QApplication.clipboard().setMimeData(mime)
            self.statusBar().showMessage("Image + SVG copied to clipboard", 3000)
        self._get_export_svg(with_svg)

    # ------------------------------------------------------------------ dark / light
    def toggle_dark(self):
        self._dark = not self._dark
        self._settings.setValue("ui_dark", self._dark)
        self._apply_dark(self._dark)
        self.statusBar().showMessage(
            "Dark mode" if self._dark else "Light mode", 2000)

    def _apply_dark(self, dark: bool):
        if dark:
            CR = QPalette.ColorRole
            pal = QPalette()
            pal.setColor(CR.Window, QColor("#1e1e2e"))
            pal.setColor(CR.WindowText, QColor("#cdd6f4"))
            pal.setColor(CR.Base, QColor("#181825"))
            pal.setColor(CR.AlternateBase, QColor("#1e1e2e"))
            pal.setColor(CR.Text, QColor("#cdd6f4"))
            pal.setColor(CR.Button, QColor("#313244"))
            pal.setColor(CR.ButtonText, QColor("#cdd6f4"))
            pal.setColor(CR.ToolTipBase, QColor("#181825"))
            pal.setColor(CR.ToolTipText, QColor("#cdd6f4"))
            pal.setColor(CR.Highlight, QColor("#7c3aed"))
            pal.setColor(CR.HighlightedText, QColor("#ffffff"))
            QApplication.setPalette(pal)
        else:
            QApplication.setPalette(QApplication.style().standardPalette())
        for tab in self._models:
            self._sync_editor_style(tab.editor)

    def _sync_editor_style(self, ed: QPlainTextEdit):
        CR = QPalette.ColorRole
        pal = ed.palette()
        if self._dark:
            pal.setColor(CR.Base, QColor("#181825"))
            pal.setColor(CR.Text, QColor("#cdd6f4"))
            pal.setColor(CR.PlaceholderText, QColor("#585b70"))
        else:
            pal.setColor(CR.Base, QColor("#ffffff"))
            pal.setColor(CR.Text, QColor("#1e1e2e"))
            pal.setColor(CR.PlaceholderText, QColor("#94a3b8"))
        ed.setPalette(pal)

    # ------------------------------------------------------------------ help
    def show_shortcuts(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard shortcuts")
        dlg.resize(520, 520)
        lay = QVBoxLayout(dlg)
        tbl = QTableWidget(len(SHORTCUTS), 2)
        tbl.setHorizontalHeaderLabels(["Shortcut", "Action"])
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.setColumnWidth(0, 200)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        for i, (k, v) in enumerate(SHORTCUTS):
            for col, txt in enumerate((k, v)):
                tbl.setItem(i, col, QTableWidgetItem(txt))
        lay.addWidget(tbl)
        dlg.exec()

    # ------------------------------------------------------------------ session
    def _session_path(self) -> Path:
        return cache_dir() / "session.json"

    def _autosave(self):
        if self._session_dirty and not self._batch:
            self._save_session()

    def _save_session(self):
        tabs = []
        for t in self._models:
            keep_text = t.dirty or t.path is None
            tabs.append({
                "path": str(t.path) if t.path else None,
                "dirty": t.dirty,
                "text": t.editor.toPlainText() if keep_text else None,
                "theme": t.theme_name,
                "colors": t.colors,
                "view": t.view,
                "recent_themes": t.recent_themes,
            })
        data = {"version": 1, "current": self._tabs.currentIndex(), "tabs": tabs}
        try:
            p = self._session_path()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, p)
            self._session_dirty = False
        except OSError:
            pass

    def _restore_session(self) -> bool:
        try:
            data = json.loads(self._session_path().read_text(encoding="utf-8"))
            entries = data["tabs"]
            if not isinstance(entries, list):
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        restored = 0
        for e in entries:
            if not isinstance(e, dict):
                continue
            path = Path(e["path"]) if isinstance(e.get("path"), str) else None
            text = e.get("text") if isinstance(e.get("text"), str) else None
            dirty = bool(e.get("dirty")) and text is not None
            if text is None or (path and not dirty):
                if not (path and path.is_file()):
                    continue
                try:
                    text, _lossy = read_text_file(path)
                except (OSError, ValueError):
                    continue
            if path and not path.is_file():
                path, dirty = None, True      # file vanished: keep the text as untitled
            self._add_tab(text, path)
            tab = self._models[-1]
            tab.dirty = dirty
            if isinstance(e.get("theme"), str) and (e["theme"] in THEMES or e["theme"] == ""):
                tab.theme_name = e["theme"]
            tab.colors = valid_colors(e.get("colors"))
            v = e.get("view")
            if isinstance(v, dict):
                try:
                    tab.view = {"zoom": float(v["zoom"]), "x": float(v["x"]),
                                "y": float(v["y"]), "fit": bool(v["fit"])}
                except (KeyError, TypeError, ValueError):
                    pass
            rt = e.get("recent_themes")
            if isinstance(rt, list):
                tab.recent_themes = [n for n in rt if n in THEMES][:MAX_RECENT_THEMES]
            restored += 1
        if not restored:
            return False
        cur = data.get("current")
        idx = cur if isinstance(cur, int) and 0 <= cur < len(self._models) else 0
        self._tabs.setCurrentIndex(idx)
        self._on_tab_switch(idx)
        return True

    # ------------------------------------------------------------------ persistence
    def _restore_state(self):
        geo = self._settings.value("geometry")
        if isinstance(geo, (bytes, bytearray)) and geo:
            try:
                self.restoreGeometry(bytes(geo))
            except Exception:
                pass
        st = self._settings.value("splitter")
        if isinstance(st, (bytes, bytearray)) and st:
            try:
                self._splitter.restoreState(bytes(st))
            except Exception:
                pass

    def closeEvent(self, event):              # noqa: N802
        if self._batch:
            event.ignore()
            return
        # Unsaved tabs are kept in the session file and come back next launch.
        self._save_session()
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("splitter", self._splitter.saveState())
        self._settings.setValue("ui_dark", self._dark)
        self._settings.setValue("export_dir", self._export_dir)
        self._settings.setValue("outline_visible", self._outline_dock.isVisible())
        event.accept()


# --------------------------------------------------------------------------
def main():
    if "--make-icon" in sys.argv:
        # Used by build_exe.ps1 to produce app.ico for PyInstaller.
        app = QApplication(sys.argv[:1])
        i = sys.argv.index("--make-icon")
        out = sys.argv[i + 1] if i + 1 < len(sys.argv) else "app.ico"
        ok = make_app_icon().pixmap(256, 256).save(out, "ICO")
        print(f"{'wrote' if ok else 'FAILED to write'} {out}")
        sys.exit(0 if ok else 1)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    w = MermaidStudio()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
