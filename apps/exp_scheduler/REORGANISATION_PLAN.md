# Experimental Scheduler validation / DSL 再編計画

## 1. この文書の目的

この文書は、Experimental Scheduler の DSL コンパイル、実行前 validation、
Runner の安全確認を整理するための設計方針と、実際の実装作業順序を定める。

対象となる主な既存モジュールは次のとおり。

- `dsl/api.py`: DSL 公開シグネチャ、docstring、LLM 用メタデータ、未使用の
  Action 蓄積実装
- `dsl/__init__.py`: `ALLOWED_FUNCTIONS`
- `dsl/normalizer.py`: LLM 生成 DSL を中心に使われる AST normalizer
- `dsl/validator.py`: AST ホワイトリスト、unit、リテラル値域の検証
- `dsl/parser.py`: AST から `Sequence` / `Action` を直接構築する
  `SequenceBuilder`
- `actions.py`: 実行・保存対象となる Action モデル
- `validator/pre_validator.py`: Sequence 全体と実装置の現在状態を調べる
  実行前検証
- `runner.py`: Sequence の実行と、実行直前の安全確認
- `ui/dsl_editor.py`, `ui/llm_panel.py`, `llm/session.py`: DSL の各入口
- `ui/scheduler_window.py`: Validate / Run の統合 UX

この再編は validation を減らすことを目的としない。複数層で同じ安全条件を
再確認することは維持しつつ、DSL の必須引数、既定値、許可関数、単位、Action
変換などの「仕様定義」が複数箇所で独立して乖離する状態を解消する。

`SPEC.md` はアプリケーションの機能仕様、`validator/VALIDATOR.md` は実行前検証項目
の一覧として引き続き維持する。この文書は、それらを安全に実装へ反映するための
移行計画を担う。

---

## 2. 背景と現在の問題

### 2.1 実際の DSL 実行経路

現在、ユーザーが記述した DSL は `dsl/api.py` の実関数を `exec()` するのではなく、
概ね次の経路で処理される。

```text
DSL text
  -> ast.parse()
  -> ASTValidator
  -> SequenceBuilder
  -> Sequence
  -> PreValidator
  -> SequenceRunner
```

`dsl/api.py` の関数シグネチャと docstring は主として LLM prompt の生成に使われる
一方、実際の Action 生成は `SequenceBuilder` 内の別実装が行う。そのため、
`api.py` で引数が必須でも、parser が `kw.get()` で取得すれば DSL 側では省略できる。

入口は単純な「Script と LLM の二本」ではなく、現状では少なくとも次の経路に分かれている。

| 呼び出し元 | 現在の処理 |
|---|---|
| `ui/dsl_editor.py` | `ASTValidator` の後、2 箇所で `ast.parse()` + `SequenceBuilder.build()` を直接実行する。normalizer は使わない。 |
| `llm/session.py` | 抽出した text を `normalize()` + `ASTValidator` で検証する。ここでは Sequence を構築しない。 |
| `ui/llm_panel.py` | session が検証した `_pending_dsl` を Apply 時に再び素の `ast.parse()` + `SequenceBuilder.build()` で Sequence 化する。 |

したがって LLM 経路の中でも「正規化・検証」と「最終的な Sequence 生成」が別入口になっている。
Phase 1 は上記三箇所を列挙して統一し、normalizer の冪等性に依存して再 parse する構造を残さない。

### 2.2 確認済みの乖離例

この節は Phase 0 の characterization test の入力になるため、古いレビュー時点の記憶ではなく
固定した baseline に対して再検証する。2026-07-17 時点の baseline は clean な
`HEAD 197856e` (`validator for DSL type and numerical input validation`) である。この baseline で
現存を確認できる乖離は次のとおり。

| 現存する乖離 | 確認結果 |
|---|---|
| `wait(duration=1, foo=123)` の未知 keyword | `ASTValidator` は拒否せず、`SequenceBuilder._build_wait()` が `foo` を捨てる。 |
| `take_xrd(oscillate=True, ...)` の振動・per-step override | `TakeXrdAction.to_dsl()` は出力するが、`_build_take_xrd()` は `exposure_ms` / `save` / `prefix` しか Action へ渡さず、round-trip で値が消える。 |
| `normal_stop()` | `dsl/api.py` と `SequenceBuilder._BUILDERS` には存在するが、`dsl/__init__.py` の `ALLOWED_FUNCTIONS` から漏れており DSL では拒否される。 |
| `ast.Assign` / `ast.If` 等の未対応 statement | validation を通る構文があり、`SequenceBuilder._build_stmt()` が `None` を返して黙って捨てる。 |
| 明示的な `wait(duration=0.0)` / `follow_sample_position(duration=0.0)` | DSL compile 時の `_NUMERIC_BOUNDS` には duration 下限がなく通る。ただし現行 `PreValidator._check_durations()` は実行前に拒否するため、「無検証でRunまで到達」ではなく compile/preflight 間のcontract不一致である。 |
| `log_message(message="")` | 必須引数は存在するため compile を通る。空文字を許可するかは Phase 0 で明示的に決める。 |

一方、旧記載にあった次の例は `58af75b` / `4d7c1a6` / `197856e` までに修正済みであり、
「現存する不正挙動」の characterization test 対象から外す。

- 引数なしの `wait()`、`log_message()`、`follow_sample_position()` は
  `_REQUIRED_KWARGS` により compile error になる。
- `set_pressure(..., rate/rate_unit 省略)` は compile error になる。
- positional argument は `visit_Call()` で明示的に拒否され、黙って無視されない。
- 非有限数は `_check_finite_args()`、既存の unit / numeric rule は
  `_VALID_UNITS` / `_NUMERIC_BOUNDS` で検査される。

`validator/pre_validator.py` は同 baseline で 2,241 行である。これは計画上の固定仕様ではないため、
Phase 6 着手直前にも `wc -l` と責務一覧を再計測する。

### 2.3 問題の本質

区別すべきものは次の二つである。

1. **検証の多層化**
   - DSL コンパイル時、実行前、実行直前、controller 内で同じ安全条件を再確認する。
   - 装置状態は時間とともに変化するため、これは必要な多層防御である。
2. **仕様定義の多重化**
   - 必須引数、既定値、許可関数、unit、Action builder を別々の表や関数で管理する。
   - これは乖離の原因であり、可能な限り一元化すべきである。

