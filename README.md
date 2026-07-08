# BL-18C Controller

高エネ研 PF BL-18Cにおける実験機器制御のための Python ベース GUI アプリケーションです。

バグ報告や改善・機能の追加の提案は、GitHub の Issues または E-mail でお願いいたします。

## アプリケーション

### ステージ制御を主たる目的とするもの

- Microscope + FPD stage control
  - 顕微鏡ステージ（Ch6, 7, 8）および検出器ステージ（Ch9）を制御する。
  - 顕微鏡ステージのIN位置およびOUT位置（Ch8）、検出器ステージのIN位置およびOUT位置（Ch9）を記憶し、ショートカットとして、１クリックでXRD測定と試料観察を切り替える。
  - 詳細な仕様は [こちら](apps/ui_stage_controller/fpd_scope_stg.md)
- Interactive camera
  - カメラ画像を基に、主に試料ステージ（Ch3,4,5）を動かす。補助的に、Ch7も一部操作することができる。
- Simple controller for all stages
  - 全てのステージをパルス数ベースで制御する。ユーザーは目的のパルス数の絶対値、ないし現在値からの相対値を入力し、ステージを動かす。ステージ移動にある程度習熟しているユーザー向け。
- DAC stage oscillation
  - 二つのCh11位置（Pos A, Pos B）を定義し、その間を指定された回数往復する。
  - 詳細な仕様は [こちら](./apps/dac_oscillation/dac_oscillation.md)

### スキャン

> いずれのスキャンにも、得られたシグナルを函数フィットする機能が備えられている。フィッティング関数として、Gaussian および erf が使える。 erf は、台形状のシグナルが得られた際に便利である。強度の立ち上がりを両端それぞれ独立にerfでフィットし、立ち上がり位置の中心点を中心として計算する。

- Collimator scan
  - Ch1およびCh2をスキャンしながら透過X線強度を取得し、フィッティングにより最適位置の推定を行う。
- DAC scan (normal)
  - Ch4およびCh5をスキャンしながら透過X線強度を取得し、フィッティングにより最適位置の推定を行う。
- DAC scan (rotation centre)
  - Ch11を変化させながらCh10をスキャンすることで、幾何学的に試料中心と回転中心の位置のズレを計算し、両者を一致させるのに必要なCh3およびCh4の移動量を推定する。
- DAC scan (XRD)
  - Ch4およびCh5をスキャンしながらFPDでX線回折強度を測定し、即時1次元化したうえで、ユーザーの指定した $2\theta$ 領域の回折強度でマッピングする。
- General 1D scan
  - 任意の並進ステージを選択し、透過X線強度をステージ位置の関数として取得し、フィッティングによる最適位置の推定を行う。
- General 2D scan
  - 任意の並進ステージを2つ選択し、透過X線強度によるマッピングと、各軸方向の Gaussian または erf フィッティングによる最適位置の推定を行う。

### X線回折データ取得に関連するもの

- Rad-icon 2022 (FPD) controller
  - CMOSフラットパネル検出器 Rad-icon 2022 の制御を行う。
  - 検出器幾何校正を本プラットフォーム内部で行うことで、取得したデータの即時1次元化も可能
- Calibrate detector geometry
  - 検出器幾何校正のためのアプリケーション。カメラ長を変えた標準試料データの取得（ないし既存データ読み出し）と、検出器幾何パラメータの最適化を行う。
  - 詳細な仕様は [こちら](./apps/calibrate_instruments/calibrate_instruements.md)

### 試料環境の制御に関連するもの

