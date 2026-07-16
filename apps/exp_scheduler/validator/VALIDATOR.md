# PreValidator の validation 項目

1. Stage: Global limits が渡されている場合、Ch3/Ch4/Ch5 の +/- mm 上限がすべて設定済みか確認する。
1. Stage: ステージ操作（`StageAction` / `microscope_out_and_fpd_in` / `fpd_out_and_microscope_in` に加え、Ch4/Ch5 の XY 追従補正と Ch3 のオートフォーカスで stage controller を直接操作する `start_following` / `follow_sample_position` も含む）がある場合、Stage controller が接続されているか確認する。
1. Stage: ステージが `PM16CControllerSim` の場合、シミュレーションモードであることを警告する。
1. Stage: ステージ操作開始前に、ステージが移動中でないか確認する。
1. Stage: `StageAction` の `operation` が `move_absolute`/`move_relative`/`set_speed`/`normal_stop`/`emergency_stop` のいずれでもない場合にエラーにする。
1. Stage: `move_absolute`/`move_relative`/`set_speed` で `ch` が1〜11の整数でない場合にエラーにする（`normal_stop`/`emergency_stop` は `ch` 未使用のため対象外）。実機・シミュレータ (`PM16CController`/`PM16CControllerSim`) とも範囲外の `ch` は例外を投げず無音で no-op になるため、ここで検出しないと「移動したつもりで後続のステップ・測定に進んでしまう」。
1. Stage: `speed` が指定されている場合、`H`/`M`/`L` のいずれでもなければエラーにする（`StageAction` と `microscope_out_and_fpd_in`/`fpd_out_and_microscope_in` の両方）。不正な `speed` も同様に `set_ch_speed`/`set_ch_speed_value` が無音で no-op になるため。
1. Stage: `move_absolute`/`move_relative` の position/delta（`ForLoopAction` 変数経由の場合はそのループの `values` 内の各値）が、数値でない・NaN/Inf・整数パルスでない・PM16C プロトコルの範囲 ±2,147,483,647 を超える、のいずれかに該当する場合にエラーにする。
1. Stage: `microscope_out_and_fpd_in` で位置が省略されている場合、`stage_settings.json` に `ch8_out` と `det_in` があるか確認する。位置が明示指定されている場合は、その値が有限の整数パルスで範囲内か確認する。
1. Stage: `fpd_out_and_microscope_in` で位置が省略されている場合、`stage_settings.json` に `det_out` と `ch8_in` があるか確認する。位置が明示指定されている場合は、その値が有限の整数パルスで範囲内か確認する。
1. Stage: `stage_settings.json` から読み込む `ch8_in`/`ch8_out`/`det_in`/`det_out` の値についても、キーの存在に加えて、数値・整数パルス・範囲内（±2,147,483,647）かを確認する。
1. Stage: Ch1-Ch11 の現在位置を全て読み取れるか確認する。
1. Stage: 現在位置が Ch8/Ch9/Ch11 の `MOVE_CONSTRAINTS` に違反していないか確認する。
1. Stage: シーケンス中の各ステージ移動を順に模擬し、各移動が Ch8/Ch9/Ch11 の `MOVE_CONSTRAINTS` に違反しないか確認する。
1. Stage: 同じ模擬の中で、Ch3/Ch4/Ch5 への移動先ごとに Global limits (±mm) との照合も行う。Global limits が設定されている場合、その移動先が validation 時点の位置（`SequenceRunner.run()` が実際に使うのと同じ baseline）から見て設定上限を超えないか確認し、超える場合はエラーにする（`SequenceRunner._check_global_limits_before_move` が実行時に行うブロックを、実行前に同じロジックで再現するもの）。`ForLoopAction` による反復も展開して確認する。
1. Stage: ステージ現在位置を validation 時の baseline として保存する。
1. Stage: Ch8/Ch9 の現在位置を読み、現在の stage mode が microscope / xrd / unknown のどれかを判定する。
1. Stage: Ch8/Ch9 の位置取得結果が `None` の場合、ステージ位置を取得できないエラーにする。
1. Stage: Ch8/Ch9 の位置取得中に通信例外が発生した場合も同様にエラーにする（mode は unknown 扱いとしつつ、fail-open にせず Run を止める）。
1. Stage: Microscope mode 中に `take_xrd` または `take_dark` が呼ばれていないか確認する。
1. Stage: Stage mode が unknown のまま `take_xrd` または `take_dark` が呼ばれる場合、FPD 位置未確認として警告する。
1. Stage: `ForLoopAction` の body 内で stage mode が変化する場合、次の反復の開始状態が変わることを警告する。
1. PACE5000: PACE5000 操作がある場合、PACE5000 が接続済みか確認する。
1. PACE5000: シーケンス中の最大設定圧力が、現在の PACE5000 +ve source 圧力を超える場合にエラーにする。
1. PACE5000: +ve source 圧力が読み取れない場合（通信エラー等）、fail-open にせずエラーにする。
1. PACE5000: 圧力関連コマンド（`set_pressure`/`wait_pressure`）がある場合、現在の Control Mode (Output State) が読み取れないと（通信エラー等）fail-open にせずエラーにする。
1. PACE5000: 圧力関連コマンド（`set_pressure`/`wait_pressure`）があるのに `set_control_mode` が一度も呼ばれず、現在の Control Mode が Measure の場合にエラーにする。
1. PACE5000: `set_control_mode` は呼ばれているが、最初に Control Mode が ON になるまでの流れが次の2パターンのいずれとも一致しない場合にエラーにする — (1) `set_pressure → set_control_mode(True) → wait_pressure`、(2) `set_control_mode(True) → set_pressure → wait_pressure`。具体的には、Control Mode が ON になる前に `set_pressure` が2回以上実行される場合（どちらの設定値が有効か不明瞭）、および Control Mode が一度も ON にならないまま `wait_pressure` が実行される場合（例: `set_pressure → wait_pressure → set_control_mode(True)` — 設定変更が反映されないまま待機してしまう）の両方を検出する。
1. PACE5000: `set_pressure` の直後のアクションが `wait`（general）または `wait_pressure` 以外の場合に警告する。
1. PACE5000: `wait_pressure` の前に一度も `set_pressure` が実行されていない場合にエラーにする。
1. PACE5000: 複数回の `set_pressure` の間に `wait_pressure` が無い場合に警告する。
1. PACE5000: `set_pressure`/`wait_pressure` のパラメータ（pressure、rate、tol < 0、unit が "MPa"/"Bar" 以外、rate_unit が想定外、NaN/inf）を検証し、不正な場合にエラーにする（DSL 入力・UI 入力の両方に適用）。
1. PACE5000: `pressure`（ループ変数として解決済みの値、または直接のリテラル値）が `float()` に変換できない場合、必ずエラーにする（未解決のループ変数名の場合は `_check_undefined_loop_vars` 側で別途エラーになるため、ここでは float 変換に失敗する非数値リテラルのみを対象とする）。
1. PACE5000: `rate` が `float()` に変換できない場合、必ずエラーにする。
1. PACE5000: `wait_pressure` の `tol` が `float()` に変換できない場合、必ずエラーにする。
1. PACE5000: `wait_pressure` の tolerance が 0.0001 MPa 未満の場合に警告する。
1. PACE5000: `set_pressure` の `rate`（`rate_unit` で MPa/sec に換算した値）が PACE5000 のハードウェア最小 slew rate（`apps/PACE5000/pace5000_backend.py` の `MIN_SLEW_RATE_MPA_PER_SEC` = 0.001 MPa/sec）を下回る場合にエラーにする。**`rate=0` もこの範囲に含まれるためエラーになる**（旧仕様の「非推奨」警告から変更）。0 は文字通りこの下限（0.001 MPa/sec）を下回っており、また PACE5000 自身の Scheduled Control 機能（`apps/PACE5000/pace5000_app.py`）も `rate<=0` を明示的に拒否しているため、`set_pressure` の `rate=0`（瞬時変化）を安全な仕様として扱う根拠がない。実機での slew rate 分解能が信頼できなくなる下限を一貫して適用する。
1. PACE5000: `set_pressure` の直後が `wait()`（`wait_pressure` ではない汎用 wait）であり、かつそこから次の `set_pressure` までの間に `wait_pressure` が無い場合、`abs(target_mpa - current_mpa) / rate_mpa_per_sec` で概算した所要時間より `wait()` の待機時間が短ければ警告する（LakeShore 335 の `set_temperature` → 汎用 `wait()` チェックと同様の仕組み）。`current_mpa` は validation 時点で PACE5000 から読み取った現在の target pressure を初期値とし、シーケンスを実行順（`ForLoopAction` は反復ごとに展開）で走査しながら各 `set_pressure` のたびに更新する。
1. PACE5000: `set_and_wait_pressure` は内部的に `set_pressure` + `wait_pressure` として展開されるため、上記の全 PACE5000 チェックがそのまま適用される（Set 直後に Wait があるとみなされるため隣接性チェックの警告は発生しない）。
1. LakeShore 335: LakeShore 335 操作がある場合、LakeShore 335 が接続済みか確認する。
1. LakeShore 335: 接続済みの場合、現在の設定値 (`get_setpoint`) を読み出せるか確認し、読み出せなければ通信エラーとする。
1. LakeShore 335`wait_temperature` がある場合、LakeShore 335 の読み取りデータがまだ無ければ警告する。
1. LakeShore 335: LakeShore 335 関連コマンド（`set_temperature`/`wait_temperature`/`set_heater`/`all_heaters_off`）が一つでもある場合、シーケンス全体を実行順（`ForLoopAction` は反復ごとに展開）で1回走査し、以下の setpoint/ヒーター状態を各ステップで追跡しながら以降のチェックを行う（ステージ位置をステップごとに模擬するのと同様の仕組み）。
1. LakeShore 335: 上記の走査を開始する前に、現在のヒーターレンジ (`get_heater_range`) を読み出せるか確認し、読み出せなければ（通信エラー等）fail-open にせずエラーにしてこの一連のチェックを中断する。
1. LakeShore 335: `wait_temperature` の前に一度も（直前・過去を問わず）`set_temperature` が実行されていない場合に警告する。
1. LakeShore 335: `set_temperature` の後、`wait_temperature` なしで次の `set_temperature` が実行される場合に警告する。
1. LakeShore 335: （DSL 直接入力向け）`ramp_rate < 0`、`tol_k <= 0`、`range_index` が 0〜3 以外、`value_k` が非数値/NaN/Inf の場合にエラーにする。
1. LakeShore 335: `wait_temperature` の `tol_k` が 0（またはそれ以下）の場合にエラー、0.01 K 未満の場合は小さすぎる旨を警告する。
1. LakeShore 335: `set_temperature` の設定値が 300 K を超える場合にエラーにする。
1. LakeShore 335: `wait_temperature` の直後に `follow_sample_position` または `start_following` が来ている場合にエラーにする（正しくは `set_temperature → start_following → wait_temperature` の順）。
1. LakeShore 335: `set_temperature` から次の `take_xrd` までの間に `wait_temperature` が無い場合、温度が安定化していない可能性があると警告する。
1. LakeShore 335: `set_temperature` から次の `take_xrd` までの間に `follow_sample_position`、または `start_following`+`stop_following` のペア（継続中の追従も含む）が無い場合、試料位置がずれている可能性があると警告する。
1. LakeShore 335: Validation 時点でヒーター出力が OFF であり、かつ最初の `set_temperature` より前に `set_heater` でヒーター出力を Off 以外に変更していない場合、温度制御ができない可能性があると警告する。
1. LakeShore 335: `set_temperature` → ヒーター OFF（`set_heater(0)` または `all_heaters_off`）→ `wait_temperature` の順の場合、`wait_temperature` が未達になる可能性が高いと警告する。
1. LakeShore 335: `set_temperature` の直後が `wait()`（`wait_temperature` ではない汎用 wait）であり、そこから次の `set_temperature` までに `wait_temperature` が無い場合、`abs(target - current_or_previous) / ramp_rate` で概算した所要時間より `wait()` の待機時間が短ければ警告する。
1. LakeShore 335: `all_heaters_off` の後、ヒーターを入れ直さないまま `set_temperature` が実行されている場合にエラーにする。
1. LakeShore 335: 冷却方向 (`new < previous`) の `set_temperature` で `ramp_rate >= 5` K/min の場合、および加熱方向 (`new > previous`) で `ramp_rate >= 10` K/min の場合、実際の速度が設定より遅くなる可能性があると警告する。
1. LakeShore 335: `set_temperature` の設定値が直前の setpoint と変化していない場合、意味のない温度設定コマンドである旨を警告する。
1. FPD: `take_xrd` または `take_dark` がある場合、Rad-icon 2022 が接続済みか確認する。
1. FPD: Global XRD settings で dark 補正が有効な場合、指定 dark file が存在しなければ警告する。
1. FPD: Global XRD settings で defect 補正が有効な場合、指定 defect file が存在しなければ警告する。
1. FPD: `take_xrd` の per-step dark file override が有効な場合、指定ファイルが存在しなければ警告する。
1. FPD: `take_xrd` の per-step defect file override が有効な場合、指定ファイルが存在しなければ警告する。
1. FPD: `take_xrd` の `save_dir` override が存在しない場合、実行時に作成されることを警告する。
1. FPD: `take_xrd` の `save_dir` override がディレクトリでない場合、エラーにする。
1. Interactive Camera: カメラ操作がある場合、指定された各 `camera_index` を `cv2.VideoCapture` で開けるか確認する。
1. Interactive Camera: `opencv-python` が無い場合、カメラ確認をスキップしたことを警告する。
1. Interactive Camera: `start_following` または `follow_sample_position` がある場合、`calibration.json` が存在するか確認する。
1. Interactive Camera: `calibration.json` が読める JSON か確認する。
1. Interactive Camera: `calibration.json` に `matrix_inv` キーがあるか確認する。
1. Interactive Camera: `start_following` または `follow_sample_position` で使う reference image が存在するか確認する。
1. Interactive Camera: `start_following` が追従中に再度呼ばれていないか確認する。
1. Interactive Camera: `follow_sample_position` が追従中に呼ばれていないか確認する。
1. Interactive Camera: `stop_following` が `start_following` より前に現れていないか確認する。
1. Interactive Camera: `start_following` に対応する `stop_following` が無い場合、シーケンス終了まで追従が続くことを警告する。
1. Interactive Camera: 追従中に `microscope_out_and_fpd_in` が呼ばれていないか確認する。
1. Interactive Camera: XRD mode 中に `start_following` が呼ばれていないか確認する。
1. Interactive Camera: XRD mode 中に `save_reference_image` または `follow_sample_position` が呼ばれていないか確認する。
1. Interactive Camera: Autofocus を使う追従アクションで `autofocus_range_um` が 0 以下でないか確認する。
1. Interactive Camera: Autofocus を使う追従アクションで `autofocus_steps` が 2 未満でないか確認する。
1. Interactive Camera: Autofocus を使う場合、Ch3 の global limits が未設定なら警告する。
1. Sequence: `ForLoopAction` のループ変数が body 内で一度も使われていない場合に警告する。
1. Sequence: アクションが参照するループ変数（直接フィールド参照、または f-string プレースホルダ `{var}` の両方）が、その位置で有効な（enclosing `ForLoopAction` が定義する）変数名のいずれとも一致しない場合にエラーにする（未定義ループ変数参照）。
1. Sequence: `ForLoopAction` の body が空の場合にエラーにする。



## Interactive Camera Save Snapshot Addendum

- `save_snapshot` is treated as an Interactive Camera action.
- It uses camera index 0, captures one frame, and saves it under the per-step `save_dir` or the Interactive Camera global snapshot directory.
- It is invalid while the sequence is in XRD mode, matching other camera image acquisition operations.
