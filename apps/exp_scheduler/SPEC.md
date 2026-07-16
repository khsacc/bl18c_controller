# Experimental Scheduler  設計仕様

このドキュメントは `apps/exp_scheduler/` の実装仕様を定義する。
実装作業を開始する前に必ずこのファイル全体を読むこと。



---

## 概要

ステージ・PACE5000・LakeShore335・Rad-icon 2022 の操作を
タイムライン形式で登録・実行する実験シーケンスシステム。

ユーザーは以下2モードで入力できる：
- **Mode 1（UI）**：ダイアログでステップをひとつずつ追加
- **Mode 2（DSL）**：独自スクリプト言語でシーケンスを記述

将来的に Mode 2 のコードをローカル LLM に自然言語から生成させる機能を追加する（現時点では UI スタブのみ）。

---

## ディレクトリ構成

```
apps/exp_scheduler/
├── SPEC.md                  ← このファイル
├── __init__.py
├── device_context.py        # 全バックエンドをまとめる DeviceContext dataclass
├── actions.py               # Action サブクラス群（操作の型定義）
├── device_registry.py       # 操作名 → Action コンストラクタのマッピング
├── sequence.py              # Sequence（Action リスト）＋ JSON シリアライズ
├── runner.py                # QThread ベースの実行エンジン
├── dsl/
│   ├── __init__.py
│   ├── parser.py            # DSL テキスト → Sequence（Python ast 使用）
│   ├── validator.py         # AST ホワイトリスト検証
│   └── api.py               # DSL から呼べる実験操作関数群
└── ui/
    ├── __init__.py
    ├── scheduler_window.py  # メインウィンドウ（2 モード切替タブ）
    ├── timeline_widget.py   # ステップ一覧 UI（ドラッグ並び替え対応）
    ├── step_editor.py       # ステップ追加/編集ダイアログ
    ├── dsl_editor.py        # テキストエディタ＋バリデーションボタン
    └── llm_panel.py         # （将来）LLM プロンプトパネル（スタブ）
```

**既存ファイルへの変更（最小限）：**
- `main.py`：`open_exp_scheduler()` メソッドと「Experimental Scheduler」ボタンを追加するのみ

---

## 装置と操作の一覧

操作は **primitive（単一装置・単一動作）** と **compound（複数ステップを順序制御する高レベル操作）** に分かれる。

### Stage（PM16CController / PM16CControllerSim）— Primitive

`ch` は 1〜11 の整数（チャンネル割り当ては CLAUDE.md 参照）。

| 操作名 | DSL シグネチャ | 完了条件 |
|--------|--------------|----------|
| `move_absolute` | `move_absolute(ch=4, position=1000)` | `get_is_moving()` が False |
| `move_relative` | `move_relative(ch=4, delta=-500)` | 同上 |
| `set_speed` | `set_speed(ch=4, speed="M")` | 即時 |
| `normal_stop` | `normal_stop()` | 即時（減速停止・ASSTP） |
| `emergency_stop` | `emergency_stop()` | 即時（緊急停止・AESTP） |

`position` / `delta` の単位はパルス（物理単位への変換は CLAUDE.md の PULSE_SCALE を参照）。
`speed` は `"H"` / `"M"` / `"L"` のいずれか。

### Stage — Compound（ショートカット操作）

既存の `stage_controller.py` のショートカットに相当する複合操作。
**位置パラメータのデフォルト値は `apps/stage_fpd_scope/__localdata/stage_settings.json` から読み出す。**
独自プリセットファイルは作らない（ステージ UI と二重管理を避けるため）。

| 操作名 | DSL シグネチャ | 動作 |
|--------|--------------|------|
| `microscope_out_and_fpd_in` | 下記参照 | Ch8→OUT, 完了後 Ch9→IN（XRD 測定モード） |
| `fpd_out_and_microscope_in` | 下記参照 | Ch9→OUT, 完了後 Ch8→IN（顕微鏡観察モード） |

```python
# パラメータなし → stage_settings.json の値を使う
microscope_out_and_fpd_in()
fpd_out_and_microscope_in()

# 個別パラメータ上書き（指定しないものは stage_settings.json から）
microscope_out_and_fpd_in(speed="M")

# 全パラメータ明示
microscope_out_and_fpd_in(fpd_in_pos=1779, microscope_out_pos=0, speed="H")
fpd_out_and_microscope_in(fpd_out_pos=-40000, microscope_in_pos=281092, speed="H")
```

Runner は各 compound action を `to_steps(stage_settings)` で展開し、
primitive StageAction のリストとして逐次実行する。
Ch8/Ch9 の移動順序は制約を自然に満たす順（CLAUDE.md の MOVE_CONSTRAINTS 参照）。

### PACE5000（Pace5000Backend）

| 操作名 | DSL シグネチャ | 完了条件 |
|--------|--------------|----------|
| `set_pressure` | `set_pressure(pressure=2.0, unit="MPa", rate=0.2, rate_unit="MPa/min")` | `abs(get_pressure() - target) < tol` で監視 |
| `wait_pressure` | `wait_pressure(tol=0.01, unit="MPa")` | 同上（明示的な待機ステップ） |
| `set_control_mode` | `set_control_mode(enabled=True)` | 即時 |

`unit` は `"MPa"` / `"Bar"` のいずれか（デフォルト `"MPa"`）。
`rate_unit` は `"MPa/min"` / `"Bar/min"` のいずれか。
※ PACE5000バックエンドは GPa をサポートしない（`initialize_device()` が `:UNIT:PRES MPA` で初期化）。

### LakeShore 335（LakeShore335Backend）

| 操作名 | DSL シグネチャ | 完了条件 |
|--------|--------------|----------|
| `set_temperature` | `set_temperature(value=300, unit="K", ramp_rate=5.0)` | 即時発行（待機は別ステップ） |
| `wait_temperature` | `wait_temperature(tol=1.0, unit="K")` | `get_data()[-1].temp_a_k` が setpoint ± tol に入る |
| `set_heater` | `set_heater(range_index=2)` | 即時 |
| `all_heaters_off` | `all_heaters_off()` | 即時 |

`unit` は現在 `"K"` のみ。
`range_index`：0=Off / 1=Low / 2=Medium / 3=High。

### Rad-icon 2022（RadiconBackend）

| 操作名 | DSL シグネチャ | 完了条件 |
|--------|--------------|----------|
| `take_xrd` | `take_xrd(exposure_ms=1000, save=True, prefix="scan")` | `snap_triggered()` 返却後、oscillate=True の場合は Ch11 が θ=0° に戻り停止してから完了 |
| `take_dark` | `take_dark(exposure_ms=1000)` | `snap_triggered()` 返却（ブロッキング） |

#### `take_xrd` のオシレーションオプション

```python
take_xrd(
    exposure_ms=1000,
    save=True,
    prefix="scan",
    oscillate=False,          # True にすると Ch11 を往復させながら露光
    osc_pos_a_deg=-5.0,       # 往復の端点 A（度）
    osc_pos_b_deg=20.0,       # 往復の端点 B（度）
    osc_dwell_ms=0,           # 各端点での待機時間（ms、0=待機なし）
    osc_speed="M",            # "H" / "M" / "L"
)
```

`oscillate=True` のとき、`SequenceRunner` は以下の手順で `take_xrd` ステップを実行する：
1. Ch11 を A→B→A→… と往復させるバックグラウンドスレッドを起動
2. `snap_triggered()` でブロッキング露光
3. 露光完了後にスレッドへ停止シグナルを送り join
4. `normal_stop()`（ASSTP）で進行中の Ch11 移動を停止
5. Ch11 を θ=0° へ絶対値移動し、停止を待機（`_wait_stage_stop()`）
6. ステップ完了

パラメータは `GlobalXrdSettings`（XRD Settings パネル）でグローバルデフォルトを設定でき、  
`TakeXrdAction` の per-step フィールドで上書き可能（`None` = グローバル設定を継承）。

### カメラ — Compound

USB カメラ（OpenCV `VideoCapture`）を使うサンプル追従操作。
キャリブレーションデータは `apps/interactive_camera/calibration.json` から読む。
`interactive_camera.py` の `_follow_task` をほぼそのまま移植する。

**排他制約：** シーケンス実行中は interactive_camera ウィンドウは閉じられているため、
カメラデバイスの競合は起きない（後述の「シーケンス実行時のウィンドウ管理」を参照）。

#### リファレンス画像の設計

リファレンス画像は **ファイルパスで統一** して扱う。

