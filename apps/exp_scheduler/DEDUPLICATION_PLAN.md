# Experimental Scheduler — 重複実装削減 計画

このファイルは 2026-07-16 に実施したコードレビュー（`apps/exp_scheduler/` と他の `apps/` 配下アプリとの重複実装調査）の結果をまとめ、修正タスクに分解したものである。
各タスクの節はそのまま Claude Code への作業プロンプトとして使える形にしてある。着手前に本ファイルの該当タスクを渡すこと。

**前提となる観察：** PACE5000 まわりは模範的である。`apps/PACE5000/pace5000_backend.py` の
`set_pressure_with_ramp()` / `wait_for_pressure()` が唯一の実装で、`apps/PACE5000/pace5000_app.py`
（UI）・`apps/PACE5000/pace5000_api.py`（HTTP API）・`apps/exp_scheduler/runner.py`
（`_do_set_pressure`/`_do_wait_pressure`）はすべてこれを呼ぶだけ。`runner.py:704-708` にも
「再実装するな」という注意コメントがある。本計画の各タスクは、この形に近づけることを目標とする。

**注意：** 以下の行番号はレビュー時点（2026-07-16, working tree）のものであり、他の変更で
ずれる可能性がある。着手時に該当箇所を検索し直すこと。

---

## タスク一覧

