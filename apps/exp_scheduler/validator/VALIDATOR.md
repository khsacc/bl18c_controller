# PreValidator の validation 項目

特に断りのない項目はエラー（Run 不可）。「警告」と書いたものは Run を止めない。
`ForLoopAction` を含む順序・状態のチェックは、ループを実際の反復回数だけ展開した実行順で行う。

## 1. DSL 構文チェック（`dsl/validator.py`、DSL 入力時のみ）

1. DSL: Python の構文エラーがあればエラーにする。
1. DSL: `import` / `class` / `def` / `lambda` / `while` / `if` / 代入 (`=`, `+=`, 型注釈付き代入) / `try` / `with` / `raise` / `del` / `global` / `nonlocal` / `yield` / `await` / async 構文をエラーにする。
1. DSL: dunder 名・dunder 属性へのアクセス、メソッド呼び出し (`obj.method()`)、動的な関数呼び出しをエラーにする。
1. DSL: DSL 関数一覧にない関数、および `exec` / `eval` / `open` / `getattr` / `print` などの禁止 built-in の呼び出しをエラーにする。
1. DSL: `for` ループはリテラルの数値リスト（有限の int/float のみ）を反復する場合だけ許可し、`for ... else` はエラーにする。
1. DSL: 位置引数をエラーにする（キーワード引数のみ対応）。
1. DSL: 必須のキーワード引数が省略されている場合にエラーにする。
1. DSL: 単位・選択肢が決まっている文字列引数（unit 等）が許可値以外の場合にエラーにする。
1. DSL: 下限が定義されている数値引数のリテラル値が下限を下回る場合にエラーにする（ループ変数の値は PreValidator 側で検証）。
1. DSL: 数値リテラル引数が NaN/Inf（`1e400` のようなオーバーフローを含む）の場合にエラーにする。
1. DSL: `take_xrd` で `osc_pos_a_deg` / `osc_pos_b_deg` / `osc_dwell_ms` / `osc_speed` を `oscillate=True` なしで指定した場合にエラーにする。

## 2. PreValidator（Validate 時）

### Sequence 構造

1. Sequence: シーケンスにトップレベルのアクションが一つもない場合にエラーにする。
1. Sequence: `ForLoopAction` の反復数が 2,000 を超える、全体の展開後ステップ数が 20,000 を超える、またはネスト深度が 4 を超える場合にエラーにする。上限を超えた場合、ループ展開が必要なチェックはスキップする。
1. Sequence: `ForLoopAction` の body が空の場合にエラーにする。
1. Sequence: `ForLoopAction` の `values` が空リストの場合にエラーにする（body が一度も実行されないため）。
1. Sequence: ループ変数が body 内で一度も使われていない場合に警告する。
1. Sequence: アクションが参照するループ変数（フィールドの直接参照、f-string の `{var}` の両方）が、その位置で定義されていない場合にエラーにする。
1. Sequence: 全く同一のアクション（種類・全パラメータが同じ）が連続している場合に警告する。
1. General: `wait()` および `follow_sample_position()` の `duration_s` が数値でない・NaN/Inf・0以下の場合にエラーにする。
1. Internal: 個々のチェック関数やスナップショット取得の内部で予期しない例外が起きた場合、validate 全体を止めずにその項目を内部エラーとしてエラー表示する。

### Global limits

1. Stage: Global limits が渡されている場合、Ch3/Ch4/Ch5 の ±mm の6値がすべて設定済みか確認する。
1. Stage: Global limits の6値が、それぞれ有限の数値かつ0以上か確認する（手編集・破損した設定ファイル対策）。

### Stage