```python
# ── シーケンス中にリファレンスを取得する場合 ──────────────────
save_reference_image()                      # → __localdata/reference_frame.png に保存
save_reference_image(path="C:/data/ref.png")  # → 指定パスに保存

follow_sample_position(duration=60, unit="min")
# reference_path 省略時は __localdata/reference_frame.png を読む

# ── 事前に撮っておいたリファレンスを使う場合 ──────────────────
follow_sample_position(
    duration=60, unit="min",
    reference_path="C:/data/ref_before_compression.png"
)
```

「事前取得」は スケジューラー UI の **Reference Image パネル** から行う（タイムラインとは独立した常設 UI）：
- **[Capture Now]**：
  - interactive_camera が開いている → `current_frame` を借用（カメラ競合なし）
  - 閉じている → 一時的に `VideoCapture(camera_index)` を開いて取得
- **[Load from file…]**：既存の画像ファイルを指定
- **Save to** フィールド：保存先パスを変更可能（デフォルト `__localdata/reference_frame.png`）

| 操作名 | DSL シグネチャ | 完了条件 |
|--------|--------------|----------|
| `save_reference_image` | `save_reference_image(path=None, camera_index=0)` | 即時（フレーム取得・保存） |
| `start_following` | 下記参照 | 即時（バックグラウンドで追従開始） |
| `stop_following` | `stop_following()` | 追従スレッド停止を待機して完了 |
| `follow_sample_position` | 下記参照 | `duration_s` 経過後に内部で追従停止（`start_following` + `wait` + `stop_following` の糖衣） |

```python
# ── バックグラウンド追従（イベント駆動型：開始・停止を明示）──────────
start_following(
    reference_path=None,             # 省略時 → __localdata/reference_frame.png
    interval=5,                      # 補正試行間隔
    interval_unit="min",             # "s" / "min"
    similarity_threshold=0.95,       # 省略時 → scheduler_presets.json
    max_correction_per_step_um=500,  # 1ステップあたりの補正上限（µm単位）。省略時 → scheduler_presets.json
    camera_index=0,
    autofocus_enabled=False,         # True にすると各補正サイクル後に Ch3 オートフォーカスを実行
    autofocus_range_um=20.0,         # ±n µm（片側の範囲）でスキャン
    autofocus_steps=10,              # スキャン点数（2 以上）
)
# ↑ 呼んだ直後に次のステップへ進む。追従はバックグラウンドで継続する。
# 後から stop_following() を呼ぶまで追従し続ける。

stop_following()
# ↑ 追従スレッドに停止信号を送り、完全に終了するまでブロックしてから次へ。

# ── 固定時間追従（シンプルな場合）────────────────────────────────
follow_sample_position(
    duration=60,
    unit="min",
    reference_path=None,
    interval=5,
    interval_unit="min",
    similarity_threshold=0.95,
    max_correction_per_step_um=500,
    camera_index=0,
    autofocus_enabled=False,
    autofocus_range_um=20.0,
    autofocus_steps=10,
)
# ↑ start_following() → wait(duration=...) → stop_following() と等価。ブロッキング。
```

**使い分け：**
- 「N分間追従してから次へ」→ `follow_sample_position(duration=N, unit="min")`
- 「温度安定後にN分追従」→ `start_following()` → `wait_temperature(...)` → `wait(...)` → `stop_following()`
- 「圧力を上げながら追従し続け、XRD が終わったら止める」→ `start_following()` → … → `stop_following()`

`similarity_threshold` と `max_correction_per_step_um` のデフォルト値は
`apps/exp_scheduler/__localdata/scheduler_presets.json` に保存（スケジューラー設定 UI から編集）：

```json
{
  "follow_sample": {
    "interval_s": 300,
    "similarity_threshold": 0.95,
    "max_correction_per_step_um": 500,
    "camera_index": 0
  }
}
```

### 汎用

| 操作名 | DSL シグネチャ | 完了条件 |
|--------|--------------|----------|
| `wait` | `wait(duration=5, unit="min")` | タイマー |
| `log_message` | `log_message(message="Step done")` | 即時 |
| `start_logging` | `start_logging(devices=["pace5000", "lakeshore"], path="run_001")` | 即時 |
| `stop_logging` | `stop_logging()` | 即時 |

`wait` の `unit` は `"s"` / `"min"` のいずれか。

---

## Action モデル（`actions.py`）

すべての操作は `Action` 基底クラスのサブクラスとして定義する。
操作ごとに個別クラスを定義する（grouped operation class は使わない）。

```python
@dataclass
class Action:
    def describe(self) -> str: ...       # UI 表示用の文字列
    def to_dict(self) -> dict: ...       # JSON シリアライズ
    def to_dsl(self) -> str: ...         # DSL テキストへの逆変換
    @classmethod
    def from_dict(cls, d: dict): ...

# ── 汎用 ─────────────────────────────────────────────────────
WaitAction(duration_s: float)                       # TYPE="wait"
LogAction(message: str)                             # TYPE="log_message"
StartLoggingAction(devices: list[str], path: str)   # TYPE="start_logging"
StopLoggingAction()                                 # TYPE="stop_logging"

# ── Stage（Primitive）──────────────────────────────────────────
# operation: "move_absolute" | "move_relative" | "set_speed" | "normal_stop" | "emergency_stop"
# value は float | str（str = ループ変数名）
StageAction(operation: str, ch: int, value: float | str, speed: str | None)

# ── PACE5000 ─────────────────────────────────────────────────
# pressure は float | str（str = ループ変数名）
# unit: "MPa" | "Bar"（GPa 不可）  rate_unit: "MPa/min" | "Bar/min"
SetPressureAction(pressure: float | str, unit: str, rate: float | None, rate_unit: str | None)
WaitPressureAction(tol: float, unit: str)
SetControlModeAction(enabled: bool)

# ── LakeShore 335 ────────────────────────────────────────────
# value_k は float | str（str = ループ変数名）
SetTemperatureAction(value_k: float | str, ramp_rate: float | None)
WaitTemperatureAction(tol_k: float)
SetHeaterAction(range_index: int)    # 0=Off 1=Low 2=Medium 3=High
AllHeatersOffAction()

# ── Rad-icon 2022 ────────────────────────────────────────────
TakeXrdAction(
    exposure_ms: int | None,        # None → GlobalXrdSettings
    save: bool, prefix: str,
    save_dir: str | None,           # None → GlobalXrdSettings
    dark_file: str | None, dark_enabled: bool | None,
    defect_file: str | None, defect_enabled: bool | None, defect_kernel: int | None,
    flip_v: bool | None, flip_h: bool | None,
    # Ch11 oscillation (None → GlobalXrdSettings)
    oscillate: bool | None,
    osc_pos_a_deg: float | None, osc_pos_b_deg: float | None,
    osc_dwell_ms: int | None, osc_speed: str | None,
)
TakeDarkAction(exposure_ms: int)

# ── Camera ───────────────────────────────────────────────────
SaveReferenceImageAction(path: str | None, camera_index: int)
StartFollowingAction(
    reference_path: str | None,                # None → __localdata/reference_frame.png
    interval_s: float | None,                  # None → scheduler_presets.json
    similarity_threshold: float | None,         # None → scheduler_presets.json
    max_correction_per_step_um: float | None,   # 1ステップの補正上限（µm）。None → scheduler_presets.json
    camera_index: int,
    autofocus_enabled: bool,                    # True → XY補正後にCh3オートフォーカス実行
    autofocus_range_um: float,                  # ±n µm（片側）のスキャン範囲
    autofocus_steps: int,                       # スキャン点数（2以上）
)
StopFollowingAction()

# ── Compound Actions（to_steps() で primitive のリストに展開）──
MicroscopeOutFpdInAction(
    fpd_in_pos: int | None,          # None → stage_settings.json
    microscope_out_pos: int | None,  # None → stage_settings.json
    speed: str,
)
FpdOutMicroscopeInAction(
    fpd_out_pos: int | None,
    microscope_in_pos: int | None,
    speed: str,
)

# FollowSampleAction は StartFollowingAction + WaitAction + StopFollowingAction の糖衣
FollowSampleAction(
    duration_s: float,
    reference_path: str | None,
    interval_s: float | None,
    similarity_threshold: float | None,
    max_correction_per_step_um: float | None,
    camera_index: int,
    autofocus_enabled: bool,
    autofocus_range_um: float,
    autofocus_steps: int,
)

# ── 制御構造（Mode 2 DSL のみ生成）─────────────────────────────
ForLoopAction(var: str, values: list, body: list[Action])
```

ループ変数参照は JSON では `_var` suffix で表現する
（例：`"pressure_var": "p"` = ループ変数 `p` の値を使う）。

`ForLoopAction` については後述の「for ループの扱い」を参照。

---

## DeviceContext（`device_context.py`）

