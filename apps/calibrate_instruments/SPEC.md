# Calibrate Detector Geometry 設計仕様

このドキュメントは `apps/calibrate_instruments/` の実装仕様を定義する。
実装作業を開始する前に必ずこのファイル全体を読むこと。

---

## 概要

Rad-icon 2022 で撮影した標準試料（calibrant）のXRDリング画像から、pyFAIの
`AzimuthalIntegrator`ジオメトリ（distance / poni1 / poni2 / rot1 / rot2 / wavelength）を
校正するアプリ。既存の `settings/pages/detector_calibration.py`（単一画像・波長固定の
簡易校正）を置き換えるものではなく、**複数の検出器位置を跨いだ本格校正**を行う上位互換として追加する。

- Rad-iconアプリ（[radicon_ui.py](../Rad_icon_2022/radicon_ui.py)）内部から
  サブアプリとして呼び出せる（同じ`RadiconBackend`インスタンスを共有）。
- `main.py`のランチャーからも独立ウィンドウとして開ける（`XrdScanWindow`と同じ形）。
- ユーザーは標準試料をセットしたまま、実際にステージを手で動かし・撮影しながら
  ステップバイステップで校正を完了できる。
- pyFAI公式チュートリアル「Fitting wavelength with multiple sample-detector
  distances」（`GoniometerRefinement` + `ExtendedTransformation`）と同じ考え方を、
  BL-18Cの検出器並進ステージ（Ch9）+ マグネスケール(mgs)という具体的なハードウェアに
  合わせて簡略化したもの。

---

## 前提：距離パラメータの決め方（重要）

検出器位置を変える軸は **Ch9（検出器 IN/OUT、`PULSE_SCALE[9]` = 10 µm/pulse）**。
ただし、Ch9のパルス値そのものを校正の距離パラメータには使わない。

BL-18Cの検出器ステージには **マグネスケール（mgs）** という絶対値リニアスケールが
付いており、これが実際の距離基準になる：

- 単位はmm。試料に近づくほど値が小さくなる（mgs=0mmが試料位置という意味ではなく、
  あくまである基準点からの相対距離）。
- **ユーザーが物理スケールを目視し、UIに手入力する**（自動読み取りはできない）。
- Ch9のパルス値は「Take XRD」時に**アプリが自動取得して記録するが、校正計算には使わない**
  （参照用ログ。将来 mgs↔Ch9パルスの対応表として使える副産物になる）。

このため、pyFAIチュートリアルの `ExtendedTransformation` は BL-18C版として以下のように単純化される：

```python
goniotrans = ExtendedTransformation(
    param_names=["dist0", "poni1", "poni2", "rot1", "rot2", "scale0", "energy"],
    pos_names=["mgs"],
    dist_expr="dist0 + mgs*scale0",   # mgs[mm] → m 換算。scale0 の初期値は 1e-3
    poni1_expr="poni1",               # 並進ステージのみなのでpositionに依存しない
    poni2_expr="poni2",
    rot1_expr="rot1",
    rot2_expr="rot2",
    rot3_expr="0",
    wavelength_expr="wavelength",      # 固定運用時。energyをfitする場合は "hc/energy*1e-10"
)
```

`pos_function(param) -> mgs値(mm)` は各位置ラベルに紐づけて保存した mgs 入力値を返すだけでよい
（チュートリアルの `distance(param): return float(param)` と同じ発想）。

---

## ディレクトリ構成

```
apps/calibrate_instruments/
├── SPEC.md                          ← このファイル
├── __init__.py
├── calibrate_instruments_backend.py  # データクラス、MultiPositionCalibrationWorker (QThread)
└── calibrate_instruments_app.py      # CalibrateInstrumentsWindow (QWidget)
```