1. Stage: ステージ操作（`StageAction`、`microscope_out_and_fpd_in`、`fpd_out_and_microscope_in`、`start_following`、`follow_sample_position`、`align_to_reference`、`take_spectrum`）がある場合、Stage controller が接続されているか確認する。
1. Stage: ステージが `PM16CControllerSim` の場合、シミュレーションモードであることを警告する。
1. Stage: ステージ操作がある場合、移動状態を読み取れるか、および現在ステージが移動中でないか確認する。
1. Stage: Stage controller が接続されている場合、Ch1〜Ch11 の現在位置をすべて読み取れるか確認する（読めないチャンネルは個別にエラー）。全チャンネルが読めた場合、その位置を validation 時の baseline として記録する。
1. Stage: `StageAction` の `operation` が `move_absolute` / `move_relative` / `set_speed` / `normal_stop` / `emergency_stop` 以外の場合にエラーにする。
1. Stage: `move_absolute` / `move_relative` / `set_speed` の `ch` が 1〜11 の整数でない場合にエラーにする（範囲外の `ch` はコントローラ側で無音の no-op になるため）。
1. Stage: `speed` が `H` / `M` / `L` 以外の場合にエラーにする（`StageAction` と `microscope_out_and_fpd_in` / `fpd_out_and_microscope_in` の両方）。
1. Stage: `move_absolute` / `move_relative` の位置・移動量（ループ変数の場合は `values` の全候補）が、数値でない・NaN/Inf・整数パルスでない・PM16C の範囲 ±2,147,483,647 を超える場合にエラーにする。
1. Stage: `microscope_out_and_fpd_in` / `fpd_out_and_microscope_in` で位置を明示指定している場合、その値が有限の整数パルスで範囲内か確認する。
1. Stage: Global Settings > Positions（Ch8/Ch9 IN/OUT）を使う場合、その4値が有限の整数パルスで範囲内か確認する。
1. Stage: Global Positions を使わず、`microscope_out_and_fpd_in` / `fpd_out_and_microscope_in` で位置が省略されている場合、`stage_settings.json` が存在して読めるか、必要なキー（`ch8_out`/`det_in` または `det_out`/`ch8_in`）があるか、その値が整数パルスで範囲内かを確認する。
1. Stage: Global Settings > Positions の Ch8/Ch9 IN/OUT が、FPD/Scope stage controller の最新入力（`stage_settings.json`）と1つでも異なる場合に警告する。`stage_settings.json` が無い・読めない場合はスキップする。
1. Stage: 現在位置が `MOVE_CONSTRAINTS`（現在有効なのは Ch8/Ch9 の干渉制約）に違反していないか確認する。
1. Stage: シーケンス中のステージ移動を実行順に模擬し、各移動が `MOVE_CONSTRAINTS` に違反しないか確認する。`microscope_out_and_fpd_in` / `fpd_out_and_microscope_in` は内部の移動ステップに展開し、`take_spectrum` はルビー位置への往復移動（Ch5→Ch4→Ch3 の順）と近傍探索範囲の端点も含め、Ch11 オシレーションは両端点を対象にする。
1. Stage: 同じ模擬の中で、Ch3/Ch4/Ch5 の各移動先が validation 時の位置から Global limits (±mm) を超えないか確認する（Runner が実行時に行うブロックを事前に再現する）。
1. Stage: `take_xrd` で Ch11 オシレーションが有効（ステップ指定または Global XRD 設定）な場合、Stage controller が接続済みか、移動状態を読めて移動中でないかを確認する。
1. Stage: `emergency_stop()` の後に `move_absolute` / `move_relative` が続く場合、意図した動作か確認を促す警告を出す（各 `emergency_stop()` につき最初の移動1回のみ）。

### Stage mode（顕微鏡 / XRD）

1. Stage: Ch6/Ch8/Ch9 の位置から stage mode を判定し、シーケンス中の移動を模擬しながら各ステップの mode を追跡する。microscope mode は「Ch6/Ch8 が登録済み X-ray beam 位置と一致し、Ch9 が OUT 以下」、XRD mode は「Ch9 が IN 位置、Ch8 が OUT 以下」、それ以外は unknown とする。
1. Stage: microscope mode で `take_xrd` / `take_dark` を実行しようとしている場合にエラーにする。
1. Stage: mode が unknown のまま `take_xrd` / `take_dark` を実行する場合に警告する。
1. Stage: XRD mode で `start_following` / `follow_sample_position` / `align_to_reference` / `save_reference_image` / `save_snapshot` を実行しようとしている場合にエラーにする。
1. Stage: microscope mode が確認できない状態（unknown）で `start_following` / `follow_sample_position` / `align_to_reference` を実行する場合にエラーにする（シミュレーション時は警告）。
1. Stage: `take_spectrum` を microscope mode 以外で実行する場合にエラーにし、前に `fpd_out_and_microscope_in()` を追加するよう促す。
1. Stage: `start_following` の追従が続いたまま `microscope_out_and_fpd_in` を実行する場合にエラーにする。

### Interactive Camera / 追従