```python
@dataclass
class DeviceContext:
    controller: PM16CController | PM16CControllerSim | None
    pace5000:   Pace5000Backend | None
    lakeshore:  LakeShore335Backend | None
    radicon:    RadiconBackend | None
    # カメラは FollowSampleAction / SaveReferenceImageAction が必要時に自分で開く。
    # 常時接続ではないので DeviceContext には含めない。
```

`ExperimentalSchedulerWindow` はこの dataclass をひとつ受け取る。
`main.py` の `open_exp_scheduler()` で現接続状態から生成して渡す。

---

## シーケンス実行時のウィンドウ管理（`main.py` に追加）

### 設計方針

シーケンス実行中に他のウィンドウから装置を操作されると干渉するため、
**シーケンス開始時に全サブウィンドウを閉じ、終了時に復元する**。

### バックエンド所有権モデルとの整合

既存コードは「バックエンドはメインウィンドウ（`ModeSelectorLauncher`）が所有し、
サブウィンドウへ shared で渡す」という設計になっている。
サブウィンドウの `closeEvent` は `_owns_backend = False` のとき切断しない。
よって **サブウィンドウを閉じてもバックエンドは切断されず**、シーケンスは引き続き利用できる。

### 実装（`main.py` への追加）

```python
# ModeSelectorLauncher.__init__ に追加
self._btn_to_open_fn: dict[QPushButton, Callable] = {}

# init_ui の末尾に追加
self._btn_to_open_fn = {
    self.btn_dac_fpd_stage:      self.open_dac_fpd_stage,
    self.btn_interactive_camera: self.open_interactive_camera,
    self.btn_simple_stage_cont:  self.open_simple_stage_cont,
    self.btn_dac_oscillation:    self.open_dac_oscillation,
    self.btn_collimator_scan:    self.open_collimator_scan,
    self.btn_dac_scan:           self.open_dac_scan,
    self.btn_dac_scan_rot:       self.open_dac_scan_rot,
    self.btn_pace5000:           self.open_pace5000,
    self.btn_lakeshore:          self.open_lakeshore,
    self.btn_radicon:            self.open_radicon,
    self.btn_xrd_scan:           self.open_xrd_scan,
}

def close_all_sub_windows(self) -> list[QPushButton]:
    """シーケンス開始時に呼ぶ。開いていたウィンドウのボタンリストを返す。
    各 closeEvent が状態を JSON に保存してから閉じる。"""
    open_btns = list(self._open_windows.keys())
    for window in list(self._open_windows.values()):
        window.close()  # closeEvent → 状態保存 → destroyed → _open_windows から自動削除
    return open_btns

def restore_sub_windows(self, btns: list[QPushButton]) -> None:
    """シーケンス終了時に呼ぶ。"""
    for btn in btns:
        fn = self._btn_to_open_fn.get(btn)
        if fn:
            fn()
```

### 閉じる前の事前チェック（警告ダイアログ）

以下の状態が検出された場合、ユーザーに確認ダイアログを出してから閉じる。

| 状態 | 対処 |
|------|------|
| interactive_camera がビデオ録画中 | 警告 → OK で録画を停止してから閉じる |
| PACE5000 が手動ロギング中 | 警告 → OK でロギングを停止してから閉じる（スケジューラーが別途ログを管理） |
| DAC/XRD スキャンが実行中 | 警告 → OK でスキャンを中断してから閉じる |

### 閉じたときに失われる情報と対処

| ウィンドウ | 失われる情報 | 対処 |
|-----------|------------|------|
| interactive_camera | reference_frame（メモリのみ） | Reference Image パネルで事前保存済みか `save_reference_image()` ステップで対処 |
| interactive_camera | shapes（X線位置マーカー等） | `closeEvent` → `_save_shapes()` で JSON 保存済み。再 open 時に自動ロード ✓ |
| interactive_camera | calibration_data | `calibration.json` に保存済み ✓ |
| stage controller | 設定値 | `stage_settings.json` に保存済み ✓ |
| PACE5000 | 手動ログバッファ末尾 | 停止時に flush 済み（上記チェックで対処）✓ |
| LakeShore | ログバッファ末尾 | 同上 ✓ |

---

## 実行エンジン（`runner.py`）

### スレッドモデル

**QThread** を使用する（メインスレッドの QTimer ポーリングではない）。

理由：
- `snap_triggered()`（Rad-icon）や stage の待機はブロッキング操作
- バックエンドはすでに `Lock` を持ちスレッドセーフ

### シグナル

```python
class SequenceRunner(QThread):
    step_started     = pyqtSignal(int, str)   # (index, description)
    step_completed   = pyqtSignal(int)         # (index)
    progress_updated = pyqtSignal(str)         # 監視中の経過メッセージ
    sequence_completed = pyqtSignal()
    sequence_stopped   = pyqtSignal()
    error_occurred   = pyqtSignal(int, str)   # (index, message)
```

### 停止制御

`threading.Event` で停止フラグを伝達。各 Action の実行後に `_check_stop()` を呼ぶ。
待機ループ内でも 200 ms ごとにフラグをチェックする。

### エラーポリシー

**デバイスエラー時は即時停止**し `error_occurred` を発火する。スキップ・継続は不可。
将来的にメール通知などの外部アラートをここに追加する。

### 並列実行

**許可しない。** すべてのステップは直列に実行する。

### バックグラウンド追従スレッド

`start_following()` / `stop_following()` のみ例外的にバックグラウンドスレッドを使う。
これは「並列ステップ実行」ではなく「SequenceRunner が管理する補助プロセス」であり、
`start_logging()` / `stop_logging()` と同じ位置づけ。

```python
# SequenceRunner 内の状態
self._follow_thread: threading.Thread | None = None
self._follow_stop_event: threading.Event = threading.Event()

# StartFollowingAction 実行時
self._follow_stop_event.clear()
self._follow_thread = threading.Thread(target=self._follow_loop, daemon=True)
self._follow_thread.start()

# StopFollowingAction 実行時
self._follow_stop_event.set()
self._follow_thread.join(timeout=10)
self._follow_thread = None

# シーケンス停止時（_stop_event が立った場合）も必ず follow スレッドを終了させる
```

追従ループ（`_follow_loop`）は `_follow_stop_event` と `_stop_event`（メイン停止フラグ）の
どちらかが立ったら終了する。エラー発生時も同様。

---

## DSL 仕様（`dsl/`）

### 設計方針

- Python のサブセットを文法として採用（独自パーサー不要、`ast.parse()` を利用）
- 安全性：AST ホワイトリスト検証後に制限付き名前空間で実行
- 任意コードの実行を許さない

### 単位の扱い

単位はすべて **文字列の named parameter** として渡す。`*` 演算子による Unit オブジェクトは使わない。

```python
# 正しい書き方
wait(duration=5, unit="min")
set_pressure(pressure=2.0, unit="MPa", rate=0.2, rate_unit="MPa/min")
set_temperature(value=300, unit="K", ramp_rate=5.0)

# 使わない（単位オブジェクトによる乗算）
wait(5 * min)         # ← 採用しない
set_pressure(150 * MPa, ...)  # ← 採用しない
```

理由：`*` がPython本来の乗算と異なる意味で使われることになり、Pythonを知るユーザーにとって逆にわかりにくい。named parameter 方式のほうが意味が明確で LLM も安定して生成できる。

有効な `unit` 文字列の一覧は各関数の docstring に記載する。バリデータが未定義 unit を検出してエラーにする。

### 使える構文

| 構文 | 備考 |
|------|------|
| `for var in list:` | リストは数値リテラルのみ許可 |
| `if cond:` / `else:` | 条件は比較演算のみ |
| `var = value` | 変数代入 |
| 関数呼び出し | ホワイトリスト内の関数のみ |
| 数値・文字列リテラル | — |
| f-string | 変数参照のみ（式不可） |
| リスト・タプルリテラル | 数値のみ |
| 四則演算・比較演算 | — |

### 禁止構文（ブラックリスト）

`import` / `from ... import` / `class` / `def` / `lambda` / `exec` / `eval` /
`__` を含む属性アクセス / 例外処理 / コンテキストマネージャ / `while`

### `for` ループの扱い

DSL の `for` ループは **展開せず `ForLoopAction` として保持する**。

```python
# DSL:
for p in [1.0, 2.0, 3.0, 4.0]:
    set_pressure(pressure=p, unit="MPa", rate=0.2, rate_unit="MPa/min")
    wait(duration=5, unit="min")
```

**展開方式（採用しない）**：コンパイル時に 4 反復 × 2ステップ = 8 Action に展開する。
問題点：反復数が多いと（例：100点測定）タイムライン UI に数百行が並ぶ。また JSON に保存後に Mode 2 へ逆変換してもループ構造が失われる。

