# Experimental Scheduler — 実装計画

このファイルは各実装タスクの「Claude Code へのプロンプト」として機能する。
タスクを依頼する前に、ユーザーはこのファイルの該当セクションをプロンプトとして渡すこと。

**必読前提：** 各タスク開始前に必ず `apps/exp_scheduler/SPEC.md` 全体を Read すること。また、実装の過程で `apps/exp_scheduler/SPEC.md`に記されている仕様より良い仕様が見つかり、`apps/exp_scheduler/SPEC.md`とは異なる実装を行った場合、実装終了後に、`apps/exp_scheduler/SPEC.md`を実際の実装に合わせて修正してからタスクを終了すること。

---

## タスク一覧

| # | タイトル | 依存 | 主要ファイル |
|---|---------|------|-------------|
| 1 | [データ層](#task-1-データ層) | なし | `device_context.py`, `actions.py`, `sequence.py` |
| 2 | [実行エンジン](#task-2-実行エンジン) | Task 1 | `runner.py` |
| 3 | [実行前検証](#task-3-実行前検証) | Task 1 | `validator/pre_validator.py` |
| 4 | [main.py 統合](#task-4-mainpy-統合) | Task 1–3 | `main.py` |
| 5 | [スケジューラーウィンドウ](#task-5-スケジューラーウィンドウ) | Task 1–4 | `ui/scheduler_window.py` |
| 6 | [タイムラインウィジェット](#task-6-タイムラインウィジェット) | Task 5 | `ui/timeline_widget.py` |
| 7 | [ステップ編集ダイアログ](#task-7-ステップ編集ダイアログ) | Task 5–6 | `ui/step_editor.py` |
| 8 | [DSL パーサー・バリデータ](#task-8-dsl-パーサーバリデータ) | Task 1 | `dsl/validator.py`, `dsl/parser.py` |
| 9 | [DSL API・DSL エディタ UI](#task-9-dsl-apidsl-エディタ-ui) | Task 8 | `dsl/api.py`, `ui/dsl_editor.py` |
| 10 | [Compound Actions](#task-10-compound-actions) | Task 1–2 | `actions.py` 追記 |
| 11 | [Visual Editor での for ループ編集](#task-11-visual-editor-での-for-ループ編集) | Task 6–8 | `ui/step_editor.py`, `ui/for_loop_editor.py`（新規）, `ui/timeline_widget.py`, `validator/pre_validator.py`, `actions.py` 追記 |

---

## Task 1: データ層

### 作業内容

`apps/exp_scheduler/` に以下を新規作成する。既存ファイルは変更しない。

```
apps/exp_scheduler/
├── __init__.py          （空）
├── device_context.py
├── actions.py
└── sequence.py
```

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（全体。特に「Action モデル」「DeviceContext」「Sequence シリアライズ」セクション）
- `apps/PACE5000/pace5000_backend.py`（型確認のため先頭 30 行程度）
- `apps/lakeshore/lakeshore335_backend.py`（同）

### 実装仕様

#### `device_context.py`

SPEC.md の「DeviceContext」セクションに従い `@dataclass` を定義する。
型ヒントは `TYPE_CHECKING` ガードで循環インポートを避ける。
フィールド：`controller`, `pace5000`, `lakeshore`, `radicon`（すべて `| None`）。

#### `actions.py`

SPEC.md の「Action モデル」セクションに従い全 Action クラスを定義する。

共通基底クラス：
```python
@dataclass
class Action:
    def describe(self) -> str: raise NotImplementedError
    def to_dict(self) -> dict: raise NotImplementedError
    def to_dsl(self) -> str: raise NotImplementedError
    @classmethod
    def from_dict(cls, d: dict) -> "Action": raise NotImplementedError
```

実装するクラス（SPEC.md の一覧を参照）：
- Primitive: `WaitAction`, `LogAction`, `StageAction`, `PressureAction`, `TemperatureAction`, `HeaterAction`, `XrdAction`, `StartLoggingAction`, `StopLoggingAction`, `SaveReferenceImageAction`, `StartFollowingAction`, `StopFollowingAction`
- Compound: `MicroscopeOutFpdInAction`, `FpdOutMicroscopeInAction`, `FollowSampleAction`（Task 10 で本実装。このタスクでは `to_steps()` を `raise NotImplementedError` でよい）
- 制御: `ForLoopAction`

`from_dict` は `"type"` フィールドで分岐するファクトリ関数 `action_from_dict(d: dict) -> Action` を `actions.py` のモジュールレベルに置く。

#### `sequence.py`

```python
@dataclass
class Sequence:
    actions: list[Action]
    name: str = ""
    version: int = 1

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "Sequence": ...
    def save(self, path: str | Path) -> None: ...
    @classmethod
    def load(cls, path: str | Path) -> "Sequence": ...
```

JSON 形式は SPEC.md の「Sequence シリアライズ」セクションに従う。
`ForLoopAction.to_dict()` は `body` を再帰的にシリアライズする。

### 注意点

- Qt 依存は一切入れない（このタスクは純 Python）
- `from_dict` の `type` キー文字列は後続タスクで使うため、各 Action クラスに `TYPE = "wait"` のようなクラス変数を置いておくと管理しやすい
- `ForLoopAction.body` の型は `list[Action]`（`ForLoopAction` を含むネストも許可）

---

## Task 2: 実行エンジン

### 前提

Task 1 完了済み。

### 作業内容

`apps/exp_scheduler/runner.py` を新規作成する。

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「実行エンジン」「バックグラウンド追従スレッド」セクション）
- `apps/PACE5000/pace5000_app.py`（`ScheduledControlRunner` クラス：既存のシングルデバイス版ランナーの参考）
- `apps/interactive_camera/interactive_camera.py`（`_follow_task` メソッド：追従ループの移植元）

### 実装仕様

```python
class SequenceRunner(QThread):
    step_started     = pyqtSignal(int, str)    # (flat_index, description)
    step_completed   = pyqtSignal(int)
    progress_updated = pyqtSignal(str)
    sequence_completed = pyqtSignal()
    sequence_stopped   = pyqtSignal()
    error_occurred   = pyqtSignal(int, str)    # (flat_index, message)

    def __init__(self, sequence: Sequence, ctx: DeviceContext, parent=None): ...
    def run(self) -> None: ...           # QThread エントリポイント
    def request_stop(self) -> None: ... # 外部から呼ぶ停止要求
```

**ステップ実行ループ：**
- `_execute_actions(actions: list[Action], depth: int)` を再帰関数として実装
- `ForLoopAction` は `for val in action.values` でループし、body を再帰呼び出し
- 各 Action 実行後に `_stop_event.is_set()` をチェック
- エラー時は `error_occurred` を emit して即リターン

**追従スレッド管理：**
- `_follow_thread: threading.Thread | None`
- `_follow_stop_event: threading.Event`
- `StartFollowingAction` → `_follow_loop()` を daemon スレッドとして起動
- `StopFollowingAction` → `_follow_stop_event.set()` + `join(timeout=10)`
- `request_stop()` 時にも follow スレッドを終了させる

**`_follow_loop()` の実装：**
`interactive_camera.py` の `_follow_task` をほぼ移植する。
`calibration.json` の `matrix_inv` を読んでピクセルずれ → パルス変換する。
`self._stop_event` と `self._follow_stop_event` 両方をループ内でチェックする。

**待機ポーリング（`wait_pressure`, `wait_temperature` など）：**
```python
deadline = time.monotonic() + timeout_s
while time.monotonic() < deadline:
    if self._stop_event.is_set():
        return
    # 条件チェック
    time.sleep(0.2)
    self.progress_updated.emit(f"Waiting... {remaining:.0f}s")
```

### 注意点

- Task 10（Compound Actions）完了まで `MicroscopeOutFpdInAction` 等の実行は `raise NotImplementedError` でよい
- バックエンド呼び出しはすべて try/except で囲み、例外を `error_occurred` に変換する
- `QThread.run()` の中では Qt シグナル emit は OK だが `QDialog` など UI コンストラクタは呼ばない

---

## Task 3: 実行前検証

### 前提

Task 1 完了済み。

### 作業内容

```
apps/exp_scheduler/
└── validator/
    ├── __init__.py   （空）
    └── pre_validator.py
```

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「実行前検証」セクション全体）
- `apps/interactive_camera/interactive_camera.py`（`calibration_data` ロード部分：378〜392 行付近）
- `apps/stage_fpd_scope/__localdata/stage_settings.json`（キー名の確認）

### 実装仕様

```python
@dataclass
class PreCheckResult:
    errors:   list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

class PreValidator:
    def validate(self, sequence: Sequence, ctx: DeviceContext) -> PreCheckResult: ...
```

SPEC.md の「操作別チェック一覧」と「構造チェック」を全て実装する。

**`_collect_all_actions(sequence)`：**
`ForLoopAction` の body を再帰展開してフラットな `list[Action]` を返す内部ユーティリティ。
ただし構造チェック（start/stop ペア）はフラット展開前の順序で行う。

**ファイル存在チェックのパス解決：**
`calibration.json` は `Path(__file__).parent.parent.parent / "interactive_camera" / "calibration.json"` のような相対パスで解決する（ハードコードしない）。

**構造チェック（start/stop ペア）：**
シーケンスを線形にスキャン（`ForLoopAction` の body も走査）し、
`start_following` / `stop_following` のネスト深度カウンタで不整合を検出する。

### 注意点

- OpenCV `cv2.VideoCapture` でカメラ接続チェックをする場合、必ず即 `release()` すること
- ファイルチェックで JSON パースエラーが出た場合もエラーとして報告する
- `stage_settings.json` のキー名は `fpd_scope_stg_controller_ui.py` の実装と合わせる（`det_in`, `det_out`, `ch8_in`, `ch8_out`）

---

## Task 4: main.py 統合

### 前提

Task 1–3 完了済み。

### 作業内容

`bl18c_controller/main.py` を編集する（既存機能を壊さないよう最小限の変更）。

### 読むべき既存ファイル

- `bl18c_controller/main.py`（全体。`ModeSelectorLauncher` クラスの構造を把握すること）
- `apps/exp_scheduler/SPEC.md`（「シーケンス実行時のウィンドウ管理」セクション）

### 実装仕様

**`ModeSelectorLauncher.__init__` に追加：**
```python
self._btn_to_open_fn: dict[QPushButton, Callable] = {}
```

**`init_ui` の末尾に追加：**
- 「Experimental Scheduler」ボタンを既存ボタン群と同列に追加
- `self._btn_to_open_fn` を既存の全ボタン → `open_*()` メソッドのマッピングで初期化
  （既存ボタン名・メソッド名は `main.py` を読んで正確に拾うこと）

**追加するメソッド：**
```python
def open_exp_scheduler(self) -> None: ...
def close_all_sub_windows(self) -> list[QPushButton]: ...
def restore_sub_windows(self, btns: list[QPushButton]) -> None: ...
```

`close_all_sub_windows` / `restore_sub_windows` の仕様は SPEC.md を参照。

`open_exp_scheduler` は `ExperimentalSchedulerWindow` を `DeviceContext` を渡してインスタンス化し、
`_open_windows` に登録する（他のウィンドウと同じパターンで実装する）。

### 注意点

- `_open_windows` の管理パターン（`destroyed` シグナルで自動削除）を必ず既存実装と同じ方式で踏襲すること
- この時点では `ExperimentalSchedulerWindow` は最低限動けばよい（Task 5 で本実装）
- `_btn_to_open_fn` は `init_ui` 完了後に初期化する（ボタンが先に存在している必要があるため）

---

## Task 5: スケジューラーウィンドウ

### 前提

Task 1–4 完了済み。

### 作業内容

```
apps/exp_scheduler/ui/
├── __init__.py    （空）
└── scheduler_window.py
```

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「UI 構成」セクション全体）
- `bl18c_controller/main.py`（`_open_windows` パターン、`_owns_backend` パターンの確認）
- 既存のサブウィンドウ（例：`apps/PACE5000/pace5000_app.py` の `Pace5000Window`）を1つ読んでウィンドウ構造の参考にする

### 実装仕様

SPEC.md の UI レイアウト図に従い `ExperimentalSchedulerWindow(QMainWindow)` を実装する：

**上部ツールバー：** Run / Stop / Save / Load ボタン、ステータスラベル

**Reference Image パネル（常設）：**
- `[Capture Now]` ボタン：interactive_camera が `_open_windows` にあれば `current_frame` を借用、なければ `VideoCapture(index)` を一時的に開いて取得
- `[Load from file…]` ボタン：`QFileDialog.getOpenFileName`
- Save to フィールド（`QLineEdit` + `[…]` ボタン）
- Status ラベル（「✓ Captured HH:MM:SS」など）
- `[🖼 Preview]` ボタン：取得済み画像を小ウィンドウで表示

**タブウィジェット：**
- Tab 1「Visual」：`TimelineWidget` を埋め込む（Task 6 で実装、このタスクではプレースホルダー）
- Tab 2「Script」：`DslEditor` を埋め込む（Task 9 で実装、プレースホルダー）
- Tab 3「AI Assist」：無効化済みのタブ（将来用スタブ）

**Run ボタン押下時の流れ：**
1. `PreValidator.validate(sequence, ctx)` を実行
2. エラーあり → エラーダイアログ（全件表示）、実行しない
3. 警告あり → 確認ダイアログ、キャンセルなら実行しない
4. `main_window.close_all_sub_windows()` を呼んで開いているウィンドウを閉じる（`main_window` への参照は初期化時に受け取る）
5. `SequenceRunner` を生成・起動
6. `sequence_completed` / `sequence_stopped` / `error_occurred` シグナルを受けて `main_window.restore_sub_windows(btns)` を呼ぶ

### 注意点

- `Capture Now` で interactive_camera の `current_frame` を借用する際は `main_window._open_windows` を走査して `InteractiveCameraWindow` を探す
- リファレンス画像のプレビュー表示は `cv2.cvtColor(frame, COLOR_BGR2RGB)` → `QImage` → `QPixmap` の変換が必要
- Stop ボタンは `runner.request_stop()` を呼ぶだけ（UI の更新はシグナル受信後）

---

## Task 6: タイムラインウィジェット

### 前提

Task 5 完了済み。

### 作業内容

`apps/exp_scheduler/ui/timeline_widget.py` を新規作成する。

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「TimelineWidget」セクション）
- Task 1 で作成した `actions.py`

### 実装仕様

```python
class TimelineWidget(QWidget):
    def set_sequence(self, sequence: Sequence) -> None: ...
    def get_sequence(self) -> Sequence: ...         # 並び替え後の順序で返す
    def highlight_step(self, flat_index: int) -> None: ...
    def mark_step_done(self, flat_index: int) -> None: ...
    def clear_highlights(self) -> None: ...
```

**内部構造：**
- `QTreeWidget` 1カラム構成（Action の `describe()` を表示）
- `ForLoopAction` → `QTreeWidgetItem` の子要素として body を表示、折りたたみ可能
- 装置ごとにアイコン色を変える（Stage: 青 / PACE5000: オレンジ / LakeShore: 赤 / XRD: 緑 / Camera: 紫 / 汎用: グレー）
- 実行中アイテムを黄色ハイライト、完了済みを薄緑に
- `flat_index` は `ForLoopAction` を展開した際のインデックス（`SequenceRunner` が emit する `step_started(int, str)` の int と対応）

**ドラッグ＆ドロップ：**
`QTreeWidget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)` で有効化。
ただし `ForLoopAction` グループのルートアイテムのみをドラッグ可能にし、body 内アイテムの独立移動は Phase 2 以降で対応（この段階では body の並び替えは無効でよい）。

**ツールバー：**
[+ Add Step]（Step Editor Dialog を開く）/ [✏ Edit]（選択ステップ編集）/ [🗑 Delete]（選択ステップ削除）/ [↑ Up] [↓ Down]（順序変更）

### 注意点

- `flat_index` の計算は `SequenceRunner` 側と一致させる必要がある。`ForLoopAction` の中の各 body アイテムは「ループ本体の0番目、1番目…」と別インデックスで扱うか、あるいはループ自体を1インデックスとして扱うか、SPEC.md の設計に従って統一すること（実装前に確認ダイアログで仕様確認してよい）

---

## Task 7: ステップ編集ダイアログ

### 前提

Task 5–6 完了済み。

### 作業内容

`apps/exp_scheduler/ui/step_editor.py` を新規作成する。

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「StepEditorDialog」セクション、「装置と操作の一覧」セクション）
- Task 1 で作成した `actions.py`

### 実装仕様

```python
class StepEditorDialog(QDialog):
    def __init__(self, action: Action | None = None, parent=None):
        """action=None なら新規作成、action 指定なら編集モード"""
    def get_action(self) -> Action | None:
        """OK 押下後に呼ぶ。バリデーションが通らなければ None"""
```

**UI 構成：**
1. 装置選択コンボ（Stage / PACE5000 / LakeShore / Radicon / Camera / General）
2. 操作選択コンボ（装置に応じて動的に変わる）
3. パラメータフォーム（操作に応じて `QStackedWidget` で切り替え）

**パラメータフォームの実装方針：**
各 Action ごとに `QFormLayout` ベースのウィジェットを作る（動的生成より静的定義が保守しやすい）。
`unit` フィールドは `QComboBox`（有効値は SPEC.md の一覧から固定）。

### 注意点

- この時点（Task 7）では `ForLoopAction` は追加できない（DSL 専用のまま）。Visual からの
  ループ作成・編集、およびループ変数を選べる入力 UI は Task 11 で追加する
- 編集モードでは既存 Action のフィールド値をフォームに初期値として反映する
- `speed` フィールド（Stage）は `"H"` / `"M"` / `"L"` の `QComboBox`

---

## Task 8: DSL パーサー・バリデータ

### 前提

Task 1 完了済み。

### 作業内容

```
apps/exp_scheduler/dsl/
├── __init__.py    （空）
├── validator.py
└── parser.py
```

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「DSL 仕様」セクション全体）
- Task 1 で作成した `actions.py`

### 実装仕様

#### `validator.py`

```python
class ASTValidator(ast.NodeVisitor):
    ALLOWED_NODES = {ast.Module, ast.Expr, ast.Call, ast.For, ast.If,
                     ast.Assign, ast.Name, ast.Constant, ast.List, ast.Tuple,
                     ast.BinOp, ast.Compare, ast.BoolOp, ast.JoinedStr,
                     ast.FormattedValue, ast.Return, ...}
    ALLOWED_FUNCTIONS: set[str]  # dsl/api.py の関数名一覧（循環参照に注意）

    def validate(self, tree: ast.AST) -> list[str]:
        """エラーメッセージのリストを返す（空 = OK）"""
```

SPEC.md の「禁止構文（ブラックリスト）」に対応する各 `visit_*` メソッドを実装する。
禁止ノード検出時は `errors` リストに行番号付きメッセージを追加する。

#### `parser.py`

```python
class SequenceBuilder(ast.NodeVisitor):
    def build(self, tree: ast.AST) -> Sequence: ...
```

- `ast.Call` → ホワイトリスト関数名 → 対応する Action インスタンスを生成
- `ast.For` → `ForLoopAction`（`iter` が `ast.List` でリテラル数値のみか検証）
- 変数参照（`ast.Name`）が `for` ループ変数として認識される場合、`ForLoopAction.body` 内でその変数を `"__var_ref__"` のようなマーカーで表現する（`pressure_var` フィールドと整合させる）

### 注意点

- `ast.parse()` で `SyntaxError` が出た場合はそのまま `validate` のエラーとして返す
- Python バージョン差異に注意（`ast.Constant` vs 古い `ast.Num`/`ast.Str`）。Python 3.8+ を前提にしてよい
- `validator.py` と `parser.py` の間でホワイトリスト関数名を共有するため、`dsl/__init__.py` に `ALLOWED_FUNCTIONS: frozenset[str]` を定義してそこから import するとよい

---

## Task 9: DSL API・DSL エディタ UI

### 前提

Task 8 完了済み。

### 作業内容

```
apps/exp_scheduler/dsl/
└── api.py
apps/exp_scheduler/ui/
└── dsl_editor.py
```

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「DSL 関数一覧」「DslEditor」セクション）
- Task 8 で作成した `dsl/parser.py`

### 実装仕様

#### `dsl/api.py`

SPEC.md の「装置と操作の一覧」にある全 DSL シグネチャを Python 関数として定義する。
各関数の本体は `Action` オブジェクトをシーケンスビルダーのコンテキストに追加する処理（`SequenceBuilder` が保持するリストに append）。

```python
# 例
def wait(duration: float, unit: str = "min") -> None:
    """Wait for a fixed duration. unit: 's' or 'min'"""
    _ctx.append(WaitAction(duration_s=duration * (60 if unit == "min" else 1)))
```

#### `ui/dsl_editor.py`

```python
class DslEditor(QWidget):
    sequence_changed = pyqtSignal(Sequence)

    def get_text(self) -> str: ...
    def set_text(self, text: str) -> None: ...
    def set_sequence(self, seq: Sequence) -> None:
        """Action リスト → DSL テキストへ逆変換して表示"""
```

- `QPlainTextEdit` でテキスト編集
- `[Validate]` ボタン：`ASTValidator` を走らせ、エラーを行番号付きで下部 `QLabel` に表示
- `[Convert to Visual]` ボタン：バリデーション通過後にパースして `sequence_changed` を emit（TimelineWidget が受け取り表示更新）

### 注意点

- `api.py` の関数一覧は `dsl/__init__.py` の `ALLOWED_FUNCTIONS` と完全に一致させること
- DSL → Action のとき `for` ループ変数（`p` など）を body の各 Action が参照する場合の表現は Task 8 で決定した方式に従う

---

## Task 10: Compound Actions

### 前提

Task 1–2 完了済み。

### 作業内容

Task 1 で作成した `actions.py` に compound actions の本実装を追加する。
`runner.py` が `StartFollowingAction` を実行する際に呼ぶ `_follow_loop` の完全実装も行う。

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「Stage — Compound」「カメラ — Compound」「バックグラウンド追従スレッド」セクション）
- `apps/interactive_camera/interactive_camera.py`（`_follow_task`, `_compute_xy_shift` メソッドを熟読）
- `apps/stage_fpd_scope/fpd_scope_stg_controller_ui.py`（`shortcut_1`, `shortcut_2` メソッドで移動順序を確認）
- `apps/stage_fpd_scope/__localdata/stage_settings.json`（キー名の確認）
- `apps/interactive_camera/calibration.json`（`matrix_inv` フィールドの形式確認）

### 実装仕様

#### `MicroscopeOutFpdInAction.to_steps(stage_settings: dict) -> list[StageAction]`

1. `microscope_out_pos`（Ch8 OUT）が `None` → `stage_settings["ch8_out"]` を使う
2. `fpd_in_pos`（Ch9 IN）が `None` → `stage_settings["det_in"]` を使う
3. 順序：Ch8 OUT → 完了後 Ch9 IN（`fpd_scope_stg_controller_ui.py` の `shortcut_1` と同じ順序）

#### `FpdOutMicroscopeInAction.to_steps(stage_settings: dict) -> list[StageAction]`

1. `fpd_out_pos`（Ch9 OUT）が `None` → `stage_settings["det_out"]` を使う
2. `microscope_in_pos`（Ch8 IN）が `None` → `stage_settings["ch8_in"]` を使う
3. 順序：Ch9 OUT → 完了後 Ch8 IN

#### `_follow_loop` の完全実装（`runner.py` に追記）

`interactive_camera.py` の `_follow_task` をほぼそのまま移植する。
主な変更点：
- `self.reference_frame` の代わりに `StartFollowingAction.reference_path` で指定されたファイルを `np.load()` で読み込む
- `self.calibration_data['matrix_inv']` の代わりに `calibration.json` を読んで取得
- `self.controller` の代わりに `self._ctx.controller` を使う
- `self.follow_interval_spinbox.value()` の代わりに `StartFollowingAction.interval_s` を使う
- `self._follow_stop_event.is_set()` で停止判定

### 注意点

- `interactive_camera.py` の `_compute_xy_shift` はそのままコピーして `runner.py` のメソッドとして使える
- カメラ `VideoCapture` は `_follow_loop` 内でオープン・クローズする（`DeviceContext` には含めない）
- `calibration.json` の `matrix_inv` は `[[a, b], [c, d]]` 形式のリスト → `np.array()` で変換
- Ch4/Ch5 への move 命令は `ctx.controller.move_relative(ch, delta_pulses)` を使う

---

## Task 11: Visual Editor での for ループ編集

### 前提

Task 6–8 完了済み（`TimelineWidget` / `StepEditorDialog` / DSL パーサー・バリデータが
一通り動く状態）。

### 作業内容

`SPEC.md` の「Visual Editor での for ループ編集（Phase 2）」セクション**全体**を読んでから
着手する。既存の `actions.py` / `runner.py` / `dsl/` 側にはループ変数の解決機構
（`float | str` フィールド、`var_context` 伝搬）がすでに実装済みであり、変更不要。
今回は UI 層とバリデータの追加のみ：

1. `ui/for_loop_editor.py`（新規）：`ForLoopEditorDialog`
2. `ui/step_editor.py`：`StepEditorDialog.__init__` に `available_loop_vars` パラメータを追加、
   対象 4 フィールド（`move_absolute.position` / `move_relative.delta` /
   `set_pressure.pressure` / `set_temperature.value_k`）に定数/ループ変数切り替え UI
   （`_val_or_var` ヘルパー）を追加
3. `ui/timeline_widget.py`：`+ Add Loop` ボタン、選択種別判定
   （`_current_selection_kind()`）、コンテキスト対応した既存ボタン群、入れ子ループの
   不透明ブロック表示、`get_sequence()` の再構築ロジック
4. `actions.py`：`rename_loop_var_refs(body, old, new)` ユーティリティを追加
5. `validator/pre_validator.py`：`_check_undefined_loop_vars` と空ループ本体チェックを追加。
   既存の `_action_uses_loop_var` / `_loop_body_uses_var` は
   `rename_loop_var_refs` と対象フィールド一覧を共有するようリファクタする

### 読むべき既存ファイル

- `apps/exp_scheduler/SPEC.md`（「Visual Editor での for ループ編集（Phase 2）」全文）
- `apps/exp_scheduler/actions.py`（`StageAction` / `SetPressureAction` /
  `SetTemperatureAction` / `ForLoopAction` の `value`/`pressure`/`value_k` 周り）
- `apps/exp_scheduler/runner.py`（`_do_stage` / `_do_set_pressure` / `_do_set_temperature`
  の `var_context` 解決パターン）
- `apps/exp_scheduler/validator/pre_validator.py`
  （`_check_stage_move_constraints._walk` のスコープ追跡パターン、
  `_action_uses_loop_var` / `_loop_body_uses_var`）
- `apps/exp_scheduler/dsl/normalizer.py`（`range()` 展開・200 要素上限のロジック
  — `ForLoopEditorDialog` の「範囲から生成」で同じ丸め規則・上限を流用する）
- `apps/exp_scheduler/ui/step_editor.py`（`_opt_float` / `_opt_speed` 等、既存の
  「行にまとめる」ウィジェットヘルパーのパターン）
- `apps/exp_scheduler/ui/timeline_widget.py`（`_current_top_level` /
  `_insert_action_at` / `_rebuild_flat_map` / `get_sequence`）

### 実装仕様

SPEC.md の該当セクションに詳細な仕様（UI 構成・ボタンの状態遷移・cascade rename・
入れ子ループのプレースホルダ表示・バリデータの追加チェック）を記載済みなのでそちらに従う。
ここでは実装順序のみ示す：

1. `actions.py` に `rename_loop_var_refs` を追加（純粋関数、Qt 非依存なのでまず着手しやすい）
2. `validator/pre_validator.py` に `_check_undefined_loop_vars` と空ループ本体チェックを追加
   （`rename_loop_var_refs` と対象フィールド一覧を共有するようリファクタ）
3. `ui/for_loop_editor.py` の `ForLoopEditorDialog`
4. `ui/step_editor.py` の `available_loop_vars` 対応
5. `ui/timeline_widget.py` の `+ Add Loop` / コンテキスト対応ボタン / `get_sequence()`

### 注意点

- ループの入れ子は Visual からは作成できない（DSL 専用のまま）。DSL 由来の入れ子は
  不透明ブロックとして表示し、Edit は無効化・案内ダイアログを出す
- ループヘッダーの Delete は body ごと消えるため確認ダイアログ必須
- Copy/Paste でループ境界をまたぐ際、貼り付け先スコープに存在しない変数参照は
  定数化した上で警告する（サイレントに壊れた参照を残さない）
- `values` は必ず `float` にキャストして保持する（DSL 側は数値をすべて float
  として扱うため、Visual 作成分と DSL 作成分で `to_dsl()` の出力を一致させる）
- `_check_undefined_loop_vars` の追加は、`_check_stage_move_constraints._apply` に
  既存する「unresolved loop variable; already flagged elsewhere」という前提
  （実は現状どこにも flag されていない潜在バグ）を実際に真にするためのものでもある

---

## 備考

- 各タスクの実装後に `python -m pytest` または簡単な動作確認を行うことを推奨
- Task 1–3 は Qt なしの純 Python なのでユニットテストが書きやすい
- 実装中に SPEC.md の仕様が不明確な箇所に気づいた場合は、実装を止めてユーザーに確認してから進めること（仮定で実装しない）

---

## 発見済み不具合・修正リスト

コードレビューで発見した問題を優先度順に記載する。
修正済みの項目には ✅ を付けること。

### 緊急（動作クラッシュ・データ損失）

#### BUG-1: `close_all_sub_windows()` がスケジューラーウィンドウ自身を閉じる

**ファイル：** `main.py:721–726` / `ui/scheduler_window.py:323–325`

**症状：**
`_on_run()` 内で `self._main_window.close_all_sub_windows()` を呼ぶと、
`ExperimentalSchedulerWindow` 自身が `_open_windows` に登録されているため、
自分自身の `close()` が呼ばれる。その時点ではランナーが未起動なので
`closeEvent` はダイアログを出さず受理してしまう。
`WA_DeleteOnClose` により C++ ウィジェットの削除がスケジュールされ、
後続の `self._timeline.clear_highlights()` やランナーからのシグナルが
破棄済みウィジェットに届いてクラッシュまたは未定義動作になる。

**原因：**
`_btn_to_open_fn` から `btn_exp_scheduler` を除外することは意識されているが、
`close_all_sub_windows()` のループから除外することが漏れている。

**修正方針：**
`close_all_sub_windows()` のループで `btn_exp_scheduler` をスキップする。
または `_on_run()` から呼ぶ前にスケジューラーウィンドウを `_open_windows` から
一時的に取り出す。最もシンプルな修正例：

```python
def close_all_sub_windows(self) -> list[QPushButton]:
    open_btns = [
        btn for btn in self._open_windows
        if btn is not self.btn_exp_scheduler   # ← スケジューラー自身を除外
    ]
    for btn in open_btns:
        self._open_windows[btn].close()
    return open_btns
```

---

#### BUG-2: エラー発生後に `sequence_completed` も emit される

**ファイル：** `runner.py:107–110`

**症状：**
デバイスエラーが発生すると `_execute_actions` 内で `error_occurred` を emit した後
`_StopRequested` を raise する。`run()` はこれをキャッチして処理を終えるが、
`_stop_event` がセットされていないため最終的に `sequence_completed` も emit される。

結果として `scheduler_window._on_error_occurred()` の後に `_on_sequence_completed()`
が実行され、エラーステータス（赤）が "Completed"（緑）に上書きされる。
ウィンドウ復元処理 `_do_restore()` も二重実行される。

**修正方針：**
エラー発生をトラッキングするフラグを追加するか、エラー時に `_stop_event` をセットする：

```python
# _execute_actions 内
except Exception as exc:
    self.error_occurred.emit(idx, str(exc))
    self._stop_event.set()        # ← 追加：エラー時も stop フラグを立てる
    raise _StopRequested()

# run() 末尾の emit 判定はそのままでよい
# _stop_event が立っているので sequence_stopped が emit される
# → しかし error_occurred + sequence_stopped の二重発火になる点は要注意
```

あるいは専用フラグを使う方が明快：

```python
class SequenceRunner(QThread):
    def __init__(...):
        ...
        self._error_occurred = False

# _execute_actions 内
except Exception as exc:
    self._error_occurred = True
    self.error_occurred.emit(idx, str(exc))
    raise _StopRequested()

# run() 末尾
if self._error_occurred:
    pass   # error_occurred は既に emit 済み
elif self._stop_event.is_set():
    self.sequence_stopped.emit()
else:
    self.sequence_completed.emit()
```

---

### 高優先度（実行前検証の誤判定）

#### BUG-3: `FollowSampleAction` がペアなし開始として誤判定される

**ファイル：** `validator/pre_validator.py:329–335`

**症状：**
`_check_follow_pairing` では `StartFollowingAction | FollowSampleAction` で
`depth += 1` し、`StopFollowingAction` で `depth -= 1` する。
`FollowSampleAction` は内部的に `start + wait + stop` の糖衣構文なので
対応する `StopFollowingAction` が存在しない。
よって `FollowSampleAction` 単独のシーケンスで走査を終えると `depth > 0` となり、
「`start_following` に対応する `stop_following` がない」という偽の警告が出る。

**修正方針：**
`FollowSampleAction` は完結したペアとして扱う（depth を変動させない）：

```python
elif isinstance(a, StartFollowingAction):   # FollowSampleAction は除く
    if depth > 0:
        errors.append("nested start_following is not allowed")
    depth += 1
elif isinstance(a, FollowSampleAction):
    if depth > 0:
        errors.append("nested follow_sample_position / start_following is not allowed")
    # depth は変動させない（内部でペアが完結しているため）
elif isinstance(a, StopFollowingAction):
    ...
```

---

#### ✅ BUG-4: フォローループの補正パルス上限に `* 1000` が含まれていた

**修正済み（`runner.py`）：**
```python
# 修正後（µm ÷ µm/pulse = pulse）
lim4 = max(0, int(max_correction_um / _UM_PER_PULSE[4]))
lim5 = max(0, int(max_correction_um / _UM_PER_PULSE[5]))
```

**合わせて実施した設計変更：**
BUG-4 への対処として per-step 制限の修正に加え、
シーケンス全体を通じた **Global Limit**（Ch3/4/5 の開始位置からの移動量上限）を新設した。
詳細は SPEC.md「Limit パネル」「GlobalLimits データクラス」セクションを参照。

---

### 中優先度（動作に影響するが致命的でない不整合）

#### BUG-5: ログデバイス名が箇所によって異なる

**ファイル：** `dsl/validator.py`・`validator/pre_validator.py`・`ui/step_editor.py`

| ファイル | 許可デバイス |
|---|---|
| `dsl/validator.py` `_VALID_UNITS["start_logging"]["devices"]` | `pace5000, lakeshore, radicon` |
| `pre_validator.py` `_LOGGING_DEVICE_NAMES` | `pace5000, lakeshore` のみ |
| `step_editor.py` `_LOG_DEVICES` | `pace5000, lakeshore, stage` |

- `step_editor.py` に `"stage"` があるが SPEC 外・DSL バリデータにも存在しない。
  UI で選択 → pre_validator が「unknown device」警告を出す。
- DSL バリデータは `"radicon"` を許可しているが、
  pre_validator には連携チェックがない。

**修正方針：** 3箇所の許可リストを SPEC に合わせて統一する。
SPEC の `start_logging(devices=["pace5000", "lakeshore"], ...)` の例から
許可セットは `{"pace5000", "lakeshore"}` が基本。
`"radicon"` を追加するなら pre_validator にもチェックを追加する。
`"stage"` は除去する。

---

### 低優先度（混乱を招くが動作自体には影響しない）

#### BUG-6: `dsl_editor.py` プレースホルダーが GPa を使用している

**ファイル：** `ui/dsl_editor.py:79`

プレースホルダーテキストのコメント例に `unit="GPa"` / `rate_unit="GPa/min"` が含まれているが、
PACE5000 バックエンドは GPa 非対応（SPEC 明記）であり、DSL バリデータも GPa を拒否する。
ユーザーがそのままコピーすると validation エラーになる。
`"GPa"` → `"MPa"` に修正する。

---

#### BUG-7: `pre_validator.py` で `lakeshore.is_connected()` をメソッド呼び出ししている

**ファイル：** `validator/pre_validator.py:187`

```python
if ctx.lakeshore is None or not ctx.lakeshore.is_connected():
```

SPEC では `lakeshore_backend.is_connected`（括弧なし）と記述されており
property の可能性がある。実際の `LakeShore335Backend` 実装を確認し、
method なら `is_connected()`、property なら `is_connected` に合わせること。
誤りの場合 `TypeError: 'bool' object is not callable` が発生する。