本計画では前者を維持し、後者を解消する。

---

## 3. 変更後も維持する設計上の不変条件

### 3.1 Validate の UX は一つに保つ

ユーザーが意識する入口は、Visual / Script のどちらでも一つの Validate 操作とする。
AST validation、Action validation、装置との通信、シーケンスシミュレーションを別々の
ボタンにしない。

Validate を押すと、実行前に判定可能な問題を可能な限り収集し、一つの結果パネルへ
まとめて表示する。

### 3.2 PreValidator は live preflight であり続ける

PreValidator から装置依存チェックを外さない。少なくとも現在実施している次の確認を
維持する。

- controller / backend の接続状態
- ステージの現在位置と移動中状態
- Ch8 / Ch9 の現在モード
- MOVE_CONSTRAINTS と Global limits のシーケンスシミュレーション
- PACE5000 の現在圧力、target pressure、+ve source pressure、control mode
- LakeShore 335 の現在値、setpoint、heater 状態
- Rad-icon、カメラ、設定ファイル、reference image の使用可否
- start/stop、wait、for loop などのシーケンス構造

Validate は読み取り専用とし、validation のために装置の setpoint、速度、位置、モードを
変更してはならない。

### 3.3 Run 時にも再確認する

Validate 成功後に装置状態が変わる可能性がある。したがって、Run は以前の Validate
成功を必要条件としつつ、開始直前に live preflight を再実行する。

Runner と controller の安全確認も削除しない。PreValidator は全体を先読みして問題を
列挙し、Runner / controller は実際の最新状態に対して fail-closed で停止する。

現行 `_on_run()` は既に `PreValidator().validate()` を毎回フルに再実行している。さらにその前に
`_check_stage_unchanged_since_validation()` が、Validate 時の `_validated_positions` と現在の
Ch1--11 を直接比較している。この二つは統合後も別の保証として維持する。

- live preflight の再実行は、現在値を新しい baseline として現時点の安全性を再評価する。
- original baseline との差分確認は、別画面・手動操作などで Validate 後にステージが動いた事実を
  検出する。新しい snapshot だけで再 validation すると、この証跡は失われる。

### 3.4 AST 直接ビルド方式を維持する

DSL を `exec()` して Action を蓄積する方式へ戻さない。AST から `Sequence` を直接構築する
方式は、次の理由で維持する。

- 任意コード実行を避けられる。
- `ForLoopAction` を展開せず保持できる。
- DSL の行番号と Action を対応付けられる。
- LLM 出力に対して fail-closed にできる。

### 3.5 validated AST を黙って捨てない

次の不変条件を導入する。

> DSL validation に成功した構文・引数は、必ず Sequence 上の意味へ変換される。
> SequenceBuilder が未対応ノードや引数を黙って無視してはならない。

仕様上許可するが未実装の構文は実装する。実装しない構文は AST validation で明示的に
拒否し、`SPEC.md` も実装に合わせる。

### 3.6 ハードウェア・threading の挙動を同時に変更しない

この再編では controller ownership、motion lease、cleanup、Runner の QThread モデルを
変更しない。device snapshot を並列取得することもしない。validation の待ち時間が問題に
なった場合の非同期化は、controller の thread safety を確認した別タスクとして扱う。

---

## 4. 目標アーキテクチャ

### 4.1 ユーザー操作から Runner まで

```text
                         Validate
                            |
                  ValidationService
                            |
             +--------------+--------------+
             |                             |
          DSL text                  Visual / loaded Sequence
             |                             |
        DslCompiler                         |
   normalize / parse / safety               |
   call binding / Sequence build            |
             +--------------+--------------+
                            |
                    static Action checks
                            |
                 PreValidator live preflight
              trace / snapshots / domain checks
                            |
                    ValidationReport
                            |
                  show results / enable Run
                            |
                           Run
                            |
                fingerprint + live revalidation
                            |
                     SequenceRunner
                            |
               runtime checks + controller checks
```

内部処理は複数段であっても、UI は一つの ValidationReport のみを扱う。

### 4.2 目標ディレクトリ構成

移行完了時の目安とし、最初から全ファイルを作る必要はない。

```text
apps/exp_scheduler/
├── actions.py
├── sequence.py
├── scheduler_settings.py       # Global*Settings / GlobalLimits
├── safety_rules.py             # Runner と validator が共有する純粋判定
├── validation_service.py       # UI から見た一つの Validate 入口
├── dsl/
│   ├── _registry.py            # 既存 DslCommandMeta を CommandSpec の正本へ拡張
│   ├── compiler.py             # DSL text -> CompileResult
│   ├── normalizer.py
│   ├── validator.py            # AST safety を中心に担当
│   └── parser.py               # compiler 内部の AST -> Sequence 変換
└── validator/
    ├── models.py               # Diagnostic / ValidationReport / certificate
    ├── execution_trace.py      # for loop を考慮した実行順
    ├── snapshots.py            # read-only の装置状態取得
    ├── pre_validator.py        # 公開 facade / checker の統括
    └── checks/
        ├── action_params.py
        ├── sequence_structure.py
        ├── stage.py
        ├── pace5000.py
        ├── lakeshore.py
        ├── xrd.py
        └── camera_follow.py
```

ファイル数を増やすこと自体は目的ではない。小さい checker は無理に一ファイルずつにせず、
装置単位で責務が明確になる粒度を採用する。

---

## 5. 中核となるデータモデル

### 5.1 Diagnostic と ValidationReport

AST、Sequence、装置通信のエラーを共通形式へ変換する。

```python
@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    phase: ValidationPhase
    source_line: int | None = None
    action_path: str | None = None
    device: str | None = None


@dataclass
class ValidationReport:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    certificate: ValidationCertificate | None = None

    @property
    def errors(self) -> list[str]: ...

    @property
    def warnings(self) -> list[str]: ...

    @property
    def ok(self) -> bool: ...
```

移行中は既存 UI を一度に書き換えないため、`PreCheckResult.errors`, `warnings`,
`baseline_positions` と互換の property を提供する。最終的に `PreCheckResult` は
`ValidationReport` の互換 alias または薄い subclass にできる。

Diagnostic の `code` はメッセージ文言に依存しないテストと、将来の i18n に利用できる。
この再編自体では scheduler 全体の i18n は実施しない。