**保持方式（採用する）**：`ForLoopAction(var="p", values=[0.5, 1.0, 1.5, 2.0], body=[...])` として1ノードに保持する。
- UI ではループを折りたたみ可能なグループとして表示（「▶ for p in [0.5, 1.0, ...]」）
- `to_dsl()` で完全にループ構造を再現できる
- ランナーがネスト構造を再帰的に実行する（実装コストは小さい）

### DSL 実行パイプライン

```
DSL テキスト
  → ast.parse()
  → ASTValidator.validate()      # ホワイトリスト検証 + 未定義関数・unit の検出
  → SequenceBuilder.build()      # AST ノード → Action / ForLoopAction に変換
  → Sequence
  → SequenceRunner.run()         # ForLoopAction は再帰的に body を実行
```

### DSL 例

```python
# 昇圧 XRD 測定シーケンス（サンプル追従付き）
start_logging(devices=["pace5000", "lakeshore"], path="run_001")

set_temperature(value=300, unit="K", ramp_rate=5.0)
wait_temperature(tol=1.0, unit="K")

# XRD モードへ移行
microscope_out_and_fpd_in()

# リファレンス画像をここで取得（初期状態を記録）
save_reference_image()

for p in [1.0, 2.0, 3.0, 4.0, 5.0]:
    set_pressure(pressure=p, unit="MPa", rate=0.2, rate_unit="MPa/min")
    wait_pressure(tol=0.01, unit="MPa")
    # 30分間サンプル追従しながら XRD 測定
    follow_sample_position(duration=30, unit="min", interval=5, interval_unit="min")
    take_xrd(exposure_ms=1000, save=True, prefix="xrd")
    log_message(message=f"XRD at {p} MPa done")

# 事前に撮っておいたリファレンスを使う場合（別パターン）
# follow_sample_position(duration=30, unit="min",
#                        reference_path="C:/data/ref_initial.png")

set_pressure(pressure=0.0, unit="MPa", rate=0.5, rate_unit="MPa/min")
fpd_out_and_microscope_in()
all_heaters_off()
stop_logging()
```

### DSL 関数一覧

「装置と操作の一覧」セクションの「DSL シグネチャ」列がそのまま `dsl/api.py` の関数定義となる。

---

## UI 構成（`ui/`）

### ExperimentalSchedulerWindow（メインウィンドウ）

メインウィンドウは上部ツールバー、左パネル（Limit + Reference Image）、右パネル（タブ）で構成する。

```
┌───────────────────────────────────────────────────────────────────────────┐
│ [▶ Run]  [■ Stop]  [Save]  [Load]          Status: Ready                 │
├───────────────────────┬───────────────────────────────────────────────────┤
│ [Limit (mm from start)]│  Visual  │  Script  │  AI Assist (将来)         │
│      −(mm)   +(mm)   │──────────────────────────────────────────────────  │
│ Ch3  [____] [____]   │                                                     │
│ Ch4  [____] [____]   │  (タイムライン or テキストエディタ)                  │
│ Ch5  [____] [____]   │                                                     │
│                       │                                                     │
│ [Reference Image]     │                                                     │
│ [Capture Now] [Load…] │                                                     │
│ Save to:[____…] [...]  │                                                     │
│ Status: ✓  [Preview]  │                                                     │
└───────────────────────┴───────────────────────────────────────────────────┘
```

**Limit パネル（`QGroupBox("Limit (mm from start)")`）：**
- Ch3/Ch4/Ch5 それぞれに − 方向と + 方向の `QDoubleSpinBox` を配置（計 6 入力）
- 単位は mm、デフォルト値 1.0 mm
- 0.0 = そのチャンネル・方向を完全ロック
- 全 6 値が 0 以上の数値として入力されている必要があり、未設定の場合は Run をブロック
- 値は `scheduler_window_settings.json` の `global_limits` キーに保存・復元

**`GlobalLimits` データクラス（`runner.py` に定義）：**
```python
@dataclass
class GlobalLimits:
    ch3_minus_mm: float | None  # None = not configured（PreValidator がブロック）
    ch3_plus_mm:  float | None
    ch4_minus_mm: float | None
    ch4_plus_mm:  float | None
    ch5_minus_mm: float | None
    ch5_plus_mm:  float | None
```

**Global Limit の動作：**
- シーケンス開始時に Ch3/4/5 の位置をベースラインとして記録
- Ch3/4/5 の移動後、および follow loop の各補正移動後に現在位置をチェック
- 違反時：`ctrl.normal_stop()`（ASSTP）→ フォロースレッド停止 → `error_occurred` emit → 停止

- **タブ 1 — Visual（Mode 1）**：タイムラインウィジェット＋ステップ追加ボタン
- **タブ 2 — Script（Mode 2）**：テキストエディタ＋「Validate」「Convert to Visual」ボタン＋「Automatically convert to Visual when switching tabs」チェックボックス（デフォルト ON）
- **タブ 3 — AI Assist（将来）**：LLM パネル（現時点はスタブ、グレーアウト）

Mode 1 → Mode 2：タブ切り替え時に自動反映（`to_dsl()` を使い常時同期）。
Mode 2 → Mode 1：チェックボックスが ON（デフォルト）なら Script タブから離れる際に自動的に
「Convert to Visual」相当の処理（バリデーション→パース→Mode 1 反映）を実行する。OFF の場合は
従来どおり「Convert to Visual」ボタンを手動で押さない限り反映されない。ボタン自体はチェック
状態によらず常に有効。

### TimelineWidget

- `QTreeWidget` ベース（`ForLoopAction` を折りたたみ可能なグループとして表示するため）
- 装置ごとにアイコン色を変える（Stage: 青 / PACE5000: オレンジ / LakeShore: 赤 / XRD: 緑 / 汎用: グレー）
- 実行中ステップをハイライト、完了済みに ✓ マーク
- ドラッグ＆ドロップ並び替え対応（ループ body 内の並び替えも Phase 2 で対応済み。詳細は
  「Visual Editor での for ループ編集（Phase 2）」を参照）

### StepEditorDialog

1. 装置を選択（コンボボックス）
2. 操作を選択（装置に応じて動的に変わる）
3. パラメータ入力（操作に応じたフォーム）

Mode 1 から `for` ループを追加・編集できる（Phase 2 で追加。詳細は下記セクション参照）。
ループ本体へのステップ追加・編集や、ループ変数を使うパラメータ入力もここに含まれる。

### DslEditor

- `QPlainTextEdit` ベース（将来的にシンタックスハイライト追加可能）
- 「Validate」ボタン：検証エラーを行番号付きで下部に表示
- 「Convert to Visual」ボタン：バリデーション通過後に Mode 1 タブへ反映
- 「Automatically convert to Visual when switching tabs」チェックボックス（デフォルト ON）：
  ON の間は Script タブから離れるだけで上記の Convert 処理が自動実行される。空スクリプトの
  場合は何もしない（エラー表示なし）。OFF なら従来どおりボタンを押すまで反映されない。

---

## Visual Editor での for ループ編集（Phase 2）

Mode 1（Visual）から `ForLoopAction` の作成・編集、およびループ本体（body）へのステップ
追加・編集・削除・並び替えを可能にする。当初の設計（Task 6/7 時点）では「Mode 1 では
`for` ループは追加できない（DSL 専用）」としていたが、実際の運用（圧力・温度を振りながら
XRD を撮る測定）ではループ変数を使う場面が最も需要が高く、DSL を書かずに GUI だけで組める
必要があると判断し、本セクションで仕様を確定する。

**前提として、バックエンド（`actions.py` / `runner.py` / `validator/pre_validator.py` /
`dsl/`）は本機能に必要な機構をほぼ実装済み**である：

- `StageAction.value` / `SetPressureAction.pressure` / `SetTemperatureAction.value_k` は
  すでに `float | str` で、文字列はループ変数参照として扱われる（JSON では `..._var`
  サフィックスキー）
- `SequenceRunner._execute_actions` は `ForLoopAction` を再帰的に実行し、`var_context` を
  伝搬してループ変数を解決する（`_do_stage` / `_do_set_pressure` / `_do_set_temperature`）
- `PreValidator._check_stage_move_constraints` はループ変数を含む `move_absolute` /
  `move_relative` についても各イテレーションをシミュレートし MOVE_CONSTRAINTS を検証する
- `PreValidator._check_unused_loop_vars` はループ変数が body 内で一度も参照されない場合に
  警告する
- `TimelineWidget` はすでに `ForLoopAction` を折りたたみ可能なグループとして表示し、
  `flat_index`（`SequenceRunner._flat_index` と対応）を正しく計算している