1. Interactive Camera: カメラを使うアクション（`save_reference_image`、`save_snapshot`、`start_following`、`follow_sample_position`、`align_to_reference`、シミュレーション以外の `take_spectrum`）がある場合、使用する各 `camera_index` を `cv2.VideoCapture` で開けるか確認する。
1. Interactive Camera: 1つのシーケンス内で複数の `camera_index` が使われている場合にエラーにする。
1. Interactive Camera: `opencv-python` が無い場合、カメラ確認をスキップしたことを警告する。
1. Interactive Camera: 追従系アクション（`start_following` / `follow_sample_position` / `align_to_reference` / シミュレーション以外の `take_spectrum`）がある場合、`calibration.json` が存在し、JSON として読め、`matrix_inv` キーがあるか確認する。
1. Interactive Camera: 追従系アクションで reference image が設定されているか（ステップ指定または Global 設定）、そのファイルが存在するか確認する。
1. Gasket Beta: 位置追従方式が Gasket Beta の場合、許容差（µm、旧設定では px）が正の有限値か、カメラ calibration から px→µm 換算できるかを確認する。
1. Gasket Beta: ステップごとに Global 設定と異なる reference image を指定している場合にエラーにする。
1. Gasket Beta: reference image がガスケット輪郭を登録した画像と同一（digest 一致）か、画像を読み込めるか、reference 画像自体からガスケット穴を検出できるか、試料中心がマークされているかを確認する。
1. Gasket Beta: X-ray beam 位置が登録済みで、reference image と同じ画像サイズで登録されているか確認する。Ch6/Ch8 の登録位置との一致はシーケンス中に顕微鏡が動きうるため実行時に確認する（Ch7 は登録時と異なっても許容）。
1. Interactive Camera: `start_following` が追従中に再度呼ばれている場合にエラーにする。
1. Interactive Camera: `follow_sample_position` / `align_to_reference` が追従中（`start_following` 〜 `stop_following` の間）に呼ばれている場合にエラーにする。
1. Interactive Camera: `stop_following` が `start_following` より前に現れる場合にエラーにする。
1. Interactive Camera: `start_following` に対応する `stop_following` が無い場合、シーケンス終了まで追従が続くことを警告する。
1. Interactive Camera: `start_following` / `follow_sample_position` / `align_to_reference` のステップ個別指定（指定時のみ）について、`interval_s` が有限の正数か、`similarity_threshold` が 0〜1 の有限値か、`max_correction_per_step_um` が 0 以上の有限値かを確認する。
1. Interactive Camera: Autofocus の `autofocus_range_um` が有限の正数か、`autofocus_steps` が 2 以上の整数かを確認する。
1. Interactive Camera: 追従系アクションがあり、Ch3 の Global limits が未設定の場合、Autofocus で Ch3 が無制限に動きうることを警告する。
1. Gasket Beta: ルビーを用いた自動加圧ステップと試料追従ステップ（`start_following` / `follow_sample_position`）が存在し、追従方式が Gasket Beta で **Allow uniform ellipse scaling (Beta)** がオフの場合、加圧による穴径の大きな変化で追従が失敗する可能性を警告する（Run は止めない）。

### FPD (Rad-icon 2022)

1. FPD: `take_xrd` または `take_dark` がある場合、Rad-icon 2022 が接続済みか確認する。
1. FPD: `take_xrd` の `exposure_ms`（指定時のみ）、および `take_dark` の `exposure_ms` が数値でない・NaN/Inf・0以下の場合にエラーにする。
1. FPD: Ch11 オシレーションが有効な `take_xrd` で、両端の角度が有限の数値か、dwell が 0 以上の整数 (ms) か、速度が L/M/H か、両端がパルス換算で異なる位置になるかを確認する。
1. FPD: Global XRD 設定のファイル名サフィックスに不正なものがある場合にエラーにする。
1. FPD: Instant 1D が有効な場合、poni calibration が登録済みか、角度分解能が 0.001〜1 deg/bin か、CSV/TSV 自動保存時に X 軸単位が1つ以上選ばれているか、X 軸単位と PDIndexer 送信方式が正しい値かを確認する。
1. FPD: Global XRD 設定で dark / defect 補正が有効な場合、指定ファイルが存在しなければ警告する。
1. FPD: `take_xrd` のステップ個別の dark / defect ファイル指定が有効な場合、指定ファイルが存在しなければ警告する。

### PACE5000