### 5.2 CompileResult と source map

```python
@dataclass
class CompileResult:
    sequence: Sequence | None
    diagnostics: list[Diagnostic]
    normalised_source: str | None
    source_map: ActionSourceMap
```

source map は保存 JSON の一部にせず、コンパイルした Sequence に付随する一時情報とする。
DSL 由来の Action では、PreValidator のエラーに DSL 行番号を併記できる。Visual 由来では
timeline の action path / row を使う。

### 5.3 ExecutionTrace

複数 checker が独自に `ForLoopAction` を展開しないよう、実行順の共通表現を作る。

```python
@dataclass(frozen=True)
class TraceEntry:
    action: Action
    path: ActionPath
    variables: Mapping[str, float]
```

`ExecutionTrace` は次を一度だけ処理する。

- nested `ForLoopAction` の実行順
- loop variable の context
- `SetAndWaitPressureAction` の検証用 primitive 展開
- step / iteration の表示名
- loop iteration、nesting、expanded steps の上限

上限超過を確認してから実体展開し、巨大ループで validation 自体が停止しないようにする。

### 5.4 ValidationSnapshot

一回の validation 内で装置状態を一貫して参照するため、必要な読み取りを snapshot として
集約する。

```python
@dataclass
class ValidationSnapshot:
    stage: StageSnapshot | None = None
    pace: PaceSnapshot | None = None
    lakeshore: LakeShoreSnapshot | None = None
    radicon: RadiconSnapshot | None = None
    collected_at: datetime | None = None
```

Sequence が使用する装置だけを読み取る。各 snapshot は値に加え、通信エラーや unavailable
状態を保持し、必要な装置情報が取得できない場合は fail-closed の Diagnostic にする。

Stage snapshot には少なくとも Ch1--11 の位置、移動中状態、stage mode を含める。
PACE snapshot には少なくとも接続状態、current / target pressure、+ve source pressure、
control mode を含める。

### 5.5 ValidationCertificate

```python
@dataclass
class ValidationCertificate:
    sequence_fingerprint: str
    settings_fingerprint: str
    snapshot: ValidationSnapshot
    validated_at: datetime
```

fingerprint は `Sequence.to_dict()` と、Runner に渡す Global settings の安定した JSON 表現から
生成する。Python の `hash()` はプロセスごとに変わるため使わない。

証明書は「将来も安全」という保証ではなく、どの Sequence / settings / snapshot に対して
Validate が成功したかを示す記録である。Run 時の live revalidation は別途必要である。

certificate の `snapshot` は Run 時に新しい snapshot で置き換えるための cache ではなく、
Validate 時の original snapshot である。`revalidate_for_run()` は次の二種類を別々に実施する。

1. fresh snapshot に対する通常の live preflight
2. certificate の original snapshot と fresh snapshot の差分確認

少なくとも Stage Ch1--11 は exact baseline comparison を行い、差分または読み取り不能を
専用 Diagnostic として Run 拒否にする。fresh snapshot を新しい baseline として通常の
PreValidator を実行しただけでは、この差分確認の代用にならない。

---

## 6. DSL CommandSpec の正本化

### 6.1 目標

DSL command ごとの以下の情報を一つの registry にまとめる。

- command 名
- Python 形式の signature
- 必須 / optional と default
- LLM 用 category、doc、example
- AST リテラル段階で判定できる enum / unit / numeric rule
- bound argument から Action を作る factory

```python
@dataclass(frozen=True)
class CommandSpec:
    name: str
    signature: inspect.Signature
    category: str
    doc: str
    example: str
    literal_rules: tuple[ArgumentRule, ...]
    factory: Callable[..., Action]
```

この registry から、許可関数名、LLM prompt、call binding、parser dispatch を導出する。
`ALLOWED_FUNCTIONS` や `_BUILDERS` を独立した手書き一覧として残さない。

新しい並行 registry を作るのではなく、既存 `dsl/_registry.py` の `@dsl_command` と
`DslCommandMeta(category, example)` を段階的に `CommandSpec` へ育てる。移行中も
`@dsl_command` だけの登録と別 registry の登録が併存する「第四の仕様置き場」を作らず、
未移行 command も同じ registry 内で明示的に識別・検査する。

### 6.2 call binding

AST の引数を評価した後、`inspect.Signature.bind()` と `apply_defaults()` を使う。

これにより次を SequenceBuilder 前に検出する。

- 必須引数不足
- 未知の keyword
- positional / keyword の重複
- 多すぎる positional argument
- keyword-only 違反

現行 DSL は positional argument を `ASTValidator.visit_Call()` で既に拒否しているため、
初期移行では keyword-only contract を維持する。binder は未知 keyword・重複・必須不足の
検出に使い、positional を新たに許可しない。将来 positional を許可する場合は、別途
breaking contract change として `DSL_VERSION` と互換性を判断する。

### 6.3 Action factory

factory は loop variable 名を表す文字列を保持できなければならない。例えば
`set_temperature(value=t, ...)` を factory 内で即座に `float(t)` へ変換してはならない。

必須引数は bound 済みなので、factory で `dict.get()` を使わない。optional 引数も
`apply_defaults()` 後の値を明示的に Action へ渡す。

`api.py` の context へ append する実関数は、外部利用がないことを再確認した上で、最終的に
削除するか CommandSpec factory を呼ぶ互換 wrapper にする。AST 直接ビルドと別の Action
生成実装を残さない。

---

## 7. 実装作業計画

各 Phase は単独でレビュー・検証可能な変更単位とする。前の Phase の完了条件を満たすまで
次へ進まない。一度に parser、PreValidator、Runner、UI を全面置換しない。

| Phase | 主な変更単位 | ユーザーから見た主な変化 |
|------:|--------------|--------------------------|
| 0 | contract inventory と regression test | なし |
| 1 | `DslCompiler` facade と共通 Diagnostic | Script / LLM の正規化経路が一致 |
| 2 | signature binding と fail-closed parser | 不正 DSL が早い段階で明確に拒否される |
| 3 | `CommandSpec` と Action factory | 有効 DSL の意味は維持、仕様追加時の乖離を防止 |
| 4 | settings model / pure safety rule の抽出 | なし |
| 5 | `ExecutionTrace` と静的 Action checker | DSL / Visual / JSON の診断が一致 |
| 6 | device snapshot と PreValidator 分割 | Validate の装置確認は維持、診断を統一 |
| 7 | `ValidationService` と UI 統合 | 一つの Validate で全結果を表示 |
| 8 | certificate と Run gate | Validate 後の変更・状態変化を明確に拒否 |
| 9 | Runner 共通 rule 化と旧経路削除 | 安全確認のタイミングは維持 |

