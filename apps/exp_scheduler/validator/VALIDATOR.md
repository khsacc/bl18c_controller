# PreValidator の validation 項目

1. Stage: Global limits が渡されている場合、Ch3/Ch4/Ch5 の +/- mm 上限がすべて設定済みか確認する。
1. Stage: Global limits の6値（Ch3/4/5 の ±mm）それぞれについて、設定済みかどうか（None でないか）に加えて、有限数かつ0以上であることを確認する。設定済みチェックは None かどうかしか見ないため、UI の spin box（範囲 [0.0, 9999.99] にクランプ済み）を経由しない手編集/破損した設定ファイル経由で NaN/Inf/負値が紛れ込むケースを別途検出する。
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
1. Stage: 上記2項目を含む stage mode 順序チェックは、`ForLoopAction` を実際の反復回数（`values` の要素数）だけ展開した実行順で1回走査する（`validator/execution_trace.py::ExecutionTrace.ordered` を使用 — REORGANISATION_PLAN.md Phase 5）。これにより、ループ本体内で stage mode が変化する場合でも、2周目以降の実際の呼び出しに対して正しくエラー・警告が判定される（例: 1周目の `fpd_out_and_microscope_in` の後、2周目の `take_xrd` が microscope mode のまま実行されようとする、といったケースも正確な Step 番号付きで検出される — `tests/test_exp_scheduler_pre_validator.py::LoopCrossIterationStateTests::test_stage_mode_ordering_state_survives_past_the_loop` で固定）。
1. Stage: `emergency_stop()` の後に `move_absolute`/`move_relative` が続く場合、意図した動作か確認を促す警告を出す。`emergency_stop()` は `SequenceRunner._resume_motion_after_self_stop()` により続行前提で設計されており、後続移動があること自体は異常ではないためエラーではなく警告とする。各 `emergency_stop()` につき、後続の最初の通常移動のみ警告し、以降の移動は重複して警告しない。
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
1. PACE5000: `pressure`（ループ変数として解決済みの値、または直接のリテラル値）が `float()` に変換できない場合、必ずエラーにする（未解決のループ変数名の場合は `validator/checks/sequence_structure.py::check_undefined_loop_vars` 側で別途エラーになるため、ここでは float 変換に失敗する非数値リテラルのみを対象とする）。
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
1. LakeShore 335: （DSL 直接入力向け）`ramp_rate` が非数値/NaN/Inf または負、`tol_k` が非数値/NaN/Inf または0以下、`range_index` が 0〜3 以外、`value_k` が非数値/NaN/Inf の場合にエラーにする。`ramp_rate`/`tol_k` はループ変数を取れないため常にリテラルか `None`（DSL でその引数を省略した場合。`dsl/parser.py` の `SequenceBuilder` は実引数を `dict.get()` で読むため、必須引数が省略されても例外を投げず `None` を代入する）のいずれかであり、以前は `a.ramp_rate < 0` のような素の比較をしていたため `None` が来ると `TypeError` で `PreValidator.validate()` 全体がクラッシュしていた。REORGANISATION_PLAN.md Phase 5 でこの4種の値検証を `validator/checks/action_params.py::check_lakeshore_params`（装置通信不要の静的 Action 値検証、`code="static.lakeshore.*"` の `Diagnostic` を生成）へ移設した。`value_k` はループ変数を取れる（`LOOP_VAR_FIELDS`）ため `check_stage_schema` と同じ「参照されているループの `values` 候補をすべて検証する」パターンを使う。`validator/checks/lakeshore.py::check_lakeshore_sequence`（下記の走査。REORGANISATION_PLAN.md Phase 6 で `pre_validator.py` から移設）は検証を重複させず、`ramp_rate`/`value_k` の解決済み数値だけを非エラーの `_try_resolve_float()` で取得して自身のヒューリスティック（冷却/加熱速度警告等）に使う。
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
1. FPD: `take_xrd` の `exposure_ms` override（指定されている場合）、および `take_dark` の `exposure_ms`（常に必須）が非数値/NaN/Inf、または0以下の場合にエラーにする。REORGANISATION_PLAN.md Phase 5 で両方とも `validator/checks/action_params.py::check_xrd_params`（旧 `check_xrd_settings` を改名・拡張）に統合した — `take_dark` の `exposure_ms` 検証は元々 `_check_radicon`（Rad-icon 接続確認）に埋め込まれていたが、装置通信不要の静的値検証をそちらから分離した。
1. Interactive Camera: カメラ操作がある場合、指定された各 `camera_index` を `cv2.VideoCapture` で開けるか確認する。
1. Interactive Camera: `opencv-python` が無い場合、カメラ確認をスキップしたことを警告する。
1. Interactive Camera: `start_following` または `follow_sample_position` がある場合、`calibration.json` が存在するか確認する。
1. Interactive Camera: `calibration.json` が読める JSON か確認する。
1. Interactive Camera: `calibration.json` に `matrix_inv` キーがあるか確認する。
1. Interactive Camera: `start_following` または `follow_sample_position` で使う reference image が存在するか確認する。
1. Interactive Camera: 追従ペアリングのチェック（以下4項目）は、`ForLoopAction` を実際の反復回数だけ展開した実行順で1回走査する（`validator/execution_trace.py::ExecutionTrace.ordered` を `validator/checks/sequence_structure.py::check_follow_pairing` へ渡す — REORGANISATION_PLAN.md Phase 5）。ループ本体の末尾で `start_following` が `stop_following` されないまま次の周回に入る場合も、2周目の `start_following` が「追従セッションが既にアクティブな状態でのネストした start_following」として正しく検出される（body を1回だけ走査していた旧実装では検出できなかった）。
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
1. Interactive Camera: `start_following`/`follow_sample_position` の per-step override（指定されている場合のみ）を検証する — `interval_s` が非数値/NaN/Inf、または0以下でないか、`similarity_threshold` が非数値/NaN/Inf、または0〜1の範囲内か、`max_correction_per_step_um` が非数値/NaN/Inf、または0以上か。いずれも `SequenceRunner._follow_loop`（バックグラウンドスレッド、例外は握りつぶされて progress ログに出るのみ）まで無検証で到達しうる値であるため、事前に検出する。
1. General: `wait()` および `follow_sample_position()` の `duration_s` が非数値/NaN/Inf、または0以下の場合にエラーにする。特に Inf は `wait(duration=1e400)` のような数値リテラルのオーバーフロー（Python の構文解析時点で関数呼び出しを介さず `inf` になる）で到達可能であり、無検証のままだと `SequenceRunner._do_wait()` の `deadline = now + duration_s` が到達不能になり、シーケンスが実質的に無期限ハングする。
1. Sequence: `ForLoopAction` のループ変数が body 内で一度も使われていない場合に警告する。
1. Sequence: アクションが参照するループ変数（直接フィールド参照、または f-string プレースホルダ `{var}` の両方）が、その位置で有効な（enclosing `ForLoopAction` が定義する）変数名のいずれとも一致しない場合にエラーにする（未定義ループ変数参照）。
1. Sequence: `ForLoopAction` の body が空の場合にエラーにする。
1. Sequence: `ForLoopAction` の `values` が空リストの場合にエラーにする。body があっても実行時に0回実行される（書いた内容が丸ごと無視される）ため、body が空の場合と同様にエラー扱いとする。
1. Sequence: シーケンスにトップレベルのアクションが一つもない場合にエラーにする。
1. Sequence: `ForLoopAction` を実際に反復展開した場合の「1ループあたりの最大反復数」「シーケンス全体の最大展開ステップ数（≒ 全反復を通した実行アクション数の合計）」「最大ネスト深度」に上限を設け、いずれかを超える場合にエラーにする（既定値: 反復数 2,000 / 展開ステップ数 20,000 / ネスト深度 4。`for_loop` は DSL 専用で Visual エディタからは作成できないため、実運用の温度スイープ・複数サンプル処理等に対しては十分に余裕を持たせた値）。

   REORGANISATION_PLAN.md Phase 5 で、この上限判定と実際の展開はすべて
   `validator/execution_trace.py::ExecutionTrace` に一元化された。
   `ExecutionTrace.build()` は必ず次の順で実行する。

   1. `compute_loop_stats()` — 実際の展開を一切試みず、再帰のみで
      「展開後ステップ数・最大反復数・最大ネスト深度」を見積もる。この
      見積もり自体がネスト深度上限＋1（既定 5）を超えて再帰しないよう
      ガードされているため、深さについては再帰上限（`RecursionError`）を
      一切気にせず常に軽量・安全に完了する（手編集/破損した Sequence
      JSON 等、実運用では通常発生しないほど深くネストした
      `ForLoopAction` チェーンに対しても安全）。深さ超過で打ち切った
      場合、`max_nesting_depth`/`total_steps` は「少なくともこの値」の
      下限として報告される（水増しはしない — 空 body の打ち切りノードは
      正しく 0 ステップとして扱われる）。
   2. `ExecutionTrace.flat`（静的 leaf projection。ForLoopAction の body を
      反復回数に関係なく1回だけ訪問する非再帰・スタックベースの
      walker）は、上記の見積もり結果に関係なく常に完全に構築される —
      幅（反復回数）に依存しないため危険性がなく、Stage/PACE5000/
      LakeShore/Rad-icon/Camera の接続確認や `action_params.py` の
      多くの静的値検証（duration/follow params/autofocus/xrd params）は
      これを使い、ループ上限超過時も常に実行され続ける。
   3. `ExecutionTrace.ordered`（真の反復展開、`SequenceRunner._flat_index`
      と同じ Step 番号付き）と `.pace_primitives()` は、上記3つの上限
      すべてを満たす場合（`within_limits`）にのみ実体化される。

   `PreValidator.validate()` はこの結果に基づき3段階でチェックを
   ゲートする（`_run_structural`/`_run_candidates`/`_run_expanded`）。

   | ゲート | 条件 | 対象 |
   |---|---|---|
   | `depth_safe` のみ | ネスト深度が上限内 | `a.values` を反復しない構造チェック（未使用/未定義ループ変数、空 loop body/values、重複アクション、pace5000 adjacency） |
   | `candidates_safe` | 上記 + 個々のループの反復数が上限内 | ループ変数の候補値をすべて検証する静的チェック（`check_stage_schema`, `check_lakeshore_params` — 1つのループの `values` が巨大な場合、総展開ステップ数の積に関係なくここで抑制する） |
   | `within_limits` | 上記 + 総展開ステップ数が上限内 | 真の反復展開に依存するチェック（stage move constraints の反復シミュレーション、PACE5000 の実行順/control mode/wait_duration/source pressure、LakeShore の実行順、追従ペアリング、stage mode 順序チェック、`emergency_stop()` 後の確認警告 — 計8箇所） |

   いずれのゲートで抑制された場合も、実際の展開がハング・メモリ枯渇・
   `RecursionError` を起こす前にスキップされる。Stage/PACE5000/
   LakeShore/Rad-icon/Camera の接続確認、および stage move constraints の
   現在位置読み取りと baseline 記録は、`ExecutionTrace.flat` ベース（幅に
   依存しない）または装置読み取りのみのため、このスキップの影響を受けず
   常に実行される。`tests/test_exp_scheduler_pre_validator.py::
   LoopLimitSafetyRegressionTests` と
   `tests/test_exp_scheduler_execution_trace.py` で深さ超過・単一ループ幅
   超過・総ステップ数超過の3ケースをそれぞれ固定している。