**既存ファイルへの変更：**
- [main.py](../../main.py)：`open_calibrate_instruments()` メソッドとランチャーボタンを追加
- [radicon_ui.py](../Rad_icon_2022/radicon_ui.py)：`RadiconWindow.__init__`に`controller`
  optional kwargを追加し、「校正ウィザード…」ボタンから`CalibrateInstrumentsWindow`を開けるようにする
- 新規 `utils/poni_io.py`：poniファイルの parse/build/write を一本化（後述「既存コードとの整理」）

---

## データモデル（`calibrate_instruments_backend.py`）

```python
@dataclass
class CalibrationPosition:
    label: str                        # "Primary", "Position 2", ... （+/-ボタンで増減）
    is_primary: bool = False
    mgs_mm: float | None = None        # ユーザー手入力。校正の pos として使用
    ch9_pulse: int | None = None       # Take XRD時に自動記録。計算には使わない（参照用）
    image: np.ndarray | None = None    # Take XRDで取得したフレーム（flip補正済み）
    control_points_n: int | None = None  # extract_cp後の検出点数（UI表示用）
    single_geometry: object | None = None  # pyFAI SingleGeometry インスタンス

@dataclass
class InitialGeometrySource:
    mode: str   # "prm" | "poni" | "manual"
    prm_path: pathlib.Path | None = None
    poni_path: pathlib.Path | None = None
    manual: "ManualInitialParams | None" = None

@dataclass
class ManualInitialParams:
    distance_mm: float
    beam_center_x_px: float
    beam_center_y_px: float
    rot1_deg: float = 0.0
    rot2_deg: float = 0.0
    wavelength_ang: float | None = None   # どちらか一方を指定
    energy_kev: float | None = None

@dataclass
class FreeParamStages:
    """段階的リファインでどこまで自由パラメータに含めるか。"""
    fit_beam_geometry: bool = True     # Stage2: poni1,poni2,rot1,rot2 を追加（デフォルトON）
    fit_mgs_scale: bool = False        # Stage3: scale0（mgs→m換算）を追加（詳細設定）
    fit_wavelength: bool = False       # Stage4: wavelength/energy を追加（詳細設定）
```

---

## Calibrant選択

- ユーザー提案の「プルダウンないしラジオボタン」は、pyFAIの登録済みcalibrantが150種以上ある
  ため、ラジオボタン一覧は非現実的。**検索可能なコンボボックス**（`QComboBox(editable=True)`
  + 前方一致フィルタ）を採用し、`pyFAI.calibrant.ALL_CALIBRANTS`のキー一覧から動的に構築する。
- デフォルトは `"CeO2"`。
- よく使う数種（CeO2 / LaB6 / Si）は「クイック選択」ボタンとしてコンボの隣に並べ、
  クリック一発で選べるようにする（クリック数を減らすための追加提案）。
- 選択したcalibrantの`wavelength`属性は、初期ジオメトリソース（③）で決まった波長で上書きする
  （既存の`calibration_worker.py`と同じパターン）。

---

## 初期パラメータのソース

3モードをタブ／ラジオボタンで切替。いずれのモードでも最終的に単一の初期
`AzimuthalIntegrator`を構築する点は共通。

| モード | 入力 | 実装 |
|---|---|---|
| **IPA prmファイル** | `.prm`を1つ選択 | 既存の[`parse_ipa_prm`/`ipa_to_poni`](../ipa_poni/ipa_to_poni.py)をそのまま再利用 |
| **既存poniファイル** | `.poni`を1つ選択 | 新設 `utils/poni_io.parse_poni`/`build_ai` を利用 |
| **手動入力** | 下記7項目のみ | 新規UI |

手動入力で要求する項目：

- Distance（mm）
- Beam center X, Y（**pixel単位**。内部でpixel sizeからPoni1/Poni2(m)に変換）
- Pixel size（µm、正方ピクセル前提）
- Rot1, Rot2（deg、デフォルト0）
- Wavelength（Å）または Energy（keV）— どちらかをトグルで選んで入力

