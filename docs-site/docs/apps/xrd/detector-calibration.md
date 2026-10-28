---
sidebar_position: 2
title: Calibrate Detector Geometry
---

# Calibrate Detector Geometry

アプリケーション：**Calibrate detector geometry**

検出器幾何パラメータの校正を行うアプリケーションです。UI を上から順にスクロールしながら、必要な段階を踏んで進んでいけば、検出器の校正を行えるようになっています。

:::info[重要]
このアプリケーションは、ビームライン備え付けの Rad-icon 2022 によって取得されたデータの解析を目的としており、その他の検出器のデータには使えません。
:::

## Step 1: 標準試料の選択、画像の取得ないし読み込み

![Step 1 のスクリーンショット](/img/apps/xrd/calibrate_det_geometry/screenshot_calibrate_1.jpg)

- まず、標準試料として何を用いるかを選択します。**Other** を選んで試料名を入力すると、対応している場合は suggestion が表示されるので、それをクリックすると適用できます。
- 2 つ**以上**の異なる検出器位置において取得された、標準試料の回折像が必要です。
- まず、データを取得した際の検出器の mgs 値を入力します。次いで、**Take XRD** ボタンを押してデータをその場で取得するか、**Load Image** ボタンを押して、すでに取得してあるデータを読み込みます。
  - Take XRD を行うためには、別ウィンドウで Rad-icon 2022 controller が立ち上がっていることが必要です。立ち上げていない場合は、メインウィンドウから立ち上げ、必要なパラメータを設定しておいてください。露光時間など測定に関するパラメータは、すべて、現在開かれている Rad-icon 2022 controller の画面から読み込まれ、そのまま利用されます。

## Step 2: 初期パラメータの設定

![Step 2 のスクリーンショット](/img/apps/xrd/calibrate_det_geometry/screenshot_calibrate_2.jpg)

- 初期パラメータの入力方法として、(1) IPAnalyzer のパラメータファイル、(2) pyFAI のパラメータファイル（PONI ファイル）、(3) 手動入力、の 3 つが選べます。
- 手動入力の場合、検出器距離と中心位置、波長を大まかに合わせておけば十分です（わりと大雑把で大丈夫です）。
  - **Auto-detect beam centre in the primary image** ボタンを押すと、Primary image（Position 1 のデータ）から中心位置を自動的に推定して補完します。真円の場合、画像をフリップさせても重心は変化しないことを利用して、中心のピクセル位置を推定しています。この推定は、とくに一部が欠けている Debye リングがある場合にはあまりうまくいかないこともありますが、続くパラメータ校正のサイクルで修正できる範囲には入る可能性が高いので、まずこれを試してみるのがおすすめです。
  - 一つ前のステップで読み込んだ Primary 画像がアプリ内に表示されています。その画像内で右クリックすると、右クリックした位置を中心位置として登録できます。

## Step 3: 校正の実行

![Step 3 のスクリーンショット](/img/apps/xrd/calibrate_det_geometry/screenshot_calibrate_3.jpg)

校正を行うパラメータを選択し（デフォルトではすべてにチェックが入っています）、**Calibrate parameters** を押します。入力された数だけサイクルを実施し、検出器のパラメータを求めます。

校正から得られたパラメータの良し悪しを判定するために、アプリケーションは以下の 4 つのプロットを表示します。

1. 求められたパラメータをもとに 1 次元化した、Primary 画像の X 線回折データ

   ![1 次元化した回折データ](/img/apps/xrd/calibrate_det_geometry/metrics_1D.jpg)

2. 求められたパラメータをもとに方位角積分した、Primary 画像の X 線回折データ（いわゆるケーキプロット）

   ![ケーキプロット](/img/apps/xrd/calibrate_det_geometry/metrics_2D.jpg)

3. 求められたパラメータをもとに標準試料の Debye リング上を方位角積分した際に、計算値と実測値がどれくらいずれているか（RMS $\Delta 2\theta$）を Primary 画像の X 線回折データに対して計算し、方位角の函数としてプロットしたもの

   ![方位角ごとの RMS Δ2θ](/img/apps/xrd/calibrate_det_geometry/metrics_deviation.jpg)

4. $\chi^2$ および RMS $\Delta 2\theta$ がサイクルを経るごとにどう変化したかを示すプロット

   ![サイクルごとの χ² と RMS Δ2θ の推移](/img/apps/xrd/calibrate_det_geometry/metrics_cycles.jpg)

## Step 4: 結果の確認

![Step 4 のスクリーンショット](/img/apps/xrd/calibrate_det_geometry/screenshot_calibrate_4.jpg)

- 得られた検出器パラメータが表示されます。また、それぞれのパラメータの定義を示すために、pyFAI のチュートリアルから抜粋した画像を表示しています。
- **Save and Apply Calibration** を押すと、pyFAI のキャリブレーションファイル（PONI ファイル）と IPAnalyzer のキャリブレーションファイル（prm ファイル）の 2 つが保存されます。同時に、本アプリ内で再利用できるように、得られたパラメータをアプリ内に適用します。
  - IPAnalyzer の prm ファイルへの変換については、[こちらのドキュメント](https://github.com/khsacc/bl18c_controller/blob/main/docs/DOC_IPA_PONI.md)を参照してください。
  - PONI ファイルのアプリ内再利用については、[README](https://github.com/khsacc/bl18c_controller/blob/main/README.md#xrdデータの解析に関する機能)を参照してください。