## Interactive Camera Save Snapshot Addendum

- `save_snapshot` is treated as an Interactive Camera action.
- It uses camera index 0, captures one frame, and saves it under the per-step `save_dir` or the Interactive Camera global snapshot directory.
- It is invalid while the sequence is in XRD mode, matching other camera image acquisition operations.

## PreValidator internal error safety net

- `validate()`'s `_run()`/`_run_structural()`/`_run_candidates()`/
  `_run_expanded()` wrappers (REORGANISATION_PLAN.md Phase 5 —
  `_run_structural`/`_run_candidates`/`_run_expanded` all delegate to a
  common `_run_gated()`, which itself calls `_run()`) catch any exception
  raised by an individual check function and turn it into a normal
  validation error ("`<label>`: internal validation error (...)") instead
  of letting it propagate out of `validate()` entirely. `ExecutionTrace.build()`
  and `validator/snapshots.py`'s `determine_requirements()`/
  `collect_snapshot()` (called directly, not through `_run` — REORGANISATION_PLAN.md
  Phase 6) are wrapped the same way — a failure building `ExecutionTrace`
  falls back to a trace whose `stats` guarantee every gated check is
  skipped (fail closed); a failure collecting the device snapshot falls
  back to an all-`None` `ValidationSnapshot`/`SnapshotRequirements`, rather
  than leaving `trace`/`snapshot`/`requirements` undefined for the rest of
  `validate()`. (The pre-Phase-6 `_detect_stage_mode` no longer exists as a
  separate step — its Ch8/Ch9 read is now part of
  `validator/snapshots.py::collect_stage_snapshot`.)
