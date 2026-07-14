# radicon_dll.dll ビルド手順と動作確認

## 前提条件

| 項目 | 内容 |
|------|------|
| OS | Windows 10/11 (64-bit) |
| Sapera LT | 8.65 以上 (`C:\Program Files\Teledyne DALSA\Sapera`) |
| Visual Studio | 2019 Community (v142 ツールセット) |
| フレームグラバ | Teledyne DALSA Xtium-CL MX4 (ドライバ導入済み) |
| カメラ | Rad-icon 2022 (CameraLink ケーブルで接続済み) |

---

## 1. CCF ファイル

CCF ファイルはすでに作成済みで `config/` ディレクトリに格納されています。

| ファイル | 解像度 | 用途 |
|----------|--------|------|
| `T_Rad-icon_2022_Xtium_FullFOV_1x1_FreeRun.ccf` | **2080 × 2238 px** | アンビニング・高解像度測定 |
| `T_Rad-icon_2022_Xtium_FullFOV_2x2_FreeRun.ccf` | **1040 × 1118 px** | 2×2ビニング・高速/低ノイズ測定 |

両ファイルとも:
- ピクセル深度: **14-bit** (uint16 で転送)
- トリガー: **FreeRun** (カメラ自走、`Snap()` で次フレームを取得)
- サーバー名: **`Xtium-CL_MX4_1`**、デバイスインデックス: **`0`**

CCF を新たに作成・変更する必要が生じた場合:
`スタートメニュー → Teledyne DALSA → Sapera LT → CamExpert` を起動し、
`File → Save Configuration` で `config/` に上書き保存する。

---

## 2. DLL のビルド

### 方法 A — Visual Studio GUI

1. エクスプローラーで `RadiconDll_2019.vcxproj` をダブルクリックして VS2019 で開く
2. ツールバーのドロップダウンを **Release** / **x64** に設定する
3. `ビルド → ソリューションのビルド` (Ctrl+Shift+B)
4. エラーがなければ `dll\Release\radicon_dll.dll` が生成される

### 方法 B — コマンドライン (MSBuild)

PowerShell または cmd を開いて:

```bat
cd "d:\FPD-PC_User Data\Kagi\Hiroki_pyCodesLab\bl18c_controller\apps\Rad_icon_2022\dll"
.\build.bat
```

成功すると最終行に以下が出る:

```
Build succeeded.  Output: ...\dll\Release\radicon_dll.dll
```

### よくあるビルドエラー

| エラーメッセージ | 原因 | 対処 |
|------|------|------|
| `cannot open include file: 'SapClassBasic.h'` | インクルードパスが通っていない | vcxproj を開き直して Sapera のパスを確認する |
| `cannot open file 'SapClassBasic.lib'` | ライブラリパスが通っていない | `Lib\Win64` が正しいか確認する |
| `LNK2019: unresolved external symbol` | `.lib` とリンクできていない | 上記と同様 |
| `v142 toolset not found` | VS2019 の C++ ワークロードが未インストール | VS インストーラで「C++ によるデスクトップ開発」を追加する |

---

## 3. 動作確認

### 3-1. サーバー名の確認

CamExpert の左パネルに表示されたサーバー名 (例: `Xtium-CL_MX4_1`) を使う。
不明な場合は以下の Python スクリプトで列挙できる (DLL 不要):

```python
# find_servers.py — Sapera サーバー名を列挙するだけのスクリプト
import ctypes, os

# corapi.dll は System32 に入っているので直接ロードできる
corapi = ctypes.WinDLL("corapi.dll")
# SapManager を通じたサーバー列挙は C++ API のため、
# 実際には CamExpert か FindCamera.exe を使う方が確実
```

> **簡単な方法**: Sapera の付属ツール `FindCamera.exe` を使う。
> `C:\Program Files\Teledyne DALSA\Sapera\Examples\Classes\FindCamera\Vc\Release64\FindCamera.exe`
> をコマンドプロンプトで実行するとサーバー名とデバイスインデックスが表示される。

