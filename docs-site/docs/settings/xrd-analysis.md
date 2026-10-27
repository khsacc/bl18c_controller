---
sidebar_position: 1
title: XRD analysis
---

# XRD analysis

X 線回折像の 1 次元化（方位角積分）に用いる検出器パラメータと、積分時のオプションを設定するページです。ここでの設定は Rad-icon 2022 controller、DAC Scan (XRD)、Experimental Scheduler などで共通に使われます。

## Poni File

- **Browse…** から pyFAI のキャリブレーションファイル（PONI ファイル）を読み込みます。読み込んだファイルは記憶され、次回起動時に自動で再読み込みされます。
- **Calibration Data** 欄に、読み込んだパラメータ（検出器距離、Poni1/Poni2、Rot1–3、波長、ピクセルサイズ）が表示されます。

## Reduction distance

- **Correct diamond thickness** にチェックを入れると、**Downstream diamond thickness** に入力した下流側ダイヤモンドの厚さを検出器距離に加えて 1 次元化を行います。
- この補正は 1 次元化のときにのみ適用され、PONI ファイル自体は書き換えられません。補正後の実効距離は **Effective 1D distance** として表示されます。

## 1D integration: spot rejection

- Rad-icon 2022 controller や Experimental Scheduler でスポット検出を有効にしている場合に使われる閾値です。
- 各 Debye リング内で「平均 + 指定した標準偏差の倍数」を超えるピクセルと、その周囲 3 ピクセル以内を 1 次元化から除外します。保存される画像は変更されません。

## 検出器較正

ページ下部のボタン（**Open Calibrate Detector Geometry…** / **Recalibrate…**）から、[検出器較正](../apps/xrd/detector-calibration.md)アプリを開けます。