### Phase 0: 現状 inventory と基準の固定

#### 目的

意図した仕様と既存バグを区別し、再編中の意図しない機能削除を防ぐ。

#### 作業

1. inventory の基準を clean な commit 一点へ固定し、commit hash、取得日、主要ファイルの
   行数、test 件数を記録する。Phase 0 は dirty worktree の HEAD と未コミット差分を混ぜて
   基準にしない。未コミット作業がある場合は、その所有者が先に commit / 退避 / 破棄の方針を
   決めてから開始する。初回基準は `197856e`、`pre_validator.py` は 2,241 行である。
2. Section 2.2 の各例をその baseline で実行し、修正済み・現存・層間contract不一致に
   仕分け直す。修正済みの例は green な contract test とし、現存バグだけを
   characterization / future regression test の対象にする。
3. 全 DSL command について次の表をテストデータとして作る。
   - 最小有効呼び出し
   - 必須引数
   - optional 引数と default
   - 対応 Action type
   - loop variable を許す引数
4. `api.py`、`ALLOWED_FUNCTIONS`、`SequenceBuilder._BUILDERS`、`_registry.py`、Action type の集合差を
   自動検査するテストを追加する。
5. 現存する silent acceptance / argument loss を characterization test にする。
   `take_xrd` の oscillation fields を最初の round-trip failure として固定する。
6. `Action.to_dsl() -> compile -> Action` の round-trip test を command ごとに追加する。
7. リポジトリ内と、ユーザーが提供可能な実運用の保存済み Sequence JSON / DSL script を
   inventory する。実データがある場合は個人パスや測定情報を除いた代表 fixture を作り、
   Phase 2 以降も load / compile できるか確認する。提供データがない場合も「実データ未確認」と
   baseline 記録へ明記する。
8. Visual / JSON から直接不正な Action が入るケースを PreValidator test に追加する。
9. hardware-free test 用に再利用可能な fake 群を用意する。
   - call recording と getter fault injection を持つ Fake Stage
   - PACE5000 の current / target / source pressure、control mode を提供する Fake PACE5000
   - setpoint / heater / readings を提供する Fake LakeShore 335
   - readiness と必要最小限の detector fields を提供する Fake Radicon
   現状は `_FakeStageController`、限定的な `_FakeLakeshore`、空の `_FakeRadicon` のみで、
   Fake PACE5000 は存在しない。この不足分を見積りに含める。
10. 実装に入る前に、現在の文書とコードで食い違う次の contract を決定し、この文書または
   `SPEC.md` に記録する。
   - `Assign` / `If` をこの再編で実装するか、実装まで DSL から拒否するか。
   - `TakeXrdAction.to_dsl()` が出力する per-step override を DSL の公開引数にするか、
     `to_dsl()` の出力対象から外すか。
   - 現行どおり positional argument を拒否する keyword-only contract を維持するか。
   - 明示的な duration 0 と空 `log_message` を DSL compile 層でも拒否するか。
   - strict 化で過去に通った DSL が拒否される場合の `DSL_VERSION` と移行告知方針。

#### 主なファイル

- 新規 `tests/test_exp_scheduler_dsl_contract.py`
- 新規 `tests/test_exp_scheduler_dsl_roundtrip.py`
- 新規または共通化 `tests/exp_scheduler_fakes.py`
- 既存 `tests/test_exp_scheduler_pre_validator.py`
- 既存 `tests/test_exp_scheduler_keithley_removed.py`

`test_exp_scheduler_keithley_removed.py` の「未知 command を SequenceBuilder が空 Sequence として
捨てる」という現在の期待は、fail-closed 化する Phase 2 で compile error の期待へ変更する。

#### 完了条件

- 現在有効な DSL command の一覧と Action 対応がテスト上で可視化されている。
- baseline commit と実測値が記録され、Section 2.2 がその commit に対して再現されている。
- 既知の不正挙動は、現状を記録する characterization test と、修正 Phase で有効化する
  regression test の対応が明確である。通常の test suite を意図的に failing のまま残さない。
- 実運用の保存済み Sequence / DSL を確認できたかどうかと、互換性上のリスクが記録されている。
- Stage / PACE5000 / LakeShore / Radicon の主要な正常値・異常値・例外を fake で再現できる。
- 物理ハードウェアを使わずテストを実行できる。

### Phase 1: 共通 Diagnostic と DslCompiler facade の導入

#### 目的

散在している DSL 処理入口を一本化する。ただし、この Phase では command 仕様や
PreValidator の挙動を大きく変えない。

#### 作業

1. `validator/models.py` に `Diagnostic`, `ValidationReport` の最小実装を追加する。
2. `dsl/compiler.py` に `DslCompiler.compile(source)` を追加する。
3. compiler 内で必ず次の同一経路を通す。
   - `normalize()`
   - AST safety validation
   - `SequenceBuilder.build()`
4. SyntaxError、NormalizationError、validator error、builder exception を Diagnostic へ変換する。
5. 次の直接呼び出しを `DslCompiler` 経由へ変更する。
   - `ui/dsl_editor.py`
   - `llm/session.py`
   - `ui/llm_panel.py`
6. LLM の self-fix は compile diagnostics のみを使用し、装置 preflight は呼ばない。
7. 既存 import の互換性を保つため、`ASTValidator` と `SequenceBuilder` 自体はまだ削除しない。

#### 注意点

現在は Script Editor の2箇所が直接 `ast.parse()` + `SequenceBuilder` を呼ぶ。LLM 側も一枚岩ではなく、
`llm/session.py` が normalize + validate した後、`ui/llm_panel.py::_on_apply()` が同じ text を
素の `ast.parse()` + `SequenceBuilder` で再処理している。この Phase では三箇所すべてを
`DslCompiler` へ統一する。正規化後の text、AST、Sequence は同じ `CompileResult` から使用し、
検証済み text を別経路で再 parse / build しない。LLM code-block 抽出中の `ast.parse()` は
構文候補の抽出だけに使うものとして、Sequence 生成入口とは区別する。

