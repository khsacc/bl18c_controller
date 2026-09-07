# 実装計画 — 1次元化データを bl18c_controller から PDIndexer へ直接送る

Status: **実装済み（Phase 0 の Windows 実機検証のみ未了）**  
最終更新: 2026-09-06

### 実装状況（2026-09-06）

| Phase | 状態 |
|---|---|
| 0 — 実測スパイク | **未着手（要 Windows 実機 + IPAnalyzer + PDIndexer）** |
| 1 — C# ヘルパー | コード一式実装済み。**未コンパイル**（本セッションに .NET SDK なし）。ロジックは手動精査済み |
| 2 — Python ラッパー | 実装・テスト済み |
| 3 — UI 統合 | Rad-icon 2022 (Instant 1D panel) / XRD Scan (ROI dialog) に統合済み、実機（PyQt6, offscreen）で動作確認済み |
| 4 — .pdi フォールバック | 実装・テスト済み |
| 5 — ドキュメント + ドリフト検出 | 実装済み。ドリフト検出は実際に upstream に対して実行し、現在ズレなしを確認済み |

**次にやるべきこと**: Windows 機で `utils/pdindexer/csharp/build.bat` を実行して
`PdiSender.exe` をビルドし、Phase 0（§3）の実測とゴールデンデータ採取、
V5〜V11 の受け入れテストを行うこと。

### 実装後レビュー（2026-09-06）で判明し修正した不具合

実装直後のコードレビューで、以下の重大〜中程度の不具合が見つかり、いずれも修正済み。
詳細と再発防止の理由は
[utils/pdindexer/IMPLEMENTATION_DETAILS.md](../utils/pdindexer/IMPLEMENTATION_DETAILS.md)
に反映済み（本節よりそちらが常に最新）。

- `.pdi` フォールバックが `.tmp`→`os.replace()` で書いていたため、Windows の
  `FileSystemWatcher` 上は `Renamed` イベントになり、`Created` しか購読していない
  PDIndexer に検出されない不具合。最終ファイル名への排他生成に変更。
- シーケンス送信のFIFOが、完了コールバックの二重経路により2件目以降を並行起動していた
  不具合。キュー前進を単一の経路に統合。
- クリップボード経路が実露光時間を`IsCPS=true`のまま送っており、PDIndexerが強度を
  露光時間で除算していた不具合（IPAnalyzer自身は常に既定値=恒等で送っている）。
  実露光時間の送信をやめ、両経路とも恒等（現状追従）に統一。
- 共有 `PdiService` の送信経路が、いずれかのウィンドウのUI更新で他ウィンドウの選択を
  上書きしていた不具合。`transport` を `send()` の必須引数に変更（共有状態を廃止）。
- 積分失敗時に直前の（無関係な）プロファイルを自動送信してしまう不具合。失敗時は
  `Send now` も含めて無効化するよう修正。
- `PdiSender.exe` の起動失敗（`FailedToStart`）時に `finished` が発火せず、
  サービスが永久にビジー状態のままになる不具合。エラー種別で判定するよう修正。
- ドリフト検出ツールが未知メンバーの存在を終了コードに反映していなかった不具合。
  既知の非メンバー行のみ許可し、それ以外は exit 1 にするよう修正。
- 2次元配列を誤って受理し、不正なXMLを生成しうる欠落チェックを追加。
- `instant1d_send_pdi` の設定が保存されず再起動時に必ずOFFへ戻る不具合を修正。
- 名前・コメントのサニタイズが大文字小文字を無視して"pt"を削除しており、
  "September"等の通常の単語を破壊していた不具合。クリップボード経路では
  この処理を廃止し、`.pdi`ファイル経路のみ、upstreamと同じ大文字小文字を
  区別した無劣化の置換を適用するよう修正。

**2回目のレビュー**で、さらに以下が判明し修正済み:

- 上記の修正（`.tmp`→`os.replace()` 廃止）だけでは、**シーケンス連続送信**では
  依然としてフレームを取りこぼすことが判明。PDIndexer は1ファイル読み込み中
  `watcher.EnableRaisingEvents = false` にしており（`FormMain.cs:678`）、
  その間に作られた次のファイルの `Created` は再生されず失われる。
  `PdiService.flush_sequence()` を追加し、シーケンス分の `.pdi` 送信は
  1フレーム1ファイルではなく、シーケンス完了時にまとめて1ファイルへ書き出す方式に変更。
  クリップボード経路は対象外（Windowsの `WM_DRAWCLIPBOARD` はメッセージキューされるため
  同じ失敗モードがない）。
- `Program.cs` の2回目の mutex 待機で `AbandonedMutexException` を捕捉しても
  `ReleaseMutex()` を呼んでいなかった不具合。abandonedでも所有権自体は付与されるため、
  解放しないとプロセス終了時に再度abandonedになり、これを捕捉していない
  PDIndexer側の `WaitOne()` に例外が波及しうる。両方の待機で解放するよう修正。
- `pdindexer_running()` が `QProcess.waitForFinished()` でGUIスレッドを
  同期的にブロックしていた不具合（自己完結exeの起動だけで数百msかかりうる）。
  非同期API `pdindexer_running_async()` に変更。
- ドリフト検出が `XrayLine` を「順序は影響しない」という誤った理由で検査対象外にしていた
  不具合。`XrayLine` は明示値を持たない列挙型のため、`Ka1` より前に新メンバーが挿入されると
  ワイヤ値が暗黙に変わる。`UniversalConstants.cs` から取得し検査対象に追加。
  また列挙型の抽出に失敗した場合、警告のみでexit 0にしていた点も、exit 1になるよう修正。
- コメント欄にも名前用サニタイザ（"whole"末尾除去）を適用しており、
  PDIndexer側はコメントの内容を見ていないにもかかわらず不要な破壊をしていた点を修正。
- crashしたヘルパーを `exit_code` のみで成功判定していた不具合
  （crash時の `exit_code` はQtの仕様上未定義）。`ExitStatus.NormalExit` も
  あわせて確認するよう修正。

### 改訂履歴

**2026-09-05（レビュー反映）** — 初版から次を修正。着手前に必ず読むこと。

| # | 修正内容 | 反映先 |
|---|---|---|
| 1 | **`PointD` のミラーから `Tag` を落としてはいけない** — 落とすと unmanaged 化して 16 バイト生コピーになり互換性が壊れる（初版の誤り） | §0.4, Phase 1-1 |
| 2 | **互換性の基準は Crystallography の `main` ではなく PDIndexer の gitlink**。Crystallography に `master` は存在しない | §0.0, §5, 全リンク |
| 3 | テストデータを **L1（HGLOBAL）と L2（論理ペイロード）の2層**に分離。`--dry-run` は L2、`Clipboard.GetData` では L1 が採れない | Phase 0, Phase 1-2, §4 |
| 4 | `--verify` を「非 null」から**値の突き合わせ**へ。入力検証の条件も明文化 | Phase 1-2 |
| 5 | **送信契機を 5 種に分離**（live / recompute / single-shot / sequence / manual）。全契機 coalesce はシーケンスを欠落させる | Phase 3-1 |
| 6 | `PdiSender` を**アプリ共通サービス**にしてランチャーから注入 | Phase 2 |
| 7 | 能力判定を `clipboard_available` / `watch_folder_configured` / `transport` に分離 — 方式 A 不可時に方式 C まで隠れる問題 | Phase 2, Phase 3-2, Phase 4 |
| 8 | `.pdi` は `Created` のみ監視 → **一意名・排他生成・`.tmp` からのリネーム**が必須。`pt` 置換は全文字列フィールドに適用 | Phase 4 |
| 9 | mutex 待ちの最悪値は **10.5 秒**（初版「1秒超」は誤り）。QProcess タイムアウト 15 秒 + terminate/kill | §0.6, Phase 2 |
| 10 | macOS クロスビルドに `EnableWindowsTargeting`、`build.bat` は `%~dp0` 起点 | Phase 1-3 |
| 11 | **PDIndexer は ACK を返さない** — 終了コード 0 は「クリップボードに置けた」だけ。UI 文言を区別 | Phase 1-2, Phase 3-2 |
| 12 | ミラーに **MIT ライセンス表示**を添える | Phase 1-1 |
| 13 | 見積を **5〜6日**へ | §7 |

---

## 0. 上流ソース調査の結果（確定事項）

### 0.0 互換性の基準 — どのコミットに合わせるか

**基準は Crystallography の最新 `main` ではなく、受信側 PDIndexer が submodule として固定しているコミット。**
PDIndexer は自分がビルド時にリンクした型定義でデシリアライズするので、我々が合わせるべき相手はそれ。

2026-09-05 時点で実測した固定状況:

| リポジトリ | HEAD | 参照する Crystallography |
|---|---|---|
| `seto77/PDIndexer`（既定ブランチ `master`） | `f1ea57f` | **`99958eb4`** ← 我々の基準 |
| `seto77/IPAnalyzer`（既定ブランチ `master`） | `8f40f95` | `7a435ee` |
| `seto77/Crystallography`（**既定ブランチは `main`。`master` は存在しない**） | `f4c72b3` | — |

`99958eb4` と `main`(`f4c72b3`) の間で、シリアライズに関わる差分は**現時点では無い**ことを確認済み
（`Profile.cs` の差分は `GetErr` の空チェック追加のみ、`PointD.cs` / `Enums.cs` は同一、
`ExtensionMethods.cs` はコメント整理のみで `MemoryPackEx` のロジックは不変）。
本書の §0.1〜0.7 の記述はどちらでも成立するが、**リンクと引用行番号は `99958eb4` を正**とする。
PDIndexer と IPAnalyzer が別々の Crystallography を指している状態は常態なので、
「IPAnalyzer から送れているから大丈夫」は根拠にならない。