1. PACE5000: PACE5000 操作（`set_pressure` / `wait_pressure` / `set_control_mode` / ルビーを用いた段階加圧）がある場合、PACE5000 が接続済みか確認する。
1. PACE5000: 圧力単位を読み取り（`:UNIT:PRES?`、設定変更はしない）、読めない・想定外の応答の場合はエラーにして圧力関連の検証をスキップする。
1. PACE5000: 圧力関連コマンドがある場合、現在の Control Mode (Output State) を読めなければエラーにする。
1. PACE5000: 圧力関連コマンドがあり、現在 Measure mode なのに `set_control_mode` が一度も呼ばれない場合にエラーにする。
1. PACE5000: Control Mode を最初に ON にするまでの流れが `set_pressure → set_control_mode(True) → wait_pressure` または `set_control_mode(True) → set_pressure → wait_pressure` のどちらでもない場合にエラーにする（ON 前に `set_pressure` が2回以上ある、ON 前に `wait_pressure` やルビー段階加圧が実行される場合）。
1. PACE5000: シーケンス中の最大設定圧力（ルビー段階加圧は許容最大ガス圧）が現在の +ve source 圧力を超える場合にエラーにする。+ve source 圧力が読めない場合もエラーにする。
1. PACE5000: `set_pressure` / `wait_pressure` の unit が MPa / Bar 以外、`rate_unit` が想定外の場合にエラーにする。
1. PACE5000: `set_pressure` の `pressure`（ループ変数は解決後の値）が数値でない・NaN/Inf・負の場合、`rate` が数値でない・NaN/Inf・負の場合にエラーにする。
1. PACE5000: `set_pressure` の `rate` を MPa/sec に換算した値が装置の最小 slew rate (0.001 MPa/sec) を下回る場合にエラーにする（`rate=0` も含む）。
1. PACE5000: `wait_pressure` の `tol` が数値でない・NaN/Inf・0以下の場合にエラー、0.0001 MPa 未満の場合に警告する。
1. PACE5000: `wait_pressure` の前に一度も `set_pressure` が実行されていない場合にエラーにする。
1. PACE5000: `set_pressure` の直後が `wait`（汎用）または `wait_pressure` でない場合に警告する。
1. PACE5000: `set_pressure` が `wait_pressure` を挟まずに続けて実行される場合に警告する。
1. PACE5000: `set_pressure` の直後が汎用 `wait()` で、次の `set_pressure` までに `wait_pressure` が無い場合、`|目標圧 − 現在の設定圧| / rate` で概算した所要時間より待機時間が短ければ警告する（現在の設定圧は装置から読んだ値から始めて実行順に更新する）。
1. PACE5000: `set_and_wait_pressure` は `set_pressure` + `wait_pressure` として扱い、上記のチェックをすべて適用する。

### ルビーを用いた段階加圧（PACE5000 + FluoRaPressée）

1. Compression: 段階加圧のパラメータを確認する — 単一のルビー位置を指定していること（全位置指定は不可）、ルビーの停止条件と併用していないこと、ガス圧単位が MPa/Bar・速度単位が MPa/min・Bar/min であること、目標試料圧・増分・許容最大ガス圧・到達許容差・（指定時の）速度が有限の正数、安定化待ち時間が有限の 0 以上であること、到達許容差が 0.0001 MPa 以上かつ増分未満であること、指定した速度が最小 slew rate 以上であること。
1. Compression: Ruby Fluorescence Settings の Pressure Calculation が有効になっているか確認する。
1. Compression: 段階加圧の開始時点で PACE5000 が Control mode になっているか、先行する `set_control_mode` を実行順に追って確認する。Control/Measure の状態が読めない場合もエラーにする。
1. Compression: 許容最大ガス圧が現在の +ve source 圧力**未満**か確認する（等しい場合もエラー）。
1. Compression: 開始時点の設定圧（先行する `set_pressure` または装置から読んだ値）が負または許容最大ガス圧を超える場合、設定圧が読めない場合にエラーにする。直前に別の段階加圧がある場合は最終設定圧が実行時まで分からないため警告にとどめ、実行時に確認する。
1. Compression: 許容最大ガス圧に達するまでに 1000 回超の増分が必要になる場合に警告する。
1. Compression: 到達確認がまだ無い状態で開始する場合、実測ガス圧が許容最大ガス圧を超えていればエラー、実測圧が設定圧の到達帯外なら警告する（最初のルビー測定前に到達を待つ）。実測圧が読めない場合はエラー、直前の `set_pressure` の到達を待つ場合は警告にする。
1. Compression: 段階加圧で使う slew rate（個別指定または装置から引き継ぐ値）が読めない場合、または最小 slew rate を下回る場合にエラーにする。