**実装時の修正**：当初「pixel sizeはRad-iconのbinning設定から自動決定できるので聞かない」と
想定していたが、このコードベースにはRad-icon 2022の物理ピクセルピッチを表す定数がどこにも
定義されておらず（既存のprm/poni経由の校正でも常にファイル側のDetector_config/pixSizeから
読んでいるだけ）、手動モードでは自動決定する元データが存在しないことが実装時に判明した。
そのため手動入力にPixel size（µm）を追加した（6→7項目）。distanceやbeam centerの実長換算は
pixel size次第で線形にスケールするため、当てずっぽうの既定値を埋め込むより明示的に聞く方が安全。

---

## UI構成（`calibrate_instruments_app.py` — `CalibrateInstrumentsWindow`）

```
┌───────────────────────────────────────────────────────────────────────┐
│ Calibrant: [CeO2 ▾ (検索可)]  [CeO2] [LaB6] [Si]  (クイック選択)        │
├───────────────────────────────────────────────────────────────────────┤
│ 初期パラメータ: (○) IPA prm  (○) poni file  (○) 手動入力                │
│   [Browse…] xxx.prm                                                    │
├───────────────────────────────────────────────────────────────────────┤
│ 検出器位置                                                    [+] [-]  │
│ ┌─────────────────────────────────────────────────────────────────┐  │
│ │ (●) Primary   mgs: [______] mm   Ch9: 12345 (参照)  [Take XRD]  ✓ 42点│
│ │ ( ) Position2 mgs: [______] mm   Ch9: 23456 (参照)  [Take XRD]  未撮影│
│ └─────────────────────────────────────────────────────────────────┘  │
│ ※ ステージの移動は本アプリでは行わない。既存のステージ操作アプリで動かし、│
│    物理スケールを読んでmgs欄に入力してから Take XRD を押す。            │
├───────────────────────────────────────────────────────────────────────┤
│ 詳細設定: [ ] scale0(mgs→m)もfitする   [ ] wavelength/energyもfitする   │
│           [Calibrate parameters]                                       │
├───────────────────────────────────────────────────────────────────────┤
│ 進捗ログ / リング検出オーバーレイ（位置ごとにタブ or グリッド表示）      │
│ 最終結果: dist / poni1 / poni2 / rot1 / rot2 / wavelength / 各位置残差  │
├───────────────────────────────────────────────────────────────────────┤
│ [primary位置で評価したジオメトリをSave poni…] → PoniStateへ反映         │
└───────────────────────────────────────────────────────────────────────┘
```

### 位置行の挙動

- `mgs`欄が空のうちは「Take XRD」を無効化。
- 直前の行と同じmgs値のまま「Take XRD」を押そうとしたら確認ダイアログ
  （ステージの動かし忘れ・入力し忘れの典型ミス対策）。
- 「Take XRD」押下時の処理：
  1. Ch9の現在パルス値を読み取り、その行に記録（参照用のみ）。
  2. `backend.snap()`でRad-iconに現在登録済みのexposure/binningのまま撮影。
  3. flip補正のみ適用（RadiconWindowから開いた場合は親windowの`_apply_flip`設定を
     共有、standalone起動時は`radicon_ui_prefs.json`から読む）。dark/defect補正は
     校正には不要と判断し適用しない。
  4. その場で`SingleGeometry.extract_cp()`を実行し、検出点数と画像上のオーバーレイを表示。
- `+`/`-`ボタンで行を増減。デフォルト2行（Primaryフラグ付き1行 + 1行）。

### ステージ移動について

このアプリ自体はCh9の移動操作を持たない（Ch9の現在位置は`ControllerPoller`と同じ
300msポーリングで読取表示するのみ）。ユーザーは既存のStage Controllerアプリ等で
物理的にステージを動かす。

---

## 校正エンジン（`MultiPositionCalibrationWorker`, QThread）