したがって今回の作業は **UI 層（`ui/step_editor.py`, `ui/timeline_widget.py`）と、
未定義ループ変数参照を検出するバリデータの追加**に限定される。

### スコープ（Phase 2 で対応する範囲）

- **ループの入れ子は Visual からは作成できない。** 1 シーケンス中のどの位置でも、Visual で
  新規作成できる `ForLoopAction` は常に非入れ子（body に `ForLoopAction` を含まない）。
  入れ子は DSL（Mode 2）でのみ作成可能（既存動作のまま）。
  理由：入れ子対応は選択状態の追跡・flat_index 計算・UI 表示のすべてが複雑化するため、
  まず単純ケースを実装し、需要が確認されてから入れ子対応を検討する。
- **ループ変数を選べるフィールドは以下の 4 つに限定する**（Phase 2 時点）：
  `move_absolute.position` / `move_relative.delta` / `set_pressure.pressure` /
  `set_temperature.value_k`。
  `wait.duration` や `take_xrd.exposure_ms`、oscillation の角度パラメータ等は対象外
  （必要になった時点で個別に追加する）。

### DSL 由来の入れ子ループを Visual で開いた場合の扱い

Script タブで入れ子 `for` を書いて「Convert to Visual」した場合、`ForLoopAction.body` に
別の `ForLoopAction` が含まれうる。この場合 Visual では **その入れ子ループを不透明
（編集不可）なブロックとして 1 行表示する**：

- 表示テキストは `"⚠ Nested loop — edit via Script tab"` + `action.describe()`
- 選択してもツールバーの Add-into-loop / Edit は無効化される（"Edit" ボタンを押すと
  「入れ子ループは Script タブで編集してください」という案内ダイアログを出す）
- 削除（そのブロックごと丸ごと削除）と、外側ループ内での上下移動は許可する
  （並び替え・削除は木構造を壊さないため安全）
- `TimelineWidget._make_primitive_item` とは別に
  `_make_nested_loop_placeholder_item(action: ForLoopAction)` を新設して実装する

### ツールバー / 操作フロー

- `TimelineWidget` のツールバーに **`+ Add Loop`** ボタンを `+ Add Step` の隣に新設する。
  StepEditorDialog に `"Control"` という疑似デバイスを作って `for_loop` 操作として混在させる
  案は採らない — ループは単一デバイスの操作ではなく「複数ステップを束ねるコンテナ」であり、
  デバイス選択リストに混ぜると概念が一段ずれて分かりにくくなるため。
- `+ Add Loop` → 新規ダイアログ `ForLoopEditorDialog(var=None, values=None, parent=...)` を
  開く。OK 押下で **空 body の `ForLoopAction`** をトップレベルに挿入する（挿入位置は
  既存の `_insert_action` と同じ規則：選択中のトップレベル項目の直後、未選択なら末尾）。
- 既存ループの**ヘッダー行**を選択して「Edit」を押すと、同じ `ForLoopEditorDialog` が
  var/values の編集用に開く（body は編集しない）。
- **ループ本体へのステップ追加は、既存の `+ Add Step` / `Edit` / `Delete` / `▲ Up` /
  `▼ Down` ボタンをコンテキスト対応にする**（ボタンを増やさない）。選択中の項目が
  ループのヘッダー行、またはそのボディの子項目である場合、これらのボタンは
  「そのループの body に対する操作」を意味する：
  - `+ Add Step`：ボディの末尾（子項目選択時はその直後）に追加。`StepEditorDialog` に
    `available_loop_vars=[loop.var]` を渡す
  - `Edit` / `Delete`：選択中のボディ子項目を編集・削除
  - `▲ Up` / `▼ Down`：ボディ内でのみ移動（ループ境界を越えない）
  - トップレベル項目（ループの外）を選択している場合は従来どおりトップレベルに対する操作
  - **曖昧さ回避のため**、ツールバー付近に現在の対象を示すラベル（例：
    `"Adding into loop 'p'"` / 未選択時は非表示）を表示する
- ループヘッダーの `Delete` は body ごと消える破壊的操作のため、**確認ダイアログを必須**と
  する（例："This loop contains 4 step(s). Delete the entire loop?"）。単一ステップの
  Delete には確認ダイアログを出さない（既存動作を変えない）。
- `Copy` / `Paste` はループ境界をまたいで実行できる。ただしコピー元アクションが上記 4
  フィールドのいずれかでループ変数参照（文字列値）を持ち、貼り付け先のスコープにその変数名
  が存在しない場合は、貼り付け時に値を `0`（定数）へ変換したうえで
  `QMessageBox.warning` で通知する（サイレントに壊れた参照を残さない）。

### `ForLoopEditorDialog`（新規、`ui/step_editor.py` または `ui/for_loop_editor.py`）

```python
class ForLoopEditorDialog(QDialog):
    def __init__(self, action: ForLoopAction | None = None, parent=None):
        """action=None なら新規作成、action 指定なら var/values の編集モード"""
    def get_action(self) -> ForLoopAction | None:
        """OK 押下後に呼ぶ。新規作成時は body=[]。編集時は既存の body をそのまま保持する。"""
```

- **`var`**（`QLineEdit`）：バリデーション — 空文字不可、正規の Python 識別子
  （`str.isidentifier()`）であること、`keyword.iskeyword()` でないこと、DSL の
  `ALLOWED_FUNCTIONS`（`dsl/__init__.py`）に含まれる関数名と衝突しないこと。
  違反時は OK ボタンを無効化し、理由をラベル表示する（既存の `_TakeXrdWidget` の
  `validity_changed` パターンを踏襲）。
- **`values`**：カンマ区切りの数値リストを入力する `QLineEdit`
  （例：`1.0, 2.0, 3.0, 4.0, 5.0`）。空／非数値トークンがあれば OK 無効化。
  加えて、点数が多いスキャン（圧力ステップ測定など）を想定し、**「範囲から生成」補助 UI**
  （開始値・終了値・刻み幅 or 点数の `QDoubleSpinBox`／`QSpinBox` ＋ `[Generate]` ボタン）
  を設け、生成結果を上記テキストフィールドへ書き戻す。テキストフィールドが常に正
  （生成 UI はあくまで入力補助であり、別データを保持しない）。
  `dsl/normalizer.py` の `range()` 展開と同じ丸め規則を流用し、**展開後 200 要素超はエラー**
  とする（`Normalizer` の既存上限と揃える）。
  値は内部的に必ず `float` にキャストして保持する（DSL は数値をすべて float として扱うため、
  Visual 作成分と DSL 作成分で `to_dsl()` の出力が食い違わないようにする）。
- **ループ変数のリネーム（cascade rename）**：既存ループを編集して `var` を変更した場合、
  body 内の全参照を自動的に新しい変数名へ書き換える。対象は
  `StageAction.value` / `SetPressureAction.pressure` / `SetTemperatureAction.value_k` が
  旧変数名と完全一致する場合と、他の文字列フィールドに旧変数名の f-string プレースホルダ
  `"{oldvar}"` が含まれる場合（`log_message` 等）。この置換ロジックは
  `validator/pre_validator.py` の `_action_uses_loop_var` / `_loop_body_uses_var` と
  同じ「アクションのどのフィールドがループ変数を保持しうるか」の知識を共有する必要が
  あるため、両者から呼べる共通ユーティリティとして実装する（例：`actions.py` に
  `rename_loop_var_refs(body: list[Action], old: str, new: str) -> None` を追加し、
  pre_validator 側は同じフィールド一覧を参照する形にリファクタする）。

### `StepEditorDialog` の拡張

```python
class StepEditorDialog(QDialog):
    def __init__(
        self,
        action: Action | None = None,
        parent=None,
        available_loop_vars: tuple[str, ...] = (),
    ):
        ...
```

- `available_loop_vars` が空（トップレベルでの追加・編集）の場合、UI は現行のまま
  （定数入力のみ）。空でない場合のみ、対象 4 フィールドに「定数 / ループ変数」切り替え
  UI を表示する。
- 切り替え UI は既存の `_opt_float` / `_opt_speed` 等と同じ「行にまとめる」パターンで
  新規ヘルパー `_val_or_var(lo, hi, v, dec, available_vars)` を追加する：
  ラジオボタン `"Constant"` / `"Loop variable"` の 2 択 ＋ `QDoubleSpinBox`（定数用）＋
  `QComboBox`（`available_vars` を列挙、変数用）を横並びにし、ラジオボタンでどちらかを
  有効化する。`build()` は "Constant" 選択時は `float`、"Loop variable" 選択時は
  `QComboBox.currentText()`（`str`）を該当フィールドにセットする。
  `fill()` は既存アクションの値が `str` なら "Loop variable" 側を選択・復元する。