#### 完了条件

- UI / LLM 内に `SequenceBuilder().build(ast.parse(...))` の直接呼び出しが残っていない。
- `ui/llm_panel.py` は session が検証した text を再 parse せず、同じ compile 結果の Sequence を使う。
- Script Editor と LLM で同じ DSL に対する compile 結果が一致する。
- 既存の有効 DSL の意味は、Script Editor に normalizer を適用する意図的な経路統一を除いて
  変わっていない。

### Phase 2: strict call binding と fail-closed parser

#### 目的

必須引数、未知引数、未対応構文が SequenceBuilder で黙って補完・破棄される状態を止める。

#### 作業

1. まず現在の `api.py` signature を参照する暫定 call binder を compiler に追加する。
2. AST 上の全 command call に `Signature.bind()` を適用する。
3. default は `apply_defaults()` で一度だけ適用する。
4. builder へ渡す引数を bound arguments に限定する。
5. required field に対する `kw.get()` を除去する。
6. unknown command、未対応 statement、未対応 expression では Diagnostic を返す。
7. SequenceBuilder の次の silent fallback を廃止する。
   - builder がない command を `None` として無視する。
   - 未対応 statement を無視する。
   - positional argument を無視する。
   - unknown keyword を辞書化後に捨てる。
8. `SPEC.md` が許可している `Assign` / `If` と実装の差を確認し、実装するか明示的に拒否する。
   この判断を別 Phase へ先送りする場合も、少なくとも silent ignore は禁止する。
9. compiler が一つの call で発見可能な複数のエラーを集約し、DSL 行番号を付ける。
10. 現在は許可される text を新たに拒否する変更を breaking DSL contract として扱い、
    `DSL_VERSION`、release note、保存済み DSL fixture の移行結果を更新する。Sequence JSON schema を
    変更しない場合も、text DSL の許容範囲変更は明示する。

#### 修正対象となる既知ケース

- `wait(foo=123)` を unknown argument error にする。
- positional argument は現行どおり keyword-only error とし、無視も新規許可もしない。
- `take_xrd` の既知の oscillation / per-step override をすべて Action へ渡す。Phase 0 で非公開と
  決めた引数がある場合は、validator と `to_dsl()` の双方から同時に外し、validated argument を
  builder だけが捨てる状態を残さない。
- `normal_stop()` の公開可否を仕様に合わせて統一する。
- `Assign` / `If` 等の未対応 statement を実装するか、Action build 前に明示的に拒否する。
- Phase 0 の決定に従い、明示的な duration 0 / 空 message の compile rule を統一する。

`wait()` 等の必須引数不足、`set_pressure` の rate / rate_unit 不足、positional argument の黙殺は
baseline ですでに修正済みであり、この Phase の未修正バグ一覧には数えない。binder 導入後も
それらの green contract test を維持する。

#### 完了条件

- compile 成功後に、必須 field が parser 起因で `None` にならない。
- validated AST の command / argument / statement が黙って失われない。
- invalid DSL は生の `KeyError` / `TypeError` ではなく、行番号付き Diagnostic になる。
- strict 化で拒否対象になった既存 DSL fixture の結果と `DSL_VERSION` 判断が記録されている。
- Visual / JSON 由来の不正 Action は引き続き PreValidator でも拒否される。

### Phase 3: CommandSpec と Action factory の一元化

#### 目的

Phase 2 の暫定的な `api.py` signature 参照を、DSL command の単一正本へ置き換える。
既に prompt metadata の正本として使われている `dsl/_registry.py` を自然に拡張し、別 registry を
並行導入しない。

#### 作業

1. `dsl/_registry.py` の `DslCommandMeta` / `@dsl_command` を後方互換に保ちながら、
   `CommandSpec`, `ArgumentRule`, signature, factory を保持できる形へ拡張する。
2. 全 command の signature、metadata、factory を同じ registry entry へ段階的に移す。
   未移行 entry は import/test 時に明示され、別の registry へ逃がさない。
3. 次を registry から生成する。
   - 許可 command 名
   - AST call binding
   - literal unit / enum / numeric checks
   - parser dispatch
   - LLM command specification / examples
4. `dsl/__init__.py` の手書き `ALLOWED_FUNCTIONS` を derived view にする。
5. `SequenceBuilder._BUILDERS` を削除し、spec factory を呼ぶ。
6. `dsl/validator.py` の `_VALID_UNITS`, `_NUMERIC_BOUNDS` を ArgumentRule へ移す。
7. `llm/prompt_builder.py` を CommandSpec registry から生成するよう変更する。
8. `dsl/api.py` の別 Action 生成実装を削除するか、factory を呼ぶ互換 wrapper に縮小する。
9. 全 optional field が factory から Action へ渡ることを round-trip test で確認する。
   特に次を確認する。
   - `take_xrd` の oscillation fields
   - XRD per-step overrides
   - follow / autofocus fields
   - pressure rate / rate_unit
10. 有効な DSL contract が変わる場合は `DSL_VERSION` を更新し、`SPEC.md` と LLM examples を更新する。

#### 完了条件

- command を追加・削除するとき、command 名の一覧を複数ファイルへ手作業で追加しなくてよい。
- registry に builder がない command は import/test 時に失敗する。
- metadata-only entry と完全な `CommandSpec` entry が無期限に混在せず、移行完了を自動検査できる。
- API / prompt / parser / validator の command 集合が常に一致する。
- `Action.to_dsl()` が出力する引数を compiler がすべて保持する。

### Phase 4: Runner 依存モデルと純粋安全ルールの抽出

#### 目的

PreValidator が Runner の private helper を import する依存を解消し、同じ判定式を
PreValidator と Runner が共有できる準備をする。

#### 作業

1. `GlobalLimits`, `GlobalXrdSettings`, `GlobalFollowSettings`,
   `GlobalCameraSettings` を `scheduler_settings.py` へ移す。
2. 既存 import を一度に壊さないため、`runner.py` から一時的に re-export する。
3. `_validate_ch11_oscillation_settings` を `safety_rules.py` の公開純粋関数へ移す。
4. 次のような装置 I/O を行わない判定を、重複が確認できたものから抽出する。
   - Ch11 oscillation settings と degree-to-pulse 解決
   - Stage move constraint 判定
   - Global limits の target 判定
   - PACE unit / rate 変換と source pressure 判定