段階的リファイン → 最終的に`refine3(method="simplex")`で追い込む、という
チュートリアルと同じ2段仕上げの方針を、BL-18C版のstage概念で表現する。

```python
class MultiPositionCalibrationWorker(QThread):
    progress         = pyqtSignal(str)                    # ステータス文字列
    ring_extracted   = pyqtSignal(str, object)             # (label, control_points可視化用データ)
    stage_completed  = pyqtSignal(str, float)              # (stage名, chi2)
    completed        = pyqtSignal(object, dict)            # (ai_primary, results dict)
    failed           = pyqtSignal(str)
```

### 実行ステップ

1. 初期`AzimuthalIntegrator`を①のソースから構築。
2. 各位置について`SingleGeometry(label, image, calibrant, detector, geometry=ai_initial)`
   → `extract_cp()` を実行し、`ring_extracted`を発火（UIがオーバーレイ表示）。
3. `GoniometerRefinement(param, bounds=..., pos_function=mgs_lookup, trans_function=goniotrans, detector=..., wavelength=...)`
   を構築し、各位置の`SingleGeometry`を`new_geometry(label, image=..., control_points=..., metadata=...)`
   で登録。
4. **Stage 1**：`dist0`のみ自由（他は初期値に固定）→ 大まかな距離オフセットを確定。
5. **Stage 2（デフォルトはここまで実行）**：`poni1, poni2, rot1, rot2`も自由化 → `refine2()`。
6. **Stage 3（`fit_mgs_scale=True`の場合）**：`scale0`も自由化。
7. **Stage 4（`fit_wavelength=True`の場合）**：`wavelength`（`energy`経由）も自由化。
8. 最後に`refine3(method="simplex")`でbounds無しの追い込み。
9. 各段階のchi²を`stage_completed`で通知。
10. **primary位置のラベルで`goniomref.get_ai(primary_label)`相当を評価**し、それを
    `completed`シグナルのAIとして返す（他の位置は校正専用のダミー点であり、実測定に
    使うジオメトリはprimary位置で評価したものであることに注意——実装で見落としやすい点）。

---

## 可視化

- 各位置の画像パネル（グリッドまたはタブ）にリング検出点を色分けオーバーレイ表示
  （チュートリアルのsubplotグリッドと同じ見せ方）。新規チャートを作る際は
  `/dataviz`スキルの配色規約に従う。
- 進捗ログ（`progress`シグナルのテキスト）をQLabelかログウィジェットに逐次表示。
- 校正完了後：chi²（Stage毎）、最終ジオメトリ（Distance/Poni1/Poni2/Rot1/Rot2/Wavelength）、
  各位置の残差を表で表示。既存`detector_calibration.py`の`_CHI2_LINE`/`_DIST_LINE`パターンを流用。
- 位置ごとの残差が大きい場合、ユーザーに「その位置を再撮影/再抽出すべき」と示唆する
  警告表示（チュートリアル本文の「control pointsの分布が偏っていたら再抽出」という注意点に対応）。

---

## 保存とPoniState連携

- 「Save poni…」は**primary位置で評価したAI**を保存する。
- 保存後、`PoniState.update(ai=ai_primary, ...)`を呼び、`RadiconWindow`のInstant 1D・
  `XrdScanWindow`にも即座に反映される（既存の`poni_changed`シグナル経由、変更なし）。
- セッション状態（calibrant選択・初期ソース・各位置のmgs値/画像パス等）は
  `apps/calibrate_instruments/__localdata/session.json`に保存し、次回起動時に復元する
  （プロジェクト規約：ファイル/ディレクトリ選択は最後に使った場所を`__localdata`に保存）。

---

## 既存コードとの整理

- `parse_poni`/`build_ai`が[`xrd_scan_backend.py`](../xrd_scan/xrd_scan_backend.py)と
  [`calibration_worker.py`](../../settings/calibration_worker.py)相当の箇所に重複しているため、
  新規 `utils/poni_io.py` に一本化し、本アプリもそこを使う。既存2箇所は影響範囲を絞るため、
  このアプリの実装が安定してから追って移行する（本アプリのブロッカーにはしない）。