> [!IMPORTANT]
> §5 のドリフト検出は、**まず PDIndexer の gitlink（submodule が指す SHA）を読み、
> その SHA の型定義を検査する**。Crystallography の `main` を単独で監視すると、
> 受信側より早く鳴ったり遅れて鳴ったりする。

草案の想定を上流ソースで検証した。**大半は正しかったが、1点だけ致命的な誤りがある。**

### 0.1 ⚠ 最重要の訂正 — 「生バイト列をクリップボードに置く」だけでは受け取れない

草案では `SetClipboardData(fmt_id, raw_bytes)` で `[ID][brotli(MemoryPack)]` をそのまま置けばよいとしていたが、**これでは PDIndexer 側で `InvalidCastException` になり、「Failed to read clipboard information」ダイアログが出るだけ**で終わる。

理由は .NET WinForms のクリップボード実装にある。`Clipboard.SetDataObject(byte[])` は HGLOBAL に生バイトを書かない。`byte[]` は `IsSerializable` なので `SaveObjectToHGLOBAL()` を通り、**16バイトの GUID マーカー + MS-NRBF レコードストリーム**でラップされる
（[dotnet/winforms `Composition.ManagedToNativeAdapter.cs`](https://github.com/dotnet/winforms/blob/main/src/System.Private.Windows.Core/src/System/Private/Windows/Ole/Composition.ManagedToNativeAdapter.cs) の `SaveDataToHGLOBAL` / `SaveObjectToHGLOBAL`）。

読み出し側 `ReadByteStreamFromHGLOBAL()` は先頭16バイトがこの GUID と一致するかを見て、
一致すれば NRBF デシリアライズして `byte[]` を返し、
**一致しなければ `MemoryStream` を返す**。PDIndexer は
`var bytes = (byte[])dataObject.GetData(typeof(byte[]));`（`FormMain.cs:571`）と
キャストするので、`MemoryStream` が返った瞬間に例外 → catch → エラーダイアログ。

したがって Python から直接書く場合は **NRBF エンベロープも自前で組み立てる必要がある**。
逆に C# 側で `Clipboard.SetDataObject()` を使えば、この層はライブラリが面倒を見てくれる
（→ §1 の方式選択に直結する）。

### 0.2 クリップボードに置かれるバイト列の完全なレイアウト

`N` = `[1バイト ID(=2)] + [Brotli(MemoryPack(DiffractionProfile2[]))]` の長さ。総サイズは `44 + N` バイト。

| offset | size | 値 | 意味 |
|---|---|---|---|
| 0 | 16 | `96 A7 9E FD 13 3B 70 43 A6 79 56 10 6B B2 88 FB` | `s_serializedObjectID`（GUID `FD9EA796-3B13-4370-A679-56106BB288FB` の `ToByteArray()` 順） |
| 16 | 1 | `00` | NRBF `SerializedStreamHeader` |
| 17 | 4 | `01 00 00 00` | RootId = 1 |
| 21 | 4 | `FF FF FF FF` | HeaderId = −1 |
| 25 | 4 | `01 00 00 00` | MajorVersion = 1 |
| 29 | 4 | `00 00 00 00` | MinorVersion = 0 |
| 33 | 1 | `0F` | NRBF `ArraySinglePrimitive` (15) |
| 34 | 4 | `01 00 00 00` | ObjectId = 1 |
| 38 | 4 | `<N>` | 配列長 |
| 42 | 1 | `02` | `PrimitiveType.Byte` |
| 43 | N | ペイロード | `[02][Brotli(MemoryPack)]` |
| 43+N | 1 | `0B` | NRBF `MessageEnd` |

（`GlobalAlloc` の切り上げで HGLOBAL がこれより大きくても、NRBF デコーダは `MessageEnd` で止まるので末尾パディングは無害。）

クリップボードフォーマット名は草案どおり `"System.Byte[]"`（`DataObject.SetData(Type, object)` が
`format.FullName` を使うため）。ただし **§3 Phase 0 で実測確認する**。

### 0.3 MemoryPack ワイヤフォーマット（[公式 README](https://github.com/Cysharp/MemoryPack#binary-wire-format-specification) より）

| 種別 | エンコード |
|---|---|
| Object | `(byte memberCount, values...)`。`memberCount` は 0–249、`255` は null |
| Unmanaged struct | **構造体のメモリレイアウトをそのまま（パディング込み）** |
| Collection (`List<T>`, `T[]`) | `(int32 length, values...)`。`-1` は null |
| String | `(int32 utf16Length, utf16bytes)` または `(int32 ~utf8ByteCount, int32 utf16Length, utf8bytes)`。`-1` は null、`0` は空 |
| enum | 基底型そのまま（本件では全て int32） |

エンディアンはリトルエンディアン固定。シリアライズは
`MemoryPackEx.Serialize(header, val)`（[`ExtensionMethods.cs:256`](https://github.com/seto77/Crystallography/blob/99958eb4a5c5527134e8c303212acc349f5a6a6d/ExtensionMethods.cs)）で
**Brotli 圧縮（`CompressionLevel.Optimal`, window 22）した後に先頭1バイトの header を付ける**。
順序に注意 — `[ID] + Brotli(...)` であり `Brotli([ID] + ...)` ではない。

### 0.4 送るべき型の定義（`Crystallography/Profile.cs`）

#### `PointD`（[`PointD.cs`](https://github.com/seto77/Crystallography/blob/99958eb4a5c5527134e8c303212acc349f5a6a6d/PointD.cs)）
`[MemoryPackIgnore] public object Tag` を持つため **unmanaged ではない**。
→ Object エンコード = `[02][double X][double Y]` の **17バイト/点**。
（草案が想定していそうな「16バイトの生配列」ではない。）

> [!WARNING]
> **ミラーを作るときに `Tag` を省いてはいけない。**
> `[MemoryPackIgnore]` が付いているからといって落とすと、
> ミラー側の `PointD` は `double` 2つだけの **unmanaged struct になってしまい**、
> MemoryPack は §0.3 の規則どおり属性を一切見ずに **16バイトを生コピー**する。
> 出力は `[02][X][Y]` ではなく `[X][Y]` になり、PDIndexer 側のデシリアライズは静かに失敗する。
> 参照型メンバーが1つ存在することが「Object エンコードになる」条件そのものなので、
> `Tag` は**シリアライズされないが必要**。同じ理屈で `HorizontalAxisProperty` の側は
> 逆に参照型を1つでも足すと生レイアウトから Object エンコードに転落する。
> **どちらの型も、managed / unmanaged の別を上流と一致させること。**

#### `Profile` — メンバー数 **4**
| # | メンバー | 型 |
|---|---|---|
| 1 | `text` | string |
| 2 | `Pt` | `List<PointD>` |
| 3 | `Err` | `List<PointD>` |
| 4 | `LineWidth` | float |

`Color` は `[MemoryPackIgnore]` なので含まれない。

#### `HorizontalAxisProperty` — **unmanaged struct（生メモリ）**
`record struct` だが全メンバーが値型なので MemoryPack は**構造体レイアウトを丸ごとコピー**する。
x64 / `LayoutKind.Sequential` の自然アライメントから導いた予想レイアウト（**Phase 0 で実測検証必須**）:

| offset | size | メンバー | 型 |
|---|---|---|---|
| 0 | 4 | `AxisMode` | `HorizontalAxis` |
| 4 | 4 | `WaveSource` | `WaveSource` |
| 8 | 4 | `WaveColor` | `WaveColor` |
| 12 | 4 | *(padding)* | |
| 16 | 8 | `WaveLength` | double **単位 nm** |
| 24 | 4 | `XrayElementNumber` | int |
| 28 | 4 | `XrayLine` | `XrayLine` |
| 32 | 8 | `ElectronAccVolatage` | double |
| 40 | 8 | `EnergyTakeoffAngle` | double |
| 48 | 8 | `TofAngle` | double |
| 56 | 8 | `TofLength` | double |
| 64 | 4 | `TwoThetaUnit` | `AngleUnitEnum` |
| 68 | 4 | `DspacingUnit` | `LengthUnitEnum` |
| 72 | 4 | `WaveNumberUnit` | `LengthUnitEnum` |
| 76 | 4 | `EnergyUnit` | `EnergyUnitEnum` |
| 80 | 4 | `TofTimeUnit` | `TimeUnitEnum` |
| — | 4 | *(trailing padding)* | |
| **計** | **88** | | |

#### `DiffractionProfile2` — メンバー数 **55**（`memberCount` バイト = `0x37`）

宣言順に厳密に一致させる必要がある。`[XmlIgnore]` は MemoryPack には無関係なので**除外されない**点に注意。

```
 1 maskingRanges      List<MaskingRange>   20 NormarizeIntensity  double    39 ExposureTime          double
 2 InterpolationOrder int (prop)           21 DoesSmoothing       bool      40 IsLogIntensity        bool
 3 InterpolationPoints int (prop)          22 SazitkyGorayM       int       41 LineWidth             float
 4 DoesMaskAndInterpolate bool             23 SazitkyGorayN       int       42 ColorARGB             int?
 5 SourceProfile      Profile              24 DoesTwoThetaOffset  bool      43 BgPointsNumber        int
 6 BgPoints           PointD[]             25 TwoThetaOffsetCoeff0 double   44 SubtractBackground    bool
 7 ConvertedProfile   Profile              26 TwoThetaOffsetCoeff1 double   45 BgMode                BackgroundMode
 8 InterpolatedProfile Profile             27 TwoThetaOffsetCoeff2 double   46 BackgroundReferrenceProfile Profile
 9 SmoothedProfile    Profile              28 DoesRemoveKalpha2   bool      47 BackgroundReferrenceScale double
10 Kalpha2RemovedProfile Profile           29 Kalpha1             double    48 Name                  string
11 BackgroundProfile  Profile              30 Kalpha2             double    49 Comment               string (prop)
12 Profile            Profile              31 IsShiftX            bool      50 IsLPOmain             bool
13 Mode               DiffractionProfileMode 32 ShiftX            double    51 IsLPOchild            bool
14 SrcProperty        HorizontalAxisProperty 33 DoesBandpassFilter bool     52 ImageArray            double[]
15 DstProperty        HorizontalAxisProperty 34 DoesLowPath       bool      53 ImageScale            double
16 DoesNormarizeIntensity bool             35 DoesHighPath        bool      54 ImageWidth            int
17 NormarizeRangeStart double              36 LowPathLimit        double    55 ImageHeight           int
18 NormarizeRangeEnd  double               37 HighPathLimit       double
19 NormarizeAsAverage bool                 38 IsCPS               bool
```

`static byte ID => 2` は静的なのでメンバーに数えない。

#### 参照する enum の値（[`Enums.cs`](https://github.com/seto77/Crystallography/blob/99958eb4a5c5527134e8c303212acc349f5a6a6d/Enums.cs) ほか）

```
HorizontalAxis   { Angle=0, d=1, WaveNumber=2, Length=3, EnergyXray=4, EnergyElectron=5,
                   EnergyNeutron=6, NeutronTOF=7, None=8 }
WaveSource       { Xray=0, Electron=1, Neutron=2, None=3 }
WaveColor        { Monochrome=0, FlatWhite=1, CustomWhite=2, None=3 }
DiffractionProfileMode { Concentric=0, Radial=1 }
BackgroundMode   { BSplineCurve=0, ReferrenceProfile=1 }
AngleUnitEnum    { Degree=0, Radian=1, CentiDegree=2, MilliRadian=3 }
LengthUnitEnum   { None=0, Meter=1, ..., NanoMeter=5, Angstrom=6, PicoMeter=7,
                   MeterInverse=8, ..., NanoMeterInverse=12, AngstromInverse=13, ... }
EnergyUnitEnum   { eV=0, KeV=1, MeV=2 }
TimeUnitEnum     { Seccond=0, MilliSecond=1, MicroSecond=2, NanoSecond=3 }
XrayLine         { Ka=0, Ka1=1, Ka2=2, ... }     ← UniversalConstants.cs:255
```

> [!IMPORTANT]
> `HorizontalAxisProperty` のプロパティ初期化子（`TwoThetaUnit = Radian` など）は
> **明示コンストラクタ経由でしか走らない**。IPAnalyzer の通常経路は
> `DiffractionProfile2` のコンストラクタが `SrcProperty.AxisMode = ...` と
> フィールドを直接触るだけなので、`SrcProperty` は `default`（全ゼロ）から始まる。
> つまり実際に飛んでいる値は `TwoThetaUnit = Degree(0)`、横軸は **degree**。
> 我々は既定値に頼らず**全フィールドを明示的に設定する**。

### 0.5 受信側 (`PDIndexer/FormMain.cs`) が要求すること

`WndProc` → `WM_DRAWCLIPBOARD`（[`FormMain.cs:558-654`](https://github.com/seto77/PDIndexer/blob/f1ea57fd37a76513ec12f96108980314cee34f93/PDIndexer/FormMain.cs)）→
`AddProfileToCheckedListBox`（同 `:3454`）を読んだ結果、送信データが満たすべき条件:

1. `bytes[0] == 2`（`DiffractionProfile2.ID`）。
2. `dp != null && dp.Length >= 1`。デシリアライズ失敗時は `MemoryPackEx.Deserialize` が
   `default`（= null）を返すので **無反応**（エラーも出ない）。
3. **`Name` を `"whole"` で終わらせない** — 終わると LPO（方位分割）モードの分岐に入る。
4. `Mode = Concentric` にする。`Radial` は別扱い。
5. `AddProfileToCheckedListBox` が `dp.SetConvertedProfile(...)` を呼び、その中で
   `ConvertedProfile.Clear()` / `InterpolatedProfile.Clear()` などを触るため、
   **7つの `Profile` フィールドは全て非 null**（空でよい）にする。
   null を送ると `NullReferenceException` → catch → エラーダイアログ。
6. `ColorARGB = null` にしておくと PDIndexer 側が色を割り当ててくれる。
7. `checkBoxChangeHorizontalAppearance` が ON なら、`SrcProperty` の
   `AxisMode` / `WaveLength` / `TwoThetaUnit` が PDIndexer の横軸 UI にそのまま反映される。

### 0.6 Mutex プロトコル（IPAnalyzer `FormMain.cs:3614-3624`）

```csharp
using var mutex = new Mutex(false, "PDIndexer");
if (mutex.WaitOne(500, true)) {
    mutex.ReleaseMutex();                       // ← 取得してすぐ解放
    Clipboard.SetDataObject(payload);
}
Thread.Sleep(500);
if (mutex.WaitOne(500, false)) mutex.ReleaseMutex();
```

受信側は `WM_DRAWCLIPBOARD` 直後に同じ mutex を最大 5 秒待つ（`FormMain.cs:564`）。
「PDIndexer が読んでいる最中に上書きしない」ための待ち合わせであり、我々も同じ手順を踏む。
Windows の mutex はスレッドアフィニティがあるので、**取得と解放は必ず同一スレッドで**行うこと。

**所要時間の見積り**: 前後の `WaitOne` をそれぞれ 5 秒にすると、
最悪 `5 s + 0.5 s + 5 s = 10.5 秒` かかる。
呼び出し側のタイムアウトはこれを上回る値にしないと、
正常に待っているだけのヘルパーを殺してしまう（→ Phase 2 では 15 秒）。

### 0.7 単位のマッピング（pyFAI → PDIndexer）

| 送る値 | 入れ先 | 単位 | pyFAI からの変換 |
|---|---|---|---|
| 2θ 配列 | `SourceProfile.Pt[i].X` | **degree** | `integrate1d(..., unit="2th_deg")` の `result.radial` をそのまま |
| 強度配列 | `SourceProfile.Pt[i].Y` | 任意 | `result.intensity` |
| σ 配列 | `SourceProfile.Err[i]` = `(x, σ)` | 強度と同じ | `result.sigma`（`error_model` 指定時のみ。無ければ空リスト） |
| 波長 | `SrcProperty.WaveLength` | **nm** | `ai.wavelength [m] × 1e9` |
| 横軸種別 | `SrcProperty.AxisMode` | — | `Angle`(=0) |
| 角度単位 | `SrcProperty.TwoThetaUnit` | — | `Degree`(=0) |
| 線源 | `SrcProperty.WaveSource` / `WaveColor` / `XrayElementNumber` | — | `Xray` / `Monochrome` / `0`（= カスタム波長） |

> [!NOTE]
> 波長は **nm**。`PDIndexer/FormMain.cs:3033` に
> `if (WaveSource == Xray && WaveLength > 1) WaveLength = EnergyToXrayWaveLength(WaveLength * 10000)`
> という旧形式互換の分岐があり、Å（BL-18C なら 0.62 Å）で入れると 1 を超えないため一見動くが、
> 波長が 1 Å を超える実験では静かに壊れる。必ず nm（0.062 など）で入れること。
>
> `ExposureTime = 1` / `IsCPS = true` は恒等（`Profile.cs:1158`）なのでそのままでよい。
> 実露光時間を入れたい場合は `IsCPS` の意味（cps 換算するか）とセットで決めること。

---

## 1. 方式の比較と推奨

§0.1 で判明した NRBF エンベロープと、§0.4 の unmanaged struct パディングにより、
「Python だけで全部やる」コストは草案時点の想定より上がった。3案を比較する。

| | **A. C# ヘルパー exe**（推奨） | **B. 純 Python** | **C. `.pdi` ファイル監視**（保険） |
|---|---|---|---|
| 送信経路 | クリップボード | クリップボード | PDIndexer の FileSystemWatcher |
| MemoryPack | ライブラリ任せ | 自前実装（55メンバー順・パディング・可変長文字列） | 不要 |
| NRBF エンベロープ | `Clipboard.SetDataObject` 任せ | 自前実装（44バイト、既知） | 不要 |
| Brotli | ライブラリ任せ | `brotli` パッケージ追加 | 不要 |
| 上流変更への耐性 | **中**（型定義をミラーするので再確認は要る／コンパイルで大半は検出） | **低**（バイト列が静かにズレる） | **高**（XML は名前ベース・欠損許容） |
| 新規依存 | .NET SDK（ビルド時のみ） | `pywin32`, `brotli` | なし（標準ライブラリのみ） |
| PDIndexer 側の事前設定 | 不要 | 不要 | **要**（監視ディレクトリの設定） |
| 実装量 | C# ~250行 + Python ~150行 | Python ~450行 | Python ~80行 |

### 推奨: **A を本命、C を保険として同時に用意する**

- **A** を本命にするのは、§0.1 と §0.4 の「静かに壊れる」部分（NRBF・パディング・メンバー順）を
  すべてライブラリと C# コンパイラに肩代わりさせられるから。
  本リポジトリには既に `apps/Rad_icon_2022/dll/build.bat` でネイティブ DLL をローカルビルドする前例があり、
  「ビルド済みバイナリを gitignore して build スクリプトを置く」パターンにそのまま乗る。
- **C** を併設するのは、実装が 1〜2 時間で済むうえ、
  クリップボード経路が上流変更で死んだときに**ビームタイム中に代替手段が残る**から。
  ファイルが残るので測定記録としても有用。
- **B** は採らない。ただし §0 に全バイトレイアウトを書き残したので、
  将来 .NET SDK を置けない事情ができたときは実装可能。

### A について: Crystallography を submodule にするか？

**しない。** 代わりに**必要な型定義だけを写した最小ミラー**（`PdiTypes.cs`, 約150行）を書く。

理由:
- `Crystallography.csproj` は `net10.0-windows` / `UseWindowsForms` / `x64` で、
  MathNet.Numerics 6.0.0-beta2, OpenTK, PureHDF, DynamicExpresso, SimdLinq, ZLinq を参照し、
  埋め込みリソース（結晶学テーブル、Brotli 圧縮バイナリ）も抱えている。
  クリップボードに1本プロファイルを流すためにこれを全部ビルドするのは割に合わない。
  開発機は macOS であり、この構成をクロスビルドさせるのは無用の摩擦。
- submodule でコミットを固定しても「上流の破壊的変更に気づかない」問題は解決しない
  （固定した瞬間に古くなるだけ）。**気づく仕組み**が必要なのであって、
  それは §5 のスキーマドリフト検出スクリプトで解く。ミラーでも submodule でも同じ仕組みが要る。
- ミラーは MemoryPack NuGet のみに依存するので、どの TFM でもビルドできる。

---

## 2. アーキテクチャとファイル配置

```
utils/pdindexer/
├── __init__.py                  # PdiService / PdiProfile / Trigger を re-export
├── service.py                   # PdiService（アプリ共通・QProcess でヘルパーを起動）
├── profile.py                   # PdiProfile dataclass, pyFAI 結果からの変換, サニタイザ
├── pdi_file.py                  # 方式 C: .pdi (XML) 書き出し（一意名・排他生成）
├── schema_snapshot.json         # 上流の型定義スナップショット + 基準 gitlink SHA
├── IMPLEMENTATION_DETAILS.md    # 本計画の確定版を移植（§0 の調査結果を保存）
├── csharp/
│   ├── PdiSender.csproj
│   ├── Program.cs               # stdin JSON → DiffractionProfile2[] → クリップボード
│   ├── PdiTypes.cs              # Crystallography 型の最小ミラー（要 SHA + MIT 表示）
│   ├── LICENSE-Crystallography.txt   # 写した型定義のライセンス全文（MIT）
│   └── build.bat                # dotnet publish → ../bin/PdiSender.exe（%~dp0 起点）
└── bin/                         # ← .gitignore（radicon dll/Release と同じ扱い）
    └── PdiSender.exe

tools/check_pdindexer_schema.py     # PDIndexer の gitlink を辿って型定義の差分検出（手動）
tests/pdi_payload_decoder.py        # L1/L2 デコーダ（Phase 0 の成果物）
tests/test_pdindexer_payload.py     # ゴールデンデータとの照合
tests/data/ipa_clipboard_hglobal.bin  # L1: GUID + NRBF + ペイロード
tests/data/ipa_payload.bin            # L2: [ID][Brotli(MemoryPack)]
```

`utils/` 直下にするのは、既存の `utils/poni_io.py` / `utils/keithley2000_reader.py` と同じ
「ハードウェア/外部フォーマットとの境界を担うライブラリ」という位置づけだから。
UI を持たないので `apps/` ではない。

---

## 3. 実装フェーズ

### Phase 0 — 実測スパイク（1.0日・要 Windows 実機）

コードを書く前に、推測している3点を実測で潰す。**すべて Windows 機（PDIndexer が入っている PC）で行う。**

1. **フォーマット名の確認**
   IPAnalyzer で普段どおり "Get Profile" して PDIndexer に送り、その直後に Python で:

   ```python
   import win32clipboard as cb
   cb.OpenClipboard()
   fmt = 0
   while (fmt := cb.EnumClipboardFormats(fmt)):
       try:    name = cb.GetClipboardFormatName(fmt)
       except: name = f"<standard {fmt}>"
       print(fmt, name)
   cb.CloseClipboard()
   ```
   `"System.Byte[]"` が出ることを確認する。

2. **ゴールデンデータの採取 — ただし2層に分けて保存する**

   ここは混同しやすい。**クリップボード上のバイト列（HGLOBAL 層）と、
   `MemoryPackEx.Serialize()` が返す論理ペイロード層は別物**で、
   前者は後者を GUID + NRBF で包んだもの（§0.2）。テストデータも検証も、この2層を分けて扱う。

   | ファイル | 層 | 内容 | 誰が作れるか |
   |---|---|---|---|
   | `tests/data/ipa_clipboard_hglobal.bin` | L1 | `GUID + NRBF + [ID][Brotli]`（44 + N バイト） | 生 HGLOBAL を読む必要がある |
   | `tests/data/ipa_payload.bin` | L2 | `[ID][Brotli(MemoryPack)]`（N バイト） | L1 から 44 バイト剥がす／C# の `Serialize()` 出力 |

   採取は Python 側で行う。pywin32 の `GetClipboardData()` はカスタムフォーマットに対して
   **HGLOBAL の中身をそのまま bytes で返す**ので、L1 がそのまま採れる:

   ```python
   raw = cb.GetClipboardData(fmt_id)                     # L1（GUID から始まるはず）
   pathlib.Path("ipa_clipboard_hglobal.bin").write_bytes(raw)
   assert raw[:16] == PDI_SERIALIZED_OBJECT_GUID
   pathlib.Path("ipa_payload.bin").write_bytes(strip_nrbf(raw))   # L2
   ```

   > [!WARNING]
   > C# 側で同じことをするなら、`Clipboard.GetData(typeof(byte[]))` は使えない —
   > **NRBF を剥がした後の `byte[]`（= L2）しか返さない**ので L1 の検証にならない。
   > `--capture` を C# で実装する場合は
   > `OpenClipboard` / `GetClipboardData` / `GlobalLock` / `GlobalSize` の P/Invoke が必要。
   > L1 の採取と検証は Python 側に寄せるのが素直。

3. **レイアウトの検証**
   L1 を §0.2 の表と突き合わせる: 先頭16バイトの GUID、offset 33 の `0F`、
   offset 42 の `02`、offset 38 の配列長が実バイト数と一致すること、末尾が `0B` であること。
   続いて L2 を検証: 先頭が `02`（= `DiffractionProfile2.ID`）、
   残りが `brotli.decompress` を通ること。
   さらに MemoryPack 部分をデコードして `DiffractionProfile2` の `memberCount == 55`、
   `Profile` の `memberCount == 4`、`PointD` の `memberCount == 2`（17バイト/点）、
   `HorizontalAxisProperty` が 88 バイトであることを確認する。

**Phase 0 の成果物**: 上記2つの `.bin` と `tests/pdi_payload_decoder.py`。
デコーダは**どちらの層でも受けられる API にする**:

```python
def strip_nrbf(raw: bytes) -> bytes: ...            # L1 → L2（GUID が無ければ ValueError）
def decode_hglobal(raw: bytes) -> dict: ...         # L1 を検証してから decode_payload へ
def decode_payload(payload: bytes) -> dict: ...     # L2 = [ID][Brotli(MemoryPack)]
```

このデコーダは捨てコードではなく、方式 A の出力検証（V2）と
上流フォーマットの回帰テスト（V4）の両方で使い続ける。

### Phase 1 — C# ヘルパー `PdiSender.exe`（1.5日）

#### 1-1. `PdiTypes.cs` — 型のミラー

`Crystallography/Profile.cs` と `PointD.cs` から、**宣言順を1文字も変えずに**
シリアライズ対象メンバーだけを写す。メソッド・`[MemoryPackIgnore]` メンバー・
`[XmlIgnore]` 属性は落とす（`[XmlIgnore]` は MemoryPack に影響しないため落として問題ない）。
namespace も `Crystallography` に合わせる（MemoryPack の生成コードは型名を持たないので必須ではないが、
上流との diff を取りやすくするため）。

```csharp
// utils/pdindexer/csharp/PdiTypes.cs  （抜粋 / 実際は下記を全メンバー分）
//
// Type declarations mirrored from seto77/Crystallography (MIT Licence),
// commit 99958eb4a5c5527134e8c303212acc349f5a6a6d — the revision pinned by
// PDIndexer f1ea57f. Sources: Profile.cs (L16, L442, L1231), PointD.cs (L134),
// Enums.cs, UniversalConstants.cs (L255).
// Copyright (c) seto77.  See utils/pdindexer/csharp/LICENSE-Crystallography.txt
//
using System.Runtime.InteropServices;
using MemoryPack;
namespace Crystallography;

public enum HorizontalAxis { Angle, d, WaveNumber, Length, EnergyXray, EnergyElectron, EnergyNeutron, NeutronTOF, None }
public enum WaveSource { Xray, Electron, Neutron, None }
public enum WaveColor { Monochrome, FlatWhite, CustomWhite, None }
public enum DiffractionProfileMode { Concentric, Radial }
public enum BackgroundMode { BSplineCurve, ReferrenceProfile }
public enum AngleUnitEnum { Degree, Radian, CentiDegree, MilliRadian }
public enum LengthUnitEnum { None, Meter, CentiMeter, MilliMeter, MicroMeter, NanoMeter, Angstrom, PicoMeter,
                             MeterInverse, CentiMeterInverse, MilliMeterInverse, MicroMeterInverse,
                             NanoMeterInverse, AngstromInverse, PicoMeterInverse }
public enum EnergyUnitEnum { eV, KeV, MeV }
public enum TimeUnitEnum { Seccond, MilliSecond, MicroSecond, NanoSecond }
public enum XrayLine { Ka, Ka1, Ka2, Kb1, Kb3, KbI2, KbII2, La1, La2, Lb1, Lb2 }

[StructLayout(LayoutKind.Sequential)]
[MemoryPackable]
public partial struct PointD          // 2 members。Tag は残す — 落とすと unmanaged 化して壊れる
{
    public double X { get; set; }
    public double Y { get; set; }

    [MemoryPackIgnore]
    public object? Tag { get; set; }   // ★ シリアライズされないが、managed struct にするために必須

    [MemoryPackConstructor] public PointD() { X = 0; Y = 0; Tag = null; }
    public PointD(double x, double y) { X = x; Y = y; Tag = null; }
}

[MemoryPackable]
public partial class Profile                    // 4 members
{
    public string text;
    public List<PointD> Pt = [];
    public List<PointD> Err = [];
    public float LineWidth = 1f;
}

[MemoryPackable]
public partial record struct HorizontalAxisProperty   // unmanaged → 生レイアウト
{
    public HorizontalAxis AxisMode { get; set; }
    public WaveSource WaveSource { get; set; }
    public WaveColor WaveColor { get; set; }
    public double WaveLength { get; set; }            // nm
    public int XrayElementNumber { get; set; }
    public XrayLine XrayLine { get; set; }
    public double ElectronAccVolatage { get; set; }
    public double EnergyTakeoffAngle { get; set; }
    public double TofAngle { get; set; }
    public double TofLength { get; set; }
    public AngleUnitEnum TwoThetaUnit { get; set; }
    public LengthUnitEnum DspacingUnit { get; set; }
    public LengthUnitEnum WaveNumberUnit { get; set; }
    public EnergyUnitEnum EnergyUnit { get; set; }
    public TimeUnitEnum TofTimeUnit { get; set; }
}

[MemoryPackable]
public partial class DiffractionProfile2         // 55 members — 宣言順が命
{
    public static byte ID => 2;
    [MemoryPackable] public partial class MaskingRange { public double[] X = new double[2]; }
    public List<MaskingRange> maskingRanges = [];
    private int interpolationOrder = 2;
    public int InterpolationOrder { get => interpolationOrder; set { if (value > 0) interpolationOrder = value; } }
    private int interpolationPoints = 20;
    public int InterpolationPoints { get => interpolationPoints; set { if (value > 0) interpolationPoints = value; } }
    public bool DoesMaskAndInterpolate = false;
    public Profile SourceProfile = new();
    public PointD[] BgPoints = [];
    public Profile ConvertedProfile = new();
    // … 以下 §0.4 の 55 メンバー表どおりに続く …
}
```

**ミラー作成時のチェックリスト**（1つでも外すと「無反応」になる）:

- [ ] 先頭コメントに**写した上流コミット SHA**（= PDIndexer の gitlink、§0.0）とファイル・行範囲を明記。
      §5 のドリフト検出スクリプトはこの SHA を読む。
- [ ] Crystallography は MIT。**ライセンス表示を添える** —
      コメントに著作権表示を書き、`utils/pdindexer/csharp/LICENSE-Crystallography.txt` に全文を置く。
- [ ] `PointD` は **managed**（`object? Tag` を残す）。`HorizontalAxisProperty` は **unmanaged**（値型のみ）。
      この managed/unmanaged の別が §0.3 のエンコード方式を決めるので、上流と必ず一致させる。
- [ ] `DiffractionProfile2` は 55 メンバーを**宣言順そのまま**。`[XmlIgnore]` 付きも含める。
- [ ] enum は**メンバー順**（= 数値）を一致させる。名前だけ合っていても順序が違えば別の値になる。
- [ ] `Profile` は 4 メンバー。`Color` だけ落とす。
- [ ] Phase 0 のゴールデンデータでデコード結果を突き合わせるまで、このチェックリストは「未検証」扱い。

#### 1-2. `Program.cs` — I/O 契約

```
PdiSender.exe                 stdin から JSON を読み、クリップボードへ送る
PdiSender.exe --no-verify     自己検証を省く（既定は検証 ON）
PdiSender.exe --dry-run FILE  クリップボードに書かず、論理ペイロード L2 を FILE に保存
PdiSender.exe --decode FILE   L2（または L1）をデコードして要約を stdout に JSON で出す
PdiSender.exe --check         PDIndexer のウィンドウが存在するか調べて終了コードで返す
```

`--dry-run` が出すのは **L2（`[ID][Brotli]`）**であって、クリップボード上のバイト列ではない。
Phase 0 のデコーダに渡すときは `decode_payload()` の側を使う（`decode_hglobal()` ではない）。
L1 の採取・検証は Python 側の責務とし、C# には `--capture` を持たせない
（`Clipboard.GetData` では L1 が採れず、P/Invoke を書く価値がないため）。

stdin JSON（数値配列は base64 の little-endian float64。2000点でも 32 KB 程度）:

```json
{
  "profiles": [
    {
      "name": "BL18C_20260904_143012",
      "comment": "exposure 60 s, binning 2x2",
      "axis_mode": "Angle",
      "two_theta_unit": "Degree",
      "wavelength_nm": 0.06199,
      "x_b64": "…", "y_b64": "…", "err_b64": null,
      "exposure_time": 1.0,
      "is_cps": true
    }
  ]
}
```

stdout（1行 JSON）: `{"ok": true, "profiles": 1, "points": 2048, "payload_bytes": 41234}`

終了コード: `0` クリップボード書き込み成功 / `2` 入力不正 / `3` クリップボード書き込み失敗 /
`4` mutex タイムアウト / `5` 自己検証失敗。診断メッセージは stderr へ。

> [!IMPORTANT]
> **PDIndexer は ACK を返さない。** 終了コード `0` は「クリップボードに置けた」であって
> 「PDIndexer が取り込んだ」ではない。PDIndexer が起動していなくても `0` になる。
> UI の文言もこの区別を守ること（→ Phase 3）。

**入力検証**（`2` を返す条件。ここで弾かないと PDIndexer 側で無反応になり切り分け不能になる）:

- `x` と `y` の要素数が一致し、**1点以上**あること
- 全要素が finite（`NaN` / `±Inf` を含まない）。pyFAI は空ビンに `0` を入れるので通常は問題ないが、
  d 値変換などを挟むと `inf` が混じり得る
- `err` を渡すなら `x` と同数
- `wavelength_nm` が有限かつ正
- base64 のデコード長が 8 の倍数（little-endian float64 として解釈できる）
- プロファイル数と総点数に上限を設ける（例: 64 プロファイル / 1,000,000 点）。
  異常な入力で数百 MB の HGLOBAL を作らないため

**自己検証（`--verify`、既定 ON）は「非 null」で終わらせない。**
同じミラー型で serialize → deserialize しただけでは、ミラーが上流とズレていても通ってしまう
（自分の誤りと自分の誤りが打ち消し合う）。round-trip 後に
**点数・全座標（`X`/`Y` をビット一致で）・`Err` の有無と長さ・`WaveLength`・`AxisMode`・
`TwoThetaUnit`・`Name`・`Mode`・7つの `Profile` が非 null** を入力と突き合わせ、
1つでも違えば `5` を返す。
これは「ミラーが上流と一致しているか」の検証ではない（それは Phase 0 のゴールデン照合と §5 の役目）が、
**組み立てミスとエンコードの取りこぼしはここで全部落ちる**。

送信本体:

```csharp
[STAThread]
static int Main(string[] args)
{
    var dpList = BuildProfiles(ReadStdinJson());          // §0.5 の 7 要件を満たすよう構築
    var payload = MemoryPackEx.Serialize(DiffractionProfile2.ID, dpList);

    if (verify && MemoryPackEx.Deserialize<DiffractionProfile2[]>(payload[1..]) is null)
        return 5;                                          // 自己検証

    using var mutex = new Mutex(false, "PDIndexer");
    if (!mutex.WaitOne(5000, true)) return 4;
    mutex.ReleaseMutex();
    Clipboard.SetDataObject(payload, copy: true);          // ★ copy: true が必須
    Thread.Sleep(500);
    if (mutex.WaitOne(5000, false)) mutex.ReleaseMutex();
    return 0;
}
```

> [!IMPORTANT]
> **`copy: true` を必ず渡すこと。** IPAnalyzer は `Clipboard.SetDataObject(payload)`（`copy: false`）で
> 済ませているが、あれはプロセスが常駐しているから成立する。
> 短命なヘルパープロセスで `copy: false` にすると、プロセス終了時に
> `OleFlushClipboard` が走らずクリップボードの中身が消える。

`BuildProfiles` で満たすべきこと（§0.5 の再掲）:
`SourceProfile` / `ConvertedProfile` / `InterpolatedProfile` / `SmoothedProfile` /
`Kalpha2RemovedProfile` / `BackgroundProfile` / `Profile` をすべて `new Profile()` で埋める、
`Mode = Concentric`、`ColorARGB = null`、`Name` は `"whole"` で終わらせない、
`SrcProperty` は全15フィールドを明示設定。

#### 1-3. `PdiSender.csproj` / `build.bat`

```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0-windows</TargetFramework>
    <UseWindowsForms>true</UseWindowsForms>
    <Nullable>enable</Nullable>
    <PublishSingleFile>true</PublishSingleFile>
    <SelfContained>true</SelfContained>
    <RuntimeIdentifier>win-x64</RuntimeIdentifier>
    <!-- macOS/Linux から net*-windows をビルドするのに必須 (NETSDK1100 回避) -->
    <EnableWindowsTargeting>true</EnableWindowsTargeting>
  </PropertyGroup>
  <ItemGroup><PackageReference Include="MemoryPack" Version="1.21.4" /></ItemGroup>
</Project>
```

```bat
@echo off
rem utils/pdindexer/csharp/build.bat — 呼び出し元のカレントディレクトリに依存しない
dotnet publish "%~dp0PdiSender.csproj" -c Release -o "%~dp0..\bin"
if errorlevel 1 (echo [FAILED] PdiSender build & exit /b 1)
echo [OK] "%~dp0..\bin\PdiSender.exe"
```

- TFM は `net8.0-windows` 以上なら生成バイト列は同じ（NRBF エンベロープは .NET 9 以降で
  BinaryFormatter から NRBF ライタに変わったが、`byte[]` に対する出力レコードは同一）。
  ビルド機に入っている SDK に合わせてよい。
- **self-contained にする**。PDIndexer は自己完結配布の可能性があり、
  BL の PC に共有ランタイムがあるとは限らない。単一ファイル約 60–70 MB。
  ランタイムが確実にあるなら `SelfContained=false` にすれば 200 KB 程度になる。
- `build.bat` は `apps/Rad_icon_2022/dll/build.bat` と同じ運用（成果物は `.gitignore`）。
  パスは必ず `%~dp0` 起点にする — ランチャーや別ディレクトリから叩かれてもよいように。

### Phase 2 — Python 側ラッパー（1.0日）

```python
# utils/pdindexer/profile.py
@dataclass(frozen=True)
class PdiProfile:
    name: str
    x: np.ndarray                    # 2theta [deg]
    y: np.ndarray                    # intensity
    err: np.ndarray | None = None
    wavelength_nm: float = 0.0
    comment: str = ""
    exposure_time: float = 1.0
    is_cps: bool = True

    @classmethod
    def from_pyfai(cls, result, wavelength_m: float, name: str, **kw) -> "PdiProfile":
        """integrate1d(..., unit='2th_deg') の戻り値からそのまま作る。"""
        return cls(name=_sanitise_name(name), x=result.radial, y=result.intensity,
                   err=getattr(result, "sigma", None),
                   wavelength_nm=wavelength_m * 1e9, **kw)
```

```python
# utils/pdindexer/service.py
class Transport(enum.Enum):
    CLIPBOARD    = "clipboard"
    WATCH_FOLDER = "watch_folder"

class PdiService(QtCore.QObject):
    """アプリ全体で1つ。main.py が生成し、各サブアプリに注入する。"""
    finished = pyqtSignal(bool, str)          # (書き込み成功か, メッセージ)

    # ── 能力の問い合わせ（それぞれ独立。片方が不可でももう片方は使える）──
    def clipboard_available(self) -> bool: ...      # win32 かつ PdiSender.exe が存在
    def watch_folder_configured(self) -> bool: ...  # 出力先が設定済みで書き込める
    def any_available(self) -> bool: ...            # 上のどちらか
    def pdindexer_running(self) -> bool: ...        # EnumWindows でタイトル "PDIndexer" を探す

    transport: Transport                            # 現在選択中の経路（ユーザーが切替可能）
    def send(self, profiles: list[PdiProfile], *, trigger: Trigger) -> None: ...
```

設計上のポイント:

- **アプリ共通サービスとして `main.py` が所有し、各ウィンドウには注入する。**
  Rad-icon ウィンドウと XRD Scan が同時に開いていれば送信も同時に起こり得るが、
  クリップボードも `"PDIndexer"` mutex もプロセス横断の単一資源なので、
  ウィンドウごとに `PdiSender` を持たせると互いに踏み合う。
  本リポジトリの `controller=` 注入パターン（`CLAUDE.md`「Controller interface pattern」）に揃え、
  `Bl18cStageControlApp` などと同じく `pdi_service=` を optional kwarg で受ける。
  未注入なら各ウィンドウが自前で生成し `_owns_service = True` とする（既存規約と同じ）。
- **能力は3つに分ける。** `clipboard_available` と `watch_folder_configured` は独立で、
  「選択中の経路」は別の状態。単一の `is_available()` にまとめると、
  **方式 A が使えないときに保険の方式 C も一緒に隠れる**という本末転倒が起きる（→ Phase 4）。
- **`QProcess` を使う**（`subprocess` の同期呼び出しではなく）。
  ヘルパーの所要時間は最悪
  **mutex 5 s + Sleep 0.5 s + mutex 5 s ≈ 10.5 秒**（§0.6）。
  `QProcess` のタイムアウトはこれを上回る **15 秒**とし、
  超えたら `terminate()` → 2 秒後に `kill()`。
  ウィンドウを閉じるとき・アプリ終了時にも実行中プロセスを確実に落とす
  （`closeEvent` で `kill()` + `waitForFinished(2000)`）。
- **キューの方針は送信契機ごとに変える**（詳細は Phase 3 の表）。
  「常に最新1件へ coalesce」を全契機に適用すると、シーケンス撮影のフレームが欠落する。
  - live / recompute … そもそも送らない
  - single-shot / manual … 実行中なら最新1件へ coalesce（連打対策）
  - sequence … **coalesce しない**。完了後に配列として1回で送るか、
    欠落しない FIFO キューで順に送る
- `_sanitise_name()`: 末尾が `"whole"` になる名前を弾き（§0.5-3）、
  文字列 `pt` を含む名前も避ける（方式 C の置換対策、→ Phase 4）。
  クリップボード経路では `pt` 制約は不要だが、経路を切り替えても同じ名前が使えるよう統一する。
- 非 Windows では `clipboard_available()` が `False` を返す
  （`CLAUDE.md`「Windows first、macOS は開発用」に従い、macOS ではサイレントに無効化）。

**テスト** (`tests/test_pdindexer_payload.py`):

1. `--dry-run` で **L2** を吐かせ、`decode_payload()` でパースして
   memberCount / 点数 / 全座標 / 波長 / 単位 / 名前が入力と一致することを確認
   （Windows かつ `PdiSender.exe` がある場合のみ実行、他は `skipif`）。
2. ゴールデンデータ 2 層の回帰テスト（プラットフォーム非依存で常時実行）:
   `ipa_clipboard_hglobal.bin` を `decode_hglobal()` に、
   `ipa_payload.bin` を `decode_payload()` に通し、IPAnalyzer 産のデータを読めることを確認。
   → 上流フォーマットが変わればここが落ちる。
3. `pdi_file.py` の `.pdi` 生成: 1 秒以内に 2 件生成しても名前が衝突しないこと、
   `pt` を含む名前がサニタイズされること（プラットフォーム非依存）。
4. `PdiService` のタイムアウト経路: 応答しない偽ヘルパーを差し替え、
   15 秒で terminate → kill され `finished(False, ...)` が飛ぶこと。

### Phase 3 — UI 統合（1.5日）

#### 3-1. 送信契機の契約 — ここを決めずに実装してはいけない

`_maybe_run_instant_1d()` は **1D 積分が走るたび**に呼ばれるフックであって、
「新しい測定データができた」フックではない。実際の呼び出し元は現状6つある:

| 呼び出し元 | 場所 | 性質 | 自動送信 |
|---|---|---|---|
| `_on_instant1d_toggled` | [radicon_ui.py:1099](../apps/Rad_icon_2022/radicon_ui.py#L1099) | チェックを入れ直しただけの再計算 | **送らない** |
| `_on_poni_changed` | [radicon_ui.py:1130](../apps/Rad_icon_2022/radicon_ui.py#L1130) | poni 変更による再計算 | **送らない** |
| `_on_live_frame` → `_display_image` | [radicon_ui.py:1404](../apps/Rad_icon_2022/radicon_ui.py#L1404) | ライブ表示の毎フレーム | **送らない** |
| `_on_snap_done` → `_display_image` | [radicon_ui.py:1453](../apps/Rad_icon_2022/radicon_ui.py#L1453) | 単発撮影の完了 | 1件送信 |
| `_on_seq_frame_ready` → `_display_image` | [radicon_ui.py:1540](../apps/Rad_icon_2022/radicon_ui.py#L1540) | シーケンスの各フレーム | キューに積む（**coalesce しない**） |
| `_on_seq_done` → `_display_image` | [radicon_ui.py:1582](../apps/Rad_icon_2022/radicon_ui.py#L1582) | シーケンス平均像 | シーケンス分をまとめて送信 |

したがって `_maybe_run_instant_1d()` に送信を直接ぶら下げてはいけない。
**`trigger: Trigger` を引数に持たせて呼び出し元が契機を宣言する**形にする:

```python
class Trigger(enum.Enum):
    LIVE        = "live"          # 送らない
    RECOMPUTE   = "recompute"     # 送らない（設定変更・poni 変更による再計算）
    SINGLE_SHOT = "single_shot"   # 1件送信。実行中なら最新へ coalesce
    SEQUENCE    = "sequence"      # 欠落させない。完了時に配列で送るか FIFO で順送り
    MANUAL      = "manual"        # 「Send now」。最後の積分結果を送る
```

ライブ表示中に自動送信するとクリップボードを毎フレーム上書きすることになり、
ユーザーの他の作業を壊す。逆にシーケンスを「最新1件へ coalesce」するとフレームが欠ける。
**V8「取りこぼしなし・フリーズなし」はこの表を守って初めて成立する。**

シーケンスの送り方（一括 or FIFO）は測定枚数で選ぶ:
数枚なら `_on_seq_done` で配列にまとめて1回（PDIndexer は `DiffractionProfile2[]` を
そのまま複数プロファイルとして受ける）。数十枚以上なら FIFO で順送りし、
UI に「n / N 送信済み」を出す。

#### 3-2. 統合先

| 統合先 | 追加するもの |
|---|---|
| [apps/Rad_icon_2022/radicon_ui.py](../apps/Rad_icon_2022/radicon_ui.py) の Instant 1D パネル（`_build_instant1d_panel`, `_maybe_run_instant_1d`） | 「Send to PDIndexer」チェックボックス（自動送信、prefs キー `instant1d_send_pdi`、既定 OFF）+「Send now」ボタン（手動送信） |
| [apps/xrd_scan/roi_dialog.py](../apps/xrd_scan/roi_dialog.py) の "Take Test Shot" | テストショットのスペクトルを送るボタン |
| [apps/calibrate_instruments/calibrate_instruments_app.py](../apps/calibrate_instruments/calibrate_instruments_app.py) | 校正後の 1D 結果を送るボタン（任意） |
| [apps/exp_scheduler/](../apps/exp_scheduler/) | DSL 動詞 `send_to_pdindexer()`（後続タスク。SPEC.md への追記が要る） |

- **自動送信は既定 OFF**、かつ対象は `SINGLE_SHOT` / `SEQUENCE` のみ（3-1 の表）。
  ツールチップに「撮影のたびにクリップボードを書き換えます」と明記する。
- 経路の選択（クリップボード / `.pdi` フォルダ）は Instant 1D パネルのプルダウンに出す。
  `clipboard_available()` と `watch_folder_configured()` を独立に見て、
  **使えない方だけを無効化**する（両方使えないときだけプルダウンごと無効）。
- 送信結果の文言は **「書けた」と「取り込まれた」を混同しない**。
  PDIndexer は ACK を返さないので、後者は原理的に確認できない:
  - 成功 + PDIndexer 検出あり → 「PDIndexer に送信しました」
  - 成功 + PDIndexer 未検出 → 「クリップボードに置きました（PDIndexer は起動していません）」
  - `--check` は起動判定のみで取り込み判定ではない旨をツールチップに書く
  - 失敗（終了コード ≠ 0）→ stderr の1行を添えて赤字表示
  既存の `_instant1d_status_label` を使い回す。
- プロファイル名は保存ファイル名の stem を使い、無ければ `BL18C_<yyyymmdd_HHMMSS>`。
- **i18n**: 新規文字列は `tr()` でくるみ、`settings/i18n_catalog.py` の `JA` に追記する。
  手順は `/i18n-integration` スキルに従う。
  （`utils/pdindexer/` 自体はロジック層なので `tr()` を持たない。エラー文言は英語で返し、
  UI 側で `tr()` して表示する。）

### Phase 4 — 方式 C: `.pdi` ファイル経由のフォールバック（半日）

PDIndexer は監視ディレクトリに **`.pdi` で終わるファイル**が作られると自動で読み込む
（`FormMain.cs:674` `watcher_Created`。`.pdi2` は `EndsWith("pdi")` に一致しないので対象外）。
`.pdi` は `XmlSerializer(typeof(DiffractionProfile[]))` の出力、
すなわち**レガシー v1 クラスの素の XML**（`XYFile.ReadPdi2File(file, version: 1)`,
[`IO/PdiFile.cs`](https://github.com/seto77/Crystallography/blob/99958eb4a5c5527134e8c303212acc349f5a6a6d/IO/PdiFile.cs)）。
標準ライブラリだけで生成できる。

```xml
<?xml version="1.0" encoding="utf-8"?>
<ArrayOfDiffractionProfile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                           xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <DiffractionProfile>
    <OriginalProfile>
      <Pt>
        <PointD><X>5.0000</X><Y>1234.5</Y></PointD>
        <!-- … -->
      </Pt>
      <Err />
    </OriginalProfile>
    <SrcAxisMode>Angle</SrcAxisMode>
    <SrcWaveLength>0.06199</SrcWaveLength>   <!-- nm -->
    <Mode>Concentric</Mode>
    <WaveSource>Xray</WaveSource>
    <WaveColor>Monochrome</WaveColor>
    <XrayElementNumber>0</XrayElementNumber>
    <Name>BL18C_20260904_143012</Name>
  </DiffractionProfile>
</ArrayOfDiffractionProfile>
```

読み込み時に `ConvertToDiffractionProfile2()` が
`AngleUnitEnum.Degree` / `LengthUnitEnum.Angstrom` を補ってくれる（`Profile.cs:2044`）ので、
2θ は degree、波長は nm でよい（クリップボード経路と同じ）。

XmlSerializer は要素名ベースかつ欠損要素を既定値のままにするので、
**上流にフィールドが増減しても静かに壊れない**のが方式 C の最大の利点。

> [!WARNING]
> **以下の「注意点」は初版時点の下書きであり、2026-09-06 のレビューで
> `.tmp → os.replace()` は根本的に間違い（`Renamed` イベントは PDIndexer が
> 購読していない）と判明して覆っている。** 実装時に採用した正しい手順は
> [utils/pdindexer/IMPLEMENTATION_DETAILS.md](../utils/pdindexer/IMPLEMENTATION_DETAILS.md)
> の「The `.pdi`-file transport must never rename into place」節を参照。
> さらに、シーケンス連続送信では PDIndexer が1ファイル読み込み中
> `EnableRaisingEvents = false` にするため（`FormMain.cs:678`）、
> 1フレーム1ファイルでは後続フレームを取りこぼす。実装は
> `PdiService.flush_sequence()` でシーケンス分をまとめて1ファイルに
> 書き出す方式にした。以下は履歴として残すのみで、実装のガイドにしないこと。

注意点（初版時点の下書き — 上記警告を参照）:

- **監視しているのは `Created` イベントだけ**
  （[`FormMain.cs:674`](https://github.com/seto77/PDIndexer/blob/f1ea57fd37a76513ec12f96108980314cee34f93/PDIndexer/FormMain.cs#L674)）。
  `Changed` は見ていない。したがって:
  - ファイル名は**必ず未使用の一意な名前**にする。既存ファイルへの上書きは
    `Created` を発火させないので、静かに何も起きない。
  - **秒精度のタイムスタンプだけに頼らない。** 1秒以内に2枚送れば衝突する。
    `<stem>_<yyyymmdd_HHMMSS>_<連番>.pdi` とし、
    さらに `open(path, "x")` で排他生成して衝突時は連番を進める。
  - ~~書きかけを読まれないよう、同じディレクトリ内の一時名（例 `.tmp` 拡張子）で書いてから
    `os.replace()` で `.pdi` へリネームする。~~
    **→ 誤り。** 同一ディレクトリ内リネームは Windows では `Renamed` イベントになり、
    `Created` しか購読していない PDIndexer には見えない。正しくは、最終ファイル名へ
    直接排他生成し、書き込み完了までハンドルを開いたままにする
    （PDIndexer 自身が `File.Open` を100msリトライするので、開いている間は読まれない）。
- `ReadPdi2File` は読み込み前にファイルを**その場で書き換え**、かつ全行に対して
  文字列 `"pt"` → `"Pt"` の単純置換を行う。これは要素名だけでなく**本文にも効く**。
  ~~`Name` / `Comment` / `Profile.text` の全文字列フィールドから `pt` を除く
  （`_sanitise_name()` と同じサニタイザを全文字列に適用する）。~~
  **→ 過剰。** 大文字小文字を無視した削除は "September" 等の通常の単語を破壊し、
  かつクリップボード経路にまで適用すると本来不要な箇所まで壊す。正しくは、
  `.pdi` 経路の書き出し時のみ、upstream と同じ大文字小文字を区別した無劣化の
  置換（`"pt"→"Pt"` 等）を適用する（`pdi_file._pdi_file_text()`）。
  併せて `"OriginalFormatType"` / `"OriginalWaveLength"` / `"OriginalTakeoffAngle"` も
  同様に置換される。
- ユーザー側の事前設定が要る:
  PDIndexer のツールバーで監視ディレクトリを指定し、
  `watchReadANewProfileToolStripMenuItem`（新規プロファイルの自動読み込み）を ON にする。
  → 出力先の既定は `__localdata/pdindexer_watch/` とし、UI にパス表示とコピーボタンを置く。
  `watch_folder_configured()` は「パスが設定済みで書き込める」までしか判定できない
  （PDIndexer 側が監視 ON かどうかは知りようがない）ので、UI に注記する。
- 検証は「生成した `.pdi` を PDIndexer で File→Open して開けるか」で先に確認できる（監視設定不要）。

### Phase 5 — ドキュメントとドリフト検出（半日）

- `utils/pdindexer/IMPLEMENTATION_DETAILS.md` に §0 の調査結果（バイトレイアウト表・
  メンバー順表・単位マッピング・受信側要件）を確定版として移す。
  本計画書はそこから参照されるだけの履歴文書になる。
- `CLAUDE.md` のサブアプリ表と「Key conventions」に1行追記。
- `DEPENDENCIES.md` に「PdiSender.exe は `utils/pdindexer/csharp/build.bat` でビルド」を追記
  （Rad-icon DLL の記述と同じ節）。
- `docs/DOC_PDINDEXER.md`（日本語・ユーザー向け）: 使い方とスクリーンショット。
- `tools/check_pdindexer_schema.py`（§5 で詳述）。

---

## 4. 検証計画

| # | 検証項目 | 方法 | 合否 |
|---|---|---|---|
| V1 | クリップボードフォーマット名 | Phase 0-1 の列挙スクリプト | `"System.Byte[]"` が存在 |
| V2a | **L1** エンベロープが正しい | IPAnalyzer 産 `ipa_clipboard_hglobal.bin` を `decode_hglobal()` | GUID・NRBF 各フィールドが §0.2 の表と一致 |
| V2b | **L2** ペイロードが構造的に正しい | `--dry-run` 出力（L2）を `decode_payload()` | memberCount 55 / 4 / 2、`HorizontalAxisProperty` 88 バイト |
| V3 | C# 自身が round-trip できる | `--verify`（既定 ON） | 点数・全座標・波長・単位・名前が入力と一致 |
| V4 | IPAnalyzer 産データを我々のデコーダが読める | 2層のゴールデンデータの回帰テスト | パース成功・値が妥当 |
| V5 | **PDIndexer が実際に受け取る** | PDIndexer 起動 → 送信 → プロファイルリストに現れるか目視 | 追加され、2θ 軸・波長が正しい |
| V6 | 横軸変換の整合 | PDIndexer 側で横軸を d 値 / q に切り替え | ピーク位置が pyFAI の計算値と一致 |
| V7 | PDIndexer 未起動時の挙動 | PDIndexer を落として送信 | 例外なく完了、UI が「置いた」と「取り込まれた」を区別 |
| V8 | 送信契機ごとの挙動 | ライブ / 単発 / シーケンス / 設定変更をそれぞれ実行 | Phase 3-1 の表どおり。シーケンスで取りこぼしなし、ライブで送信されない、フリーズ・mutex デッドロックなし |
| V9 | 方式 C | `.pdi` を監視ディレクトリに置く。1秒以内に2件連続も試す | 両方とも自動で読み込まれる（名前衝突なし） |
| V10 | 経路の独立性 | `PdiSender.exe` を消した状態で起動 | 方式 C が選択・実行できる（UI ごと隠れない） |
| V11 | ヘルパーのハング耐性 | `--dry-run` を無限ループ化した偽 exe を置く | 15 秒で terminate → kill、UI が復帰。ウィンドウを閉じてもプロセスが残らない |

V5 が本命の受け入れ基準。V6 まで通れば単位系の誤りは実質的に否定できる。

異常系については草案の指摘どおり **PDIndexer は無反応になるだけ**なので、
`--verify` による自己デシリアライズ（V3）と自作デコーダ（V2）で
「送る前に落とす」のが切り分けの要になる。

---

## 5. 上流変更への追従

草案で挙げられていた「PDI の破壊的変更に気づかずビームラインのソフトが更新されて壊れる」への対策。

`tools/check_pdindexer_schema.py`:

1. **まず `seto77/PDIndexer` の HEAD ツリーを取得し、`Crystallography` の gitlink SHA を読む**
   （mode `160000` のエントリ）。これが我々が合わせるべき唯一の基準（§0.0）。
   Crystallography の `main` を直接見てはいけない — 受信側より早く鳴ったり遅れて鳴ったりする。
2. **その SHA の** `Profile.cs` / `PointD.cs` / `Enums.cs` / `UniversalConstants.cs` を取得。
3. `DiffractionProfile2` / `Profile` / `PointD` / `HorizontalAxisProperty` の
   **シリアライズ対象メンバーを宣言順に抽出**（`[MemoryPackIgnore]` と static を除外）。
   関連 enum のメンバー順も抽出。
   加えて各型の **managed / unmanaged 判定**（参照型メンバーを1つでも持つか）を記録する —
   ここが変わるとエンコード方式ごと変わるため（§0.4 の警告）。
4. `utils/pdindexer/schema_snapshot.json` と比較し、差分があれば
   人間可読な diff を出して **exit 1**。スナップショットには基準 SHA も保存し、
   **gitlink SHA が動いただけでも情報として報告する**（スキーマ差分が無ければ exit 0、
   スナップショットの SHA だけ更新すればよい）。
5. 併せて `PDIndexer/FormMain.cs` の `WM_DRAWCLIPBOARD` ブロックの sha256 も見張り、
   受信側ロジックが変わったら警告する。
6. 参考情報として IPAnalyzer の gitlink SHA も表示する。
   PDIndexer と別のコミットを指しているのは常態（2026-09-05 時点で
   PDIndexer→`99958eb4` / IPAnalyzer→`7a435ee`）なので、
   **一致していないこと自体は異常ではない**。
   「IPAnalyzer から送れているから我々も大丈夫」という推論を封じるために出す。

運用: **ビームタイム前と、PDIndexer/IPAnalyzer を更新した直後に手で走らせる**。
差分が出たら `PdiTypes.cs` を追随させて Phase 1 から再検証する。
（CI を回すならこのスクリプトを週次で。ただしネットワーク前提のテストなので通常のテストスイートには入れない。）

方式 C（`.pdi` XML）はこのドリフトの影響をほぼ受けないので、
チェックが落ちてから修正が終わるまでの繋ぎとして機能する。

---

## 6. リスクと未決事項

| リスク | 影響 | 緩和 |
|---|---|---|
| `HorizontalAxisProperty` の実際のパディングが §0.4 の予想と違う | 方式 A では**影響なし**（C# がやる）。方式 B なら致命的 | Phase 0-3 で実測確認して記録 |
| MemoryPack がフィールドとプロパティを宣言順でなく並べる | ペイロードが無効 → PDIndexer 無反応 | Phase 0 のゴールデン照合で検出。方式 A ではミラーが宣言順を保つ限り上流と自動一致 |
| BL の PC に .NET ランタイムがない | exe が起動しない | self-contained 発行を既定にする |
| クリップボード上書きがユーザーの作業を邪魔する | 運用上の不満 | 自動送信を既定 OFF・ツールチップで明示 |
| 大量点数（`npt` が大きい）で JSON stdin が重い | 送信遅延 | 数値は base64 float64。2048点で 32 KB、実測で問題なし想定。必要なら一時ファイル渡しに切替 |
| PDIndexer 側の mutex を長く保持してしまう | IPAnalyzer からの送信をブロック | 取得→即解放の IPAnalyzer 流儀を厳守。保持時間 0 |
| **ミラーで managed/unmanaged の別を取り違える**（`PointD` の `Tag` を落とす等） | エンコード方式ごと変わり PDIndexer 無反応 | §0.4 の警告 + Phase 1 チェックリスト + §5 の判定記録 + Phase 0 ゴールデン照合（V2b） |
| **ヘルパーがハングする / 応答しない** | 送信ボタンが押せないまま UI が固まる | 最悪 10.5 秒を見込んで QProcess タイムアウト 15 秒 → terminate → kill。閉じるときも kill（V11） |
| **PDIndexer が ACK を返さない** | 「送れた」と誤認して測定が進む | UI 文言で「置いた」と「取り込まれた」を分ける。最終的な確認は V5 の目視 |
| `.pdi` の名前衝突で静かに無視される | 方式 C でデータが落ちる | 排他生成 + 連番、**最終ファイル名へ直接書き込み**（`.tmp`→`os.replace()` は不採用 — 同一ディレクトリ内リネームは `Renamed` イベントになり `Created` しか購読していない PDIndexer に見えない）。V9 で1秒以内2連続を試験 |
| シーケンス連続送信で `.pdi` 経由がフレームを取りこぼす | PDIndexer が1ファイル読込み中 `EnableRaisingEvents=false` にするため、その間に作られた次のファイルの `Created` が失われる | `PdiService.flush_sequence()` でシーケンス分をまとめて1ファイルに書き出す（1フレーム1ファイルにしない） |

### ユーザー判断が要る点

1. **方式 A（C# ヘルパー）で進めてよいか。** submodule を使わず最小ミラーにする判断を含む。
   .NET SDK を Windows 機に入れる必要がある（開発機 macOS でもビルド自体は可能）。
2. **方式 C を同時に作るか、A がうまくいかなかったときの保険として後回しにするか。**
   先に作ると半日で「とりあえず PDIndexer にデータが飛ぶ」状態には到達できる。
3. **自動送信の契機（Phase 3-1 の表）でよいか。** 現案は
   単発撮影とシーケンスのみ自動送信・既定 OFF、ライブと再計算は送らない。
   XRD スキャンの全グリッド点を送りたいといった要望があれば設計が変わる。
4. **シーケンスは一括送信か FIFO か。** 通常何枚撮るかで決めたい。
   数枚なら完了時に配列で1回、数十枚以上なら順送り + 進捗表示。
5. **既定の経路をどちらにするか。** 現案はクリップボード（設定不要なので）。
   運用上ファイルが残る方が都合よければ `.pdi` を既定にしてもよい。
6. **`ExposureTime` / `IsCPS` を実測値にするか。** 現案は恒等（1 / true）。
   PDIndexer 側で cps 表示したいなら実露光時間を送る。

---

## 7. 工数見積

| Phase | 内容 | 見積 |
|---|---|---|
| 0 | 実測スパイク + **2層**ゴールデン採取 + デコーダ | 1.0日（**要 Windows 実機**） |
| 1 | C# ヘルパー（型ミラー・入力検証・深い自己検証・CLI・build.bat） | 1.5日 |
| 2 | Python ラッパー（共通サービス・契機・タイムアウト/kill）+ テスト | 1.0日 |
| 3 | UI 統合（送信契機の配線 / 経路選択 / 文言）+ i18n | 1.5日 |
| 4 | 方式 C（`.pdi` フォールバック、名前の一意性含む） | 0.5日 |
| 5 | ドキュメント + ドリフト検出スクリプト（gitlink 追跡） | 0.5日 |
| | **合計** | **5〜6日** |

初版の「約4日」は楽観的だった。レビュー指摘の反映（2層テストデータ、深い自己検証、
送信契機の分離、経路の独立化、ハング時の後始末）と、
Windows 実機での L1 検証・UI キュー/終了テストを含めるとこの幅になる。

Phase 0 と Phase 3・5 の一部は Windows 実機が要る。
Phase 1・2・4 は macOS で書ける（Phase 1 のビルド確認のみ Windows か、
`net8.0-windows` のクロスビルドで代替）。

---

## 付録 A. 参照した上流ソースの位置

行番号は **PDIndexer `f1ea57f` / Crystallography `99958eb4`（PDIndexer の gitlink）** 時点のもの。
dotnet/winforms は `main`。

| 内容 | 場所 |
|---|---|
| 送信側の実装（mutex + クリップボード） | `IPAnalyzer/IPAnalyzer/FormMain.cs:3610-3626` |
| 送信側のプロファイル構築 | 同 `:3484-3600` |
| 受信側 `WM_DRAWCLIPBOARD` | `PDIndexer/PDIndexer/FormMain.cs:558-654` |
| 受信側のリスト追加・軸変換 | 同 `:3454-3520` |
| 受信側の `.pdi` ファイル監視 | 同 `:672-694`、`:2944-2970` |
| `Profile` / `DiffractionProfile2` / `HorizontalAxisProperty` | `Crystallography/Profile.cs:16, 442, 1231` |
| レガシー `DiffractionProfile`（`.pdi` v1） | 同 `:1874-2140` |
| `PointD` | `Crystallography/PointD.cs:134` |
| `MemoryPackEx.Serialize/Deserialize` | `Crystallography/ExtensionMethods.cs:240-280` |
| `.pdi` の読み書き | `Crystallography/IO/PdiFile.cs:12-175` |
| 汎用 enum 定義 | `Crystallography/Enums.cs`、`UniversalConstants.cs:255` |
| クリップボード HGLOBAL の書き込み | `dotnet/winforms` `Ole/Composition.ManagedToNativeAdapter.cs:277-312` |
| 同 読み出し（GUID 判定） | `dotnet/winforms` `Ole/Composition.NativeToManagedAdapter.cs:150-184` |
| NRBF ヘッダ / 配列レコード | `dotnet/winforms` `BinaryFormat/Serializer/SerializationHeader.cs`, `ArraySinglePrimitive.cs` |