5. PreValidator と Runner の双方を共有関数へ切り替える。
6. controller 内の最終 MOVE_CONSTRAINTS enforcement は維持する。

#### 完了条件

- `validator/pre_validator.py` が `runner.py` の private 名を import していない。
- 抽出した pure rule は fake device なしで unit test できる。
- Runner の実行順、QThread、cleanup、motion lease に差分がない。

### Phase 5: ExecutionTrace と静的 Action validation の共通化

#### 目的

DSL、Visual、JSON のすべてから入った Sequence に同じ静的意味検証を適用し、各 checker の
for loop 走査を統一する。

#### 作業

1. `validator/execution_trace.py` を追加する。
2. 既存 `_loop_expansion_stats`, `_expand_execution_order`, `_walk_pace_actions` の用途を整理する。
3. loop 上限チェックを、実体展開より必ず先に実行する。
4. `SetAndWaitPressureAction` の検証用 primitive 展開を trace の明示的 API にする。
5. `validator/checks/action_params.py` に、装置通信不要の Action 値検証を移す。
   - finite number
   - unit / enum
   - required field
   - pulse range / integer
   - duration / tolerance / rate
6. `sequence_structure.py` に start/stop pairing、未定義 loop variable、空 loop body 等を移す。
7. 既存 PreValidator の helper を一つずつ新実装へ委譲し、同じ項目を二重実行しない。
8. action path と loop context を Diagnostic に付与する。

#### 注意点

Action static validation は PreValidator の一部として実行する。DSL compile に成功しただけで
Run を有効にしない。また、Visual / JSON は DslCompiler を通らないため、Action static
validation を Action constructor のみに依存させない。

#### 完了条件

- 同じ Sequence に対する loop 展開順が全 checker で共通になる。
- DSL / Visual / JSON の同じ不正 Action が同じ Diagnostic code で拒否される。
- loop 上限を超える Sequence でも、巨大な list を生成せず validation が終了する。

### Phase 6: read-only device snapshot と PreValidator の装置別分割

#### 目的

装置通信を含む実行前検証という価値を維持しつつ、baseline `197856e` で 2,241 行の
PreValidator を装置別に分離し、一回の validation 内で一貫した装置状態を参照する。行数は
Phase 6 着手直前に再計測し、古い概算を作業量の根拠にしない。

現行 `PreValidator.validate()` は既に `_run()` で checker ごとの例外を捕捉し、一つの
`PreCheckResult` に結果を集約しながら他の checker を継続する。したがって Phase 6 の主要な
新規価値は「初めて集約すること」ではなく、現在 `_check_stage`、
`_check_stage_move_constraints`、`_detect_stage_mode` 等が同じ validate 内で個別に hardware getter を
呼ぶことによる読み取り時点の不整合をなくし、PM16C / PACE5000 / LakeShore への重複した
通信ラウンドトリップを削減することである。Diagnostic への型付き集約は既存挙動の移行・強化として扱う。

#### 作業

1. `validator/snapshots.py` を追加する。
2. ExecutionTrace から使用装置を判定し、必要な装置だけを読み取る。
3. Stage snapshot collector を実装する。
   - Ch1--11 current position
   - moving state
   - Ch8 / Ch9 からの stage mode
   - 各読み取りの例外を Diagnostic 化
4. PACE snapshot collector を実装する。
   - connected
   - current / target pressure
   - +ve source pressure
   - control mode
5. LakeShore、Rad-icon、camera/config は既存チェックを保ったまま順次 snapshot / checker へ移す。
6. Phase 0 の Fake PACE5000 / Fake LakeShore / Fake Radicon を snapshot interface に合わせて拡張し、
   getter の call count、返却値、例外、`None` / NaN / Inf をテストから指定できるようにする。
   実 backend と同じ public surface のうち validation に必要な部分を明示する。
7. `validator/checks/` を装置単位で追加し、現在の `_check_*` を一つずつ移す。
8. `PreValidator.validate()` は次だけを担当する facade に縮小する。
   - trace 作成
   - snapshot 収集
   - checker 実行
   - Diagnostic 集約
   - validation log 出力
9. 現行 `_run()` と同様、一つの checker / 通信が例外を出しても、可能な他のチェックは継続する。
   ただし必要情報が読めなかった安全判定は fail-open にしない。
10. snapshot collector の call-recording test を追加し、同じ物理値を複数 checker が再読しないことを
    確認する。
11. `validator/VALIDATOR.md` の各項目に対応する checker test が存在することを一覧で確認する。

#### 移行順

1. Stage
2. PACE5000
3. LakeShore 335
4. XRD / Rad-icon
5. Camera / Follow
6. cross-device / sequence checks

Stage と PACE は安全性と live state 依存が大きいため、最初に移す。ただし一つの装置分を
移し終えてテストが通るまで次の装置へ進まない。

#### 完了条件

- Validate は引き続き実装置の現在状態を読み取る。
- validation の装置通信は read-only である。
- 同じ validation 中の各 checker が同じ snapshot を参照する。
- 同じ snapshot field のための hardware getter が checker ごとに重複して呼ばれず、call-count test で
  固定されている。
- 通信失敗を含む全エラーが一つの ValidationReport に集約される。
- Stage baseline positions が UI へ引き続き渡る。
- `validator/VALIDATOR.md` の既存項目に欠落がない。

### Phase 7: ValidationService と一つの Validate UX

#### 目的

Script、Visual、Run の validation orchestration を一か所へ集約する。

#### 作業

1. `validation_service.py` を追加する。
2. 次の公開 API を用意する。

   ```python
   validate_dsl(source, devices, settings) -> ValidationReport
   validate_sequence(sequence, devices, settings, source_map=None) -> ValidationReport
   revalidate_for_run(sequence, devices, settings, certificate) -> ValidationReport
   ```

3. `validate_dsl()` は compile error がある場合、可能な compile diagnostics を全て返す。
   Sequence を安全に構築できないため、その場合は live preflight を開始しない。
4. compile 成功時は static checks と live preflight を続け、結果を一つの report にまとめる。
5. Visual / loaded JSON は `validate_sequence()` から同じ static / live checks を通す。
6. `ui/scheduler_window.py` の次の経路を service へ置き換える。
   - Visual Validate
   - Script Validate
   - Run 押下時の再validation
   Phase 7 では full PreValidator 再実行の重複だけを service へ移し、
   `_check_stage_unchanged_since_validation()` と `_validated_positions` は削除しない。original baseline
   guard の certificate への移行は Phase 8 で、その同等性をテストしてから行う。