- Phase 1 では `available_loop_vars` は常に要素 0 または 1（入れ子非対応のため）。
  ただし `QComboBox` を使う実装にしておくことで、将来ループの入れ子を許可した際に
  複数変数を選べるよう拡張しやすくする。

### `TimelineWidget` の変更

- `get_sequence()`：ループのトップレベル項目について、キャッシュされた元の `ForLoopAction`
  オブジェクトをそのまま返すのではなく、**現在の子項目（body）から
  `ForLoopAction(var=.., values=.., body=[子項目から取得した Action のリスト])` を
  都度再構築する**方式に変更する（子項目が入れ子プレースホルダの場合はそのまま
  元の `ForLoopAction` を返す）。
- 選択項目の種別判定を `_current_top_level()` から拡張し、
  `_current_selection_kind() -> Literal["top_level", "loop_header", "loop_body_child", "nested_loop_placeholder", "none"]`
  を新設する。ツールバーの各ボタンの有効・無効とラベル表示はこれに基づいて切り替える。
- `_rebuild_flat_map()` / `highlight_step()` / `mark_step_done()` の既存ロジックは
  変更不要（すでにループ本体の子項目を反復回数分だけ `flat_map` に積む実装になっており、
  body の中身が変わっても構造は同じため、追加・削除・並び替え後に呼び直せば整合する）。

### `validator/pre_validator.py` の追加チェック

- **`_check_undefined_loop_vars`（新規）**：各アクションの対象フィールドが文字列値を
  持つ場合、その文字列がその位置で有効な（enclosing `ForLoopAction` の）変数名の
  いずれかと一致するかを、シーケンスを `var_context` を積みながら再帰的に走査して検証する。
  一致しなければエラー："ループ変数 `'x'` はこの位置では未定義です"。
  実装は `_check_stage_move_constraints._walk` と同じ「スコープを追いながら木を歩く」
  パターンを一般化し、`_action_uses_loop_var` が知っている「フィールド一覧」を再利用する
  （`rename_loop_var_refs` 用に追加する共通ユーティリティと同じ情報源を使う）。
  - この新チェックは Visual Editor の機能追加のためだけでなく、**既存の潜在バグを
    塞ぐ**：`_check_stage_move_constraints._apply` はすでに
    `# unresolved loop variable; already flagged elsewhere` というコメント付きで
    未解決の変数参照を黙ってスキップしている。現状ではその「elsewhere」が実は存在せず、
    誤って別スコープの変数名を使った `move_absolute` 等が MOVE_CONSTRAINTS
    チェックを素通りしてしまう。本チェックの追加によりこの穴を塞ぐ。
- **空ループ本体のチェック（新規）**：`ForLoopAction.body` が空の場合はエラー
  （実行不可）。`+ Add Loop` で作成した直後の空ループのまま Run しようとするケースを
  防ぐ。あわせて `TimelineWidget` はループヘッダーの body が空の間、警告アイコン
  （例："⚠ empty"）をヘッダーテキストに付与して視覚的に気づけるようにする。

### 決定済み事項（追加分）

| 事項 | 決定内容 |
|------|----------|
| Visual からの for ループ作成・編集 | Phase 2 で対応。`+ Add Loop` ボタン＋ `ForLoopEditorDialog` |
| ループの入れ子（Visual 作成） | Phase 2 では非対応。DSL 由来の入れ子は Visual 上で不透明ブロック表示 |
| ループ変数を選べるフィールド | `move_absolute.position` / `move_relative.delta` / `set_pressure.pressure` / `set_temperature.value_k` の 4 つのみ（Phase 2 時点） |
| ループ本体へのステップ追加 UI | 専用ボタンを増やさず、既存ボタンをコンテキスト対応にする（対象ラベルを表示） |
| ループヘッダーの Delete | body ごと消えるため確認ダイアログ必須 |
| ループ境界をまたぐ Copy/Paste | 許可。ただし変数スコープ外になる場合は値を定数化し警告 |
| ループ変数のリネーム | cascade rename（body 内の全参照を自動置換）。共通ユーティリティを validator と共有 |
| 未定義ループ変数参照の検出 | `PreValidator._check_undefined_loop_vars` を新設（既存の潜在バグの修正も兼ねる） |
| 空ループ本体 | `PreValidator` がエラーとしてブロックする |

---

## Sequence シリアライズ（`sequence.py`）

JSON 形式で保存・読み込み。

```json
{
  "version": 1,
  "schema": "exp_scheduler",
  "actions": [
    {"type": "start_logging", "devices": ["pace5000", "lakeshore"], "path": "run_001"},
    {"type": "set_temperature", "value_k": 300.0, "ramp_rate": 5.0},
    {"type": "wait_temperature", "tol_k": 1.0},
    {
      "type": "for_loop",
      "var": "p",
      "values": [1.0, 2.0, 3.0, 4.0, 5.0],
      "body": [
        {"type": "set_pressure", "pressure_var": "p", "unit": "MPa",
         "rate": 0.2, "rate_unit": "MPa/min"},
        {"type": "wait_pressure", "tol": 0.01, "unit": "MPa"},
        {"type": "wait", "duration_s": 300.0},
        {"type": "take_xrd", "exposure_ms": 1000, "save": true, "prefix": "xrd"}
      ]
    },
    {"type": "all_heaters_off"},
    {"type": "stop_logging"}
  ]
}
```

`pressure_var` / `value_var` のように `_var` suffix がついているフィールドはループ変数参照を示す。

---

## Mode 1 ↔ Mode 2 双方向変換

| 方向 | 方法 |
|------|------|
| Mode 1 → Mode 2 | 各 Action の `to_dsl()` を改行で連結。`ForLoopAction` はインデント付きのループブロックを生成 |
| Mode 2 → Mode 1 | DSL パース → Sequence → タイムラインに反映。`ForLoopAction` は折りたたみグループとして表示 |

---

## LLM 連携（`llm/` + `ui/llm_panel.py`）

### 設計方針

- バックエンド：Ollama HTTP API（`/api/chat`、非ストリーミング）
- 推奨モデル：`qwen3-coder:14b` → `qwen3-coder:8b` → `qwen2.5-coder:7b`（UI で変更可）
- 生成パラメータ：`temperature=0.1, top_p=0.8`（創造性より再現性を優先）
- 「LLM が生成したコードを直接実行しない」原則を厳守

### ファイル構成

```
apps/exp_scheduler/
├── dsl/
│   ├── _registry.py       # @dsl_command デコレーター + registry
│   ├── normalizer.py      # AST 正規化（range展開、int→float）
│   └── api.py             # @dsl_command + NumPy スタイル docstring
└── llm/
    ├── __init__.py
    ├── client.py          # OllamaChatWorker / OllamaConnectionWorker (QThread)
    ├── prompts.py         # PromptTemplate dataclass + 共有テキスト
    ├── prompt_builder.py  # api.py から自動生成
    └── session.py         # 会話管理・DSL抽出・self-fix・履歴圧縮
ui/
└── llm_panel.py           # AI Assist タブ + ExplainDialog
```

### System Prompt の自動生成（Single Source of Truth）

`prompt_builder.py` が `dsl/api.py` を `inspect` でスキャンし、
関数シグネチャ・型アノテーション・docstring から System Prompt を自動生成する。

新しい DSL 関数を追加する手順：
1. `dsl/api.py` に `@dsl_command(category="...", example="...")` デコレーター付きで関数を追加し、docstring を LLM 仕様書として丁寧に書く
2. `dsl/__init__.py` の `ALLOWED_FUNCTIONS` に追加

**これだけで LLM の System Prompt が自動更新される。`llm/` 以下のコード変更は不要。**

### PromptTemplate — テンプレート分離

```python
@dataclass
class PromptTemplate:
    header: str     # 用途別（generate / selffix / explain）
    grammar: str    # 共有（DSL文法）
    commands: str   # 自動生成（カテゴリ別関数仕様）
    examples: str   # 自動生成（@dsl_command(example=...) から）
    footer: str     # 用途別

    def render(self) -> str: ...
```

`GRAMMAR` セクションは DSL 文法が変わるまで共有。ヘッダー・フッターだけ用途別。

### DSL 生成パイプライン

```
ユーザー入力
  → LlmSession.build_messages()
  → Ollama /api/chat（OllamaChatWorker, QThread）
  → LlmSession.try_extract_and_validate()
      → _extract_dsl()           # 多段フォールバック抽出
      → normalize()              # range展開・int→float
      → ASTValidator.validate()  # ホワイトリスト検証
  → 失敗時: build_selffix_messages() → 再生成（最大3回）
  → 成功時: LlmPanel 右ペインに表示、[Apply to Timeline] 有効化
  → Apply → SequenceBuilder.build() → sequence_applied シグナル
  → TimelineWidget + DslEditor 同時更新
```

