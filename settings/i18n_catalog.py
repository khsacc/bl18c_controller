"""English source string -> Japanese translation catalog.

Keyed by the exact English string passed to ``i18n.tr(...)`` at each call
site. A missing key is not an error — :func:`settings.i18n.tr` silently
falls back to the English source — so when you add or edit a ``tr(...)``
call, add or update the matching entry here too.
"""
from __future__ import annotations

JA: dict[str, str] = {
    # main.py — window / status templates
    "BL-18C Controller Main": "BL-18C 制御アプリ メインウィンドウ",
    "Connecting…": "接続中…",
    "Scanning…": "スキャン中…",
    "● Simulation": "● シミュレーション",
    "● Connected": "● 接続済み",
    "✕ Failed": "✕ 失敗",
    "✕ Not found": "✕ 見つかりません",
    "● Connected  {detail}": "● 接続済み  {detail}",
    "● Connected  (Talk-Only)  {detail}": "● 接続済み  (Talk-Only)  {detail}",
    "● Connected  (Talk-Only)": "● 接続済み  (Talk-Only)",
    "● Connected  {width} × {height} px": "● 接続済み  {width} × {height} px",
    "● Simulation  {width} × {height} px": "● シミュレーション  {width} × {height} px",

    # main.py — menu bar
    "Settings": "設定",
    "Settings…": "設定…",
    "Tools": "ツール",
    "Ruby Finder": "Ruby Finder",
    "Single crystal measurements": "単結晶測定",
    "Sequential Relative Moves": "連続相対値移動",
    "Speed Controller": "速度変更",
    "Convert IPA prm file to poni format": "IPA prm ファイルを poni 形式に変換",

    # main.py — dialogs
    "Connection Error": "接続エラー",
    "Could not connect to stage controller:\n{error}\n\n"
    "Sub-applications will not be able to control the stage.":
        "ステージコントローラーに接続できませんでした:\n{error}\n\n"
        "サブアプリケーションはステージを制御できません。",
    "Rad-icon 2022 Not Connected": "Rad-icon 2022 未接続",
    "To open Single crystal measurements,\n"
    "please connect Rad-icon 2022 first (Hardware Connections checkbox).":
        "単結晶測定を開くには、\n"
        "まず Rad-icon 2022 を接続してください（Hardware Connections チェックボックス）。",
    "Stage Not Connected": "ステージ未接続",
    "To open Single crystal measurements,\n"
    "a connection to the stage controller is required.":
        "単結晶測定を開くには、\n"
        "ステージコントローラーへの接続が必要です。",
    "PACE5000 Connection Error": "PACE5000 接続エラー",
    "Could not connect to PACE5000:\n{error}": "PACE5000 に接続できませんでした:\n{error}",
    "Keithley 2000 Error": "Keithley 2000 エラー",
    "pyvisa is not installed.\nRun: pip install pyvisa":
        "pyvisa がインストールされていません。\n実行してください: pip install pyvisa",
    "Keithley 2000 Connection Error": "Keithley 2000 接続エラー",
    "Could not connect to Keithley 2000:\n{error}": "Keithley 2000 に接続できませんでした:\n{error}",
    "Rad-icon 2022 Connection Error": "Rad-icon 2022 接続エラー",
    "Could not connect to Rad-icon 2022:\n{error}": "Rad-icon 2022 に接続できませんでした:\n{error}",
    "Keithley 2000 Not Connected": "Keithley 2000 未接続",
    "To open Keithley Reader,\n"
    "please connect Keithley 2000 first (Hardware Connections checkbox).":
        "Keithley Reader を開くには、\n"
        "まず Keithley 2000 を接続してください（Hardware Connections チェックボックス）。",
    "Confirm Exit": "終了確認",
    "Are you sure to close all the windows and exit the controller program?":
        "すべてのウィンドウを閉じて制御プログラムを終了してもよろしいですか？",
    "Please wait...": "お待ちください...",

    # main.py — hardware connection checklist
    "Hardware Connections": "ハードウェア接続",
    "Stage Controller  192.168.1.55:7777": "ステージ  192.168.1.55:7777",
    "Rad-icon 2022 FPD Controller": "Rad-icon 2022 FPD",
    "None  (2080 * 2238 px)": "なし  (2080 * 2238 px)",

    # main.py — launcher sections / buttons
    "Language": "言語",
    "Stage Control": "ステージ制御",
    "Scan": "スキャン",
    "Sample Environment": "試料環境",
    "Automation": "自動化",
    "Microscope + FPD stage control": "顕微鏡 + FPD ステージ制御",
    "Interactive camera": "Interactive Camera",
    "Simple controller for all stages": "全パルスステージ制御",
    "DAC stage oscillation": "DAC ステージ揺動",
    "Collimator Scan": "コリメータースキャン",
    "DAC Scan (Normal)": "DAC スキャン（通常）",
    "DAC Scan (Rotation Centre)": "DAC スキャン（回転中心）",
    "DAC Scan (XRD)": "DAC スキャン（XRD）",
    "General 1D Scan": "1D スキャン",
    "General 2D Scan": "2D スキャン",
    "Rad-icon 2022 (FPD) Controller": "Rad-icon 2022（FPD）制御",
    "Experimental Scheduler": "自動実験スケジューラー",

    # apps/Rad_icon_2022/radicon_ui.py — detector / save settings
    "(No image)": "（画像なし）",
    "Detector settings": "検出器設定",
    "None": "なし",
    "{width} × {height} px  (binning: {binning})": "{width} × {height} px  (ビニング: {binning})",
    "Resolution:": "解像度:",
    "Exposure time:": "露光時間:",
    "Vertical": "縦方向",
    "Horizontal": "横方向",
    "Flip:": "フリップ:",
    "Save settings": "保存設定",
    "Browse...": "参照...",
    "Save to:": "保存先:",
    "Filename:": "ファイル名:",
    "Suffix:": "サフィックス:",

    # apps/Rad_icon_2022/radicon_ui.py — acquisition
    "Acquisition": "取込",
    "Live view": "Live表示",
    "Single shot": "単発取込",
    "Continuously captures at the current exposure time without saving, until stopped": "現在の露光時間で撮影を繰り返します。停止するまでファイルには保存されません",
    "Live view running... (not saved)": "Live表示中...（保存されません）",
    "Live view error": "Live表示エラー",
    "Capturing...": "取込中...",
    "Capturing... (max {sec:.0f} s)": "単発取込中... (最大 {sec:.0f} 秒)",
    "Sequential acquisition": "連続取込",
    "Sequential acquisition error": "連続取込エラー",
    "Frames:": "枚数:",
    "Interval [ms]:": "間隔[ms]:",
    "Save individually": "個別保存",
    "Save as stack": "一括保存",
    "Save average": "平均保存",
    "Capturing sequence... (0 / {n})": "連続取込中... (0 / {n})",
    "Capturing sequence... ({current} / {total})": "連続取込中... ({current} / {total})",
    "Individual: {n} files": "個別: {n} ファイル",
    "Stack: {name}": "一括: {name}",
    "Average: {name}": "平均: {name}",
    "Done: ": "完了: ",
    "Done (nothing saved)": "完了 (保存なし)",
    "\n[Warning] ": "\n[警告] ",
    "\n[Warning] {warn}": "\n[警告] {warn}",
    "Please select either individual save or average save": "個別保存・平均保存のいずれかを選択してください",
    "Estimated: {sec:.1f} s": "推定: {sec:.1f} s",
    "Estimated: --": "推定: --",
    "Dark-current correction": "暗電流補正",
    "When checked, subtracts the dark image from acquired images": "チェック時、取込画像から暗電流画像を引き算します",
    "Pixel-defect correction (median)": "画素欠陥補正（メジアン）",
    "Pixel-defect correction:": "画素欠陥補正:",
    "Replace with -1": "-1で置き換える",
    "No defect file selected": "欠陥ファイル未選択",
    "Idle": "待機中",
    "Stop acquisition": "取り込み停止",
    "Stopping...": "停止処理中...",
    "Stopped by user": "ユーザー操作により停止しました",
    "Stopped by user (no frames captured)": "ユーザー操作により停止しました（画像なし）",
    "Stopped by user: ": "ユーザー操作により停止: ",

    # apps/Rad_icon_2022/radicon_ui.py — dark current
    "Dark current": "暗電流",
    "Dark-current acquisition error": "暗電流取得エラー",
    "Load": "読み込み",
    "Load error": "読み込みエラー",
    "Load error: {error}": "読み込みエラー: {error}",
    "Acquire": "取得",
    "Acquiring...": "取得中...",
    "Acquiring... (0 / {n})": "取得中... (0 / {n})",
    "Acquiring... ({current} / {total})": "取得中... ({current} / {total})",
    "Accumulations:": "積算回数:",
    "Apply after saving": "保存後に反映",
    "Current dark current: none": "現在の暗電流: なし",
    "Current dark current: {name}\n[Warning] ": "現在の暗電流: {name}\n[警告] ",
    "Current dark current: {name}{suffix}": "現在の暗電流: {name}{suffix}",
    "unknown": "不明",
    "Exposure mismatch (dark: {dark_ms} ms / current: {cur_ms} ms)":
        "露光時間不一致 (暗電流: {dark_ms} ms / 現在: {cur_ms} ms)",
    "Vertical flip mismatch (dark: {dark_v} / current: {cur_v})":
        "縦フリップ不一致 (暗電流: {dark_v} / 現在: {cur_v})",
    "Horizontal flip mismatch (dark: {dark_h} / current: {cur_h})":
        "横フリップ不一致 (暗電流: {dark_h} / 現在: {cur_h})",
    "Confirm flip setting change": "フリップ設定の変更確認",
    "Change {name} flip to {state}?\n\n"
    "Note: this will break consistency with the existing dark-current image.\n"
    "Please re-acquire the dark current after changing this setting.":
        "{name}フリップを {state} に変更しますか？\n\n"
        "注意: 既存の暗電流画像と整合性が崩れます。\n"
        "設定変更後は暗電流を再取得してください。",

    # apps/Rad_icon_2022/radicon_ui.py — errors / file dialogs
    "Select save folder": "保存先フォルダを選択",
    "Saved: {name}  ({w} × {h} px, max={max})": "保存完了: {name}  ({w} × {h} px, max={max})",
    "Save failed: {name}": "保存失敗: {name}",
    "Error: {msg}": "エラー: {msg}",
    "Capture error": "取込エラー",
    "Input error": "入力エラー",
    "Please enter an integer for the frame count": "枚数に整数を入力してください",
    "Please enter an integer [ms] for the interval": "間隔に整数[ms]を入力してください",
    "Please enter an integer for the accumulation count": "積算回数に整数を入力してください",
    "Select dark-current file": "暗電流ファイルを選択",
    "Could not read the file": "ファイルを読み込めません",
    "A grayscale image is required (shape={shape})": "グレースケール画像が必要です (shape={shape})",
    "Save dark-current image": "暗電流画像を保存",
    "Save cancelled": "保存をキャンセルしました",
    "Save error": "保存エラー",
    "Failed to save: {path}": "保存に失敗しました: {path}",
    "Save failed": "保存失敗",
    "Saved: {name}": "保存完了: {name}",
    "Select pixel-defect file": "画素欠陥ファイルを選択",
    "Defect file load error": "欠陥ファイル読み込みエラー",
    "Defect pixels: {n} px": "欠陥画素: {n} px",
    "Defect mask size does not match ({mask_shape} vs {img_shape})":
        "欠陥マスクのサイズが一致しません ({mask_shape} vs {img_shape})",
    "Dark-current image size does not match ({dark_shape} vs {img_shape})":
        "暗電流画像のサイズが一致しません ({dark_shape} vs {img_shape})",

    # apps/scan2d/free_2d_scan_app.py / free_2d_scan_backend.py — 2D Scan
    "2D Scan": "2D スキャン",
    "Channel Selection": "チャンネル選択",
    "X channel:": "Xチャンネル:",
    "Y channel:": "Yチャンネル:",
    "Ch{ch} (X) Scan": "Ch{ch} (X) スキャン",
    "Ch{ch} (Y) Scan": "Ch{ch} (Y) スキャン",
    "Scan size (µm):": "スキャン範囲 (µm):",
    "Grid points:": "グリッド点数:",
    "Speed": "速度",
    "Settle time after move": "移動後の整定時間",
    "Accumulation": "積算",
    "Reads per point:": "1点あたりの読み取り回数:",
    "Start Scan": "スキャン開始",
    "Stop": "停止",
    "Emergency Stop": "非常停止",
    "Ready": "準備完了",
    "Color map:": "カラーマップ:",
    "Fitting": "フィッティング",
    "Model:": "モデル:",
    "X:  —": "X:  —",
    "Y:  —": "Y:  —",
    "Go to suggested position": "推奨位置へ移動",
    "Transmission Map": "透過率マップ",
    "Intensity": "強度",
    "Ch offset": "チャンネルオフセット",
    "pulses": "パルス",
    "Y Profile": "Yプロファイル",
    "X Profile": "Xプロファイル",
    "Ch{ch} (X) [pulse]": "Ch{ch} (X) [パルス]",
    "Ch{ch} (Y) [pulse]": "Ch{ch} (Y) [パルス]",
    "Ch{ch} (X) [µm from centre]": "Ch{ch} (X) [中心からのµm]",
    "Ch{ch} (Y) [µm from centre]": "Ch{ch} (Y) [中心からのµm]",
    "Ch{ch} (Y) Profile": "Ch{ch} (Y) プロファイル",
    "Ch{ch} (X) Profile": "Ch{ch} (X) プロファイル",
    "Ch{ch_x}: ±{half_x:.1f} pulses, step {step_x:.2f} p\n"
    "Ch{ch_y}: ±{half_y:.1f} pulses, step {step_y:.2f} p":
        "Ch{ch_x}: ±{half_x:.1f} パルス, ステップ {step_x:.2f} p\n"
        "Ch{ch_y}: ±{half_y:.1f} パルス, ステップ {step_y:.2f} p",
    "Error": "エラー",
    "Stage controller not connected.": "ステージコントローラーが接続されていません。",
    "X channel and Y channel must be different.": "XチャンネルとYチャンネルは異なる必要があります。",
    "Cannot read current position:\n{error}": "現在位置を読み取れません:\n{error}",
    "Ch{ch}:  —": "Ch{ch}:  —",
    "Keithley 2000 not connected": "Keithley 2000 未接続",
    "Keithley 2000 is not connected.\n"
    "The scan will record zero intensity for all points.\n\n"
    "Connect the Keithley 2000 from the main window before starting the scan.\n\n"
    "Continue anyway?":
        "Keithley 2000 が接続されていません。\n"
        "スキャンはすべての点でゼロ強度を記録します。\n\n"
        "スキャンを開始する前に、メインウィンドウから Keithley 2000 を接続してください。\n\n"
        "このまま続行しますか？",
    "Scan cancelled.": "スキャンをキャンセルしました。",
    "Starting scan…": "スキャン開始中…",
    "Aborting…": "中断中…",
    "EMERGENCY STOP — AESTP sent.": "非常停止 — AESTP を送信しました。",
    "Scan complete. Running fit…": "スキャン完了。フィッティング実行中…",
    "Scan aborted. Fitting available data…": "スキャン中断。取得済みデータをフィッティング中…",
    "Scan aborted.": "スキャンを中断しました。",
    "No data available for fitting.": "フィッティング用のデータがありません。",
    "Ch{ch}:  abs={abs_pulse} pulses\n"
    "  (rel={rel:+.1f},  {width_kind}={width:.1f} p)":
        "Ch{ch}:  絶対={abs_pulse} パルス\n"
        "  (相対={rel:+.1f},  {width_kind}={width:.1f} p)",
    "Ch{ch}:  fit failed": "Ch{ch}:  フィッティング失敗",
    "Fit complete.": "フィッティング完了。",
    "Saved → {path}  (.json / .npz / .png)": "保存先 → {path}  (.json / .npz / .png)",
    "Moving to suggested position…": "推奨位置へ移動中…",
    "Busy": "使用中",
    "A scan is in progress.": "スキャンが実行中です。",
    "A move is already in progress.": "既に移動が実行中です。",
    "Moving…": "移動中…",
    "Move complete.": "移動完了。",
    "Move failed: {error}": "移動に失敗しました: {error}",
    "Move Error": "移動エラー",
    "Go to this position  (Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} pulses)":
        "この位置へ移動  (Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} パルス)",
    "Moving to Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} pulses…":
        "Ch{ch_x}={x_pulse}, Ch{ch_y}={y_pulse} パルスへ移動中…",
    "Moving to row {row}/{total}…": "行 {row}/{total} へ移動中…",
    "Scanning: {done}/{total} points": "スキャン中: {done}/{total} 点",
    "Returning to start position…": "開始位置へ戻り中…",
    "Scan error: {error}": "スキャンエラー: {error}",

    # apps/ui_stage_controller/fpd_scope_stg_controller_ui.py — Bl18cStageControlApp
    "X-ray": "X線",
    "Detector\n(Ch9)": "検出器\n(Ch9)",
    "Microscope\n(Ch6,7,8)": "顕微鏡\n(Ch6,7,8)",
    "Stage Moving": "ステージ移動中",
    "Stage is moving...": "ステージ移動中...",
    "Stop all stages\n(slow stop)": "全ステージ停止\n（減速停止）",
    "EMERGENCY STOP\n(Immediate halt)": "非常停止\n（即時停止）",
    "Camera unavailable\n{error}": "カメラ利用不可\n{error}",
    "BL-18C FPD + Scope Stage Control": "BL-18C FPD + 顕微鏡ステージ制御",
    "Could not connect to stage controller:\n{error}\n\n"
    "Run in simulation mode instead?":
        "ステージコントローラーに接続できませんでした:\n{error}\n\n"
        "シミュレーションモードで実行しますか？",
    "Detector (Ch9)": "検出器 (Ch9)",
    "OUT": "OUT",
    "IN": "IN",
    "OUT pos.": "OUT位置",
    "IN pos.": "IN位置",
    "Microscope (Ch6, 7, 8)": "顕微鏡 (Ch6, 7, 8)",
    "Move": "移動",
    "Ch6 Target:": "Ch6 目標位置:",
    "Ch7 Target:": "Ch7 目標位置:",
    "Ch8 IN pos.": "Ch8 IN位置",
    "Ch8 OUT pos.": "Ch8 OUT位置",
    "Shortcuts:": "ショートカット:",
    "See the sample by the microscope:\nDetector→OUT and Microscope→IN (High SPD)":
        "顕微鏡でサンプルを見る:\n検出器→OUT、顕微鏡→IN（高速）",
    "Take XRD Data:\nMicroscope→OUT and Detector→IN (High SPD)":
        "XRDデータを取得:\n顕微鏡→OUT、検出器→IN（高速）",
    "Stop all stages (slow stop)": "全ステージ停止（減速停止）",
    "Spd:": "速度:",
    "Ch{ch} is moving.": "Ch{ch} が移動中です。",
    "Shortcut is in operation.": "ショートカット動作中です。",
    "Shortcut Error": "ショートカットエラー",
    "Step1 retry failed for Ch{ch}:\n{error}": "Ch{ch} のStep1リトライに失敗しました:\n{error}",
    "Ch{ch} did not reach target {target:+} even after retry.\n"
    "Current position: {current}.\n"
    "All motors have been stopped.":
        "Ch{ch} はリトライ後も目標位置 {target:+} に到達しませんでした。\n"
        "現在位置: {current}。\n"
        "すべてのモーターを停止しました。",
    "Move Blocked": "移動がブロックされました",
    "Controller Error": "コントローラーエラー",
    "Invalid Value": "不正な値",
    "Ch9 OUT position must be ≤ {boundary:+}.": "Ch9のOUT位置は {boundary:+} 以下である必要があります。",
    "Ch8 OUT position must be ≤ 0.": "Ch8のOUT位置は0以下である必要があります。",
    "Det OUT position must be ≤ {boundary:+}\n"
    "(required for Microscope to move IN safely).":
        "検出器のOUT位置は {boundary:+} 以下である必要があります\n"
        "（顕微鏡を安全にINへ移動するために必要です）。",

    # apps/simple_stage_cont.py — StageControllerApp (11-channel raw control)
    "Ch{ch}:": "Ch{ch}:",
    "--reading--": "--読取中--",
    "Move Abs": "絶対値移動",
    "Relative -": "相対値移動 -",
    "Relative +": "相対値移動 +",
    "High": "High",
    "Medium": "Medium",
    "Low": "Low",
    "Software Limit": "ソフトウェアリミット",
    "Failed to move: {error}": "移動に失敗しました: {error}",
    "Failed to set speed: {error}": "速度設定に失敗しました: {error}",
    "ERROR": "エラー",
    "CAUTION: Stage in Motion": "注意: ステージ移動中",
    "CAUTION: STAGE IS IN MOTION": "注意: ステージが移動中です",
    "EMERGENCY STOP": "非常停止",
    "Normal Stop": "通常停止",
    "🛑 EMERGENCY STOP ACTIVATED 🛑": "🛑 非常停止を実行しました 🛑",
    "Normal Stop sent — decelerating...": "通常停止を送信しました — 減速中...",
    "Stage Motor Controller": "ステージモーターコントローラー",
    "Could not connect: {error}": "接続できませんでした: {error}",
    "BL-18C PM16C Motor Controller - Manual Control": "BL-18C PM16C モーターコントローラー - 手動制御",
    "Motor Controls": "モーター制御",
    "Refresh All Positions": "全位置を更新",
    "Normal Stop All Motors": "全モーター通常停止",
    "Emergency Stop All Motors": "全モーター緊急停止",
    "Paused (window inactive)": "一時停止中（ウィンドウが非アクティブ）",
    "Normal stop sent": "通常停止を送信しました",
    "Error stopping motors: {error}": "モーター停止エラー: {error}",
    "Emergency stop activated": "緊急停止を実行しました",

    # apps/interactive_camera/interactive_camera.py — MainWindow / CalibrationDialog
    "r (px):": "r (px):",
    "Stage Calibration": "ステージキャリブレーション",
    "2×2 matrix calibration — corrects for camera/stage axis tilt:\n\n"
    "Step 1: Click 'Record Origin', then click the reference point on the camera image.\n"
    "Step 2: Move ONLY Ch4 (any amount), click 'Record Ch4-Moved', then click reference point.\n"
    "Step 3: Move ONLY Ch5 (Ch4 unchanged), click 'Record Ch5-Moved', then click reference point.\n"
    "Step 4: Click 'Calculate Calibration', then close this window.":
        "2×2行列キャリブレーション — カメラ／ステージ軸の傾きを補正します:\n\n"
        "手順1: 「Record Origin」をクリックし、カメラ画像上の基準点をクリックしてください。\n"
        "手順2: Ch4のみを移動し（任意量）、「Record Ch4-Moved」をクリックしてから基準点をクリックしてください。\n"
        "手順3: Ch5のみを移動し（Ch4は変更しない）、「Record Ch5-Moved」をクリックしてから基準点をクリックしてください。\n"
        "手順4: 「Calculate Calibration」をクリックし、このウィンドウを閉じてください。",
    "1. Record Origin Motor Position": "1. 原点モーター位置を記録",
    "2. Record Ch4-Moved Position  (move Ch4 only first)": "2. Ch4移動後の位置を記録  (まずCh4のみ移動)",
    "3. Record Ch5-Moved Position  (move Ch5 only, from step 2 position)": "3. Ch5移動後の位置を記録  (手順2の位置からCh5のみ移動)",
    "4. Calculate Calibration": "4. キャリブレーションを計算",
    "Step 1: Click 'Record Origin Motor Position'.": "手順1: 「Record Origin Motor Position」をクリックしてください。",
    "Calibration Procedure:": "キャリブレーション手順:",
    "Could not read motor positions.": "モーター位置を読み取れませんでした。",
    "Origin": "原点",
    "Ch4-moved": "Ch4移動後",
    "Ch5-moved": "Ch5移動後",
    "{label} motor recorded: Ch4={ch4}, Ch5={ch5}.\n"
    "Now, click the reference point on the camera image.":
        "{label}のモーター位置を記録しました: Ch4={ch4}, Ch5={ch5}。\n"
        "次に、カメラ画像上の基準点をクリックしてください。",
    "Origin pixel ({x}, {y}) recorded.\n"
    "Move ONLY Ch4 a noticeable amount, then click 'Record Ch4-Moved Position'.":
        "原点ピクセル ({x}, {y}) を記録しました。\n"
        "Ch4のみをはっきり分かる量だけ移動し、「Record Ch4-Moved Position」をクリックしてください。",
    "Ch4-moved pixel ({x}, {y}) recorded.\n"
    "From current position move ONLY Ch5, then click 'Record Ch5-Moved Position'.":
        "Ch4移動後のピクセル ({x}, {y}) を記録しました。\n"
        "現在位置からCh5のみを移動し、「Record Ch5-Moved Position」をクリックしてください。",
    "Ch5-moved pixel ({x}, {y}) recorded.\n"
    "Click 'Calculate Calibration' to finish.":
        "Ch5移動後のピクセル ({x}, {y}) を記録しました。\n"
        "「Calculate Calibration」をクリックして完了してください。",
    "Complete all three recording steps first.": "先に3つの記録手順をすべて完了してください。",
    "Ch4 barely moved between steps 1 and 2.\nMove Ch4 more and redo step 2.":
        "手順1と2の間でCh4がほとんど移動していません。\nCh4をもっと移動して手順2をやり直してください。",
    "Ch5 barely moved between steps 2 and 3.\nMove Ch5 more and redo step 3.":
        "手順2と3の間でCh5がほとんど移動していません。\nCh5をもっと移動して手順3をやり直してください。",
    "Calibration matrix is singular — Ch4 and Ch5 appear to move in the same direction.\n"
    "Ensure step 2 moves ONLY Ch4 and step 3 moves ONLY Ch5.":
        "キャリブレーション行列が特異です — Ch4とCh5が同じ方向に動いているようです。\n"
        "手順2ではCh4のみ、手順3ではCh5のみを動かすようにしてください。",
    "Calibration complete!\n"
    "Ch4 axis: {ch4:.1f}° from camera X-axis\n"
    "Ch5 axis: {ch5:.1f}° from camera X-axis":
        "キャリブレーション完了！\n"
        "Ch4軸: カメラX軸から{ch4:.1f}°\n"
        "Ch5軸: カメラX軸から{ch5:.1f}°",
    "Calibration": "キャリブレーション",
    "Calibration completed!\n"
    "Ch4 axis: {ch4:.1f}°  Ch5 axis: {ch5:.1f}°\n\n"
    "Close this window to save and apply the resultant calibration.":
        "キャリブレーション完了！\n"
        "Ch4軸: {ch4:.1f}°  Ch5軸: {ch5:.1f}°\n\n"
        "このウィンドウを閉じると、結果のキャリブレーションが保存・適用されます。",
    "Interactive Camera Stage Control": "Interactive Camera ステージ制御",
    "Could not connect to motor controller: {error}": "モーターコントローラーに接続できませんでした: {error}",
    "Camera Error": "カメラエラー",
    "Could not open camera.": "カメラを開けませんでした。",
    "Calibrate": "キャリブレーション",
    "Enable Click-to-Move": "クリックで移動を有効化",
    "Click-to-Move is ON: clicking on the image will move Ch4/5 to centre that position.":
        "クリックで移動: ON — 画像をクリックするとCh4/5がその位置を中心に移動します。",
    "Start Auto-Focus": "オートフォーカス開始",
    "Stop Auto-Focus": "オートフォーカス停止",
    "Auto focusing in progress. Ch3 is moving.": "オートフォーカス実行中。Ch3が移動しています。",
    "Auto focusing in progress. Ch7 is moving.": "オートフォーカス実行中。Ch7が移動しています。",
    "Scan Range (um):": "スキャン範囲 (um):",
    "±scan range in um (1 pulse = 2 um)": "±スキャン範囲 (um) (1パルス = 2 um)",
    "Tenengrad": "Tenengrad",
    "Laplacian": "Laplacian",
    "Sharpness metric:\n"
    "Tenengrad — mean squared Sobel gradient (default)\n"
    "Laplacian — variance of Laplacian":
        "鮮鋭度指標:\n"
        "Tenengrad — Sobel勾配の二乗平均（既定）\n"
        "Laplacian — ラプラシアンの分散",
    "Step (pulse):": "ステップ (パルス):",
    "Ch3 step size per scan position (pulses). 1 pulse = 2 um.": "スキャン1点あたりのCh3ステップ幅（パルス）。1パルス = 2 um。",
    "Frames/pos:": "フレーム数/位置:",
    "Number of frames averaged per scan position.\n"
    "1 = no averaging (default). Higher values reduce noise but slow the scan.":
        "スキャン1点あたりの平均フレーム数。\n"
        "1 = 平均化なし（既定）。値を大きくするとノイズは減りますがスキャンが遅くなります。",
    "Highest sharpness": "鮮鋭度最大",
    "Gaussian fit": "ガウシアンフィット",
    "Snapshot": "スナップショット",
    "Start Recording": "録画開始",
    "Stop Recording": "録画停止",
    "Show/Hide Marks": "マーク表示/非表示",
    "Show/Hide Timestamp": "タイムスタンプ表示/非表示",
    "Circle — drag from centre outward": "円 — 中心から外側へドラッグ",
    "Rectangle — drag corner to corner": "四角形 — 対角にドラッグ",
    "Line — click two points (hold Shift for H/V/45°)": "直線 — 2点をクリック（Shiftで水平/垂直/45°に固定）",
    "Cross/Crosshair — drag from centre outward": "十字/クロスヘア — 中心から外側へドラッグ",
    "Laser position: ({x}, {y})": "レーザー位置: ({x}, {y})",
    "Laser position: unregistered": "レーザー位置: 未登録",
    "x-ray beam position: unregistered": "X線ビーム位置: 未登録",
    "Ready.": "準備完了。",
    "Ch4/5 Speed:": "Ch4/5速度:",
    "Stage Control (Relative Move)": "ステージ制御（相対値移動）",
    "Auto-Focus (Ch3)": "オートフォーカス (Ch3)",
    "Speed:": "速度:",
    "Find best by:": "最良点の判定方法:",
    "Annotation": "アノテーション",
    "Draw:": "描画:",
    "Recording": "録画",
    "Interactive Camera": "Interactive Camera",
    "Sample Tracking (Advanced)": "サンプルトラッキング（詳細）",
    "Drag to draw.": "ドラッグして描画。",
    "Click first point.": "1点目をクリック。",
    "Draw [{mode}]: {hint}": "描画 [{mode}]: {hint}",
    "Removed mark #{n}.": "マーク #{n} を削除しました。",
    "Calibration pixel recorded at ({x}, {y}).": "キャリブレーションピクセルを ({x}, {y}) に記録しました。",
    "Centering... Moving Ch4={ch4:+}, Ch5={ch5:+}": "センタリング中... Ch4={ch4:+}, Ch5={ch5:+} を移動",
    "Movement complete. Ready.": "移動完了。準備完了。",
    "Error moving stage.": "ステージ移動エラー。",
    "Line: click second point (hold Shift to constrain angle).": "直線: 2点目をクリック（Shiftで角度を固定）。",
    "Line added.": "直線を追加しました。",
    "Selected {type} #{n}. Drag to move.": "{type} #{n} を選択しました。ドラッグで移動。",
    "{type} added.": "{type} を追加しました。",
    "Normal auto focus (Ch3)": "通常オートフォーカス (Ch3)",
    "Execute Auto Focus": "オートフォーカス実行",
    "Execute Auto-Focus inside this circle": "この円内でオートフォーカス実行",
    "Auto Focus by Ch7": "Ch7によるオートフォーカス",
    "Execute Auto-focus by scanning Ch7": "Ch7をスキャンしてオートフォーカス実行",
    "Execute Auto-focus inside this circle by scanning Ch7": "この円内でCh7をスキャンしてオートフォーカス実行",
    "Mark": "マーク",
    "Remove this mark ({name} #{n})": "このマークを削除 ({name} #{n})",
    "Line Thickness": "線の太さ",
    "Thin": "細",
    "Regular": "標準",
    "Bold": "太",
    "Move this position to:": "この位置へ移動:",
    "Laser spot": "レーザースポット",
    "X-ray beam": "X線ビーム",
    "Centre of the image": "画像の中心",
    "Remember this position as:": "この位置を記憶:",
    "Laser spot position": "レーザースポット位置",
    "X-ray beam position": "X線ビーム位置",
    "Remember the centre of this circle as the x-ray beam position": "この円の中心をX線ビーム位置として記憶",
    "Calibration dialog opened. Record motor positions and click on the video image.":
        "キャリブレーションダイアログを開きました。モーター位置を記録し、映像画像をクリックしてください。",
    "Could not switch to remote mode: {error}": "リモートモードへの切替に失敗しました: {error}",
    "Calibration completed. Click-to-move is now available.": "キャリブレーション完了。クリックで移動が利用可能になりました。",
    "Calibration cancelled or incomplete.": "キャリブレーションがキャンセルまたは未完了です。",
    "Centring... Ch4={ch4:+}, Ch5={ch5:+}": "センタリング中... Ch4={ch4:+}, Ch5={ch5:+}",
    "Centring complete.": "センタリング完了。",
    "Error centring: {error}": "センタリングエラー: {error}",
    "Laser spot set to pixel ({x}, {y}).": "レーザースポットをピクセル ({x}, {y}) に設定しました。",
    "Moving to laser spot... Ch4={ch4:+}, Ch5={ch5:+}": "レーザースポットへ移動中... Ch4={ch4:+}, Ch5={ch5:+}",
    "Moved to laser spot position.": "レーザースポット位置へ移動しました。",
    "Error moving to laser spot: {error}": "レーザースポットへの移動エラー: {error}",
    "x-ray beam position set to pixel ({x}, {y}).": "X線ビーム位置をピクセル ({x}, {y}) に設定しました。",
    "Moving to x-ray beam position... Ch4={ch4:+}, Ch5={ch5:+}": "X線ビーム位置へ移動中... Ch4={ch4:+}, Ch5={ch5:+}",
    "Moved to x-ray beam position.": "X線ビーム位置へ移動しました。",
    "Error moving to x-ray beam position: {error}": "X線ビーム位置への移動エラー: {error}",
    "Move in progress, please wait.": "移動中です。お待ちください。",
    "Moving Ch{ch} {diff:+} pulses...": "Ch{ch} を {diff:+} パルス移動中...",
    "Ch{ch} moved {diff:+} pulses. Ready.": "Ch{ch} を {diff:+} パルス移動しました。準備完了。",
    "enabled": "有効",
    "disabled": "無効",
    "Click-to-move {state}.": "クリックで移動: {state}。",
    "Auto-Focus with ROI: circle center=({cx}, {cy}), r={r}": "ROI付きオートフォーカス: 円の中心=({cx}, {cy}), r={r}",
    "Warning": "警告",
    "Stage is currently moving. Please wait.": "ステージが現在移動中です。お待ちください。",
    "Auto Focus": "オートフォーカス",
    "Auto-focus is already running.": "オートフォーカスは既に実行中です。",
    "Auto-focus started... (scan range: ±{um} um)": "オートフォーカス開始... (スキャン範囲: ±{um} um)",
    "Failed to start auto-focus.": "オートフォーカスの開始に失敗しました。",
    "Failed to start auto-focus: {error}": "オートフォーカスの開始に失敗しました: {error}",
    "Auto-Focus (Ch7) with ROI: circle center=({cx}, {cy}), r={r}": "ROI付きオートフォーカス (Ch7): 円の中心=({cx}, {cy}), r={r}",
    "Scan Ch7 by ±{range} µm. Are you sure?": "Ch7を ±{range} µm スキャンします。よろしいですか？",
    "Auto-focus (Ch7) started... (±{range} µm, step {step} µm)": "オートフォーカス (Ch7) 開始... (±{range} µm, ステップ {step} µm)",
    "Failed to start auto-focus (Ch7).": "オートフォーカス (Ch7) の開始に失敗しました。",
    "Failed to start auto-focus (Ch7): {error}": "オートフォーカス (Ch7) の開始に失敗しました: {error}",
    "Auto-focus stopped.": "オートフォーカスを停止しました。",
    "No auto-focus operation was running.": "実行中のオートフォーカスはありませんでした。",
    "Show all marks set to {value}.": "すべてのマークの表示を {value} に設定しました。",
    "shown": "表示",
    "hidden": "非表示",
    "Timestamp {state}.": "タイムスタンプ: {state}。",
    "No frame available.": "利用可能なフレームがありません。",
    "Save Snapshot": "スナップショットを保存",
    "Snapshot saved: {name}": "スナップショット保存: {name}",
    "Could not start video recording.": "録画を開始できませんでした。",
    "Recording...": "録画中...",
    "Save Video": "動画を保存",
    "Video saved: {name}": "動画保存: {name}",
    "Recording discarded.": "録画を破棄しました。",
    "Error: Could not read frame.": "エラー: フレームを読み取れませんでした。",
    "FOLLOWING": "追従中",
    "Click-to-move enabled": "クリックで移動: 有効",
    "Calibration mode": "キャリブレーションモード",
    "Auto-focusing...": "オートフォーカス中...",
    "Stage moving...": "ステージ移動中...",
    "Draw [{mode}]": "描画 [{mode}]",
    "REC": "REC",
    "Take Reference Photo": "参照写真を撮影",
    "No reference photo taken.": "参照写真が未撮影です。",
    "Log directory:": "ログディレクトリ:",
    "Directory where tracking_log_from_{timestamp}.csv will be saved.":
        "tracking_log_from_{timestamp}.csv の保存先ディレクトリ。",
    "Save image after every tracking attempt (when similarity threshold is met)":
        "トラッキング試行のたびに画像を保存する（類似度しきい値を満たした場合）",
    "Start Sample Position Tracking by moving Ch3,4,5": "Ch3,4,5を動かしてサンプル位置トラッキングを開始",
    "Stop Tracking": "トラッキング停止",
    "Sample position tracking in progress. Do not move the stages manually.":
        "サンプル位置トラッキング実行中。ステージを手動で動かさないでください。",
    "Interval (min):": "間隔 (分):",
    "Minimum similarity to be satisfied (0–1, 1 is the perfect match with the reference):":
        "満たすべき最小類似度 (0～1、1は参照画像と完全一致):",
    "Normalized cross-correlation similarity (0–1).\n"
    "1.0 = perfect match with reference.\n"
    "If similarity after correction is below this value,\n"
    "XY re-correction is attempted immediately (up to 3 retries).":
        "正規化相互相関類似度 (0～1)。\n"
        "1.0 = 参照画像と完全一致。\n"
        "補正後の類似度がこの値を下回る場合、\n"
        "直ちにXY再補正を試みます（最大3回リトライ）。",
    "Auto-Focus Settings (for Z-correction during tracking)": "オートフォーカス設定（トラッキング中のZ補正用）",
    "Per-attempt movement limit (mm)": "1回あたりの移動制限 (mm)",
    "Total movement limits from start position (mm)": "開始位置からの累計移動制限 (mm)",
    "Min (-)": "最小 (-)",
    "Max (+)": "最大 (+)",
    "Select Log Directory": "ログディレクトリを選択",
    "Please take a reference photo first.": "先に参照写真を撮影してください。",
    "Could not read motor positions: {error}": "モーター位置を読み取れませんでした: {error}",
    "Reference: {name}": "参照: {name}",
    "Reference photo saved: {name}": "参照写真を保存しました: {name}",
    "CSV log: {path}": "CSVログ: {path}",
    "Images dir: {path}": "画像ディレクトリ: {path}",
    "Warning: could not create images dir: {error}": "警告: 画像ディレクトリを作成できませんでした: {error}",
    "Warning: could not open CSV: {error}": "警告: CSVを開けませんでした: {error}",
    "Tracking started. Origin Ch3={ch3}, Ch4={ch4}, Ch5={ch5}": "トラッキング開始。原点 Ch3={ch3}, Ch4={ch4}, Ch5={ch5}",
    "Tracking automatically stopped ({reason}).": "トラッキングが自動的に停止しました ({reason})。",
    "Tracking stopped.": "トラッキングを停止しました。",
    "{prefix} "
    "Start: Ch3={s3}, Ch4={s4}, Ch5={s5} [pulse] | "
    "Total movement: ΔCh3={d3:+d}, ΔCh4={d4:+d}, ΔCh5={d5:+d} [pulse] | "
    "End: Ch3={e3}, Ch4={e4}, Ch5={e5} [pulse]":
        "{prefix} "
        "開始: Ch3={s3}, Ch4={s4}, Ch5={s5} [パルス] | "
        "累計移動: ΔCh3={d3:+d}, ΔCh4={d4:+d}, ΔCh5={d5:+d} [パルス] | "
        "終了: Ch3={e3}, Ch4={e4}, Ch5={e5} [パルス]",
    "Saving plots...": "プロットを保存中...",
    "Warning: matplotlib not installed — skipping plots.": "警告: matplotlibがインストールされていません — プロットをスキップします。",
    "No data to plot.": "プロットするデータがありません。",
    "Plots saved → {dir} ({stem}_Ch3/4/5.png)": "プロット保存 → {dir} ({stem}_Ch3/4/5.png)",
    "Warning: could not save plots: {error}": "警告: プロットを保存できませんでした: {error}",
    "Similarity: {sim:.3f}": "類似度: {sim:.3f}",
    "Similarity below threshold ({threshold:.2f}) — re-correcting XY (attempt {attempt}/{max_retries})":
        "類似度がしきい値 ({threshold:.2f}) を下回っています — XY再補正中 (試行 {attempt}/{max_retries})",
    "Image saved: {name}": "画像保存: {name}",
    "Warning: could not save image: {error}": "警告: 画像を保存できませんでした: {error}",
    "Ch{ch} min ({val:.3f} mm)": "Ch{ch} 最小 ({val:.3f} mm)",
    "Ch{ch} max ({val:.3f} mm)": "Ch{ch} 最大 ({val:.3f} mm)",
    "Total limit exceeded: {hits}": "累計制限を超過: {hits}",
    "ΔCh3={d3:+d}, ΔCh4={d4:+d}, ΔCh5={d5:+d} [pulse] | Total: Ch3={t3:+d}, Ch4={t4:+d}, Ch5={t5:+d}":
        "ΔCh3={d3:+d}, ΔCh4={d4:+d}, ΔCh5={d5:+d} [パルス] | 累計: Ch3={t3:+d}, Ch4={t4:+d}, Ch5={t5:+d}",
    "Autofocus data saved: {name}.csv": "オートフォーカスデータを保存しました: {name}.csv",

    # apps/PACE5000/pace5000_ui_main.py / pace5000_app.py — Pace5000Window
    "PaceMaker:Druck PACE5000 Controller": "Druck PACE5000 制御「PaceMaker」",
    "Connection": "接続",
    "TCP (Ethernet)": "TCP（イーサネット）",
    "Serial (RS232C)": "シリアル（RS232C）",
    "IP Address:": "IPアドレス:",
    "Port:": "ポート:",
    "COM Port:": "COMポート:",
    "Baud:": "ボーレート:",
    "Connect": "接続",
    "Disconnect": "切断",
    "Status: Disconnected": "状態: 未接続",
    "Manual Control": "手動制御",
    "Scheduled Control": "スケジュール制御",
    "Live Pressure": "現在の圧力",
    "Current Pressure:  ---  {unit}": "現在の圧力:  ---  {unit}",
    "−ve source:  ---    +ve source:  ---": "−ve source:  ---    +ve source:  ---",
    "Pressure vs Time": "圧力 vs 時間",
    "Pressure ({unit})": "圧力 ({unit})",
    "Time": "時間",
    "Display window: show only the latest N seconds": "表示ウィンドウ: 直近N秒のみ表示",
    "Window:": "ウィンドウ:",
    "sec": "秒",
    "Clear Graph": "グラフをクリア",
    "Control Parameters [Press Enter to apply changes]": "制御パラメータ【Enterキーで変更を反映】",
    "Measure": "計測",
    "Control": "制御",
    "Enter target pressure and press ENTER": "目標圧力を入力してENTERを押してください",
    "Press Enter to submit target pressure safely": "Enterキーで目標圧力を安全に送信します",
    "Enter rate and press ENTER": "変化速度を入力してENTERを押してください",
    "Confirm before apply": "適用前に確認する",
    "Mode:": "モード:",
    "Pressure Unit:": "圧力単位:",
    "Target Pressure:": "目標圧力:",
    "Slew Rate:": "変化速度:",
    "Relative Pressure Change": "相対圧力変化",
    "step": "ステップ",
    "Data Logging": "データロギング",
    "● Start Logging": "● ロギング開始",
    "■ Stop / Save Log": "■ 停止 / ログ保存",
    "Log: Stopped": "ログ: 停止中",
    "Records: {n}": "記録数: {n}",
    "Interval:": "間隔:",
    "Add / Edit Schedule Item": "スケジュール項目の追加/編集",
    "Item Type:": "項目タイプ:",
    "Wait": "待機",
    "Change Pressure": "圧力変更",
    "seconds": "秒",
    "Duration (s):": "時間 (秒):",
    "value": "値",
    "Pressure:": "圧力:",
    "Rate:": "変化速度:",
    "ℹ A Change Pressure step monitors automatically until the target pressure is reached.\n"
    " You do not need to include settling time in the following Wait step.":
        "ℹ 「圧力変更」ステップは目標圧力に到達するまで自動的に監視します。\n"
        " 続く「待機」ステップに整定時間を含める必要はありません。",
    "✕ Cancel Edit": "✕ 編集をキャンセル",
    "＋ Add to Schedule": "＋ スケジュールに追加",
    "✎ Update Item {n}": "✎ 項目 {n} を更新",
    "Schedule": "スケジュール",
    "Save Schedule...": "スケジュールを保存...",
    "Load Schedule...": "スケジュールを読み込み...",
    "▲ Up": "▲ 上へ",
    "▼ Down": "▼ 下へ",
    "✎ Edit": "✎ 編集",
    "✕ Delete": "✕ 削除",
    "Logging & Execution": "ロギングと実行",
    "Save to:": "保存先:",
    "Log save folder (leave blank to choose via dialog on completion)":
        "ログ保存フォルダ（空欄の場合は完了時にダイアログで選択）",
    "Browse...": "参照...",
    "▶ Start Schedule": "▶ スケジュール開始",
    "■ Stop": "■ 停止",
    "Status: Ready": "状態: 準備完了",
    "Scheduled Control — Live Pressure": "スケジュール制御 — 現在の圧力",
    "Elapsed Time": "経過時間",
    "Status: Stopped": "状態: 停止",
    "Status: Complete ✓": "状態: 完了 ✓",
    "Running [{n}/{total}]: Wait {duration:.1f} s": "実行中 [{n}/{total}]: 待機 {duration:.1f} 秒",
    "Running [{n}/{total}]: Pressure → {target} (monitoring...)":
        "実行中 [{n}/{total}]: 圧力 → {target} (監視中...)",
    "Running [{n}/{total}]: Wait — {remaining:.1f} s remaining":
        "実行中 [{n}/{total}]: 待機 — 残り {remaining:.1f} 秒",
    "  ⚠ ETA exceeded by {delay_min:.1f} min": "  ⚠ 予定到達時刻を {delay_min:.1f} 分超過",
    "⚠  Pressure has not reached target {target} — "
    "{delay_min:.1f} min past the estimated arrival time.\n"
    "The sequence continues monitoring. Press Stop to abort.":
        "⚠  圧力が目標値 {target} に到達していません — "
        "推定到達時刻を {delay_min:.1f} 分超過しています。\n"
        "シーケンスは監視を継続します。中止するには停止を押してください。",
    "Running [{n}/{total}]: Pressure → {target}  (current: {current}){warning}":
        "実行中 [{n}/{total}]: 圧力 → {target}  (現在: {current}){warning}",
    "Error": "エラー",
    "Please enter an IP Address.": "IPアドレスを入力してください。",
    "Port must be an integer.": "ポート番号は整数で入力してください。",
    "Please enter a COM port (e.g. COM1).": "COMポートを入力してください（例: COM1）。",
    "Baud rate must be an integer.": "ボーレートは整数で入力してください。",
    "Status: Connecting...": "状態: 接続中...",
    "Status: Connected": "状態: 接続済み",
    "Current Pressure:  {value:.4f}  {unit}": "現在の圧力:  {value:.4f}  {unit}",
    "−ve source:  {negative:.4f}  {unit}    +ve source:  {positive:.4f}  {unit}":
        "−ve source:  {negative:.4f}  {unit}    +ve source:  {positive:.4f}  {unit}",
    "Save Log Data": "ログデータを保存",
    "File Error": "ファイルエラー",
    "Cannot open log file.\n{error}": "ログファイルを開けません。\n{error}",
    "Log: Recording ●": "ログ: 記録中 ●",
    "Logging Stopped": "ロギングを停止しました",
    "Saved {n} records.\n\n{path}": "{n} 件のレコードを保存しました。\n\n{path}",
    "Write Error": "書き込みエラー",
    "Failed to write log.\n{error}": "ログの書き込みに失敗しました。\n{error}",
    "Failed to write schedule log.\n{error}": "スケジュールログの書き込みに失敗しました。\n{error}",
    "Device not connected!": "デバイスが接続されていません！",
    "Invalid target pressure. Numbers only.": "目標圧力が不正です。数値のみ入力してください。",
    "Target Exceeds +ve Source": "目標値が+ve sourceを超過",
    "Set value ({val:.4g} {unit}) exceeds +ve source pressure "
    "({source:.4g} {unit}).\n"
    "Target has not been updated.":
        "設定値 ({val:.4g} {unit}) が +ve source 圧力 "
        "({source:.4g} {unit}) を超過しています。\n"
        "目標値は更新されていません。",
    "Go to {val} {unit} at {rate} {rate_unit}?": "{rate} {rate_unit} で {val} {unit} へ変化させますか？",
    "Confirm": "確認",
    "Success": "成功",
    "Target updated to: {val} {unit}": "目標値を {val} {unit} に更新しました",
    "Invalid rate value. Numbers only.": "変化速度が不正です。数値のみ入力してください。",
    "Rate updated to: {val} {unit}": "変化速度を {val} {unit} に更新しました",
    "No valid target pressure in the input field.": "入力欄に有効な目標圧力がありません。",
    "Invalid step value.": "ステップ値が不正です。",
    "Please enter a positive number of seconds.": "正の秒数を入力してください。",
    "Please enter valid numbers for pressure and rate.": "圧力と変化速度に有効な数値を入力してください。",
    "{n}.  Wait — {duration:.1f} s": "{n}.  待機 — {duration:.1f} 秒",
    "{n}.  Change Pressure — {pressure:.4g} {pressure_unit}  @  {rate:.4g} {rate_unit}":
        "{n}.  圧力変更 — {pressure:.4g} {pressure_unit}  @  {rate:.4g} {rate_unit}",
    "Save Schedule": "スケジュールを保存",
    "Schedule is empty.": "スケジュールが空です。",
    "Save Error": "保存エラー",
    "Failed to save schedule.\n{error}": "スケジュールの保存に失敗しました。\n{error}",
    "Load Schedule": "スケジュールを読み込み",
    "Cannot load while a schedule is running.": "スケジュール実行中は読み込めません。",
    "Load Error": "読み込みエラー",
    "Failed to load schedule.\n{error}": "スケジュールの読み込みに失敗しました。\n{error}",
    "Select Log Save Folder": "ログ保存フォルダを選択",
    "Device is not connected.": "デバイスが接続されていません。",
    "Save Schedule Log": "スケジュールログを保存",
    "Status: Starting...": "状態: 開始中...",
    "Complete": "完了",
    "Stopped": "停止",
    "Scheduled Control — Live Pressure [{suffix}]": "スケジュール制御 — 現在の圧力 [{suffix}]",
    "Schedule Complete": "スケジュール完了",
    "Schedule Stopped": "スケジュール停止",

    # apps/LakeShore335/lakeshore335_app.py — LakeShore335Window
    "LakeShore 335 Temperature Controller": "LakeShore 335 温度制御",
    "Not connected": "未接続",
    "Display window (s):": "表示ウィンドウ (秒):",
    "Simulation": "シミュレーション",
    "Temperature Monitor": "温度モニター",
    "Elapsed Time (s)": "経過時間 (秒)",
    "Temperature (K)": "温度 (K)",
    "Ch A": "Ch A",
    "Ch B": "Ch B",
    "Setpoint (ramp)": "設定値（ランプ）",
    "Setpoint (K)": "設定値 (K)",
    "Current:": "現在値:",
    "---": "---",
    "New:": "新規:",
    "Apply": "適用",
    "Ramp Rate (K/min)": "ランプレート (K/分)",
    "Enable Ramp": "ランプを有効化",
    "Heater Output": "ヒーター出力",
    "Live Readings": "現在の測定値",
    "Ch A:": "Ch A:",
    "Ch B:": "Ch B:",
    "Setpoint:": "設定値:",
    "Heater:": "ヒーター:",
    "--- K": "--- K",
    "ALL\nOFF": "全て\nOFF",
    "Data Logging": "データロギング",
    "Log directory:": "ログディレクトリ:",
    "Browse…": "参照…",
    "Start Logging": "ロギング開始",
    "Stop Logging": "ロギング停止",
    "Idle": "待機中",
    "Connection Error": "接続エラー",
    "Hardware": "ハードウェア",
    "● Connected ({label})": "● 接続済み ({label})",
    "Input Error": "入力エラー",
    "Setpoint must be a number.": "設定値は数値で入力してください。",
    "{value:.3f} K": "{value:.3f} K",
    "Ramp rate must be a number.": "ランプレートは数値で入力してください。",
    "{rate:.2f} K/min": "{rate:.2f} K/分",
    "Off": "オフ",
    "Turn all heaters OFF?": "すべてのヒーターをOFFにしますか？",
    "Select Log Directory": "ログディレクトリを選択",
    "Please select a log directory first.": "先にログディレクトリを選択してください。",
    "Directory not found:\n{dir}": "ディレクトリが見つかりません:\n{dir}",
    "Could not start logging:\n{error}": "ロギングを開始できませんでした:\n{error}",
    "Logging: {filename}": "ロギング中: {filename}",
    "Idle  (last: {rows} rows saved)": "待機中  (前回: {rows} 行保存)",
    "✕ Error: {msg}": "✕ エラー: {msg}",
    "{base}  ({rows} rows)": "{base}  ({rows} 行)",
    "Not Connected": "未接続",
    "Please connect to the instrument first.": "先に機器に接続してください。",

    # apps/single_crystal/single_crystal_app.py — SingleCrystalWindow
    "Single Crystal Measurements (XRD Oscillation)": "単結晶測定（XRD揺動法）",
    "Oscillation (Ch11)": "揺動 (Ch11)",
    "Min angle:": "最小角度:",
    "Max angle:": "最大角度:",
    "Step:": "ステップ:",
    "Corrections": "補正",
    "No dark-current file selected": "暗電流ファイル未選択",
    "Save directory": "保存先",
    "Min": "最小",
    "Max": "最大",
    "Auto": "自動",
    "⚠ Invalid range or step": "⚠ 範囲またはステップが無効です",
    "\n⚠ Step is too small (min 0.004 deg)": "\n⚠ ステップが小さすぎます（最小 0.004 deg）",
    "{n_steps} steps  |  step size: {step_pulses} pulse\n"
    "Estimated time: {m}m{s:02d}s{warn}":
        "{n_steps} ステップ  |  ステップ幅: {step_pulses} pulse\n"
        "推定時間: {m}分{s:02d}秒{warn}",
    "Please select a grayscale TIFF": "グレースケール TIFF を選択してください",
    "Loaded: {name}\n[Warning] {warning}": "読み込み済み: {name}\n[警告] {warning}",
    "  ({ms} ms)": "  ({ms} ms)",
    "Loaded: {name}{suffix}": "読み込み済み: {name}{suffix}",
    "Max angle must be greater than Min angle.": "Max angle は Min angle より大きくしてください。",
    "Step is too small (min {min_deg} deg = 1 pulse).": "ステップが小さすぎます（最小 {min_deg} deg = 1 pulse）。",
    "Save directory does not exist.": "保存先フォルダが存在しません。",
    "Confirm Scan Start": "スキャン開始確認",
    "The scan will start with the following settings.\n\n"
    "  Angle range:  {min_deg:.3f} → {max_deg:.3f} deg\n"
    "  Step:  {step_deg:.3f} deg  ({step_pulses} pulse)\n"
    "  Frame count: {n_steps}\n"
    "  Exposure time:  {exp_s:.3f} s\n"
    "  Estimated time:  {m}m{s:02d}s\n\n"
    "Ch11 will first move to {min_deg:.3f} deg ({min_pulse} pulse).\n"
    "Continue?":
        "以下の設定でスキャンを開始します。\n\n"
        "  角度範囲:  {min_deg:.3f} → {max_deg:.3f} deg\n"
        "  ステップ:  {step_deg:.3f} deg  ({step_pulses} pulse)\n"
        "  フレーム数: {n_steps}\n"
        "  露光時間:  {exp_s:.3f} s\n"
        "  推定時間:  {m}分{s:02d}秒\n\n"
        "まず Ch11 を {min_deg:.3f} deg ({min_pulse} pulse) に移動します。\n"
        "よろしいですか？",
    "Moving Ch11 to {min_deg:.3f} deg…": "Ch11 を {min_deg:.3f} deg に移動中…",
    "Stopping…": "停止中…",
    "Scanning… {done}/{total}  |  current: {omega:.3f} deg  |  remaining: {m}m{s:02d}s":
        "スキャン中… {done}/{total}  |  現在: {omega:.3f} deg  |  残り: {m}分{s:02d}秒",
    "Capturing  {done}/{total}  frames  |  {omega:.3f} deg  |  remaining {m}m{s:02d}s":
        "撮影中  {done}/{total}  フレーム  |  {omega:.3f} deg  |  残り {m}分{s:02d}秒",
    "Done: {n} frames saved  →  {dir}/": "完了: {n} フレーム保存  →  {dir}/",
    "Done  —  {n} frames saved": "完了  —  {n} フレーム保存",
    "Aborted: {n} frames saved": "中断: {n} フレーム保存済み",
    "Aborted  —  {n} frames saved": "中断  —  {n} フレーム保存済み",
    "An error occurred": "エラーが発生しました",
    "Scan Error": "スキャンエラー",
    "[Warning] Step {n}: exposure overrun {overrun:.2f}s": "[警告] ステップ {n}: 露光時間超過 {overrun:.2f}s",
    "Scan in Progress": "スキャン中",
    "A scan is in progress. Abort and close?": "スキャンが実行中です。中断して閉じますか？",

    # apps/dac_oscillation/dac_oscillation_app.py — DacOscillationWindow
    "DAC Stage Oscillation (Ch11)": "DAC ステージ揺動 (Ch11)",
    "Oscillation Parameters": "揺動パラメータ",
    "Unit:": "単位:",
    "Pulse": "パルス",
    "Degrees": "度",
    "Pos A ({suffix}):": "位置 A ({suffix}):",
    "Pos B ({suffix}):": "位置 B ({suffix}):",
    "Dwell (ms):": "滞在時間 (ms):",
    "Cycles (0=∞):": "サイクル数 (0=∞):",
    "Speed:": "速度:",
    "▶ Start Oscillation": "▶ 揺動開始",
    "Go to θ = 0°": "θ = 0° へ移動",
    "Ch11: — pulse": "Ch11: — パルス",
    "Ch11: {pos:+d} pulse  ({deg:+.3f}°)": "Ch11: {pos:+d} パルス  ({deg:+.3f}°)",
    "← Moving to A  Cycle {cycles}{elapsed}": "← Aへ移動中  サイクル {cycles}{elapsed}",
    "⏸ At A  Cycle {cycles}{elapsed}": "⏸ Aで待機中  サイクル {cycles}{elapsed}",
    "→ Moving to B  Cycle {cycles}{elapsed}": "→ Bへ移動中  サイクル {cycles}{elapsed}",
    "⏸ At B  Cycle {cycles}{elapsed}": "⏸ Bで待機中  サイクル {cycles}{elapsed}",
    "Moving to θ=0°…": "θ=0° へ移動中…",
    "Invalid Input": "不正な入力",
    "All oscillation fields must be valid numbers.": "揺動パラメータは全て有効な数値で入力してください。",
    "Pos A and Pos B must be different.": "位置Aと位置Bは異なる値にしてください。",
    "Dwell and Cycles must be ≥ 0.": "滞在時間とサイクル数は0以上にしてください。",
    "■ Stop Oscillation": "■ 揺動停止",
    "Oscillation Error": "揺動エラー",

    # apps/dac_scan/dac_scan_rot_app.py — DacScanRotWindow
    "Rotation Angles — Ch11": "回転角度 — Ch11",
    "θ list (degrees), comma-separated:": "θリスト（度）、カンマ区切り:",
    "e.g. 0, 10, 20, 30, -6": "例: 0, 10, 20, 30, -6",
    "Ch10 Scan Range  (centred on current position)": "Ch10 スキャン範囲  (現在位置を中心)",
    "Half-range (µm):": "半範囲 (µm):",
    "Step (µm):": "ステップ (µm):",
    "Post-scan actions": "スキャン後の動作",
    "Return to θ=0° after scan": "スキャン後にθ=0°へ戻る",
    "Move Ch10 to centre at θ=0°": "θ=0°でCh10を中心へ移動",
    "Analysis Result": "解析結果",
    "A  [X eccentricity] = —": "A  [X偏心] = —",
    "B  [Y eccentricity] = —": "B  [Y偏心] = —",
    "C  [global offset]  = —": "C  [全体オフセット]  = —",
    "Ch3: —": "Ch3: —",
    "Ch4: —": "Ch4: —",
    "Ch10 compensation: —": "Ch10補正: —",
    "── Suggested Motion ──": "── 推奨移動量 ──",
    "Also compensate Ch10 (= Ch4 move)": "Ch10も補正する (= Ch4移動量)",
    "Return to θ=0° && Centre Ch10": "θ=0°へ戻り && Ch10を中心化",
    "Apply Correction (Move Ch3, Ch4 & Ch10)": "補正を適用 (Ch3, Ch4, Ch10を移動)",
    "Re-run Analysis": "解析を再実行",
    "±{half_pulse} pulse (±{half_um:.0f} µm) / "
    "step {step_pulse} pulse ({step_um:.0f} µm) / "
    "{n} pts":
        "±{half_pulse} パルス (±{half_um:.0f} µm) / "
        "ステップ {step_pulse} パルス ({step_um:.0f} µm) / "
        "{n} 点",
    "(half-range too small)": "(半範囲が小さすぎます)",
    "Ch10 Transmission Scans": "Ch10 透過率スキャン",
    "Ch10 pulse": "Ch10 パルス",
    "Intensity (a.u.)": "強度 (a.u.)",
    "θ vs Aperture Centre": "θ vs アパーチャ中心",
    "θ (degrees)": "θ (度)",
    "Aperture centre (Ch10 pulse)": "アパーチャ中心 (Ch10 パルス)",
    "A·sin+B·cos+C fit": "A·sin+B·cos+C フィット",
    "Cannot parse theta list.\n"
    "Use comma-separated numbers, e.g.: 0, 10, 20, 30, -6":
        "θリストを解析できません。\n"
        "カンマ区切りの数値で入力してください。例: 0, 10, 20, 30, -6",
    "Theta list is empty.": "θリストが空です。",
    "Safety Check": "安全確認",
    "Have you confirmed that the stage will not collide "
    "within the specified rotation range?":
        "指定した回転範囲内でステージが衝突しないことを確認しましたか？",
    "Cannot read Ch10 position:\n{error}": "Ch10位置を読み取れません:\n{error}",
    "Half-range is too small (< 1 pulse).": "半範囲が小さすぎます (< 1 パルス)。",
    "Keithley 2000 is not connected.\n"
    "Please connect the Keithley 2000 from the main window "
    "before starting the scan.":
        "Keithley 2000 が接続されていません。\n"
        "メインウィンドウで Keithley 2000 を接続してからスキャンを開始してください。",
    "Ch10 centre: {center}  range: {start}…{stop}  step: {step}":
        "Ch10 中心: {center}  範囲: {start}…{stop}  ステップ: {step}",
    "Returning to θ=0°…": "θ=0° へ戻り中…",
    "Moving Ch10 to centre ({n} pulse)…": "Ch10 を中心 ({n} パルス) へ移動中…",
    "Scan complete — analyzing…": "スキャン完了 — 解析中...",
    "Scan complete. Running analysis…": "スキャン完了。解析実行中…",
    "Scan aborted": "スキャン中断",
    "Scan aborted ({n} angle(s) completed; need ≥ 3 for rotation fit).":
        "スキャン中断（{n} 角度完了; 回転フィットには3以上必要）。",
    "Ready.": "準備完了。",
    "Scan complete": "スキャン完了",
    "Post-scan move failed: {error}": "スキャン後の移動に失敗しました: {error}",
    "Need ≥ 3 completed angles for rotation fit.": "回転フィットには3角度以上の完了が必要です。",
    "Rotation fit failed: {error}": "回転フィット失敗: {error}",
    "A  [X eccentricity] = {A:+.2f} pulse  ({A_um:+.1f} µm)":
        "A  [X偏心] = {A:+.2f} パルス  ({A_um:+.1f} µm)",
    "B  [Y eccentricity] = {B:+.2f} pulse  ({B_um:+.1f} µm)":
        "B  [Y偏心] = {B:+.2f} パルス  ({B_um:+.1f} µm)",
    "C  [global offset]  = {C:.2f} pulse": "C  [全体オフセット]  = {C:.2f} パルス",
    "Ch3: {pulse:+d} pulse  ({um:+.1f} µm)": "Ch3: {pulse:+d} パルス  ({um:+.1f} µm)",
    "Ch4: {pulse:+d} pulse  ({um:+.1f} µm)": "Ch4: {pulse:+d} パルス  ({um:+.1f} µm)",
    "Ch10: {pulse:+d} pulse  ({um:+.1f} µm)  (= Ch4)": "Ch10: {pulse:+d} パルス  ({um:+.1f} µm)  (= Ch4)",
    "Analysis complete.": "解析完了。",
    "No correction needed": "補正の必要はありません",
    "Both corrections are 0 pulse.": "両方の補正が0パルスです。",
    "\nMove Ch10 by {pulse:+d} pulse  ({um:+.1f} µm)  (compensation)":
        "\nCh10 を {pulse:+d} パルス  ({um:+.1f} µm) 移動  (補正)",
    "Apply Correction": "補正を適用",
    "Move Ch3 by {ch3_pulse:+d} pulse  ({ch3_um:+.1f} µm)\n"
    "Move Ch4 by {ch4_pulse:+d} pulse  ({ch4_um:+.1f} µm)"
    "{ch10_line}\n\nProceed?":
        "Ch3 を {ch3_pulse:+d} パルス  ({ch3_um:+.1f} µm) 移動\n"
        "Ch4 を {ch4_pulse:+d} パルス  ({ch4_um:+.1f} µm) 移動"
        "{ch10_line}\n\n実行しますか？",
    "Applying correction…": "補正適用中…",
    ", Ch10 {pulse:+d} pulse": "、Ch10 {pulse:+d} パルス",
    "Correction applied: Ch3 {ch3:+d} pulse, Ch4 {ch4:+d} pulse{ch10_note}.":
        "補正を適用しました: Ch3 {ch3:+d} パルス, Ch4 {ch4:+d} パルス{ch10_note}。",
    "Move failed: {error}": "移動に失敗しました: {error}",
    "Unapplied Correction": "未適用の補正があります",
    "The scan analysis is complete, but the Ch3/Ch4 correction "
    "has not been applied yet.\n"
    "Close the window anyway?":
        "スキャン解析が完了していますが、Ch3/Ch4 の補正がまだ適用されていません。\n"
        "このままウィンドウを閉じますか？",

    # apps/dac_scan/collimator_scan_app.py — CollimatorScanWindow
    "Gaussian Fit Result": "ガウシアンフィット結果",
    "Transmitted": "透過",
    "Ch{ch} offset": "Ch{ch} オフセット",
    "Ch{ch_x}: ±{half_x:.0f} pulses, step {step_x:.2f} p\n"
    "Ch{ch_y}: ±{half_y:.0f} pulses, step {step_y:.2f} p":
        "Ch{ch_x}: ±{half_x:.0f} パルス, ステップ {step_x:.2f} p\n"
        "Ch{ch_y}: ±{half_y:.0f} パルス, ステップ {step_y:.2f} p",

    # apps/xrd_scan/xrd_scan_app.py — XrdScanWindow
    "Calibration": "キャリブレーション",
    "Not calibrated": "未キャリブレーション",
    "Not loaded": "未読み込み",
    "Clear": "クリア",
    "Integration / ROI": "積分 / ROI",
    "Bins:": "ビン数:",
    "Set ROI…": "ROIを設定…",
    "No ROI defined": "ROI未定義",
    "Exposure": "露光",
    "Exposure:": "露光:",
    "Save TIFF images": "TIFF画像を保存",
    "Displayed ROI:": "表示ROI:",
    "Ch{ch_x}: ±{half_x:.1f} pulses, step {step_x:.2f} p\n"
    "Ch{ch_y}: ±{half_y:.0f} pulses, step {step_y:.1f} p":
        "Ch{ch_x}: ±{half_x:.1f} パルス, ステップ {step_x:.2f} p\n"
        "Ch{ch_y}: ±{half_y:.0f} パルス, ステップ {step_y:.1f} p",
    "XRD Intensity Map": "XRD 強度マップ",
    "ROI intensity": "ROI 強度",
    "✕ Not calibrated": "✕ 未キャリブレーション",
    "Please calibrate via Tools → Calibrate poni (IPAnalyzer + CeO2).":
        "Tools → Calibrate poni (IPAnalyzer + CeO2) でキャリブレーションしてください。",
    "● Calibrated": "● キャリブレーション済み",
    "IPA:  {name}": "IPA:  {name}",
    "CeO2: {name}": "CeO2: {name}",
    "chi²: {before:.5f} → {after:.5f}": "chi²: {before:.5f} → {after:.5f}",
    "Control points: {n}": "制御点: {n}",
    "Select dark current TIFF": "暗電流TIFFを選択",
    "Dark load error": "暗電流読み込みエラー",
    "Dark loaded: {name}  ({w}×{h} px)": "暗電流読み込み完了: {name}  ({w}×{h} px)",
    "Dark cleared.": "暗電流をクリアしました。",
    "No camera": "カメラなし",
    "Rad-icon 2022 is not connected.": "Rad-icon 2022 が接続されていません。",
    "#{n} {label} [{tmin:.1f}–{tmax:.1f}°, {mode}]": "#{n} {label} [{tmin:.1f}–{tmax:.1f}°, {mode}]",
    "ROI#{n}: {label}": "ROI#{n}: {label}",
    "XRD Intensity Map — ROI#{n}: {label}": "XRD 強度マップ — ROI#{n}: {label}",
    "Rad-icon 2022 not connected.": "Rad-icon 2022 が接続されていません。",
    "No calibration available.\n"
    "Please calibrate via Tools → Calibrate poni (IPAnalyzer + CeO2).":
        "キャリブレーションがありません。\n"
        "Tools → Calibrate poni (IPAnalyzer + CeO2) でキャリブレーションしてください。",

    # apps/xrd_scan/roi_dialog.py — RoiDialog
    "XRD ROI Settings": "XRD ROI設定",
    "Take Test Shot": "テスト撮影",
    "2θ (deg)": "2θ (度)",
    "Label": "ラベル",
    "2θ min (deg)": "2θ最小 (度)",
    "2θ max (deg)": "2θ最大 (度)",
    "Mode": "モード",
    "+ Add ROI": "+ ROIを追加",
    "Close": "閉じる",
    "Cannot get scan parameters:\n{error}": "スキャンパラメータを取得できません:\n{error}",
    "No poni file": "poniファイルなし",
    "Please load a poni file in the main window first.": "先にメインウィンドウでponiファイルを読み込んでください。",
    "Acquiring…": "取得中…",
    "Shot failed": "撮影失敗",
    "ROI#{n} ({label}): {val:.1f}": "ROI#{n} ({label}): {val:.1f}",
    "Delete this ROI": "このROIを削除",

    # apps/scan1d/scan1d_app.py — Scan1DScanWindow
    "1D Scan": "1D スキャン",
    "Channel:": "チャンネル:",
    "Ch{ch} Scan": "Ch{ch} スキャン",
    "± range (µm):": "± 範囲 (µm):",
    "Ch{ch} [pulse]": "Ch{ch} [パルス]",
    "Ch{ch} [µm from centre]": "Ch{ch} [中心からのµm]",
    "Ch{ch}: ±{half:.1f} pulses, step {step:.2f} p": "Ch{ch}: ±{half:.1f} パルス, ステップ {step:.2f} p",
    "Intensity Profile": "強度プロファイル",
    "Go to fitted center": "フィット中心へ移動",
    "Fit failed.": "フィット失敗。",
    "Moving to fitted center…": "フィット中心へ移動中…",

    # apps/ipa_poni/ipa_poni_dialog.py — IpaPoniDialog
    "IPA .prm → pyFAI .poni Converter": "IPA .prm → pyFAI .poni 変換ツール",
    "Input: IPA .prm file": "入力: IPA .prm ファイル",
    "Select a .prm file…": ".prm ファイルを選択…",
    "IPA Parameters (from .prm)": "IPAパラメータ（.prmより）",
    "CameraLength1:": "CameraLength1:",
    "CameraLength2:": "CameraLength2:",
    "DirectSpot (X, Y):": "DirectSpot (X, Y):",
    "Foot (X, Y):": "Foot (X, Y):",
    "PixSize (X, Y):": "PixSize (X, Y):",
    "TiltPhi:": "TiltPhi:",
    "TiltTau:": "TiltTau:",
    "Wavelength:": "波長:",
    "PixKsi (skew):": "PixKsi（歪み）:",
    "Computed pyFAI poni Parameters": "算出されたpyFAI poniパラメータ",
    "Distance:": "距離:",
    "Poni1:": "Poni1:",
    "Poni2:": "Poni2:",
    "Rot1:": "Rot1:",
    "Rot2:": "Rot2:",
    "Rot3:": "Rot3:",
    "PixelSize1 (axis1/Y):": "PixelSize1 (axis1/Y):",
    "PixelSize2 (axis2/X):": "PixelSize2 (axis2/X):",
    "Note: PixKsi (pixel skew angle) is not representable in poni format "
    "and is ignored. For detectors with significant skew, use pyFAI spline correction.":
        "注意: PixKsi（画素の歪み角）はponi形式では表現できないため無視されます。"
        "歪みが大きい検出器の場合はpyFAIのスプライン補正を使用してください。",
    "Output: pyFAI .poni file": "出力: pyFAI .poni ファイル",
    "Select output path…": "出力先を選択…",
    "Save .poni File": ".poni ファイルを保存",
    "Open IPA .prm file": "IPA .prm ファイルを開く",
    "Save pyFAI .poni file": "pyFAI .poni ファイルを保存",
    "Could not parse .prm file:\n{error}": ".prm ファイルを解析できません:\n{error}",
    "{value:.6f}°  (ignored)": "{value:.6f}°  （無視）",
    "Failed to save .poni file:\n{error}": ".poni ファイルの保存に失敗しました:\n{error}",
    "Saved": "保存完了",
    "Saved:\n{path}": "保存しました:\n{path}",

    # apps/seq_move/seq_move_app.py — SeqMoveWindow
    "Move Pattern": "移動パターン",
    "Step": "ステップ",
    "Add Step": "ステップを追加",
    "Remove Selected": "選択項目を削除",
    "Save JSON…": "JSONを保存…",
    "Load JSON…": "JSONを読み込み…",
    "Execution": "実行",
    "▶  Start Sequential Move": "▶  連続移動を開始",
    "Go to Next Step  →": "次のステップへ  →",
    "Stop Sequence  ■": "シーケンス停止  ■",
    "Return to Original Position": "元の位置に戻る",
    "Stop at Present Position": "現在位置で停止",
    "Empty Pattern": "パターンが空です",
    "No moves to save.": "保存する移動がありません。",
    "Save Pattern": "パターンを保存",
    "Load Pattern": "パターンを読み込み",
    "Expected a JSON array of steps": "ステップのJSON配列が必要です",
    "Step {n} must be a list": "ステップ {n} はリストである必要があります",
    "Step {n} has {count} moves; max is {max_count}": "ステップ {n} に {count} 個の移動がありますが、最大は {max_count} です",
    "Each move in step {n} must have 'Ch' and 'diff' keys": "ステップ {n} の各移動には 'Ch' と 'diff' キーが必要です",
    "No Controller": "コントローラーなし",
    "No stage controller is connected.": "ステージコントローラーが接続されていません。",
    "Add at least one step with a channel and Δpulse value.": "チャンネルとΔpulse値を持つステップを1つ以上追加してください。",
    "Read Error": "読み取りエラー",
    "Could not read current position of Ch{ch}.": "Ch{ch} の現在位置を読み取れませんでした。",
    "Executing step {n} / {total}…": "ステップ {n} / {total} を実行中…",
    "All {total} step(s) completed.": "全 {total} ステップが完了しました。",
    "Step {n} / {total} done. Ready for step {next}.": "ステップ {n} / {total} 完了。ステップ {next} の準備ができました。",
    "Stopped after step {n} / {total}.": "ステップ {n} / {total} の後で停止しました。",
    "Sequence finished. Stopped at current position.": "シーケンス終了。現在位置で停止しました。",
    "Returning to original position…": "元の位置に戻り中…",
    "Returned to original position.": "元の位置に戻りました。",

    # apps/speed_controller/speed_controller_app.py — SpeedControllerWindow
    "Reading current speed values…": "現在の速度値を読み取り中…",
    "Channel": "チャンネル",
    "{level} current": "{level} 現在値",
    "{level} new value": "{level} 新規値",
    "Apply": "適用",
    "Load previous speed data": "以前の速度データを読み込み",
    "Backup Required": "バックアップが必要です",
    "Current speed values will be saved before any operation.": "操作の前に、現在の値を保存します。",
    "Select Backup Directory": "バックアップ先ディレクトリを選択",
    "Save Error": "保存エラー",
    "Could not save backup file:\n{error}": "バックアップファイルを保存できませんでした:\n{error}",
    "Ready.  Backup saved to {path}": "準備完了。バックアップを {path} に保存しました",
    "Read Error": "読み取りエラー",
    "Could not read current speed values:\n{error}": "現在の速度値を読み取れませんでした:\n{error}",
    "read error": "読み取りエラー",
    "Confirm Close": "終了確認",
    "Revert all channels to the speed values recorded when this window opened?":
        "元の速度の値に戻しますか？",
    "Load Previous Speed Data": "以前の速度データを読み込み",
    "Load Error": "読み込みエラー",
    "Invalid speed data file:\n{error}": "速度データファイルの形式が不正です:\n{error}",
    "Apply Loaded Speeds": "読み込んだ速度を適用",
    "Apply the speed values loaded from this file to all channels?":
        "読み込んだファイルの値を反映します。よろしいですか？",
    "Applying loaded speed values…": "読み込んだ速度値を適用中…",
    "Ch{ch} {level}: expected {target}, got {actual}": "Ch{ch} {level}: 期待値 {target}、実際 {actual}",
    "Loaded with {n} failure(s).": "{n} 件の反映に失敗しました。",
    "Some Speeds Not Applied": "一部の速度が反映されませんでした",
    "Loaded speed values applied successfully.": "読み込んだ速度値をすべて反映しました。",
    "Missing 'channels' key": "'channels' キーがありません",
    "'channels' must be an object": "'channels' はオブジェクトである必要があります",
    "Missing channel {ch}": "チャンネル {ch} がありません",
    "Channel {ch} entry must be an object": "チャンネル {ch} の項目はオブジェクトである必要があります",
    "Channel {ch} missing '{level}'": "チャンネル {ch} に '{level}' がありません",
    "Channel {ch} '{level}' must be an integer": "チャンネル {ch} の '{level}' は整数である必要があります",
    "Channel {ch} '{level}' out of range ({min}-{max})":
        "チャンネル {ch} の '{level}' が範囲外です ({min}〜{max})",

    # settings/settings_window.py — sidebar page names
    "Detector Calibration": "検出器校正",
    "Logging": "ロギング",
    "Notifications": "通知",

    # settings/pages/detector_calibration.py — DetectorCalibrationPage
    # (also used as CalibrateInstrumentsWindow's window title)
    "Calibrate Detector Geometry": "検出器ジオメトリ校正",
    "Poni File": "poniファイル",
    "Poni file (.poni):": "poniファイル (.poni):",
    "Select poni file": "poniファイルを選択",
    "Calibration Data": "キャリブレーションデータ",
    "⚠ No calibration data loaded.": "⚠ キャリブレーションデータがありません。",
    "● Loaded from: {name}": "● 読み込み元: {name}",
    "● In-session calibration data (not loaded from a file)":
        "● セッション内のキャリブレーションデータ（ファイルからの読み込みではありません）",
    "Distance = {dist_mm:.4f} mm    Poni1 = {poni1_mm:.4f} mm    Poni2 = {poni2_mm:.4f} mm\n"
    "Rot1 = {rot1_deg:.4f}°    Rot2 = {rot2_deg:.4f}°    Rot3 = {rot3_deg:.4f}°\n"
    "Wavelength = {wavelength_ang:.6f} Å    Pixel size = {px1_um:.1f} × {px2_um:.1f} µm":
        "Distance = {dist_mm:.4f} mm    Poni1 = {poni1_mm:.4f} mm    Poni2 = {poni2_mm:.4f} mm\n"
        "Rot1 = {rot1_deg:.4f}°    Rot2 = {rot2_deg:.4f}°    Rot3 = {rot3_deg:.4f}°\n"
        "波長 = {wavelength_ang:.6f} Å    ピクセルサイズ = {px1_um:.1f} × {px2_um:.1f} µm",
    "Recalibrate…": "再キャリブレーション…",
    "Open Calibrate Detector Geometry…": "検出器ジオメトリ校正を開く…",
    "Not available when this page is opened standalone.":
        "このページを単独で開いた場合は利用できません。",

    # apps/calibrate_instruments/calibrate_instruments_app.py — save button
    "Save and apply calibration…": "キャリブレーションを保存して適用…",
    "Save poni file": "poniファイルを保存",
    "Save Warning": "保存に関する警告",
    "poni file saved, but the IPAnalyzer parameter file could not be written:\n{error}":
        "poniファイルは保存されましたが、IPAnalyzer用パラメータファイルを書き込めませんでした:\n{error}",

    # settings/pages/logging_page.py — LoggingPage
    "Details log output directory": "Details logの保存先ディレクトリ",
    "Reset": "リセット",
    "Per-app save location": "各アプリの保存先",
    "Log Saving": "ログ保存",
    "Select all": "すべて選択",
    "Unselect all": "すべて解除",
    "DAC Scan": "DAC スキャン",
    "DAC Scan (Rot.)": "DAC スキャン（回転）",
    "XRD Scan": "XRD スキャン",
    "Autofocus": "オートフォーカス",
    "※ Checkbox selections are reset on restart": "※ チェックは再起動時にリセットされます",
    "Running in --details mode. All apps save continuously.":
        "--details モードで起動しています。全アプリで常時保存されます。",
    "Select the Details log save folder": "Details log の保存先フォルダを選択",
    "Using default settings.": "デフォルト設定を使用しています。",
    "Default: {default}": "デフォルト: {default}",

    # settings/pages/notification_page.py — NotificationPage
    "Notification Sound (Completion of Collimator scan, DAC scan, and XRD measurement)":
        "完了音（コリメータースキャン、DACスキャン、XRD測定の完了時）",
    "Select a sound to notify the user when the operation is completed.\n"
    "Source: OtoLogic (https://otologic.jp/), 効果音ラボ (https://soundeffect-lab.info/)":
        "取込が完了したときに再生するサウンドを選択してください。\n"
        "Source: OtoLogic (https://otologic.jp/), 効果音ラボ (https://soundeffect-lab.info/)",
}