- 既存の[`settings/pages/detector_calibration.py`](../../settings/pages/detector_calibration.py)
  （単一画像・CeO2固定・波長固定の簡易校正）は**残す**。位置づけは「クイック再校正
  （既存の1枚のTIFF画像から）」とし、本格的な複数位置校正は本アプリを案内する。

---

## main.py / radicon_ui.py への変更

```python
# main.py
def open_calibrate_instruments(self):
    self._launch_window(
        self.btn_calibrate_instruments,
        lambda: CalibrateInstrumentsWindow(
            controller=self.controller,
            backend=self.radicon_backend,
            poni_state=self.poni_state,
        ),
    )
```

```python
# radicon_ui.py — RadiconWindow.__init__ に controller optional kwarg 追加
def __init__(self, backend: RadiconBackend, poni_state: "PoniState | None" = None,
             controller=None, parent=None):
    ...
    self._controller = controller
```

`RadiconWindow`内に「校正ウィザード…」ボタンを追加し、`_open_detector_calibration`と
同様のパターンで`CalibrateInstrumentsWindow`を非モーダル表示する
（`controller`が`None`の場合はボタンを無効化し、ツールチップで理由を表示）。

---

## 決定済み事項

| 事項 | 決定内容 |
|------|----------|
| 検出器位置の移動軸 | Ch9（検出器 IN/OUT） |
| 距離の基準値 | マグネスケール(mgs, mm単位, ユーザー手入力)。Ch9パルス値は参照記録のみで計算に使わない |
| ステージ移動操作 | 本アプリには埋め込まない。読取表示のみで、移動は既存のStage Controllerアプリを併用 |
| 自由パラメータの段階 | Stage1(dist0) → Stage2(+poni1,poni2,rot1,rot2, デフォルトここまで) → Stage3(+scale0, 任意) → Stage4(+wavelength/energy, 任意) → simplexで仕上げ |
| Calibrant選択UI | 検索可能なコンボボックス（pyFAI ALL_CALIBRANTS全件）+ CeO2/LaB6/Siのクイック選択ボタン。デフォルトCeO2 |
| 初期パラメータのソース | IPA prm / 既存poniファイル / 手動入力（Distance, Beam center px, Pixel size, Rot1・Rot2, Wavelength or Energyの7項目。Pixel sizeはコードベースに固定値が存在しなかったため実装時に追加）の3モード |
| 保存するジオメトリ | primary位置ラベルで評価したAIのみ（他位置は校正専用データ点） |
| 既存 `settings/pages/detector_calibration.py` | 残す。「クイック単一画像校正」として位置づけ、本アプリはフル校正（推奨）として案内 |
| poni parse/buildの重複 | `utils/poni_io.py`に一本化する方針だが、本アプリのブロッカーにはしない（後日移行） |

---

## 実装状況（2026-07-07）

一通り実装済み：
- `utils/poni_io.py`（parse_poni/build_ai/write_poni 共通化。既存のxrd_scan/detector_calibrationは未移行のまま）
- `apps/calibrate_instruments/calibrate_instruments_backend.py`
  （`CalibrationPosition`/`ManualInitialParams`/`FreeParamStages`、`MultiPositionCalibrationWorker`の段階的refine実装）
- `apps/calibrate_instruments/calibrate_instruments_app.py`（`CalibrateInstrumentsWindow` — 全UI）
- `main.py`：ランチャーに「Calibrate Detector Geometry」ボタン追加（XRDセクション）、radicon接続時に有効化
- `radicon_ui.py`：`RadiconWindow`に`controller`引数追加、Instant 1Dパネルに「Calibration wizard…」ボタン追加