- Since REORGANISATION_PLAN.md Phase 7, every one of these internal-error
  fallbacks also records a proper `Diagnostic` (via
  `validator/models.py::emit_static`/`emit_diagnostic`) instead of only
  appending a plain string to `PreCheckResult.errors` — `ValidationReport`
  (`apps/exp_scheduler/validation_service.py`) treats `.diagnostics` as its
  sole source of truth, so an internal error that only touched `.errors`
  would silently disappear from it. `_run()`/`_run_gated()`/
  `_run_structural()`/`_run_candidates()`/`_run_expanded()` all take a
  required keyword-only `phase` (and, for PREFLIGHT checkers, `device`)
  from their call site in `validate()`, matching whichever
  `validator/checks/*.py` module the wrapped checker function belongs to
  (`action_params.py`/`sequence_structure.py` → `STATIC`; `stage.py`/
  `pace5000.py`/`lakeshore.py`/`xrd.py`/`camera_follow.py` → `PREFLIGHT`
  with that module's own device name) — this is a required argument, not a
  default, precisely so a future checker added without specifying it fails
  loudly (`TypeError`) instead of being silently mistagged. The snapshot
  collection failure above is tagged `PREFLIGHT` with `device=None` (the
  failure is in deciding/reading device state in general, not attributable
  to one device); the `ExecutionTrace.build()` failure is tagged `STATIC`
  (it never touches a device).
- This is a defensive safety net, not a substitute for fixing the
  underlying check — every checker should still validate its inputs
  properly (see `validator/checks/action_params.py::parse_finite_number`/
  `require_finite_number` above). Its purpose is that a single unanticipated
  bug in one checker (e.g. a raw comparison against a value that turned out
  to be `None`) no longer aborts every other check in the same `validate()`
  call — all three UI call sites (`_on_run`, `_on_validate_visual`,
  `_validate_dsl_text` in `ui/scheduler_window.py`) go through
  `apps/exp_scheduler/validation_service.py` (Phase 7), which itself calls
  `PreValidator().validate(...)` with no try/except of their own, so an
  uncaught exception there previously meant an unhandled crash instead of a
  validation dialog.

## DSL ASTValidator (`dsl/validator.py`) additions

`ASTValidator` runs on the raw DSL text before `SequenceBuilder` ever
builds a `Sequence`, so problems caught here never reach `PreValidator` at
all. Three additions close gaps that let malformed DSL calls silently
produce a broken `Action` instead of a syntax-level error:

- **Missing required arguments** (`_check_required_kwargs` /
  `_REQUIRED_KWARGS`): `dsl/parser.py`'s `SequenceBuilder` reads call
  keywords via `dict.get()` rather than calling the real `dsl/api.py`
  function, so a required argument omitted from the DSL (e.g.
  `set_temperature(value=300.0)` without `ramp_rate`) previously built
  successfully with the field silently set to `None` — which then either
  crashed `PreValidator` on a raw comparison or reached a device backend
  call at run time. Now flagged as `` `set_temperature(): missing required
  argument(s): ramp_rate` ``.
- **Positional arguments** (in `visit_Call`): `SequenceBuilder._build_call`
  only ever reads `node.keywords`, so a positionally-passed argument (e.g.
  `move_absolute(4, 10000)`) was silently dropped and the corresponding
  field fell back to its default (0/`None`) with no error at all. Now
  rejected outright: `` `move_absolute(): positional arguments are not
  supported — use keyword arguments` ``.
- **Non-finite numeric literals** (`_check_finite_args`, and the same check
  added to `visit_For`'s list-element validation): Python's own literal
  grammar can produce `inf` from an ordinary-looking overflow (e.g.
  `wait(duration=1e400)`) with no function call involved, so this cannot be
  caught by validating function calls alone — every numeric-literal keyword
  argument of every whitelisted function (and every `for ... in [...]` list
  element) is now checked for NaN/Inf independently of whether that
  argument has a configured lower bound in `_NUMERIC_BOUNDS`.