### DSL Version

`dsl/__init__.py` に `DSL_VERSION = "1.0.0"` を定義。
System Prompt の冒頭に埋め込まれ、仕様変更時にバージョンを上げることで
LLM が古い知識と混同しないようにする。

### `@dsl_command` デコレーター

```python
# dsl/_registry.py
def dsl_command(category: str, example: str = "") -> Callable:
    """DSL コマンドメタデータを registry に登録するデコレーター。"""

# 使用例（dsl/api.py）
@dsl_command(
    category="Temperature",
    example='set_temperature(value=300.0, unit="K", ramp_rate=5.0)\nwait_temperature(tol=1.0, unit="K")'
)
def set_temperature(value: float, *, unit: str = "K", ramp_rate: float) -> None:
    """Set the LakeShore 335 target temperature. Does NOT wait...
    ...
    """
```

### 会話履歴の管理

- 最大 10 メッセージ（system 除く）を保持
- DSL 生成成功後に履歴をルールベース要約に圧縮（LLM 再呼び出し不要）
- 圧縮後は直前の成功コンテキストのみを保持

### Normalizer（`dsl/normalizer.py`）

| 変換 | 内容 |
|------|------|
| `range(1, 6)` → `[1.0, 2.0, 3.0, 4.0, 5.0]` | ASTValidator 前に展開。展開後 200 要素超でエラー |
| `int` リテラル → `float` | `1` → `1.0`（DSL は数値をすべて float として扱う）|

### Explain モード（`_ExplainDialog`）

`[Explain Current DSL]` ボタンがモーダルダイアログを開き、LLM に日本語での説明を要求する。
チャット履歴とは完全分離（専用 System Prompt `EXPLAIN_HEADER`）。
対象 DSL は AI Assist タブのプレビュー、または Script タブのテキストをフォールバックで使用。

### 依存関係

`requests` パッケージが必要（`pip install requests`）。
`requests` が未インストールの場合、接続テスト・生成ともにエラーメッセージを表示する（クラッシュしない）。

---

## 既存バックエンドへの変更

基本的に**変更しない**。スケジューラ層がアダプターとして完了条件の判定を担う。

推奨追加（任意）：
- `LakeShore335Backend.get_current_temperature() -> float`：`get_data()[-1].temp_a_k` のショートカット

---

## 決定済み事項

| 事項 | 決定内容 |
|------|----------|
| 単位の表現方法 | named parameter（`unit="MPa"` など）を使う。Unit オブジェクト乗算は採用しない |
| 並列実行 | 許可しない。すべて直列 |
| エラーポリシー | デバイスエラー時は即時停止。将来的にメール通知などを追加 |
| for ループ内部表現 | `ForLoopAction` として保持（展開しない） |
| タイムライン UI の基底 | `QTreeWidget`（ループのネスト表示のため） |
| ステージ compound のプリセット読み出し元 | `stage_settings.json`（stage_controller UI と共用。二重管理しない） |
| follow パラメータのプリセット読み出し元 | `apps/exp_scheduler/__localdata/scheduler_presets.json`（新規） |
| リファレンス画像の統一方式 | 画像ファイルパス。デフォルト `__localdata/reference_frame.png` |
| シーケンス実行中のウィンドウ排他 | 開始時に全サブウィンドウを close()、終了時に復元 |
| カメラの DeviceContext への含有 | 含めない。各 Action が必要時に自分で `VideoCapture` を開く |
| `follow_sample_position` の終了条件 | `start_following()` / `stop_following()` ペアで任意のタイミングに対応。`follow_sample_position(duration=...)` は固定時間の糖衣構文として残す |
| 追従バックグラウンドスレッド | `SequenceRunner` が `_follow_thread` として管理。`start_logging` / `stop_logging` と同じ位置づけ（並列ステップ実行ではない）|

---

## 実行前検証（`validator/pre_validator.py`）

シーケンス実行ボタンを押したとき、**Runner を起動する前に** シーケンス全体を静的解析し、
予測可能なエラーをまとめてユーザーに提示する。
エラーが1件でもあれば実行を拒否する（警告のみのものは確認ダイアログ）。

`validator/pre_validator.py` に検証項目を追加したときは、`validator/VALIDATOR.md` にも追加する。
記述は簡潔な日本語とし、主としてチェック対象となる装置ごとにまとめ、Markdown の番号付き箇条書きはすべて `1.` で始める。

### 検証の種類

| 種別 | 内容 | 結果 |
|------|------|------|
| **接続チェック** | 使用するデバイスが接続されているか | エラー（実行不可） |
| **準備状態チェック** | デバイスが操作可能な状態か | エラー（実行不可） |
| **設定ファイルチェック** | 必要な JSON / 画像ファイルが存在するか | エラー（実行不可） |
| **構造チェック** | start/stop の対応など論理的整合性 | エラー（実行不可） |
| **警告** | 推奨されないが実行は可能な状態 | 警告ダイアログ（続行可） |

### 操作別チェック一覧

#### Stage（Primitive）

| 操作 | チェック内容 | 種別 |
|------|------------|------|
| 全ステージ操作 | `controller is not None` | 接続 |
| 全ステージ操作 | `isinstance(controller, PM16CControllerSim)` であれば「シミュレーションモードで実行中」と警告 | 警告 |
| `move_absolute` / `move_relative` | シーケンス開始前にステージが停止中（`get_is_moving() == False`） | 準備状態 |

#### Stage Compound

| 操作 | チェック内容 | 種別 |
|------|------------|------|
| `microscope_out_and_fpd_in` | 位置が引数で未指定の場合、`stage_settings.json` に `ch8_out`（顕微鏡アウト）と `det_in`（FPD イン）キーが存在するか | 設定ファイル |
| `fpd_out_and_microscope_in` | 位置が引数で未指定の場合、`stage_settings.json` に `det_out`（FPD アウト）と `ch8_in`（顕微鏡イン）キーが存在するか | 設定ファイル |

#### PACE5000

| 操作 | チェック内容 | 種別 |
|------|------------|------|
| 全 PACE5000 操作 | `pace5000_backend is not None and pace5000_backend._is_connected` | 接続 |

#### LakeShore 335

| 操作 | チェック内容 | 種別 |
|------|------------|------|
| 全 LakeShore 操作 | `lakeshore_backend is not None and lakeshore_backend.is_connected` | 接続 |
| `wait_temperature` | `lakeshore_backend.get_data()` が空でないか（最低1回の読み取りが済んでいるか） | 準備状態 |

#### Rad-icon 2022

| 操作 | チェック内容 | 種別 |
|------|------------|------|
| `take_xrd` / `take_dark` | `radicon_backend is not None` | 接続 |

#### Camera / Follow

| 操作 | チェック内容 | 種別 |
|------|------------|------|
| `save_reference_image` / `start_following` / `follow_sample_position` | カメラインデックス `camera_index` で `cv2.VideoCapture(idx).isOpened()` → 接続確認して即 release | 接続 |
| `start_following` / `follow_sample_position` | `apps/interactive_camera/calibration.json` が存在し、`matrix_inv` キーを持つか | 設定ファイル |
| `start_following` / `follow_sample_position` | `reference_path` が指定されていればその画像ファイルが存在するか；省略時は `__localdata/reference_frame.png` が存在するか | 設定ファイル |

#### 構造チェック（シーケンス全体を走査）

| チェック内容 | 種別 |
|------------|------|
| `stop_following` がシーケンス内で `start_following` よりも前に現れていないか | 構造 |
| `start_following` が連続して2回呼ばれていないか（unpaired） | 構造 |
| `start_following` に対応する `stop_following` が存在するか（なければ警告：シーケンス終了後も追従が残る） | 警告 |
| `start_logging` が連続して2回呼ばれていないか | 構造 |
| `stop_logging` が `start_logging` より前に呼ばれていないか | 構造 |

#### 汎用

| 操作 | チェック内容 | 種別 |
|------|------------|------|
| `start_logging(devices=[...])` | リスト内の各デバイス名に対応するバックエンドが接続済みか（`"pace5000"`, `"lakeshore"` など） | 接続 |
| `wait` / `log_message` / `stop_logging` / `stop_following` | チェック不要 | — |

### 実装メモ