`decompress_to_target_pressure_using_ruby` にも同じ Ruby・Control mode・到達待ち・slew rate・測定値・単位の確認を適用する。ガス圧の許容値は `min_allowed_gas_pressure`（0 以上）とし、開始時の設定圧と実測圧が下限未満ならエラー、下限までの減分が 1000 回を超える場合は警告にする。現在の設定圧は +ve source 圧力未満でなければならない。実行時も各減圧・Ruby 測定の前後で下限、Control mode、設定圧、実測圧、供給圧を再確認する。

### LakeShore 335

1. LakeShore 335: LakeShore 335 操作（`set_temperature` / `wait_temperature` / `set_heater` / `all_heaters_off`）がある場合、接続済みか確認する。
1. LakeShore 335: 接続済みの場合、現在の設定値と現在のヒーターレンジを読めるか確認する（読めなければエラー。ヒーターレンジが読めない場合は順序チェックを中断する）。
1. LakeShore 335: `wait_temperature` があり、LakeShore 335 の読み取りデータがまだ無い場合に警告する。
1. LakeShore 335: `set_temperature` の `ramp_rate` が数値でない・NaN/Inf・負の場合、`value_k`（ループ変数は全候補）が数値でない・NaN/Inf の場合、300 K を超える場合にエラーにする。
1. LakeShore 335: `wait_temperature` の `tol_k` が数値でない・NaN/Inf・0以下の場合にエラー、0.01 K 未満の場合に警告する。
1. LakeShore 335: `wait_temperature` の `channel` が `A` / `B` 以外の場合にエラーにする（省略時は `B`）。
1. LakeShore 335: `set_heater` の `range_index` が 0〜3 以外の場合にエラーにする。
1. LakeShore 335: 以降の項目は、設定値とヒーター状態を実行順に追跡しながら判定する。
1. LakeShore 335: `wait_temperature` の前に一度も `set_temperature` が実行されていない場合に警告する。
1. LakeShore 335: `set_temperature` が `wait_temperature` を挟まずに続けて実行される場合に警告する。
1. LakeShore 335: `set_temperature` の設定値が直前の setpoint から変化していない場合に警告する。
1. LakeShore 335: 冷却方向で `ramp_rate` が 5 K/min 以上、加熱方向で 10 K/min 以上の場合、実際の速度が設定より遅くなる可能性があると警告する。
1. LakeShore 335: `set_temperature` の直後が汎用 `wait()` で、次の `set_temperature` までに `wait_temperature` が無い場合、`|目標 − 直前の設定値| / ramp_rate` で概算した所要時間より待機時間が短ければ警告する。
1. LakeShore 335: Validation 時点でヒーターが OFF で、最初の `set_temperature` より前に `set_heater` で ON にしていない場合に警告する。
1. LakeShore 335: ヒーターが OFF の状態（初期状態、`set_heater(0)`、`all_heaters_off` のいずれか）で `wait_temperature` を実行する場合に警告する。
1. LakeShore 335: `all_heaters_off` の後、ヒーターを入れ直さないまま `set_temperature` を実行する場合にエラーにする。
1. LakeShore 335: `wait_temperature` の直後に `follow_sample_position` / `start_following` が来る場合にエラーにする（正しくは `set_temperature → start_following → wait_temperature`）。
1. LakeShore 335: `set_temperature` から次の `take_xrd` までの間に `wait_temperature` が無い場合に警告する。
1. LakeShore 335: `set_temperature` から次の `take_xrd` までの間に `follow_sample_position`、または `start_following` + `stop_following`（継続中の追従を含む）が無い場合に警告する。

### FluoRaPressée (`take_spectrum`)