簡略化・軽微な設計変更（実装中に決定）：
- 手動入力は6→7項目に変更（Pixel size追加。理由は上記「初期パラメータのソース」節）。
- 各位置行にCh9のライブ表示は持たせず、Take XRD時にコントローラーから直接読んで記録するのみ
  （毎行に同じ値を表示するのは冗長と判断）。
- セッション状態（`__localdata/calibrate_instruments_prefs.json`）は calibrant名・最終使用ディレクトリのみ保存。
  各位置のmgs値・画像・初期ソースの選択状態は永続化していない（再起動したら撮り直しが必要）。
  必要になったら拡張する。

## pyFAI計算ロジックの実地検証（2026-07-07 追記）

開発環境にpyFAIが導入されたため、`calibrant.fake_calibration_image()`で「正解が既知の」
CeO2リング画像を合成し、`MultiPositionCalibrationWorker`が正しく元のジオメトリを復元できるかを
実際に検証した。その過程で以下の**実質的なバグ**を発見し、修正済み：

| # | バグ | 症状 | 修正 |
|---|------|------|------|
| 1 | `GoniometerRefinement.refine2()`の戻り値はchi2ではなくパラメータ配列 | `float(chi2)`が配列変換エラーでクラッシュ | `gonioref.chi2()`を別途呼んで取得 |
| 2 | `gonioref.bounds`はコンストラクタ後は**辞書ではなくリスト**（`param_names`順） | `bounds["poni1"] = ...`が`TypeError`でクラッシュ | `param_names.index(name)`で位置を引いてリストに代入 |
| 3 | `refine3()`（simplex）は`bounds`を一切見ず、`fix=`引数で明示しない限り全パラメータを自由化する | `scale0`・`energy`を固定していたつもりが最終段で暴れ、距離が数十%ずれる | `fix=["scale0", "energy"（fit_wavelength=False時）]`を明示 |
| 4 | `dist0`の初期値に`ai_initial.dist`をそのまま使っていた | `dist0`は「mgs=0mmでの距離」であり「primary位置での距離」ではないため、初期値が実質的に大きく誤っていた | `dist0_init = ai_initial.dist - primary.mgs_mm * scale0`で逆算 |
| 5 | **（最も影響大）** 全position共通の1つの`ai_initial`でリング検出（`extract_cp`）していた | position間で実距離が大きく異なる（例：100mm vs 200mm）と、遠い位置ほどリング次数の誤認識が起き、フィット全体が破綻（本来の半分の距離に収束する等）していた | position毎に`dist0_init + mgs*scale0`で推定した距離を使う個別のai（poni/rot/波長は共通）でリング検出するよう変更 |

**検証結果**：現実的な初期値（実際のprm/poniファイルに近い精度、真値から数%以内）を与えた場合、
最終的な距離・poni1・poni2の誤差は0.1%未満に収束することを確認した（3位置、CeO2、rot1/rot2含む
全パラメータ同時フィット）。初期値が真値から大きく乖離している場合（10%超）は収束が不安定になる
場合があるため、初期パラメータのソース（IPA prm / 既存poniファイル）はできるだけ現実の状態に
近いものを使うことを推奨する。

## 要確認事項（Windows実機で追加確認すること）

- `pyFAI.calibrant.ALL_CALIBRANTS`のキー列挙方法（macOS開発環境のpyFAIでは動作確認済みだが、
  Windows側のpyFAIバージョンでも同様に動作するか）。
- 実際のRad-icon画像（合成画像ではなく本物のCeO2リング）でのend-to-end動作確認。
- 検出器のpixel size・shape・マスク（飽和ピクセル等）を考慮していない点 — 本実装は
  `detector_calibration.py`の`_make_mask`のような飽和マスクを組み込んでいない。実機で
  飽和が問題になる場合は追加が必要。

その他の未解決事項：
- Ch9パルス値↔mgs値の対応ログを、将来「mgs不要の簡易再校正」に転用する構想があるか
  （現時点では純粋な参照ログとしてのみ保持）。