7. `ui/dsl_editor.py` は validation の全体を所有せず、text 編集と compile source location 表示に
   集中する。
8. Validation Results panel は Diagnostic を一つの一覧として表示する。
   通常ユーザー向けには AST / PreValidator など内部 class 名を前面に出さない。
9. validation 中に Sequence / settings を UI へ適用するタイミングを統一する。
   compile 成功だけで validated 状態にせず、full report が error なしのときだけ Run を有効化する。

#### 完了条件

- ユーザーが押す Validate は一つで、DSL と装置状態の双方が確認される。
- Script と Visual で同じ Sequence に対する PreValidator 結果が一致する。
- LLM self-fix は装置未接続を DSL 修正エラーとして扱わない。
- `_on_run`, `_on_validate_visual`, `_validate_sequence_from_dsl` に PreValidator の呼び出し手順が
  重複していない。
- Phase 8 完了前も `_check_stage_unchanged_since_validation()` による original baseline guard が
  引き続き有効である。

### Phase 8: ValidationCertificate と Run gate

#### 目的

「何を、どの状態で Validate したか」を明示し、Validate 後の編集や装置状態変化を安全に扱う。
Run 直前の full `PreValidator.validate()` と、Validate 時 baseline からの Stage 移動検出は現行
`scheduler_window.py` に既に存在する。この Phase はそれらを新規導入するのではなく、certificate と
`ValidationService.revalidate_for_run()` に役割を分けて明示的に移し、どちらも失わないことを目的とする。

#### 作業

1. full validation 成功時に certificate を作成する。
2. 次の変更で certificate を破棄し、Run を無効化する。
   - Sequence / timeline の編集
   - DSL text の変更
   - Global limits / XRD / Follow / Camera settings の変更
   - Sequence load / mode conversion
   - DeviceContext の backend 差し替え
3. 現在の `_validated_positions` を certificate の immutable な original StageSnapshot へ移行する。
   Run 時の fresh snapshot で上書きしない。
4. Run 押下時に sequence / settings fingerprint を照合する。
5. fingerprint が一致しても、certificate の original StageSnapshot と Run 直前の fresh StageSnapshot を
   Ch1--11 ごとに比較する。位置差分だけでなく getter 失敗も専用 Diagnostic として明示的に報告し、
   baseline を fresh snapshot にリセットして続行しない。
6. original snapshot との差分確認とは別に、現行 `_on_run()` と同じ full live preflight を再実行する。
7. Stage position、PACE source pressure、接続状態などが変化して新しい error が出た場合は Run を
   拒否する。
8. warning の内容が変わった場合は、最新 warning に対してのみ続行確認を出す。
9. original baseline comparison と fresh live preflight の両方が成功した後にのみ
   `SequenceRunner` を生成・start する。

#### 完了条件

- Validate 後に Sequence または settings を変更すると Run できない。
- ステージ位置が変化した場合、fresh snapshot 単独の再validation結果にかかわらず、certificate の
  original snapshot との差分として Run が拒否される。
- Run 直前の full live preflight も現行どおり毎回実行される。
- PACE source pressure 等の live 値も Run 直前に再取得される。
- certificate があっても Runner / controller の安全チェックを迂回できない。

### Phase 9: Runner の共通 safety rule 利用と cleanup

#### 目的

多層防御を残しながら判定式のコピーを減らし、移行用互換コードを整理する。

#### 作業

1. Runner の各 hardware action 直前チェックを、Phase 4 の pure rule へ順次切り替える。
2. PreValidator は Sequence 全体の予測状態、Runner は直前の実状態を渡す。
3. Runner の実行時エラーを共通 Diagnostic code または明確な runtime error へ対応付ける。
4. controller の MOVE_CONSTRAINTS、limit、stop / cleanup は維持する。
5. 互換期間が終わった次のコードを削除する。
   - 使用されない `DSL_NAMESPACE` / `api_context` 実行経路
   - 手書き `ALLOWED_FUNCTIONS`
   - parser の旧 `_BUILDERS`
   - UI / LLM の直接 ASTValidator / SequenceBuilder 呼び出し
   - runner からの settings model 一時 re-export
6. import cycle、unused import、dead helper を確認する。
7. `SPEC.md`, `validator/VALIDATOR.md`, `CLAUDE.md` の app-specific 記述を最終構成へ更新する。

#### 完了条件

- 同じ安全判定の式が PreValidator と Runner にコピーされていない。
- 確認タイミングは compile / preflight / runtime / controller の複数層に残っている。
- Runner の停止、例外、cleanup、motion lease の既存テストと手動確認項目が維持される。
- 旧 DSL 実行経路を検索しても利用箇所が残っていない。

---

## 8. Phase ごとの検証方針

### 8.1 毎 Phase で実施する共通確認

1. 変更対象の narrow test を実行する。
2. `tests/test_exp_scheduler*.py` を hardware-free で実行する。
3. 最終 diff を独立に読み直す。
4. 既存の `validator/VALIDATOR.md` 項目が削除・弱体化されていないことを確認する。
5. unrelated file を変更していないことを確認する。
6. 実機依存部分と未確認事項を記録する。

想定する基本コマンドは次のとおり。

```bash
python -m unittest discover -s tests -p 'test_exp_scheduler*.py'
```

Qt UI import や optional dependency のため test stub が必要な場合、既存 test と同様に test 側で
最小限の stub を使用し、本番コードへテスト専用分岐を入れない。

### 8.2 DSL compiler test matrix

各 command について少なくとも次を確認する。

- 最小有効 DSL
- 全 optional 引数を明示した DSL
- required 引数を一つずつ省略
- unknown keyword
- positional argument
- loop variable を使う field
- invalid unit / enum
- NaN / Inf / numeric bound
- `to_dsl()` round-trip
- JSON round-trip 後の `to_dsl()` round-trip

全 command 共通で次を確認する。

- unknown function
- method call / attribute access
- import / def / lambda / while 等
- oversized / nested loop
- 未対応 statement が silent ignore されないこと

### 8.3 PreValidator test matrix