- Druck PACE5000 controller "PaceMaker"
  - メンブレン駆動DAC用ガス圧制御装置 Druck PACE5000 の制御および値の読み出し、ログ取りを行う。
  - このアプリケーションは、単体（スタンドアロン）でも起動でき、BL-18C 以外でも利用できる。→ [https://github.com/khsacc/PaceMaker](https://github.com/khsacc/PaceMaker)
- LakeShore 335 controller
  - 低温実験用温度コントローラ LakeShore 335 の制御と値の読み出し、ログ取りを行う。

### 実験の自動化を目的としたもの

- Experimental Scheduler
  - 本プラットフォームで制御可能な全ての機器を組み合わせて、自動実験を計画し実行する。


## XRDデータの解析に関する機能

CMOS 型フラットパネル検出器 Rad-icon 2022 で取得されたX線回折画像に対する基本的な解析機能を備えている。本プログラム内での解析には、Pythonを用いた2次元X線回折データ解析ライブラリである [PyFAI](https://pyfai.readthedocs.io/en/latest/index.html)  を用いている（[Kieffer and Karkoulis (2013a)](#Kieffer2013a), [Kieffer and Karkoulis (2013b)](#Kieffer2013b)）。 PyFAI の検出器幾何は公式ドキュメントの [General Introduction > Image representation in Python > Default geometry in pyFAI](https://pyfai.readthedocs.io/en/latest/geometry.html#default-geometry-in-pyfai) および [Example of usage > Tutorials > Geometries in pyFAI](https://pyfai.readthedocs.io/en/stable/usage/tutorial/Geometry/geometry.html) に記載されているとおりである。

PyFAI の検出器幾何校正ファイルは PONI ファイルと呼ばれる。PONI ファイルは、アプリ内の Settings > Detector Calibration から登録することができ、一度登録すると、本アプリ内のあらゆるサブアプリケーションから即時読み出せるようになる。また、 PONI ファイルを作成するための機能も備えており、**Calibrate detector geometry** アプリケーションから行える。

BL-18C のユーザーが多く利用している IPAnalyzer （[瀬戸ら (2010)](#seto2010)） における検出器幾何の定義は、 PyFAI のそれとは異なり、またパラメータファイル自体の形式も異なるため、そのままでは流用することはできない。これに対応するため、 **Calibrate detector geometry** アプリケーションの内部では、pyFAI での処理に用いる PONI ファイルに加えて、 IPAnalyzer での処理に必要な prm ファイルを同時に作成する機能を用いている。具体的には、 pyFAI の検出器幾何定義をIPAでの定義に変換する変換式を用い、 pyFAI で最適化された検出器幾何パラメータを数値的に変換してファイルを作成している。 PyFAI の PONI ファイルと、 IPAnalyzer の prm ファイル間の変換については、[別ドキュメント](apps/ipa_poni/ipa_poni_file_conversion.md) にまとめているので、必要な場合は参照されたい。

PyFAI を用いた処理は、具体的には以下のアプリで用いられている。

1. **DAC Scan (XRD)**: 特定の物質の Bragg 反射の強度をもとにマッピングを行う。試料ステージを動かしながら、 FPD で XRD データを取得し、それを pyFAI を用いて即時１次元化し、ある散乱角 $2\theta$ 領域（ROI）の強度を抽出する。
1. **Rad-icon 2022 (FPD) controller**: オプション機能として、得られたデータを即時2次元化し、CSV, TSV, GSAS (.gsa), Z-Rietveld (.histogramIgor) 形式で画像と保存する機能を備えている。オプション機能を利用するときは、 pyFAI に対応した校正情報（PONIファイル）が必要だが、PONIファイルが登録されていない状態でも、データの取得や画像の保存は可能である。
1. **Calibrate detector geometry**: 先述の通り、２つ以上の異なるカメラ長で取得された標準試料XRDデータをもとに、検出器位置を校正する。なお、本アプリは、検出器そのものの制御機能も備えており、標準試料データの取得も本アプリ内で行える。
1. **Experimental Scheduler**: 自動測定シーケンス内で XRD 測定を行う場合には、 pyFAI の校正情報が必要。

## 参考文献

- <a id="Kieffer2013a"></a><i>PyFAI, a versatile library for azimuthal regrouping</i>. J. Kieffer and D. Karkoulis. <i>J. Phys.: Conf. Ser.</i>, 425 202012 (2013) [http://dx.doi.org/10.1088/1742-6596/425/20/202012](http://dx.doi.org/10.1088/1742-6596/425/20/202012)
- <a id="Kieffer2013b"></a><i>PyFAI: a Python library for high performance azimuthal integration on GPU</i>. J. Kieffer and J. P. Wright. <i>Powder Diffraction</i>, 28, S2, S339--S350. (2013) [http://dx.doi.org/10.1017/S0885715613000924](http://dx.doi.org/10.1017/S0885715613000924)
- <a id="Seto2010"></a><i>X線回折実験における統合解析支援ソフトウェアの開発</i>. 瀬戸 雄介, 浜根 大輔, 永井 隆哉, 佐多 永吉. <i>高圧力の科学と技術</i>, 20, 3, 269--276. (2010)

## 開発関係

### オフライン起動


```bash
# Simulates the PM16C stage controller — no network connection required
python main.py --debug
```



### ライセンス

GPL-3.0

### 開発者
- Hiroki Kobayashi (Geochemical Research Center, Graduate School of Science, The University of Tokyo, Japan), hiroki [at] eqchem.s.u-tokyo.ac.jp

> 開発に際しては Claude Code の補助を利用しています。