### 3-2. Python からの最小動作確認

```python
# test_snap.py
# このスクリプトを bl18c_controller/ ディレクトリから実行する:
#   python apps/Rad_icon_2022/test_snap.py

import sys
from pathlib import Path

# スタンドアロン実行用のパス設定
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from apps.Rad_icon_2022.radicon_backend import RadiconBackend, RadiconError

SERVER_NAME  = "Xtium-CL_MX4_1"
DEVICE_INDEX = 0
# 2x2ビニング版 (1040×1118 px)
CCF_PATH = r"d:\FPD-PC_User Data\Kagi\Hiroki_pyCodesLab\bl18c_controller\apps\Rad_icon_2022\config\T_Rad-icon_2022_Xtium_FullFOV_2x2_FreeRun.ccf"
# アンビニング版 (2080×2238 px) を使う場合はこちら:
# CCF_PATH = r"d:\FPD-PC_User Data\Kagi\Hiroki_pyCodesLab\bl18c_controller\apps\Rad_icon_2022\config\T_Rad-icon_2022_Xtium_FullFOV_1x1_FreeRun.ccf"

print("初期化中...")
try:
    with RadiconBackend(SERVER_NAME, DEVICE_INDEX, CCF_PATH) as det:
        print(f"センサーサイズ: {det.width} x {det.height} px")

        print("露光時間の取得...")
        exp = det.get_exposure_us()
        print(f"現在の露光時間: {exp} µs ({exp/1000:.1f} ms)")

        print("スナップ取得中 (10 秒タイムアウト)...")
        img = det.snap(timeout_ms=10_000)
        print(f"取得成功: shape={img.shape}, dtype={img.dtype}")
        print(f"  min={img.min()}, max={img.max()}, mean={img.mean():.1f}")

except FileNotFoundError as e:
    print(f"DLL が見つかりません: {e}")
    print("先に dll\\build.bat を実行してください。")
except RadiconError as e:
    print(f"検出器エラー: {e}")
```

### 3-3. 期待される出力 (正常時)

```
初期化中...
センサーサイズ: 2064 x 2236 px
露光時間の取得...
現在の露光時間: 100000 µs (100.0 ms)
スナップ取得中 (10 秒タイムアウト)...
取得成功: shape=(2236, 2064), dtype=uint16
  min=0, max=16383, mean=312.4
```

`max=16383` は 14bit センサーの飽和値 (2^14 - 1)。

### 3-4. よくあるランタイムエラー

| エラーメッセージ | 原因 | 対処 |
|------|------|------|
| `FileNotFoundError: radicon_dll.dll not found` | ビルド未実施 | Section 2 を実施する |
| `Sapera server not found: "Xtium-CL_MX4_1"` | サーバー名が違う | CamExpert か FindCamera.exe で正しい名前を確認する |
| `No CameraLink acquisition resources` | フレームグラバのドライバ未ロード | PC を再起動、またはデバイスマネージャーで確認する |
| `SapAcquisition::Create() failed` | CCF パスが間違い、またはカメラ未接続 | CCF ファイルのパスと CameraLink ケーブルを確認する |
| `Snap timeout after 10000 ms` | フレームが届かない | CCF のトリガー設定を Software Trigger に変更する |

---

## 4. ファイル構成

```
dll/
├── radicon_dll.h          公開 C API 定義 (Python ctypes で参照)
├── radicon_dll.cpp        Sapera C++ ラッパー実装
├── RadiconDll_2019.vcxproj VS2019 プロジェクトファイル
├── build.bat              コマンドラインビルドスクリプト
├── BUILDING.md            このファイル
└── Release/               (ビルド後に生成)
    └── radicon_dll.dll    Python が読み込む DLL
```

---

## 5. 露光時間の変更について

`rad_set_exposure_us()` が `-1` を返す場合、使用中の CCF がランタイムでの
露光時間変更をサポートしていない。その場合は CamExpert で値を変更して
CCF を保存し直すこと。

CCF を更新した後は `rad_shutdown()` → `rad_init()` で再初期化が必要。