1. FluoRaPressée: `take_spectrum` がある場合、Spectrum settings が設定済みか確認する。
1. FluoRaPressée: ルビーのファイル名サフィックスに不正なものがある場合にエラーにする。
1. FluoRaPressée: `all_positions` が真偽値でない場合にエラーにする。`all_positions=True` で登録済みのルビー位置が一つも無い場合にエラーにする。
1. FluoRaPressée: 使用する各ルビー位置について、XRD 位置とスペクトル位置の両方が登録されているか確認する。
1. FluoRaPressée: ルビー位置を登録したときの位置追従方式が、現在の追従方式と異なる場合にエラーにする（再登録を促す）。
1. FluoRaPressée: 露光時間（ステップ指定または Global）が 0.01〜5 秒か確認する。
1. FluoRaPressée: EM Gain 上限（ステップ指定と Global の両方）が 1〜1000 の整数か確認する。
1. FluoRaPressée: R² 閾値が 0〜1、最大積算回数が 1〜10 の整数、フィット関数が Moffat / Pseudo Voigt / Gauss / Lorentz、フィット背景が constant / linear / quadratic / auto_polynomial のいずれかか確認する。
1. FluoRaPressée: ステージ速度が H / M / L のいずれかか確認する。
1. FluoRaPressée: settle time が 0 以上の有限値、timeout が 0 より大きい有限値か確認する。
1. FluoRaPressée: シミュレーション以外では API key が設定されているか確認する。
1. FluoRaPressée: 近傍探索の設定を確認する — 有効/無効が真偽値であること、Global 設定使用の指定が真偽値であること、タイミングが `always` / `on_low_quality` であること、Ch3/4/5 から重複なく1〜3軸を選んでいること、各軸の範囲が 0 より大きく 100 µm 以下・ステップが有限の正数であること、パルス丸め後に各軸2点以上あること、全グリッドが512点以下であること。
1. FluoRaPressée: ルビーの停止条件（目標圧到達でシーケンス終了）を使う場合、圧力計算が有効か、目標圧が有限の正数 (GPa) か、許容差が 0 以上かつ目標未満か確認する。目標圧なしで許容差だけ指定している場合もエラーにする。
1. FluoRaPressée: 圧力計算が有効な場合、圧力スケールが選択されているか、各ルビーの λ₀ が登録済みで 690〜700 nm の範囲か確認する。
1. FluoRaPressée: 温度補正が有効な場合、温度補正のオン/オフが真偽値か、温度補正スケールが選択されているか、温度ソースが LakeShore 335 Ch B または手入力か、手入力温度が 0.1〜1000 K か、Ch B 使用時の読み取り有効期限が 1〜600 秒か、各ルビーの T₀ が登録済みで 1〜1000 K か確認する。
1. FluoRaPressée: 温度補正の温度ソースが LakeShore 335 Ch B の場合、LakeShore が接続済みで、Ch B に有効な温度と時刻があり、その読み取りが有効期限内か確認する。
1. FluoRaPressée: 実機では `GET /status` で認証、busy でないこと、カメラ接続を確認する。
1. FluoRaPressée: `GET /hardware/camera/em-gain` で現在の EM Gain を1回読み、各ルビーステップの上限（ステップ指定または Global）を超えていればエラーにする。読めない場合もエラーにする。
1. FluoRaPressée: EM Gain を読んだ後に `GET /status` を再実行する。状態トークンが無い場合はエラー、読み取り中にトークンが変わった場合は暫定的に警告として試験取得をスキップする。
1. FluoRaPressée: 1D スペクトルモードであることを確認する。圧力計算が有効な場合は、さらに波長校正が適用済みで、軸の単位が波長 (nm) であることを確認する。
1. FluoRaPressée: 上記がすべて問題ない場合、最初のルビーステップの露光時間・積算1回・最新の状態トークンを付けて `POST /acquire` で試験取得を1回行う。HTTP 409 の `state_token_mismatch` だけは暫定的に警告とし、その他の失敗（busy、通信障害、不正な応答、新しい状態トークンが無い等）はエラーにする。ステージ移動や EM Gain の書き込みはしない。
1. FluoRaPressée: シミュレーションモードでは FluoRaPressée に接続せず、上記の実機確認は行わない。

### X-ray shutter

1. X-ray shutter: 「XRD 測定中のみシャッターを開ける」設定が有効なのに、シャッター制御 (STARS) が使えない場合にエラーにする。
1. PF ring: XRD の前後に Ring.Status を確認する設定が有効な場合、共有 STARS 接続に ring status reader がなければエラーにする。Validate 中は Ring.Status を読まず、実行時の各 take_xrd の前後で 4/5 のみ許容する。

## 3. Run 直前の再検証（`validation_service.revalidate_for_run`）

1. Run gate: Run の直前に、上記 PreValidator の全チェックを装置から読み直して再実行する。
1. Run gate: 一度も Validate に成功していない場合にエラーにする。
1. Run gate: Validate 後にシーケンスが変更されている場合にエラーにする。
1. Run gate: Validate 後に Global 設定が変更されている場合にエラーにする。
1. Run gate: Validate 後に接続機器（Stage / PACE5000 / LakeShore / Rad-icon / シャッターのインスタンス）が変わっている場合にエラーにする。
1. Run gate: Validate 時と現在で Ch1〜Ch11 の位置が1つでも異なる場合、またはどちらかで全チャンネルの位置が揃っていない場合にエラーにする。