実機通信は fake backend / `PM16CControllerSim` で再現する。
ただし baseline では `_FakeStageController`、限定的な `_FakeLakeshore`、空の `_FakeRadicon` しかなく、
Fake PACE5000 は存在しない。Phase 0 で共通 fake の土台を作り、Phase 6 で snapshot getter と
call-recording / fault-injection を完成させる作業を、既存前提ではなく明示的な実装コストとして扱う。

- 正常 snapshot
- 接続なし
- 各 getter が例外
- getter が `None` / NaN / Inf / 不正型を返す
- validation 中に必要な装置だけが読まれる
- Stage の current position / moving / mode
- MOVE_CONSTRAINTS と Global limits の sequence simulation
- PACE source pressure 不足と読み取り失敗
- LakeShore setpoint / heater / ramp
- XRD oscillation と detector readiness
- camera / config / reference file
- nested loop と loop variable
- 一つの checker が失敗しても他の診断が集約されること

simulation は制御ロジックの確認には使えるが、PACE5000 や LakeShore の実通信応答までは保証
しない。物理ハードウェア確認が必要な項目は完了報告で明示する。

### 8.4 実機で最終確認する項目

実装全体が hardware-free test を通った後、装置を動かさない read-only validation として
次を確認する。実機確認はユーザーの管理下で実施する。

- Validate で Stage Ch1--11 の位置が読み取られるが move command は送られない。
- moving 中または位置取得失敗時に Run が拒否される。
- PACE +ve source pressure が読み取られ、超過 target が事前に拒否される。
- source pressure 取得失敗が fail-open にならない。
- Validate 後に Stage を別画面から動かすと Run が拒否される。
- Run 押下時に original baseline との差分確認と fresh live preflight の両方が実行される。
- validation error 時に Runner thread が作成・start されない。

---

## 9. リスクと回避策

### 9.1 大規模置換による validator 項目の欠落

PreValidator を一度に書き換えない。既存 `_check_*` を一つずつ新 checker へ移し、同じ test と
`VALIDATOR.md` 項目が通った後に旧 helper を削除する。

### 9.2 compile error で後段の問題が見えなくなる

Sequence を安全に構築できない場合、装置 preflight は実行できない。代わりに AST を一回の
compile で可能な限り走査し、複数の call-shape / safety error を収集する。compile 修正後の
次回 Validate で live preflight を実行する。

### 9.3 snapshot による古い状態の使用

snapshot は一回の validation 内の一貫性のために使い、Run 時には再取得する。Runner も各操作
直前に必要な safety rule を最新状態で確認する。

### 9.4 Action constructor で厳格化しすぎる

Visual editor、JSON load、loop variable は一時的に未解決値を持つことがある。すべてを
`__post_init__` で例外にすると全エラー集約や既存 JSON の診断が難しくなる。constructor の
最低限の構造保証と、Action static validator の集約検証を分ける。

### 9.5 Diagnostic 化で既存 UI が一度に壊れる

`errors`, `warnings`, `baseline_positions`, `ok` の互換 property を先に用意し、UI は Phase 7
まで段階的に移す。

### 9.6 hardware read の回数・順序が変わる

snapshot 導入前に既存 getter 呼び出しを test fake で記録し、必要な read が欠落しないことを
確認する。速度目的で並列化せず、controller/backend の既存 lock と所有権を維持する。

### 9.7 strict DSL 化による後方互換性

unknown keyword や silent statement を fail-closed にすると、従来は通っていた DSL text が
compile error になる。コマンド名の追加・削除がなくても DSL の許容範囲を狭める breaking change と
みなし、`DSL_VERSION` と移行説明を更新する。Phase 0 で利用可能な保存済み Sequence JSON / DSL を
inventory し、匿名化した代表 fixture を各 Phase で再実行する。実データを入手できない場合は、
合成 fixture だけで互換性を判断したことを残存リスクとして明記する。

### 9.8 baseline が作業途中で動く

characterization test と inventory は clean な commit hash に固定する。Phase 0 開始後に別作業が
merge / commit された場合は、基準を暗黙に混ぜず、変更点を再 inventory して baseline hash と実測値を
更新する。特に `pre_validator.py` の行数は Phase 6 着手直前に再計測する。

---

## 10. スコープ外

次はこの再編に便乗して変更しない。

- 新しい DSL command や実験機能の追加
- hardware protocol の変更
- MOVE_CONSTRAINTS のルール変更
- Runner の QThread / background follow / oscillation thread 設計変更
- controller ownership / disconnect / cleanup の変更
- scheduler 全体の i18n 対応
- validation の非同期化・並列化
- Sequence JSON schema の不必要な version up
- unrelated UI redesign

ただし、既存 DSL command の引数が parser で失われている場合、その復旧は本計画の対象である。

---

## 11. 全体の完了条件

次をすべて満たした時点で再編完了とする。

1. UI / LLM の DSL 処理入口が `DslCompiler` に統一されている。
2. required 引数、unknown keyword、未対応構文が Action 生成前に行番号付きで拒否される。
3. validated AST の情報が SequenceBuilder で黙って失われない。
4. DSL command の signature、metadata、validator rule、Action factory が CommandSpec を正本として
   管理される。
5. `Action.to_dsl()` の全出力を compiler が欠落なく round-trip できる。
6. Visual / Script / JSON が同じ Action static validation と PreValidator を通る。
7. PreValidator が装置との read-only 通信を含む live preflight として機能する。
8. Stage current positions、PACE source pressure 等が Validate と Run 直前に確認され、Stage は
   certificate の original baseline との差分確認と fresh live preflight の両方を通る。
9. ユーザーの validation UX が一つの Validate 操作と結果一覧に保たれている。
10. Runner / controller の実行時安全確認が維持され、共有可能な判定式は pure rule として共通化
    されている。
11. `validator/VALIDATOR.md` の全項目に対応する実装と hardware-free test がある。
12. strict 化前の保存済み DSL / Sequence fixture の互換性結果と `DSL_VERSION` 判断が記録されている。
13. narrow test、scheduler test、simulation test が通り、残る実機依存リスクが文書化されている。

この再編の最終目標は validator を一つにすることではない。ユーザーには一つの明快な
Validate 体験を提供しながら、内部では compile、preflight、runtime、controller の各層が
同じ正本と安全ルールを共有し、必要なタイミングで繰り返し検証できる状態を作ることである。