```python
@dataclass
class PreCheckResult:
    errors:   list[str]   # 実行を阻止するエラー
    warnings: list[str]   # 警告（確認後続行可）

class PreValidator:
    def validate(self, sequence: Sequence, ctx: DeviceContext) -> PreCheckResult:
        errors, warnings = [], []
        used_actions = self._collect_all_actions(sequence)  # ForLoopAction を再帰展開

        # 接続チェック
        if any(isinstance(a, StageAction | MicroscopeOutFpdInAction | ...)
               for a in used_actions):
            if ctx.controller is None:
                errors.append("Stage controller not connected")
            elif isinstance(ctx.controller, PM16CControllerSim):
                warnings.append("Stage is in simulation mode")
        # ... 以下同様 ...

        # 設定ファイルチェック
        for a in used_actions:
            if isinstance(a, StartFollowingAction | FollowSampleAction):
                cal_path = Path("apps/interactive_camera/calibration.json")
                if not cal_path.exists():
                    errors.append("calibration.json not found")
                else:
                    data = json.loads(cal_path.read_text())
                    if "matrix_inv" not in data:
                        errors.append("calibration.json has no 'matrix_inv' key")
                # reference image
                ref = a.reference_path or "__localdata/reference_frame.png"
                if not Path(ref).exists():
                    errors.append(f"Reference image not found: {ref}")

        # 構造チェック
        errors.extend(self._check_follow_pairing(sequence))
        errors.extend(self._check_logging_pairing(sequence))

        return PreCheckResult(errors=errors, warnings=warnings)
```

- 全エラーを収集してから一括表示する（最初のエラーで止めない）
- `ForLoopAction` の body も再帰的に走査する
- 検証は必ず Runner 起動前に実行し、エラーがあれば Runner を起動しない

---

## 実装順序（推奨）

1. `device_context.py` + `actions.py` + `sequence.py`（データ層）
2. `runner.py`（実行エンジン）
3. `validator/pre_validator.py`（実行前検証）
4. `main.py` への統合（`_btn_to_open_fn` / `close_all_sub_windows` / `restore_sub_windows` 追加）
5. `ui/scheduler_window.py` + `ui/timeline_widget.py` + `ui/step_editor.py`（Mode 1 UI）
   - Reference Image パネルはこの段階で実装
6. `dsl/`（Mode 2 パーサー・バリデータ・API）
7. `ui/dsl_editor.py`（Mode 2 UI）
8. Compound Actions（`MicroscopeOutFpdInAction` / `FpdOutMicroscopeInAction` / `SaveReferenceImageAction` / `FollowSampleAction`）
9. `ui/llm_panel.py`（LLM スタブ → 将来実装）
10. Visual Editor での for ループ編集（Phase 2、上記セクション参照）：
    `ui/step_editor.py`（`available_loop_vars` 対応・`_val_or_var`）→
    `ui/for_loop_editor.py`（`ForLoopEditorDialog` 新規）→
    `ui/timeline_widget.py`（`+ Add Loop` / コンテキスト対応ボタン / `get_sequence()` 再構築）→
    `validator/pre_validator.py`（`_check_undefined_loop_vars` / 空ループ本体チェック）

## ログ仕様（`log_manager.py`）

`start_logging(devices=[...], path="run_001")` を呼ぶと、以下のファイル群が生成される。

```
__localdata/logs/run_001_<YYYYMMDD_HHMMSS>/
├── metadata.json     # 実行シーケンスJSON・GlobalLimits・デバイスリスト（一度だけ書き込み）
├── conditions.csv    # 科学データ：T/P/ステージポジション/XRDファイル名の時系列
└── ops.log           # 操作監査ログ：全コマンドとイベントのテキスト記録
```

### `conditions.csv` の列

| 列 | 内容 |
|----|------|
| `timestamp` | ISO 8601（ミリ秒精度） |
| `elapsed_s` | シーケンス開始からの経過秒 |
| `event_type` | `start` / `periodic` / `xrd_taken` / `pressure_reached` / `temperature_reached` / `user_log` / `error` / `stop` / `logging_stopped` |
| `step_index` | ステップ番号（フラットインデックス） |
| `T_K` | LakeShore 温度（K）、未接続または `devices` 外なら空欄 |
| `P_MPa` | PACE5000 圧力（MPa）、同上 |
| `Ch3_pulse` / `Ch4_pulse` / `Ch5_pulse` | ステージ現在位置（パルス） |
| `xrd_file` | 保存した XRD ファイル名（`take_xrd()` 時のみ） |
| `note` | 補足テキスト |

書き込みタイミング：バックグラウンドスレッドが N 秒ごとに `periodic` 行を書く（デフォルト 30 s。`scheduler_presets.json` の `logging.science_poll_interval_s` で変更可）。加えて、各イベント（XRD 撮影・T/P 安定完了・`log_message()` 等）の発生時に即時書き込みされる。

### `ops.log` の書式

```
2026-07-02 10:30:00.123 [SEQ:START] run_001_20260702_103000
2026-07-02 10:30:00.124 [STEP #0000 START] Set temperature 300.0 K ramp 5.0 K/min
2026-07-02 10:30:00.125 [LAKESHORE] set_ramp_parameter(rate=5.0000 K/min, enable=True)
2026-07-02 10:30:00.326 [LAKESHORE] ramp rate verified → 5.0000 K/min
2026-07-02 10:30:00.327 [LAKESHORE] set_setpoint(300.00 K)
2026-07-02 10:30:00.328 [STEP #0000 DONE ] 0.20 s
...
2026-07-02 10:35:28.502 [STEP #0003 START] XRD 1000 ms save→scan
2026-07-02 10:35:28.503 [RADICON] set_exposure_ms(1000) + snap_triggered()
2026-07-02 10:35:29.850 [RADICON] saved → scan_20260702_103528.npy
2026-07-02 10:35:29.851 [STEP #0003 DONE ] 1.35 s
...
2026-07-02 10:45:00.000 [LIMIT ERROR] Global limit exceeded on Ch4: +1.050 mm (limit +1.000 mm)
2026-07-02 10:45:00.001 [STAGE] normal_stop() ASSTP — global limit violation
2026-07-02 10:45:00.002 [SEQ:ABORT] Sequence aborted due to error
```

### 実装クラス

- `RunLogger`（`log_manager.py`）：3ファイルの開閉、バックグラウンドポーリングスレッド管理、`log_science()` / `log_ops()` を提供
- `SequenceRunner`（`runner.py`）：`self._logger = RunLogger(ctx)` として保持。`StartLoggingAction` で `start()`、`StopLoggingAction` または `run()` の `finally` で `stop()`

---

## 要確認事項

- Ch8およびCh9に関するソフトウェアリミッターは正しく効いているのか？
- ログの取り方についての検討
- XRDデータの保存先、補正の設定方法
- ✅ Ch3,4,5のglobal limitterを設定する（`GlobalLimits` dataclass、Limit パネル UI、`_check_global_limits()` 実装済み）
- ✅ ログの取り方についての検討（`log_manager.py` の `RunLogger` で実装済み。詳細は下記「ログ仕様」セクションを参照）
- LLMを用いたDSLの自動生成
- ✅ DAC oscillationの組み込み（`take_xrd` の `oscillate` オプションとして実装済み。詳細は「Rad-icon 2022」セクションを参照）
- シーケンス自体の保存と読み出し
- Follow sample positionのデフォルト値
- LimitのUIデザインの崩れ
- validateされていないとrunできないように。またvisual editingにもvalidatorをつける
- validatorのbug
- visual とDSLの即時反映
- ✅ Visual Editor から for ループ（変数利用ループ含む）を作成・編集できるようにする。
  仕様確定・実装済み（2026-07-15）：「Visual Editor での for ループ編集（Phase 2）」セクション参照。
  `ui/for_loop_editor.py`（新規）/ `ui/step_editor.py` の `_val_or_var` / `ui/timeline_widget.py` の
  `+ Add Loop` とコンテキスト対応ボタン / `actions.py` の `rename_loop_var_refs` /
  `validator/pre_validator.py` の `_check_undefined_loop_vars`・`_check_empty_loop_body`

### Interactive Camera Save Snapshot Addendum

- `save_snapshot(save_dir=None)` captures one USB camera frame and saves it as `snapshot_YYYYMMDD_HHMMSS_mmm.png`.
- The operation takes only a save directory. When `save_dir` is `None`, the runner uses `GlobalCameraSettings.snapshot_save_dir`, then falls back to `apps/exp_scheduler/__localdata/snapshots`.
- If a run contains any Interactive Camera action, `SequenceRunner` opens the USB camera once at run start, keeps a latest-frame capture loop alive for the full sequence, and releases it during cleanup.
- Sequence JSON may include `global_camera: {"snapshot_save_dir": "..."}` for the Interactive Camera global snapshot directory.