| # | タイトル | 優先度 | 主要ファイル |
|---|---------|--------|-------------|
| D-1 | ✅ [カメラ／オートフォーカス共有化](#d-1-カメラオートフォーカス共有化) | 高 | `runner.py`, `apps/interactive_camera/autofocus.py`, `interactive_camera.py` |
| D-2 | ✅ [ステージ待機処理・Ch8/9 順序の共有化](#d-2-ステージ待機処理ch89-順序の共有化) | 高 | `runner.py`, `actions.py`, `stage_fpd_scope/fpd_scope_stg_controller_ui.py`, `utils/stage/control_stage.py` |
| D-3 | ✅ [XRD 露光中 Ch11 揺動の仕様整合](#d-3-xrd-露光中-ch11-揺動の仕様整合) | 中 | `runner.py`, `dac_oscillation/dac_oscillation_app.py` |
| D-4 | [xrd_scan の image_utils 未使用の是正](#d-4-xrd_scan-の-image_utils-未使用の是正) | 中 | `xrd_scan/xrd_scan_backend.py`, `Rad_icon_2022/image_utils.py` |
| D-5 | [LakeShore バックエンドへの先回り共有メソッド追加](#d-5-lakeshore-バックエンドへの先回り共有メソッド追加) | 低 | `LakeShore335/lakeshore335_backend.py`, `lakeshore335_app.py`, `runner.py` |

**推奨着手順：** D-1 → D-2 →（D-3, D-4 は独立に並行可）→ D-5。D-1, D-2 は既存の共有実装を
呼ぶだけで済む箇所が多く、費用対効果が高い。

---

## D-1: カメラ／オートフォーカス共有化

### 背景

`apps/interactive_camera/autofocus.py` は Qt 非依存の `AutoFocus` クラスとしてオートフォーカス
処理を切り出し済みで、`interactive_camera.py` 自身が Ch3 用（`self.autofocus`）と Ch7 用
（`self.autofocus_ch7`）の 2 箇所で使い回している。しかし `apps/exp_scheduler/runner.py` は
これを import せず、独自に再実装している。特に `_compute_xy_shift`/`_compute_similarity` は
docstring に **"Port of interactive_camera._compute_xy_shift"**、`_follow_loop` は
**"Ported from interactive_camera._follow_task"** と明記されている通り、意図的な移植＝重複である。

### 対応が必要な箇所

| runner.py 側（重複実装） | 元の共有実装 |
|---|---|
| `_gaussian` (`runner.py:26-27`) | `autofocus.py:13-14` と一字一句同一 |
| `_af_find_best_pos` (`runner.py:30-63`) | `AutoFocus._find_best_position` (`autofocus.py:93-171`) の縮小コピー |
| `_do_follow_autofocus` (`runner.py:1489-1609`) | `AutoFocus.perform_autofocus()` (`autofocus.py:182-269`) と同じスキャン→測定→フィット→移動ループ |
| `_compute_xy_shift`/`_compute_similarity` (`runner.py:1613-1635`) | `interactive_camera.py:2029-2047` と同一 |
| `_follow_loop` (`runner.py:1328-1487`) | `interactive_camera.py:2512` `_follow_task` の移植 |

### 読むべき既存ファイル

- `apps/interactive_camera/autofocus.py`（全体。`AutoFocus` クラスの公開インターフェース）
- `apps/interactive_camera/interactive_camera.py`（`self.autofocus`/`self.autofocus_ch7` の使い方：390-395 行、1461-1532 行付近。`_compute_xy_shift`/`_compute_similarity`：2029-2047 行。`_follow_task`：2512 行〜）
- `apps/exp_scheduler/runner.py`（`_af_find_best_pos`, `_do_follow_autofocus`, `_compute_xy_shift`, `_compute_similarity`, `_follow_loop`）

### 作業内容

1. **オートフォーカス部分**：`runner.py` の `_gaussian`／`_af_find_best_pos` を削除し、
   `apps.interactive_camera.autofocus.AutoFocus` を直接 import して使う。
   - `AutoFocus` は `controller`/`cap` を渡すコンストラクタなので、`_do_follow_autofocus` の
     呼び出し元で `AutoFocus(ctx.controller, <frame取得用の何か>, ...)` を都度生成するか、
     `SequenceRunner.__init__` で一つ保持する形にするか設計判断が必要
       （`AutoFocus` は `self.cap.read()` を直接呼ぶ前提のため、`runner.py` の
       `_get_camera_frame()` 経由のフレーム取得と整合させる必要がある — ここは
       `AutoFocus` 側に「フレーム取得コールバック」を注入できるようにする小改修が
       必要になる可能性が高い。設計を変える場合は着手前にユーザーに確認すること）
   - `perform_autofocus()` は内部でスレッドを起動し `completion_callback` で結果を返す
     非同期 API。`_do_follow_autofocus` は同期的に完了を待つ必要があるため、
     `threading.Event` で待ち合わせるラッパーを書くか、`AutoFocus` に同期版メソッドを
     追加するか検討する。
2. **追従ループ部分**：`_compute_xy_shift`/`_compute_similarity`/追従ループ本体は
   `interactive_camera.py` 側にも独立モジュールが無い（`MainWindow` にベタ書き）。
   まず両者から呼べる非 Qt モジュール（例：`apps/interactive_camera/sample_tracking.py`）に
   `_compute_xy_shift`/`_compute_similarity` を抽出し、`interactive_camera.py` と
   `runner.py` の両方がそこから import する形にする。
   - `_follow_loop`/`_follow_task` 本体（XY 補正のリトライループ・グローバル制限
     チェックなど）は `runner.py` 版が `GlobalFollowSettings`/`GlobalLimits` で拡張されており、
     完全に一本化するには `interactive_camera.py` 側にも同じ拡張が必要になる。
     まずは `_compute_xy_shift`/`_compute_similarity` の共有化だけを行い、
     ループ本体の統合は別タスクとして切り出すことを推奨（無理に一本化すると
     `interactive_camera.py` 単体アプリの独立実行性を壊すおそれがあるため）。

### 注意点

- `AutoFocus` は `print()` でログを出す設計（Qt シグナル経由ではない）。`runner.py` は
  `self._logger.log_ops(...)`／`self.progress_updated.emit(...)` でログを統一しているため、
  `AutoFocus` 側にログコールバックを注入できるようにするか、`runner.py` 側で `print` を
  リダイレクトするかの判断が必要。
- `interactive_camera.py` は標準実行と `--debug`（`PM16CControllerSim`）の両方で動く前提。
  共有モジュール化しても両方から問題なく import できることを確認する。
- 変更後、`apps/interactive_camera/interactive_camera.py` を単体起動して Ch3/Ch7 オートフォーカス、
  Sample Tracking タブの追従動作に regression がないか目視確認する。

---

## D-2: ステージ待機処理・Ch8/9 順序の共有化

### 背景

- `PM16CController.wait_until_stop()`（`utils/stage/control_stage.py:829-865`）という
  「`is_all_motors_stopped()` を 4 回連続確認してから `switch_to_loc()`」を行う共有メソッドが
  既にあり、`apps/stage_simple_all/simple_stage_cont.py:156, 181` はこれを正しく呼んでいる。
  しかし `apps/exp_scheduler/runner.py::_wait_stage_stop`（`runner.py:632-646`）は同じロジックを
  ゼロから再実装している。
- Ch8/Ch9 の移動順序（`MicroscopeOutFpdInAction`/`FpdOutMicroscopeInAction` の
  `to_steps()`、`actions.py:193-208`/`248-263`）が、`apps/stage_fpd_scope/fpd_scope_stg_controller_ui.py`
  の shortcut_1/shortcut_2（939-997 行付近）に独立にハードコードされている。
  さらに `runner.py:1324` のフォールバック値辞書
  `{"det_out": "-40000", "det_in": "1779", "ch8_out": "0", "ch8_in": "281092"}` は
  `fpd_scope_stg_controller_ui.py:78-84` の `_DEFAULT_SETTINGS` の完全なコピペ。

### 作業内容

1. **`_wait_stage_stop` の置き換え（低コスト・即対応可）**：`runner.py:632-646` を削除し、
   呼び出し箇所（`_do_stage`, `_osc_loop` 内の待機, `_return_ch11_to_zero` など）を
   `ctrl.wait_until_stop(motion=self._motion_lease)` の呼び出しに置き換える。
   - `wait_until_stop()` が `_stop_event`（`exp_scheduler` 側の停止要求）を認識できない点に注意。
     `confirm_count` 等のパラメータで途中停止に対応できるか `control_stage.py` の実装を
     確認し、対応できなければ `wait_until_stop` 側に stop コールバック／event を渡せる
     引数を追加するか、現状の停止確認ループを維持しつつ `switch_to_loc` 呼び出し部分だけ
     共通化するかを判断する。
2. **Ch8/Ch9 順序ロジックの共有化**：`actions.py` の `to_steps()` と
   `fpd_scope_stg_controller_ui.py` の shortcut_1/shortcut_2 が両方参照できる共有関数
   （例：`utils/stage/` 配下、または新規 `apps/stage_fpd_scope/move_sequences.py`）に
   「Ch8 OUT→Ch9 IN」「Ch9 OUT→Ch8 IN」の順序決定ロジックと `stage_settings.json` の
   読み込み＋フォールバック値を切り出す。
   - フォールバック値の辞書は 1 箇所だけに定義し、両方がそこから import する。

### 読むべき既存ファイル

- `utils/stage/control_stage.py`（`wait_until_stop()` の実装、`confirm_count` 引数の有無）
- `apps/exp_scheduler/runner.py`（`_wait_stage_stop:632-646`, `_do_stage`, `_osc_loop`, `_return_ch11_to_zero`）
- `apps/exp_scheduler/actions.py`（`MicroscopeOutFpdInAction.to_steps`, `FpdOutMicroscopeInAction.to_steps`）
- `apps/stage_fpd_scope/fpd_scope_stg_controller_ui.py`（shortcut_1/shortcut_2, `_DEFAULT_SETTINGS`）
- `apps/stage_simple_all/simple_stage_cont.py:156, 181`（`wait_until_stop` の正しい呼び出し例）

### 注意点

- `apps/stage_fpd_scope/fpd_scope_stg_controller_ui.py` は `ControllerPoller`
  （QTimer @300ms、`get_cached_is_moving()`）による非ブロッキング方式を使っており、
  `wait_until_stop()`（ブロッキング）とは設計が異なる。GUI スレッドをブロックできない
  という制約があるため、無理に同じ関数へ統合しようとせず、「移動順序のロジックと
  デフォルト値」だけを共有し、「待機方式」はそれぞれの実行コンテキスト
  （バックグラウンドスレッド vs GUI スレッド）に応じた実装のままで良い。
- 本タスク着手前に `CLAUDE.md` の既知バグ（`apps/stage_simple_all/simple_stage_cont.py` と
  `fpd_scope_stg_controller_ui.py` はスタンドアロン実行時に `utils.stage.control_stage` を
  解決できない）に触れないよう、import 方法は変更しないこと。

---

## D-3: XRD 露光中 Ch11 揺動の仕様整合

### 背景

Ch11（回転ステージ）に関係する処理には、目的と実行コンテキストの異なる二種類の XRD 揺動がある。
両者は統合対象ではない。

1. **露光中に A↔B を往復する揺動**：`runner.py::_osc_loop`（`runner.py:1052-1101`）＋
   `_return_ch11_to_zero`（`runner.py:1103-1122`）。`exp_scheduler` の `take_xrd` オプションであり、
   1 枚の露光中に揺動を実行して露光完了後に 0° へ復帰する。これは、手動運用では
   `apps/dac_oscillation/` で揺動を開始してから `apps/Rad_icon_2022/` で測定を始めていた
   二段階の操作を、自動シーケンスでは一つの測定操作として扱えるようにしたものである。
2. **角度ステップごとに XRD 露光する揺動スキャン**：
   `apps/Rad_icon_2022/radicon_backend.py::XrdOscillationWorker`（`radicon_backend.py:529-741`、
   特に `_run_scan`: 609-742）。露光と Ch11 の微小移動をステップごとに同期させる
   single-crystal measurements 用のアルゴリズムであり、
   `apps/single_crystal/single_crystal_app.py:627` から使用する。

`XrdOscillationWorker` は single-crystal measurements 専用であり、現時点の
`exp_scheduler` には同等の測定アクションが存在しない。したがって本タスクでは
`XrdOscillationWorker`、`single_crystal_app.py`、および `Rad_icon_2022` の測定フローを変更しない。

`dac_scan_rot` にある Ch11 の絶対位置移動・待機、および 0° への復帰は、いずれも通常の
`move_ch_absolute()` の呼び出しである。これらだけのために Ch11 専用ヘルパーを増やしても、
重複削減の利益より抽象化・保守の負担が大きい。本タスクの対象には含めない。

### 作業内容

1. **揺動仕様の照合**：`apps/dac_oscillation/dac_oscillation_app.py` の A→B→A、端点での
   ドウェル、速度、停止、および Ch8/Ch11 干渉制約を基準に、`runner.py::_osc_loop` と
   `take_xrd(oscillate=True)` の実行フローを照合する。`exp_scheduler` では露光終了が
   揺動終了条件であり、手動アプリの有限サイクル数は持ち込まない。
2. **必要な差分だけを是正する**：照合の結果、露光中揺動の安全性・停止性・設定値の意味が
   手動揺動と異なる箇所だけを修正する。候補は Ch8/Ch11 制約の事前確認、A と B が同値の場合の
   入力検証、停止時の減速停止とモーションリースの扱いである。ただし、低レベルの
   `move_ch_absolute()`／`move_ch_relative()` や 0° 復帰を共通ヘルパーに抽出しない。
3. **実行モデルは分離したままにする**：`dac_oscillation` は Qt の `QTimer` による
   非ブロッキング状態機械であり、`exp_scheduler` は露光と並行するバックグラウンドループである。
   揺動シーケンス全体を共通クラスへ抽出すると、停止通知、モーションリース、Qt スレッド境界を
   複雑化するため、現時点では行わない。

### 読むべき既存ファイル

- `apps/exp_scheduler/runner.py`（`_osc_loop`, `_return_ch11_to_zero`）
- `apps/exp_scheduler/SPEC.md`（`take_xrd(oscillate=True)` の終了条件）
- `apps/dac_oscillation/dac_oscillation_app.py`（揺動状態機械、Ch8/Ch11 制約、停止処理）
- `utils/stage/control_stage.py`（`move_ch_absolute()`、`request_normal_stop()`、
  モーションリースと移動制約の実装）

### 注意点

- Ch11 は角度軸（`PULSE_SCALE[11]`）であり、スケジューラの設定値は度、コントローラへの
  指令値はパルスである。変換の丸め規則を `dac_oscillation` と一致させること。
- `take_xrd(oscillate=True)` は、露光終了後に減速停止して Ch11 を 0° に戻ることが外部仕様である。
  停止・復帰の順序は変更しない。
- `single_crystal` アプリと `XrdOscillationWorker` は本タスクの対象外である。用途が異なるため、
  これらの呼び出し元・アルゴリズム・測定フローを変更しないこと。

### 実施結果（2026-07-16）

- `take_xrd(oscillate=True)` の実行前に、端点を `PULSE_SCALE[11]` でパルスへ丸めてから
  A/B の相違、ドウェル、速度、および両端点の Ch8/Ch11 移動制約を検証するようにした。
- PreValidator でも、グローバル設定とステップ上書きを解決した後に同じ設定検証と
  シーケンス順序を考慮した Ch11 移動制約検証を行う。揺動を伴う XRD 測定には
  ステージコントローラ接続と全軸停止を要求する。
- `dac_oscillation` の Qt 状態機械、`dac_scan_rot` の単発移動、および
  single-crystal 用 `XrdOscillationWorker` は変更していない。

---

## D-4: xrd_scan の image_utils 未使用の是正

### 背景

`runner.py` は暗電流／欠陥補正・TIFF 保存について、共有モジュール
`apps/Rad_icon_2022/image_utils.py` の `apply_dark_correction`/`parse_defect_file`/
`build_defect_mask`/`save_tiff` を正しく呼んでいる（模範的な側）。
一方 `apps/xrd_scan/xrd_scan_backend.py` は `image_utils` を一切 import しておらず：

- 暗電流補正を `img_f - self._dark`（`xrd_scan_backend.py:236-237`）で独自にインライン実装
- TIFF 保存を `tifffile.imwrite(str(fname), img)`（`xrd_scan_backend.py:233`）で
  メタデータ無しで独自実装
- 欠陥マスク補正機能自体が無い

### 作業内容

1. `xrd_scan_backend.py` の暗電流減算を `image_utils.apply_dark_correction()` の呼び出しに
   置き換える。
2. TIFF 保存を `image_utils.save_tiff()` に置き換え、`runner.py` の `_do_take_xrd` と
   同等のメタデータ（`exposure_ms`, `binning`, `flip_v`/`flip_h`, `dark_corrected` など）を
   付与する。
3. 欠陥マスク補正の追加は本タスクの必須スコープではないが、`runner.py` 側の
   `_load_xrd_defect_mask`（`runner.py:913-935`）を流用できる形にしておくと将来
   `xrd_scan` に同機能を追加する際の重複を避けられる（ユーザーと相談の上、
   スコープに含めるか決定する）。

### 読むべき既存ファイル

- `apps/Rad_icon_2022/image_utils.py`（`apply_dark_correction`, `parse_defect_file`,
  `build_defect_mask`, `save_tiff` のシグネチャ）
- `apps/xrd_scan/xrd_scan_backend.py`（`_dark` の読み込み・使用箇所：187, 233, 236-237, 669, 679, 728, 993 行付近）
- `apps/exp_scheduler/runner.py`（`_do_take_xrd`, `_load_xrd_dark`, `_load_xrd_defect_mask` — 参考実装として）
- `/pyfai-integration` スキル（poni/pyFAI 関連の規約を確認する場合）

### 注意点

- `xrd_scan` は pyFAI 積分結果の物理量（強度・radial）に直結する処理のため、
  暗電流補正の適用タイミング・単位（float32 か float64 か）を変更する際は既存の
  積分結果と数値的に一致することを確認してから置き換える。

---

## D-5: LakeShore バックエンドへの先回り共有メソッド追加

### 背景

`apps/LakeShore335/lakeshore335_backend.py` はプリミティブ（`set_ramp_parameter`,
`get_ramp_parameter`, `set_setpoint`, `get_setpoint`, `get_data`）のみを公開しており、
PACE5000 の `set_pressure_with_ramp`/`wait_for_pressure` に相当する高レベルメソッドが無い。
`lakeshore335_app.py` の UI（`_apply_ramp:357-370`, `_apply_setpoint:343-355`）は
それぞれ独立した単発 Apply ボタンで、ランプ検証→setpoint 送信のシーケンス処理を
持たないため、**現状は `runner.py` にしか実装がなく重複はしていない**。
ただし `runner.py::_do_set_temperature`/`_do_wait_temperature`（`runner.py:779-847`）が
持つロジックは、いずれ別の呼び出し元が必要になった際に重複が生まれる形をしている。

### 作業内容

1. `LakeShore335Backend` に `set_temperature_with_ramp(value_k, ramp_rate, ...)` と
   `wait_for_temperature(tol_k, stop_event=None, on_update=None)` を追加する
   （`Pace5000Backend.set_pressure_with_ramp`/`wait_for_pressure` のシグネチャ・
   コールバック設計を参考にする）。
   - ランプ検証：「3 回連続失敗でエラー」というリトライ上限は `runner.py:791-809` の
     現行仕様を踏襲する。
2. `runner.py::_do_set_temperature`/`_do_wait_temperature` を、追加した
   バックエンドメソッド呼び出しに置き換える。
3. 余裕があれば `lakeshore335_app.py` の Apply ボタンもこの新メソッドを使う形に
   変更できないか検討する（必須ではない — UI 側は単発適用のままで良いという
   判断もあり得るため、ユーザーと相談）。

### 読むべき既存ファイル

- `apps/PACE5000/pace5000_backend.py`（`set_pressure_with_ramp`, `wait_for_pressure` の設計）
- `apps/LakeShore335/lakeshore335_backend.py`（既存プリミティブ）
- `apps/LakeShore335/lakeshore335_app.py`（`_apply_ramp:357-370`, `_apply_setpoint:343-355`）
- `apps/exp_scheduler/runner.py`（`_do_set_temperature:779-815`, `_do_wait_temperature:817-847`）

### 注意点

- 優先度は低（現状バグではない）。D-1〜D-4 が一段落してからで良い。

---

## 備考

- 各タスク完了後、該当アプリを単体起動して regression がないか目視確認すること
  （`python apps/interactive_camera/interactive_camera.py` など、`CLAUDE.md` の
  「Running the app」セクション参照）。
- 実装中に本計画と異なる方が良い設計が見つかった場合は、実装を止めてユーザーに確認してから
  進めること（仮定で進めない）。
- 完了したタスクにはタスク一覧表の該当行に ✅ を付けて更新すること。
