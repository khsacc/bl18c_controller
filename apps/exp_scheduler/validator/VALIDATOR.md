# PreValidator の validation 項目

1. Stage: Global limits が渡されている場合、Ch3/Ch4/Ch5 の +/- mm 上限がすべて設定済みか確認する。
1. Stage: ステージ操作がある場合、Stage controller が接続されているか確認する。
1. Stage: ステージが `PM16CControllerSim` の場合、シミュレーションモードであることを警告する。
1. Stage: ステージ操作開始前に、ステージが移動中でないか確認する。
1. Stage: `microscope_out_and_fpd_in` で位置が省略されている場合、`stage_settings.json` に `ch8_out` と `det_in` があるか確認する。
1. Stage: `fpd_out_and_microscope_in` で位置が省略されている場合、`stage_settings.json` に `det_out` と `ch8_in` があるか確認する。
1. Stage: Ch1-Ch11 の現在位置を全て読み取れるか確認する。
1. Stage: 現在位置が Ch8/Ch9 の `MOVE_CONSTRAINTS` に違反していないか確認する。
1. Stage: シーケンス中の各ステージ移動を順に模擬し、各移動が Ch8/Ch9 の `MOVE_CONSTRAINTS` に違反しないか確認する。
1. Stage: ステージ現在位置を validation 時の baseline として保存する。
1. Stage: Ch8/Ch9 の現在位置を読み、現在の stage mode が microscope / xrd / unknown のどれかを判定する。
1. Stage: Ch8/Ch9 の位置取得結果が `None` の場合、ステージ位置を取得できないエラーにする。
1. Stage: Microscope mode 中に `take_xrd` または `take_dark` が呼ばれていないか確認する。
1. Stage: Stage mode が unknown のまま `take_xrd` または `take_dark` が呼ばれる場合、FPD 位置未確認として警告する。
1. Stage: `ForLoopAction` の body 内で stage mode が変化する場合、次の反復の開始状態が変わることを警告する。
1. PACE5000: PACE5000 操作がある場合、PACE5000 が接続済みか確認する。
1. PACE5000: シーケンス中の最大設定圧力が、現在の PACE5000 +ve source 圧力を超える場合に警告する。
1. LakeShore 335: LakeShore 335 操作がある場合、LakeShore 335 が接続済みか確認する。
1. LakeShore 335`wait_temperature` がある場合、LakeShore 335 の読み取りデータがまだ無ければ警告する。
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


