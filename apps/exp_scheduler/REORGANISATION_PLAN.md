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

`ui/dsl_editor.py::set_sequence()` は Script tab へ切り替えた際、Visual の全 Action に
`to_dsl()` を適用する。このため `Action.to_dsl()` と compiler signature / factory の不一致は
単なる手書きDSLの問題ではなく、アプリ自身が生成したScriptを自分で拒否または欠落変換する
Visual -> Script の自己破壊になる。strict binding は、この round-trip を先に固定してから有効化する。

### 2.2 確認済みの乖離例

この節は Phase 0 の characterization test の入力になるため、古いレビュー時点の記憶ではなく
固定した baseline に対して再検証する。2026-07-17 時点の baseline は clean な
`HEAD e6cb526` (`update GUI`) である。この baseline で
現存を確認できる乖離は次のとおり。

| 現存する乖離 | 確認結果 |
|---|---|
| `wait(duration=1, foo=123)` の未知 keyword | `ASTValidator` は拒否せず、`SequenceBuilder._build_wait()` が `foo` を捨てる。 |
| `take_xrd(...)` の per-step override 計13 field | `TakeXrdAction.to_dsl()` は acquisition/correction 8 field (`save_dir`, `dark_file`, `dark_enabled`, `defect_file`, `defect_enabled`, `defect_kernel`, `flip_v`, `flip_h`) と oscillation 関連5 field (`oscillate`, `osc_pos_a_deg`, `osc_pos_b_deg`, `osc_dwell_ms`, `osc_speed`) を出力しうる。しかし `dsl/api.py::take_xrd()` に前者8 fieldは存在せず、`_build_take_xrd()` は両群を一つも Action へ渡さない。parserで失われるのは8+5=13 fieldである。 |
| `normal_stop()` | `dsl/api.py` と `SequenceBuilder._BUILDERS` には存在するが、`dsl/__init__.py` の `ALLOWED_FUNCTIONS` から漏れており DSL では拒否される。 |
| `ast.Assign` / `ast.If` 等の未対応 statement | `SPEC.md` の「使える構文」には掲載済みだが、validation を通った後に `SequenceBuilder._build_stmt()` が `None` を返して黙って捨てる。文書契約と実装が直接矛盾する。 |
| 未束縛の bare name 引数 | 例: `pressure=oressure_typo` は loop variable でなくても `_eval_arg()` が同名文字列へ変換し、compileが成功する。現行PreValidatorは未定義loop変数として最終的に拒否するが、compile時の行番号付き診断にならない。 |
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

具体例として、MOVE_CONSTRAINTS は controller 内で最終的に再確認されること自体は必要な
「検証の多層化」である。一方、`PM16CController._check_move_constraints_using()`、
`PM16CControllerSim._check_move_constraints_locked()`、PreValidator の
`_violates_move_constraints()` / `_violates_move_constraints_for_move()` が同じ matching loop と
operator tableを個別に持つ状態は「仕様定義の多重化」である。Phase 4では検証層を削らず、後者だけを
一つのpublic pure evaluatorへ集約する。

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

例外として、既に `utils/stage/control_stage.py`、`control_stage_sim.py`、PreValidator の二 helper に
重複している MOVE_CONSTRAINTS の純粋判定ループは、ルールやwire protocolを変えずに
`utils/stage` の公開pure evaluatorへ一元化してよい。これはhardware挙動の変更ではなく、同じ
判定の複製を減らす安全性refactorとしてPhase 4で行う。既存controllerのpublic methodと例外文言は
互換wrapperで維持する。

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

MOVE_CONSTRAINTS の共有pure evaluatorは `apps/exp_scheduler/` 内に第三実装を作らず、
`utils/stage/`（例: `move_constraints.py`）に置く。real controller、simulator、PreValidatorが同じ
関数を利用し、`control_stage.py` の既存APIからは必要に応じてre-export / delegateする。

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

現行 `GlobalLimits` / `GlobalXrdSettings` / `GlobalFollowSettings` / `GlobalCameraSettings` には
`to_dict()` がない。Phase 4でdataclassへ集約した後、`dataclasses.asdict()` または明示的な
canonical serializerを使い、field名を含むsorted-key JSONへ変換する。fingerprint対象field、
Path / enum等の正規化、schema/version文字列をテストで固定し、`repr()` やオブジェクトidentityへ
依存しない。

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
   決めてから開始する。初回基準は `e6cb526`、`pre_validator.py` は 2,241 行である。
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
   `take_xrd` の acquisition/correction 8 fieldとoscillation関連5 field（計13 field）を個別に
   parameterizeし、Visual Action -> `to_dsl()` -> compileでどの値が拒否・欠落するかを最初の
   round-trip failureとして固定する。Visual -> Script自動変換で生成されたtextそのものもfixtureにする。
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
   - `Assign` / `If` は本再編ではfail-closedに拒否し、`SPEC.md` の「使える構文」を実装へ合わせて
     修正することをdefaultとする。実装を選ぶ場合は、変数scope、比較評価、Action表現を設計する
     別projectとして切り出し、本計画へ暗黙に追加しない。
   - `TakeXrdAction.to_dsl()` が出力する13個のper-step overrideをDSL公開引数にするか、別のlosslessな
     Visual/JSON -> Script表現を設計するか。全ActionのDSL round-tripという本計画の目標からは、
     13 fieldをsignature/factoryへ追加する案をdefaultとする。非公開にする場合は単に`to_dsl()`から
     削除して情報を失うことを許さず、round-trip目標とUI変換仕様を同時に改訂する。
   - `dsl/api.py::take_xrd()` のsignature変更は、現行`prompt_builder.py`が`inspect.signature()`を
     直接読むため、追加fieldを即座にLLM語彙へ露出する。DSL公開可否とLLMへ推奨・表示する引数を
     同一視するか、CommandSpecに`llm_visible`等のmetadataを設けて分離するかを決める。
   - MOVE_CONSTRAINTSは`apps/exp_scheduler/safety_rules.py`へ新しい判定を作らず、既存
     `_check_move_constraints_using()`を基に`utils/stage`の共有pure evaluatorへ昇格し、real / sim /
     validatorを同じ実装へ切り替える。共有module変更をPhase 4の明示的scopeとして承認する。
   - 現行どおり positional argument を拒否する keyword-only contract を維持するか。
   - 明示的な duration 0 と空 `log_message` を DSL compile 層でも拒否するか。
   - strict 化で過去に通った DSL が拒否される場合の `DSL_VERSION` と移行告知方針。

#### 主なファイル

- 新規 `tests/test_exp_scheduler_dsl_contract.py`
- 新規 `tests/test_exp_scheduler_dsl_roundtrip.py`
- 新規または共通化 `tests/exp_scheduler_fakes.py`
- 既存 `tests/test_exp_scheduler_pre_validator.py`
- 既存 `tests/test_exp_scheduler_dsl_validator.py`
- 既存 `tests/test_exp_scheduler_keithley_removed.py`

`test_exp_scheduler_keithley_removed.py` の「未知 command を SequenceBuilder が空 Sequence として
捨てる」という現在の期待は、fail-closed 化する Phase 2 で compile error の期待へ変更する。
`test_exp_scheduler_dsl_validator.py` は現在messageの部分文字列をassertしているため、Phase 1で
Diagnostic導入後に`Diagnostic.code`のassertへ移すか、互換message adapterを維持するかを明示的に
決める。既存95行のテストをinventory外に置かない。

#### 完了条件

- 現在有効な DSL command の一覧と Action 対応がテスト上で可視化されている。
- baseline commit と実測値が記録され、Section 2.2 がその commit に対して再現されている。
- `take_xrd` の13 override fieldとVisual -> Script生成textがround-trip matrixに含まれている。
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
8. 既存 `tests/test_exp_scheduler_dsl_validator.py` のmessage部分文字列assertをinventoryし、
   Diagnostic化した箇所は安定した`Diagnostic.code` assertへ移す。移行期間にstring list APIを
   残す場合はcompatibility adapterのテストとして意図を明記し、無警告に削除・skipしない。

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

1. まずVisual -> Scriptが生成する全DSL（特に`take_xrd`の13 override field）を既存builderが
   losslessに受け取れる状態にする。app-generated DSLがround-trip testでgreenになる前に
   unknown keyword拒否を有効化しない。
2. 現在の `api.py` signature を参照する暫定 call binder を compiler に追加する。
3. AST 上の全 command call に `Signature.bind()` を適用する。
4. default は `apply_defaults()` で一度だけ適用する。
5. builder へ渡す引数を bound arguments に限定する。
6. required field に対する `kw.get()` を除去する。
7. unknown command、未対応 statement、未対応 expression では Diagnostic を返す。
8. SequenceBuilder の次の silent fallback を廃止する。
   - builder がない command を `None` として無視する。
   - 未対応 statement を無視する。
   - positional argument を無視する。
   - unknown keyword を辞書化後に捨てる。
9. call引数中の`ast.Name`は、そのsource位置を包含する`for`で束縛済みの場合だけloop variableとして
   許可する。未束縛bare nameはタイプミスとして行番号付きDiagnosticにし、`_eval_arg()`で文字列化
   しない。将来Assignを別projectで導入する場合のみ、そのscopeをbinderの定義集合へ追加する。
10. `Assign` / `If` は本再編では明示的なunsupported-statement Diagnosticとして拒否し、同じPhaseで
    `SPEC.md` の「使える構文」から外して実装と一致させる。実装する判断へ変更する場合は別projectの
    設計・見積りを先に承認し、このPhaseへそのまま混ぜない。
11. compiler が一つの call で発見可能な複数のエラーを集約し、DSL 行番号を付ける。
12. 現在は許可される text を新たに拒否する変更を breaking DSL contract として扱い、
    `DSL_VERSION`、release note、保存済み DSL fixture の移行結果を更新する。Sequence JSON schema を
    変更しない場合も、text DSL の許容範囲変更は明示する。

#### 修正対象となる既知ケース

- `wait(foo=123)` を unknown argument error にする。
- `set_pressure(pressure=oressure_typo, ...)` 等の未束縛bare nameを、PreValidatorまで遅延せず
  compile errorにする。正しいnested `for` scopeとshadowingは引き続き許可する。
- positional argument は現行どおり keyword-only error とし、無視も新規許可もしない。
- `take_xrd` の既知のacquisition/correction 8 fieldとoscillation関連5 fieldをすべてActionへ渡す。Phase 0で非公開と
  決めた引数がある場合は、validator と `to_dsl()` の双方から同時に外し、validated argument を
  builder だけが捨てる状態を残さない。
- `normal_stop()` の公開可否を仕様に合わせて統一する。
- `Assign` / `If` 等の未対応 statement をAction build前に明示的に拒否し、`SPEC.md`も修正する。
- Phase 0 の決定に従い、明示的な duration 0 / 空 message の compile rule を統一する。

`wait()` 等の必須引数不足、`set_pressure` の rate / rate_unit 不足、positional argument の黙殺は
baseline ですでに修正済みであり、この Phase の未修正バグ一覧には数えない。binder 導入後も
それらの green contract test を維持する。

#### 完了条件

- compile 成功後に、必須 field が parser 起因で `None` にならない。
- app自身がVisualから生成したDSLがstrict binderを通り、13個の`take_xrd` overrideを保持する。
- 未束縛bare nameはsource line付きDiagnosticとなり、正しいfor-loop変数だけがActionへ残る。
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
   現行promptは`inspect.signature(dsl_api.<fn>)`を直接展開するため、signatureへ追加した引数は即座に
   LLM語彙へ現れる。DSLとしてround-trip可能な引数集合と、LLMへ積極的に提示する引数集合を
   分ける場合は、`llm_visible` / prompt group等をCommandSpecの正式metadataとして持たせる。
8. `dsl/api.py` の別 Action 生成実装を削除するか、factory を呼ぶ互換 wrapper に縮小する。
9. 全 optional field が factory から Action へ渡ることを round-trip test で確認する。
   特に次を確認する。
   - `take_xrd` のacquisition/correction 8 field
   - `take_xrd` のoscillation関連5 field
   - follow / autofocus fields
   - pressure rate / rate_unit
10. 有効な DSL contract が変わる場合は `DSL_VERSION` を更新し、`SPEC.md` と LLM examples を更新する。

#### 完了条件

- command を追加・削除するとき、command 名の一覧を複数ファイルへ手作業で追加しなくてよい。
- registry に builder がない command は import/test 時に失敗する。
- metadata-only entry と完全な `CommandSpec` entry が無期限に混在せず、移行完了を自動検査できる。
- API / prompt / parser / validator の command 集合が常に一致する。
- `Action.to_dsl()` が出力する引数を compiler がすべて保持する。
- DSL公開引数とLLM prompt表示引数を分ける場合、その差がCommandSpec metadataとtestで明示される。

### Phase 4: Runner 依存モデルと純粋安全ルールの抽出

#### 目的

PreValidator が Runner の private helper を import する依存を解消し、同じ判定式を
PreValidator と Runner が共有できる準備をする。

#### 作業

1. `GlobalLimits`, `GlobalXrdSettings`, `GlobalFollowSettings`,
   `GlobalCameraSettings` を `scheduler_settings.py` へ移す。
   同時に`dataclasses.asdict()`または明示的serializerを使うcanonical settings表現を定義し、
   sorted-key JSON、schema/version、全fingerprint対象fieldをtestで固定する。
2. 既存 import を一度に壊さないため、`runner.py` から一時的に re-export する。
3. `_validate_ch11_oscillation_settings` を `safety_rules.py` の公開純粋関数へ移す。
4. MOVE_CONSTRAINTSについて、少なくとも次の既存matching loopとerror semanticsをcharacterizeする。
   - `PM16CController._check_move_constraints_using()`（public checkとwire-level readerから利用）
   - `PM16CControllerSim._check_move_constraints_locked()`
   - PreValidator `_violates_move_constraints()`
   - PreValidator `_violates_move_constraints_for_move()`
5. `_check_move_constraints_using(ch, target_pos, read_pos)`の注入可能reader設計を基に、
   `utils/stage`へpublicなpure evaluatorを一つ定義する。`MOVE_CONSTRAINTS` schemaとoperator解決は
   evaluator内部の正本とし、`apps/exp_scheduler/safety_rules.py`へ同じmatching loopを新設しない。
6. real controllerとsimulatorの既存methodをcompatibility wrapperとして共有evaluatorへdelegateし、
   PreValidatorの現在snapshot検査・prospective move検査も同じevaluatorを使う。error messageと
   fail-closedな位置読み取り失敗をparity testで維持する。
7. その他の装置I/Oを行わない判定を、重複が確認できたものから抽出する。
   - Ch11 oscillation settings と degree-to-pulse 解決
   - Global limits の target 判定
   - PACE unit / rate 変換と source pressure 判定
8. PreValidator と Runner の双方を該当する共有関数へ切り替える。
9. controller 内の最終 MOVE_CONSTRAINTS enforcement は維持し、rule内容、wire command、ownership、
   motion leaseは変更しない。

#### 完了条件

- `validator/pre_validator.py` が `runner.py` または`utils.stage.control_stage`のprivate名
  （特に`_OPS`）をimportしていない。
- real controller、simulator、PreValidatorにMOVE_CONSTRAINTSのmatching loopのコピーが残らず、
  一つのpublic pure evaluatorとcompatibility wrapperを使う。
- 現行4実装に対するcharacterization/parity testが、同じallow/block結果と安全上重要なerror情報を保つ。
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

装置通信を含む実行前検証という価値を維持しつつ、baseline `e6cb526` で 2,241 行の
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
4. Run 押下時にsequence fingerprintと、Phase 4で定義したcanonical settings serializerによる
   settings fingerprintを照合する。Global settingsに`to_dict()`があることを前提にしない。
5. fingerprint が一致しても、certificate の original StageSnapshot と Run 直前の fresh StageSnapshot を
   Ch1--11 ごとに比較する。位置差分だけでなく getter 失敗も専用 Diagnostic として明示的に報告し、
   baseline を fresh snapshot にリセットして続行しない。
6. original snapshot との差分確認とは別に、現行 `_on_run()` と同じ full live preflight を再実行する。
7. Stage position、PACE source pressure、接続状態などが変化して新しい error が出た場合は Run を
   拒否する。
8. warning の内容が変わった場合は、最新 warning に対してのみ続行確認を出す。
9. original baseline comparison、fresh live preflight、最新warningの続行確認がすべて成功した後にのみ
   `close_all_sub_windows()`を呼ぶ。失敗・キャンセル経路では他windowを閉じる副作用を起こさない。
10. 上記確認とsubwindow closeが完了した後にのみ
   `SequenceRunner` を生成・start する。

#### 完了条件

- Validate 後に Sequence または settings を変更すると Run できない。
- ステージ位置が変化した場合、fresh snapshot 単独の再validation結果にかかわらず、certificate の
  original snapshot との差分として Run が拒否される。
- Run 直前の full live preflight も現行どおり毎回実行される。
- validation error / baseline差分 / warningキャンセル時には`close_all_sub_windows()`が呼ばれない。
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
- Visual editorで設定可能なfieldを含むVisual -> Script生成textのcompile

`take_xrd` は一般的な「全optional引数」一件だけで済ませず、acquisition/correction 8 fieldと
oscillation関連5 fieldの計13 fieldについて、一つずつ欠落を検出できるparameterised testを置く。
API signatureへ追加したfieldがLLM promptへ公開されるか、CommandSpec metadataで非表示になるかも
期待値として固定する。

全 command 共通で次を確認する。

- unknown function
- method call / attribute access
- import / def / lambda / while 等
- oversized / nested loop
- 未対応 statement が silent ignore されないこと
- enclosing `for`で束縛されたbare Nameはnested loopとshadowingを含めて解決されること
- 束縛されていないbare Nameはtypoとしてsource line付きDiagnosticになること
- `Assign` / `If` はsilent ignoreされずunsupported-statement Diagnosticになり、`SPEC.md`の
  supported syntaxと一致すること
- 既存`tests/test_exp_scheduler_dsl_validator.py`はmessage文言への偶発的依存ではなく
  `Diagnostic.code`、または明示的に維持すると決めたcompatibility messageを検査すること

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
`SPEC.md`に記載されているがAction model / runner semanticsが存在しない`Assign` / `If`の新規実装は
本計画の対象外とし、Phase 2ではfail-closed化とSPEC訂正だけを行う。実装する場合は変数scope、
比較評価、分岐Action、execution traceを設計する別projectとする。

また、既存MOVE_CONSTRAINTS判定を一つのpublic pure evaluatorへ集約する`utils/stage`内のrefactorは
本計画の対象である。ただしconstraint rule、hardware protocol、controller ownership、motion lifecycleは
変更しない。

---

## 11. 全体の完了条件

次をすべて満たした時点で再編完了とする。

1. UI / LLM の DSL 処理入口が `DslCompiler` に統一されている。
2. required 引数、unknown keyword、未束縛のbare Name、未対応構文が Action 生成前に
   行番号付きで拒否される。
3. validated AST の情報が SequenceBuilder で黙って失われない。
4. DSL command の signature、metadata、validator rule、Action factory が CommandSpec を正本として
   管理される。
5. `Action.to_dsl()` の全出力を compiler が欠落なく round-tripでき、Visualから生成したDSLも
   strict binderが受理する。特に`take_xrd`の13 override fieldを保持する。
6. Visual / Script / JSON が同じ Action static validation と PreValidator を通る。
7. PreValidator が装置との read-only 通信を含む live preflight として機能する。
8. Stage current positions、PACE source pressure 等が Validate と Run 直前に確認され、Stage は
   certificate の original baseline との差分確認と fresh live preflight の両方を通る。
9. ユーザーの validation UX が一つの Validate 操作と結果一覧に保たれている。
10. Runner / controller の実行時安全確認が維持され、MOVE_CONSTRAINTSのmatching loopは
    real controller、simulator、PreValidatorで一つのpublic pure evaluatorを共有し、private `_OPS`
    importや第三の実装が残っていない。
11. `validator/VALIDATOR.md` の全項目に対応する実装と hardware-free test がある。
12. strict 化前の保存済み DSL / Sequence fixture の互換性結果と `DSL_VERSION` 判断が記録されている。
13. narrow test、scheduler test、simulation test が通り、残る実機依存リスクが文書化されている。
14. `SPEC.md` のsupported DSL syntaxがcompilerの実装と一致し、`Assign` / `If`は別projectで実装される
    までは明示的に拒否される。
15. Run gateの全検証とwarning確認が成功する前にsubwindow closeやRunner startの副作用が起きない。

この再編の最終目標は validator を一つにすることではない。ユーザーには一つの明快な
Validate 体験を提供しながら、内部では compile、preflight、runtime、controller の各層が
同じ正本と安全ルールを共有し、必要なタイミングで繰り返し検証できる状態を作ることである。

---

## 12. Phase 0 実施記録（2026-07-17）

Phase 0 の作業（§7 Phase 0）を実施した記録。コード本体への変更は行っていない
（inventory と hardware-free test の追加のみ）。

### 12.1 baseline の再確認

- 基準 commit は引き続き `e6cb526`（"update GUI"）。作業開始時の実際の HEAD は
  `306484e`（"update planning"）だったが、`git diff e6cb526 HEAD` は
  `REORGANISATION_PLAN.md` / `SPEC.md` / `validator/VALIDATOR.md` 以外に差分がなく、
  コード面では `e6cb526` と同一であることを確認済み。
- 主要ファイルの行数（`e6cb526` と一致、再計測値）：
  `validator/pre_validator.py` 2241、`dsl/api.py` 697、`actions.py` 1016、
  `runner.py` 1794、`dsl/parser.py` 338、`dsl/validator.py` 384、
  `dsl/normalizer.py` 103、`dsl/_registry.py` 51、`dsl/__init__.py` 46。
- 既存 test 総数（Phase 0 開始前）: 34（`test_exp_scheduler_dsl_validator.py` 9、
  `test_exp_scheduler_keithley_removed.py` 5、`test_exp_scheduler_pre_validator.py`
  20 skip込み）。全て green（`python -m unittest discover -s tests -p
  'test_exp_scheduler*.py'`）。

### 12.2 §2.2 の再検証結果

§2.2 の表に記載された各項目を baseline 上で個別に再現し、すべて記載どおりであることを
確認した（表の修正は不要）。加えて、Phase 0 の round-trip characterization 作業中に、
§2.2 に未記載だった以下 2 件の silent data-loss を新たに確認した。いずれも
`tests/test_exp_scheduler_dsl_roundtrip.py` に characterization test として記録済み。

| 新規確認した乖離 | 確認結果 |
|---|---|
| `StageAction`（`speed` 併用の `move_absolute`/`move_relative`）の Visual→Script 往復 | `to_dsl()` は `set_speed(...)\nmove_absolute(...)` の2行を出力し、compile すると1つの Action ではなく2つの独立した `StageAction` に分かれる。単一 Action が持っていた「ch/value/speed をまとめて1操作」という意味は失われる（`actions.py` `StageAction.to_dsl()`）。 |
| `StartFollowingAction` / `FollowSampleAction` の `camera_index` | 他フィールドと異なり `if self.camera_index is not None` のようなガードすらなく、`to_dsl()` が**常に** `camera_index` を出力しない。0 以外のカメラを指定した Visual step を Script tab に変換すると常にカメラ指定が失われる（`actions.py` 該当 `to_dsl()`）。`autofocus_range_um`/`autofocus_steps` は §7 Phase 3 完了条件の「follow / autofocus fields」に相当し、`dsl/api.py` にそもそもパラメータが存在しないため同様に失われる。 |

これらは §7 Phase 2/3 の作業範囲（`Action.to_dsl()` の全出力を compiler が欠落なく
round-trip できるようにする）にそのまま含めてよい。新規の別 Phase は不要と判断した。

### 12.3 実データ (Sequence JSON / DSL script) の inventory

リポジトリ内を検索したが、保存済み Sequence JSON、DSL script fixture は見つからなかった
（`*.dsl`、`sequence*.json`、`"schema": "exp_scheduler"` を含む JSON のいずれも 0 件）。
**実データ未確認** として記録する。§9.7 の残存リスクどおり、Phase 2 以降の互換性判断は
合成 fixture（本 Phase 0 で追加した round-trip test 群）のみに基づいており、実運用データに
よる裏付けはない。ユーザーが保存済み Sequence を持っている場合は、個人情報・測定情報を
除いた代表例を後続 Phase のどこかで提供してもらうことを推奨する。

### 12.4 追加した hardware-free test 資産

- `tests/exp_scheduler_fakes.py`（新規）: `FakeStageController` / `FakePace5000`
  （新規追加）/ `FakeLakeshore` / `FakeRadicon`。call recording
  （`.calls` / `.call_count(name)`）と fault injection（`.fail_on`）を共通で持つ。
  `tests/test_exp_scheduler_pre_validator.py` のローカル定義をこちらに一本化した
  （挙動は変更していない — 20/20 green を確認済み）。
- `tests/exp_scheduler_dsl_inventory.py`（新規）: §7 Phase 0 item 3 の「全 DSL command
  についての表」。`ALLOWED_FUNCTIONS` の23 command + orphan の `normal_stop` について、
  最小有効呼び出し・required/optional kwargs・対応 Action type・loop-variable 対応
  kwarg を記録。`test_exp_scheduler_dsl_contract.py` がこの表自体を実際の
  `ALLOWED_FUNCTIONS`/`DSL_NAMESPACE`/`_BUILDERS`/registry と突き合わせて検査する。
- `tests/test_exp_scheduler_dsl_contract.py`（新規、14 test）: command 集合の
  set-diff 検査、全 command の min-valid-call compile 検査、全 required kwarg の
  欠落検査、`_VALID_UNITS` 全 kwarg の invalid value 検査、および §2.2 の
  silent-acceptance 系バグ（unknown keyword、`normal_stop`、`Assign`/`If`、未束縛
  bare name、`duration=0`、空 `log_message`）の characterization test。
- `tests/test_exp_scheduler_dsl_roundtrip.py`（新規、33 test）: 全 Action type の
  `to_dsl() → compile → Action` round-trip、`take_xrd` の13 override field
  matrix（§7 Phase 0 item 5 で明示要求されている個別 parameterize 済み）、
  JSON (`to_dict`/`from_dict`) round-trip、および上記12.2の新規発見2件。
- `tests/test_exp_scheduler_pre_validator.py` に追記（2 test）: Visual/JSON
  経由で直接構築された不正 Action（DSL の `ASTValidator` を経由しない）を
  PreValidator が独立に検出することを確認する
  `ExpSchedulerPreValidatorDirectActionInjectionTests`（§7 Phase 0 item 8）。

Phase 0 終了時点の `tests/test_exp_scheduler*.py` 総数: 83 test（1 skip、他 green）。

### 12.5 §7 Phase 0 item 10 の contract 決定

作業開始前に決定・記録することとされていた項目について、本文中の default 案をそのまま
採用したものは「確定」、本文に default が無く新たに提案するものは「提案（要確認）」と
した。「提案（要確認）」はPhase 2着手前にユーザー確認を得ること。

| # | 論点 | 状態 | 内容 |
|---|---|---|---|
| 1 | `Assign` / `If` | 確定 | Phase 2 で明示的に fail-closed 拒否する。`SPEC.md` の使える構文を実装に合わせて修正する。実装（変数 scope・分岐 Action 設計）は別 project とし、本計画には含めない。 |
| 2 | `take_xrd` の13 override field | 確定 | DSL 公開引数にする方針をデフォルト採用。Phase 3 で `dsl/api.py::take_xrd()` の signature / factory へ追加する。 |
| 3 | 13 field 追加による LLM prompt 露出 | 提案（要確認） | `prompt_builder.py` が `inspect.signature()` を直接展開するため、追加即 LLM 語彙に出る。Phase 3 で `CommandSpec` に `llm_visible` 相当の metadata を持たせて分離することを提案するが、「デフォルトで LLM に見せる」か「デフォルトで隠す」かは Phase 3 着手前にユーザー判断を仰ぐ。 |
| 4 | MOVE_CONSTRAINTS 共有 evaluator | 確定 | `utils/stage` へ既存 `_check_move_constraints_using()` を基に昇格。Phase 4 の明示 scope として承認済み（本文 §3.6・§9 で既承認）。 |
| 5 | positional argument の扱い | 確定 | 現行どおり keyword-only を維持し、positional は今後も拒否する。新規許可はしない。 |
| 6 | `duration=0` / 空 `log_message` の compile 層拒否 | 提案（要確認） | 現状: `wait`/`follow_sample_position` の `duration=0` は compile を通り `PreValidator._check_durations()` が実行前に拒否する（層間 contract 不一致として現存）。空 `log_message` はどの層でも拒否されない。提案: `duration` は compile 層でも `> 0` を要求し（PreValidator と contract を一致させ、行番号付きで早期に落とす）、空 `log_message` は許可のまま据え置く（区切り用の空ログ行など正当な用途がありうるため）。ただしこれは DSL の許容範囲を狭める breaking change に該当するため、Phase 2 着手前にユーザー確認を得る。 |
| 7 | `DSL_VERSION` / 移行方針 | 提案（要確認） | §12.3 のとおり実データ未確認のため互換性リスクは主に理論上のもの。提案: Phase 2 で fail-closed 化を有効にする回で `DSL_VERSION` を `2.0.0` → `2.1.0` に上げ、リリースノートに breaking change 一覧（unknown keyword 拒否、未束縛 bare name 拒否、`Assign`/`If` 拒否、上記6番が承認された場合は `duration=0` 拒否）を記載する。 |

### 12.6 Phase 0 完了条件チェック（§7 Phase 0「完了条件」対応）

- 現在有効な DSL command の一覧と Action 対応: `tests/exp_scheduler_dsl_inventory.py` +
  `test_exp_scheduler_dsl_contract.py` で可視化・自動検査済み。
- baseline commit と実測値: 本 §12.1 に記録済み。
- `take_xrd` の13 override fieldとVisual→Script生成textのround-trip matrix:
  `test_exp_scheduler_dsl_roundtrip.py::TakeXrdPerStepOverrideRoundTripTests`
  （13 field 個別 parameterize）。
- 既知の不正挙動の characterization/regression 対応: §2.2 全項目 +
  12.2 の新規2件を characterization test 化済み。通常 test suite を故意に
  failing のまま残していない（83 test 中 82 green、1 skip は既存の
  Ch8/Ch11 collision rule 未復旧によるものでPhase 0以前から skip）。
- 実運用データの確認状況: §12.3 のとおり「実データ未確認」と明記。
- Stage / PACE5000 / LakeShore / Radicon の fake: §12.4 のとおり
  `tests/exp_scheduler_fakes.py` に一本化・拡張済み（call recording +
  fault injection 対応、Fake PACE5000 を新規追加）。
- 物理ハードウェア不要でテスト実行可能: 上記すべて `python -m unittest
  discover -s tests -p 'test_exp_scheduler*.py'` で実行可能（要 `pyserial`
  スタブ、既存パターンを踏襲）。

Phase 0 はこれで完了条件を満たす。Phase 1（`DslCompiler` facade と共通
Diagnostic の導入）着手前に、上表12.5の「提案（要確認）」2件（#3, #6, #7）を
ユーザーに確認すること。

---

## 13. Phase 1 実施記録（2026-07-17）

ユーザー承認を得て Phase 1 に着手（§12.5 の「提案（要確認）」#3・#6・#7 は
Phase 2/3 着手前に確認する前提を維持し、Phase 1 の作業自体はブロックしない
と判断 — これらは compile 層の許容範囲を変える話であり、Phase 1 は「経路の
統一」のみで許容範囲を変えないため）。

### 13.1 追加・変更したファイル

- 新規 `validator/models.py`: `Severity` / `ValidationPhase` / `Diagnostic` /
  `ValidationReport`（§5.1 のとおり）。`ValidationReport.errors` /
  `.warnings` / `.ok` は既存 `PreCheckResult` と互換の property。
  `certificate` field は Phase 8 で追加する（§5.5 の型がまだ存在しないため）。
- 新規 `dsl/compiler.py`: `DslCompiler.compile(source) -> CompileResult`。
  `normalize() → ASTValidator().validate() → SequenceBuilder().build()` を
  必ずこの順で通す。`SyntaxError` / `NormalizationError` / validator error /
  builder exception をすべて `Diagnostic` に変換する。`ASTValidator` の
  "Line N: ..." 形式の自由文を安定した `Diagnostic.code`
  （`dsl.required_argument_missing` 等）へ分類する
  `_classify_legacy_message()` は、Phase 3 で `ASTValidator` 自体を
  Diagnostic-native に書き換えるまでの一時的な shim と明記した。
  `ActionSourceMap`（§5.2 の `source_map`）は top-level statement の行番号
  のみを記録する暫定実装とし、`SequenceBuilder` が文を silently drop しな
  くなる Phase 2 まで Action-index に揃えられないことをdocstringに明記した。
- 変更 `ui/dsl_editor.py`: `_on_validate()` / `_on_convert()` を
  `DslCompiler` 経由に統一。直接 `ast.parse()` + `SequenceBuilder` を呼ぶ
  箇所、`ASTValidator` の直接 import を除去。意図的な副作用として、Script
  Editor の compile 経路に初めて `normalize()` が適用されるようになった
  （§7 Phase 1 注意点のとおり）。
- 変更 `llm/session.py`: `_normalise_and_validate()` を `_compile()` に
  置き換え、内部で `DslCompiler` を使用。`try_extract_and_validate()` /
  `apply_selffix_response()` の公開シグネチャ（`(dsl_text, errors)` の
  tuple）は変更せず、新たに `last_sequence` property で compile 済み
  `Sequence` を保持・公開する。装置 preflight は一切呼ばない
  （§7 Phase 1 item 6）。
- 変更 `ui/llm_panel.py`: `_on_apply()` が `self._session.last_sequence`
  を直接使うようになり、`self._pending_dsl` を再度 `ast.parse()` +
  `SequenceBuilder` で再構築する処理を削除。
- 変更 `tests/test_exp_scheduler_dsl_validator.py`: 9 test すべてを
  `ASTValidator` 直接呼び出し + message 部分文字列 assert から、
  `DslCompiler` 経由 + `Diagnostic.code` assert へ移行（§7 Phase 1
  item 8）。`ASTValidator` 自体への直接 unit test は Phase 0 で追加した
  3 ファイル（contract / roundtrip / keithley_removed）に残っており、
  「compiler 経由の contract」と「validator 実装の unit test」を役割分担
  させている。
- 新規 `tests/test_exp_scheduler_dsl_compiler.py`（12 test）:
  `DslCompiler` 自体の contract（成功時の Sequence/diagnostics、各種
  Diagnostic 分類、normalizer が確実に適用されること、source_map の
  行番号、複数エラーの集約）。

### 13.2 完了条件チェック（§7 Phase 1「完了条件」対応）

- UI / LLM 内に `SequenceBuilder().build(ast.parse(...))` の直接呼び出しが
  残っていないことを `grep` で確認済み（残るのは `llm/session.py::_extract_dsl()`
  内の候補抽出用 `ast.parse()` のみで、これは意図的にSequence生成入口とは
  区別されている — §7 Phase 1 注意点のとおり）。
- `ui/llm_panel.py` は session が検証した text を再 parse せず、同じ
  compile 結果の Sequence を使う: offscreen Qt smoke test で
  `_on_apply()` が emit する Sequence オブジェクトが
  `session.last_sequence` と同一オブジェクトであることを確認済み
  （identity check、コピーではない）。
- Script Editor と LLM で同じ DSL に対する compile 結果が一致する:
  両方とも `DslCompiler().compile()` を唯一の入口として使うため、実装上
  自明に保証される（同じ関数を呼んでいる）。
- 既存の有効 DSL の意味は、Script Editor への normalizer 適用という
  意図的な経路統一を除いて変わっていない: 95 test 全て green
  （Phase 0 の 83 + 本 Phase 追加 12 new compiler tests、
  `test_exp_scheduler_dsl_validator.py` は書き換えのみで純増ではない）。

### 13.3 動作確認

PyQt6 の `QT_QPA_PLATFORM=offscreen` を使い、実際の Widget
（`DslEditor` / `LlmPanel`）を実インスタンス化してのスモークテストを
実施（実 GUI 操作の代わりとして — ヘッドレス環境のため）。

- `DslEditor`: 有効スクリプトの Validate（エラーなし）、無効スクリプト
  の Validate（`ramp_rate` 欠落を正しく報告）、Convert to Visual
  （2 Action の Sequence を emit）、`set_sequence()` で生成した Script
  が再度 Validate を通ること、空テキストの扱い、を確認。
- `LlmPanel`: フェイクの LLM 応答（コードブロック付き）を
  `session.try_extract_and_validate()` に通し、`session.last_sequence`
  が正しく populate されること、`_on_apply()` が同一オブジェクトを
  emit すること（再 parse していないことの直接証拠）、無効 DSL 応答が
  compile-only のエラーとして表面化すること（装置 preflight を呼んで
  いないこと）を確認。

いずれも一時スクリプトで実施し、恒久テストには追加していない
（GUI smoke test を hardware-free suite に混ぜることは本 Phase の
スコープ外と判断）。

Phase 1 はこれで完了条件を満たす。Phase 2（strict call binding と
fail-closed parser）は DSL の許容範囲を狭める breaking change を含むため、
着手前に §12.5 の未確定事項（#3 の LLM 可視性は Phase 3 まで保留可、
#6 duration=0/空 log_message の compile 層拒否、#7 DSL_VERSION 方針）を
ユーザーに確認する。

---

## 14. Phase 2 実施記録（2026-07-17）

着手前にユーザーへ §12.5 の残 2 件を確認：#6（`duration=0` の compile 層拒否）は
提案どおり採用（`wait`/`follow_sample_position` とも compile 層で `duration > 0`
を要求し、空 `log_message` は据え置きで許可のまま）。#7（`DSL_VERSION` bump）は
不採用 — `DSL_VERSION` は `2.0.0` のまま変更しない
（`tests/test_exp_scheduler_keithley_removed.py::test_dsl_contract_does_not_export_read_intensity`
の既存 assertion もそのため無変更）。#3（`take_xrd` 追加 13 field の LLM
可視性を隠すか）は元々 Phase 3 まで保留可としていたとおり、今回は何もせず
そのまま LLM prompt に露出する default 挙動とした。

### 14.1 変更したファイル

- `dsl/__init__.py`: `ALLOWED_FUNCTIONS` に `normal_stop` を追加。実装
  （`dsl/api.py`、`SequenceBuilder._BUILDERS`、`_registry.py`）には元々存在して
  おり、ホワイトリストだけが漏れていた既知の乖離（§2.2）を解消。副作用として
  `StageAction(operation="normal_stop").to_dsl()` の自己破壊的 round-trip
  （§2.1）も解消される。
- `dsl/validator.py`:
  - `visit_Assign` / `visit_AnnAssign` / `visit_AugAssign` / `visit_If` を追加
    し、§12.5 決定 #1 のとおり `Assign`/`If` を明示的 fail-closed 拒否に変更
    （メッセージは既存の "is not allowed" パターンに合わせたため、
    `compiler.py::_classify_legacy_message` 側の変更は不要 —
    そのまま `dsl.construct_not_allowed` に分類される）。
  - `_NUMERIC_BOUNDS` に `wait.duration` / `follow_sample_position.duration`
    の下限 `(0.0, exclusive)` を追加し、§12.5 決定 #6 を実装。
- `dsl/api.py`:
  - `take_xrd()` に 8 個の acquisition/correction field
    （`save_dir`, `dark_file`, `dark_enabled`, `defect_file`,
    `defect_enabled`, `defect_kernel`, `flip_v`, `flip_h`）を追加し、
    signature を13 override field 全体（既存5 oscillation field 含む）に
    拡張（§12.5 決定 #2）。
  - `start_following()` / `follow_sample_position()` に
    `autofocus_range_um` / `autofocus_steps` を追加 —
    §12.2 で「Phase 2/3 の作業範囲にそのまま含めてよい」とされていた
    追加発見（`to_dsl()` は出力するが api.py signature に存在しなかった）。
    未対応のまま strict unknown-keyword 拒否を有効化すると、これらの
    field を使う Visual シーケンスの Script 変換が新たに compile error に
    なってしまうため、Phase 2 item 1（"まず Visual→Script 生成 DSL を
    losslessly 受理できる状態にしてから拒否を有効化する"）の一部として
    take_xrd の13 field と同時に対応した。
- `dsl/parser.py`（全面書き換え）:
  - `SequenceBuildError(Exception)` を新設。`SequenceBuilder.build()` は
    ツリー全体を走査して見つかった `Diagnostic` を `self._diagnostics` に
    集約し、1件でもあれば最後に一括してこの例外を送出する（§7 Phase 2
    item 11 の複数エラー集約）。成功時の戻り値（`Sequence` を直接返す）は
    変更していないため、既存テストのうち「有効な DSL が compile できる」
    ことだけを検証している大半の呼び出し箇所は無改修で動作する。
  - `_API_SIGNATURES`（`dsl/api.py::DSL_NAMESPACE` から `inspect.signature()`
    で構築）を「§6.2 の暫定 call binder」として導入。`_build_call()` は
    各キーワード引数を評価した後 `Signature.bind()` + `apply_defaults()` を
    適用し、bound arguments のみを `_build_*` へ渡す。
  - `_eval_arg()` を `(value, error)` タプルを返す形に変更し、
    `ast.Name` が `loop_vars`（そのソース位置を包含する `for` で束縛済みの
    集合）に無ければ `dsl.unbound_name` Diagnostic にする
    （旧実装は束縛済みかどうかに関わらず任意の bare name をそのまま
    文字列として受理していた — §2.2 の既知バグ）。
  - `_build_stmt()` / `_build_for()` / `_build_call()` から silent
    fallback（builder 不在 command を無視、未対応 statement を無視、
    positional argument を無視、unknown keyword を辞書化後に捨てる）を
    すべて除去し、`dsl.unsupported_statement` / `dsl.unknown_function` /
    `dsl.positional_argument_not_supported` / `dsl.unknown_argument` の
    いずれかの Diagnostic に置き換えた。これらのチェックは
    `ASTValidator` を経由せず `SequenceBuilder` を直接呼んだ場合にも独立に
    働く（`tests/test_exp_scheduler_keithley_removed.py` が実例）。
  - `_build_wait()` / `_build_follow_sample_position()` の `unit` 引数を
    `kw.get("unit", "s")` から bound な `kw["unit"]` に変更した副作用として、
    2つの関数とも `unit` 省略時の実際の既定値が `dsl/api.py` の signature
    どおり `"min"` になった（旧実装は "s" にフォールバックしており、
    LLM prompt が示す既定値と実際の compile 結果が食い違っていた —
    `tests/test_exp_scheduler_dsl_compiler.py::test_wait_without_unit_now_defaults_to_minutes_not_seconds`
    で固定）。
  - `_build_take_xrd()` を13 field すべてを `TakeXrdAction` へ渡す実装に
    書き換え。oscillation 5 field は `dsl/api.py::take_xrd()` 本体と同じ
    「`oscillate` が truthy のときだけ osc_* を反映し、それ以外は
    `None`（グローバル設定を継承）」という条件式をそのまま踏襲した。
  - `_build_start_following()` / `_build_follow_sample_position()` に
    `autofocus_range_um` / `autofocus_steps` の受け渡しを追加。
- `dsl/compiler.py`: `SequenceBuildError` を捕捉して `exc.diagnostics` を
  そのまま `CompileResult.diagnostics` に使うルートを追加。想定外の例外用の
  `dsl.build_error` フォールバックは維持（コメントを "builder raises plain
  ValueError today" から "defensive fallback for unforeseen bugs" に更新）。
  `ActionSourceMap` の docstring も、Phase 2 到達後は成功時
  `statement_lines[i]` が `sequence.actions[i]` と1:1に対応するようになった
  ことを反映して更新。
- `llm/prompts.py`: `GRAMMAR` から `assign_stmt` を削除（文法定義上
  `var = value` を有効な statement としていたが、実装は一度もこれを
  Action化しておらず、Phase 2 で明示的拒否になった以上 LLM に教える文法とも
  矛盾する）。`ABSOLUTELY PROHIBITED` に変数代入・`if`/`else`・未知
  keyword・未束縛 bare name の4項目を追記。
- `SPEC.md`: 「使える構文」表から `if`/`else` と `var = value` を削除し、
  「禁止構文」に移動。実装と文書の食い違いを解消した経緯を短い注記として
  追加（§12.5 決定 #1）。

### 14.2 テストの変更

- `tests/exp_scheduler_dsl_inventory.py`: `normal_stop` の
  `in_allowed_functions=False` を削除（デフォルトの `True` に）。`take_xrd`
  の `optional_kwargs` に8 field を追加。`start_following` /
  `follow_sample_position` の `optional_kwargs` に `autofocus_range_um` /
  `autofocus_steps` を追加。
- `tests/test_exp_scheduler_dsl_contract.py`: `KnownDiscrepancyCharacterizationTests`
  を `Phase2FailClosedRegressionTests` に改名し、§2.2 の該当7項目のうち
  6項目（unknown keyword、`normal_stop`、`Assign`、`If`、未束縛 bare name、
  `duration=0` ×2）を「現状のバグを記録する」から「修正後の fail-closed
  挙動を固定する」向きに反転。`log_message(message="")` のみ据え置きで
  許可のまま（§12.5 決定 #6）。未束縛 bare name についてはネストした
  for ループの scope 越境（兄弟ループ間の変数漏れ）を追加検証する新規
  テストも追加。`CommandSurfaceContractTests` の2テストも
  `normal_stop` が `ALLOWED_FUNCTIONS` に入ったことに合わせて更新。
- `tests/test_exp_scheduler_dsl_roundtrip.py`: `normal_stop` の round-trip
  テストを成功系に反転。`take_xrd` の13 field テストと `start_following`/
  `follow_sample_position` の autofocus field テストを「失われる」から
  「保持される」に反転。`move_relative` の bare loop-var round-trip
  テストは、for ループ外で単独の loop-var 参照を作ることがそもそも
  無効な DSL になった（§2.2 の既知バグ修正）ため、拒否を確認するテストと、
  実際に `ForLoopAction` の body 内に置いた場合は正しく round-trip する
  ことを確認するテストの2本に分割。
- `tests/test_exp_scheduler_keithley_removed.py`: `SequenceBuilder().build()`
  が未知 command に対して空 `Sequence` を返す旧仕様のテストを、
  `SequenceBuildError`（`dsl.unknown_function`）を送出する新仕様のテストに
  更新（§7 Phase 0 の予告どおり）。
- `tests/test_exp_scheduler_dsl_compiler.py`: 既存の
  `test_unknown_function_gets_a_stable_code` が `normal_stop()` を
  「未知関数」の例に使っていたため、実在しない関数名に差し替え。新規
  `DslCompilerPhase2FailClosedTests`（7 test）を追加し、`DslCompiler`
  経由での unknown keyword・unbound name（行番号付き）・`Assign`/`If`・
  `normal_stop` 成功・`duration=0`・take_xrd 13 field 保持・`wait()` の
  `unit` 既定値修正を確認。

Phase 2 完了時点の `tests/test_exp_scheduler*.py` 総数: 105 test
（1 skip、他 green。skip は Phase 0 以前からの既知の Ch8/Ch11 collision
rule 未復旧によるもので Phase 2 と無関係）。

### 14.3 完了条件チェック（§7 Phase 2「完了条件」対応）

- compile 成功後に、必須 field が parser 起因で `None` にならない:
  `Signature.bind()` が必須引数の欠落を検出し、`apply_defaults()` が
  optional field の既定値を一度だけ適用するため、`_build_*` 内の
  `kw.get(name, fallback)` はすべて `kw[name]` に置き換わった。
- app自身がVisualから生成したDSLがstrict binderを通り、13個の`take_xrd`
  overrideを保持する: `test_exp_scheduler_dsl_roundtrip.py::
  TakeXrdPerStepOverrideRoundTripTests::test_all_13_fields_are_preserved_by_the_builder`
  で確認。
- 未束縛bare nameはsource line付きDiagnosticとなり、正しいfor-loop変数
  だけがActionへ残る: `dsl.unbound_name` の `source_line` を
  `test_exp_scheduler_dsl_compiler.py` で確認。ネスト・兄弟ループの
  scope境界も手動スモークテストと新規ユニットテストの両方で確認済み
  （§14 本文中の手動確認: shadowing 正常動作、兄弟ループ間の漏れは拒否）。
- validated AST の command / argument / statement が黙って失われない:
  `_build_stmt` / `_build_for` / `_build_call` の silent fallback を
  全廃し、対応する Diagnostic に置換済み。
- invalid DSL は生の `KeyError` / `TypeError` ではなく、行番号付き
  Diagnostic になる: `_build_for` に `node.target` が `ast.Name` でない
  場合や `node.iter` が数値リテラルリストでない場合の防御的チェックを
  追加（旧実装は `ASTValidator` が先に弾く前提で無条件に `.id` へ
  アクセスしており、`SequenceBuilder` を直接呼ぶ経路では `AttributeError`
  になり得た）。
- strict 化で拒否対象になった既存 DSL fixture の結果と `DSL_VERSION`
  判断が記録されている: §12.3 のとおり実運用 fixture は元々存在しない
  （「実データ未確認」）。`DSL_VERSION` は本文冒頭のとおりユーザー判断で
  bump しないことを確定した。
- Visual / JSON 由来の不正 Action は引き続き PreValidator でも拒否
  される: Phase 2 は `validator/pre_validator.py` を変更しておらず、
  `tests/test_exp_scheduler_pre_validator.py`（Phase 0 で追加した直接
  Action injection テストを含む）は無改修のまま green。

Phase 2 はこれで完了条件を満たす。Phase 3（`CommandSpec` と Action
factory の一元化）では、本 Phase で `dsl/parser.py::_API_SIGNATURES` と
いう暫定手段で `dsl/api.py` の signature を直接参照した部分を、
`dsl/_registry.py` を育てた正式な `CommandSpec` registry からの導出に
置き換える。§12.5 決定 #3（`take_xrd` 追加13 fieldをLLM promptに
そのまま露出したままにするか、`CommandSpec` に `llm_visible` 相当の
metadataを設けて隠すか）は Phase 3 着手前に改めてユーザー判断を仰ぐこと。

---

## 15. Phase 2 外部レビュー対応（2026-07-17）

`d815dd4`（Phase 2 実装直後、clean worktree）に対して外部レビューを実施し、
7件の指摘（Critical 1件、High 6件）と、テスト基盤・UI に関する追加の
観察事項を受けた。指摘はすべて実際に再現確認した上で対応方針を決定した
（下表）。UI・test-infra 側の観察事項（DslEditor の空スクリプト扱い、
LLM 側の古い Sequence 保持リスク、hardware-free test 中の実カメラ
アクセス）は Phase 2（DSL compiler/parser）のスコープ外と判断し、
本節では対応していない — 該当する Phase（7/8、Fake device 拡充）で
改めて扱う。

### 15.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 | `oscillate=False` が round trip で `None` になり、Global 設定を継承してしまう | 確認済み（`TakeXrdAction(oscillate=False)` → `to_dsl()` → 再compile で `oscillate=None`） | 修正済み。`TakeXrdAction.to_dsl()` は `oscillate is False` を明示的に出力し、`dsl/api.py::take_xrd()` の signature default を `False` から `None` に変更、`_build_take_xrd()` は bound value をそのまま（truthy 変換せず）保持する。 |
| 2a | `wait(duration=1.0, duration=2.0, ...)` のような重複 keyword が `ast.parse()` レベルでは拒否されず、dict 化時に黙って上書きされる | 確認済み（`ast.parse()` は重複 keyword を許容することを実測で確認 — Python 3.11.5） | 修正済み。`_build_call()` が `node.keywords` を評価する際に重複 `kw.arg` を検出し `dsl.duplicate_keyword_argument` Diagnostic を返す。 |
| 2b | for ループ変数がどの引数にも渡せてしまう（例: `set_speed(speed=p)` が `StageAction(speed="p")` として compile 成功する） | 確認済み | 修正済み。`dsl/parser.py::_LOOP_VAR_ARGS`（`actions.LOOP_VAR_FIELDS` が実際に解決する (function, kwarg) の対応表）を新設し、対象外の引数への bare name 参照を `dsl.loop_variable_not_supported_here` として拒否。`tests/exp_scheduler_dsl_inventory.py` の `loop_var_kwargs` との自動突き合わせテストも追加し、二つの手書き表が乖離したままにならないようにした。 |
| 2c | `Signature.bind()` は型を検査しないため `set_control_mode(enabled="False")` → `True`、`move_absolute(ch=4.9, ...)` → `ch=4` のような危険な暗黙変換が残る | 確認済み | 修正済み。`_annotation_accepts()` を追加し、bound 済み・非 loop-var 引数を `dsl/api.py` の実 annotation（bool/int/float/str と `X \| None`）と突き合わせる。実装中に `dsl/api.py` の `from __future__ import annotations` により `inspect.signature()` が annotation を文字列のまま返し型チェックが無効化されていたバグも発見・修正（`eval_str=True` を追加）。int 引数は normalizer が全ての整数リテラルを float 化する前提のもと、整数値を持つ float（例 `4.0`）のみ許容し、`4.9` のような非整数 float は拒否する。 |
| 3 | f-string の `{p}` は実行時に一切解決されず、未束縛名や複雑な式も compile を通ってしまう | 確認済み（`runner.py` の `LogAction` 処理は `var_context` を一切参照していなかった） | 修正済み。`dsl/parser.py::_eval_fstring()` を `(value, error)` を返す形に変更し、bare な bound loop 変数以外（未束縛名・式）を compile 時に拒否。`runner.py::_execute_one()` の `LogAction` 分岐で `action.message.format(**var_context)` による実行時解決を実装（不正な template は元の文字列にフォールバック）。 |
| 4 | `to_dsl()` の文字列出力が素の f-string 埋め込みで、Windows path（`\U`, `\t`, `\n` 等）が SyntaxError または無音の値破壊を起こす | 確認済み（`C:\Users\...` は SyntaxError、`C:\temp\new` は `\t`/`\n` が実際の制御文字に化けることをバイト単位で確認） | 修正済み。`actions.py` に `_dsl_str()`（`repr()` ベース）を追加し、`to_dsl()` 内の全ての文字列 keyword 埋め込み（`LogAction.message` の手動 quote-escape 含む）をこれに統一。 |
| 5a | `StartFollowingAction`/`FollowSampleAction.to_dsl()` が `camera_index` を出力しない | 確認済み（既存テストが「1が0になる」ことを正常としてassertしていた） | 修正済み。両 `to_dsl()` に `camera_index != 0` の出力を追加し、既存テスト2件を lossless 側の期待に反転。 |
| 5b | `runner.py` の `FollowSampleAction` 実行時、手書きの `StartFollowingAction` 再構築が `autofocus_range_um`/`autofocus_steps` を渡していない | 確認済み（`actions.py::FollowSampleAction.to_steps()` は正しく含めているが、`runner.py` はそれを使わず独自に再構築していた） | 修正済み。`runner.py` を `action.to_steps()` を使う実装に変更し、フィールドリストの二重管理を解消。 |
| 6 | `normalizer.py` の `range()` 展開が (a) `ValueError`/`OverflowError` を `DslCompiler.compile()` の外へ漏らす、(b) 上限チェック前に全要素を list 化する、(c) 非整数 float を `int()` で無音切り捨てする | 確認済み（`range(0, 3, 0)` が `DslCompiler().compile()` から未捕捉の `ValueError` として漏れることを実測） | 修正済み。`_expand_range()` は `range(*args)` オブジェクトの `len()`（非展開・O(1)）を先に `_MAX_RANGE_ELEMENTS` と比較してから list 化し、`TypeError`/`ValueError`/`OverflowError` をすべて `NormalizationError` に変換する。`_eval_int_args()` は非整数 float を明示的に拒否する。 |
| 7 | `validator/pre_validator.py` の `_check_pace5000_wait_duration`（および 2144 行目）が Validate 中に PACE5000 へ `:UNIT:PRES MPA` を write しており、§3.2 の read-only 不変条件に反する | 確認済み（該当2箇所で write を確認） | **未修正**。`apps/PACE5000/` は別リポジトリのgit submodule（CLAUDE.md により変更対象外）であり、書き込みを避けるには `:UNIT:PRES?` のような read-only query を使う代替実装が必要だが、実機なしでは応答フォーマット（例: 大文字/小文字、末尾空白、"MPA" 以外の表記）を確認できず、誤った推測で修正すると「常にMPaとして解釈する」現状より悪い誤変換を静かに埋め込むリスクがある。Phase 6（device snapshot 分離）の対象として、実機アクセス可能な状態で改めて対応することを推奨する。 |

### 15.2 追加したテスト

`tests/test_exp_scheduler_dsl_phase2_review_fixes.py`（新規、26 test）:
上表 #1〜#6 それぞれの再現ケースを regression test として固定。
`tests/test_exp_scheduler_dsl_contract.py` に
`test_inventory_loop_var_kwargs_matches_the_binder` を追加し、
`tests/exp_scheduler_dsl_inventory.py::loop_var_kwargs` と
`dsl/parser.py::_LOOP_VAR_ARGS` の突き合わせを自動化（#2b の根本原因—
2つの手書き表が独立していて互いを検査していなかったこと—への対応）。
`tests/test_exp_scheduler_dsl_roundtrip.py` の
`test_start_following_core_fields_survive` /
`test_follow_sample_position_core_fields_survive` は #5a の修正に合わせて
lossy 側の期待から lossless 側の期待へ反転。

Phase 2 外部レビュー対応後の `tests/test_exp_scheduler*.py` 総数: 132 test
（1 skip、他 green）。

### 15.3 未対応事項

- **PACE5000 write（#7）**: 上表のとおり、実機検証なしでの修正はリスクが
  高いため見送った。Phase 6 着手時、実機アクセス可能な担当者が
  `:UNIT:PRES?` の実際の応答フォーマットを確認した上で対応することを
  推奨する。
- **hardware-free test 中の実カメラアクセス**: レビューで指摘された
  `PreValidator` テスト実行中の `cv2.VideoCapture` 呼び出しは、
  Phase 2（DSL compiler/parser）のスコープ外のため未調査。Fake camera
  backend の注入は Phase 6 の Fake device 拡充と合わせて検討する。
- **DslEditor の空スクリプト scoped Validate / LLM 側の古い Sequence
  保持リスク**: いずれも Phase 7（`ValidationService` 統合）・Phase 8
  （certificate と Run gate）で正式に扱う設計上の課題であり、Phase 2
  では変更していない。

---

## 16. Phase 3 実施記録（2026-07-18）

着手前に §14 末尾で予告されていた保留事項（§12.5 決定 #3 / §15 の
「`take_xrd` 追加13 fieldをLLM promptにそのまま露出するか、`CommandSpec`
に `llm_visible` 相当のmetadataを設けて隠すか」）をユーザーへ確認。
「現状維持でよい」と回答を得たため、`llm_visible` 等の可視性分離
metadataは導入せず、13 fieldとも従来どおりLLM promptへそのまま露出する
default 挙動を維持した。

### 16.1 変更したファイル

- `dsl/_registry.py`（拡張）: `DslCommandMeta` を `ArgumentRule` +
  `CommandSpec` に置き換え。`CommandSpec` は `name`, `category`,
  `example`, `doc`, `signature`, `factory`, `argument_rules` を持ち、
  `required_kwargs` は `signature` から都度導出するプロパティにした
  （手書きの `_REQUIRED_KWARGS` を廃止し、"positional argument は
  常に拒否される＝required は default なしパラメータと同値" という
  §6.2 の性質をそのままプロパティ化）。`dsl_command()` は
  decoration 時に `inspect.signature(fn, eval_str=True)` と
  `fn.__doc__` を自動採取するため、`factory` はキーワード専用の
  必須引数（default なし）とし、渡し忘れは import 時に
  `TypeError` になる（§7 Phase 3 完了条件「registry に builder が
  ない command は import/test 時に失敗する」）。
- 新規 `dsl/_factories.py`: `dsl/parser.py::SequenceBuilder` にあった
  24個の `_build_*` メソッドを `self` を外した module-level 関数として
  移設（ロジックは無変更）。`actions.py` のみに依存し、`api.py` /
  `parser.py` / `_registry.py` のいずれにも依存しないため、
  「`api.py` → `_factories.py` → `parser.py` → `api.py`」のような
  循環 import を避けている。
- `dsl/api.py`: 各 `@dsl_command(...)` 呼び出しに
  `factory=_factories.<name>` を追加し、unit/数値下限/loop-var 制約を
  持つ13コマンドには `argument_rules={...}`（旧 `_VALID_UNITS` /
  `_NUMERIC_BOUNDS` / `dsl/parser.py::_LOOP_VAR_ARGS` の値をそのまま
  移設）を付与した。各関数本体は `_ctx().append(_factories.<name>({...}))`
  という一行に置き換え、独自の Action 構築ロジックを削除した。
  この過程で `set_temperature()` の旧本体が `value`（loop-var 対応
  引数）を無条件に `float()` していたバグ（§2.3 で「仕様定義の
  多重化」の具体例として指摘されていたもの — `dsl/parser.py`
  側の `_build_set_temperature` は正しく素通ししていたが、
  `dsl/api.py` 側だけ食い違っていた）を発見。AST 直接ビルド経路が
  本番で使われるため実害はなかった（`dsl/api.py` の関数本体は
  `DSL_NAMESPACE`/`api_context()` 経由でしかテストから呼ばれず、
  外部・本番コードからの呼び出しはないことを確認済み）が、
  共有 factory への統一によって自動的に解消された。
- `dsl/__init__.py`: `ALLOWED_FUNCTIONS` を `from . import api as _api`
  で registry 登録をトリガーした後、`frozenset(get_registry().keys())`
  から導出する形に変更。`DSL_VERSION` は `2.0.0` のまま変更しない
  （このPhaseはDSLの受理/拒否の意味を一切変えていないため）。
- `dsl/parser.py`: `_API_SIGNATURES`, `_LOOP_VAR_ARGS`, `_BUILDERS`,
  24個の `_build_*` メソッド、`from .api import DSL_NAMESPACE` を削除。
  `_build_call()` は `get_spec(fname)` で `CommandSpec` を取得し、
  `spec.signature` で bind、`spec.argument_rules[...].loop_var_allowed`
  で loop-var 許容判定、`spec.factory(dict(bound.arguments))` で
  Action を生成する。`_annotation_accepts` / `_annotation_str` /
  `_classify_bind_error` はコマンド非依存のため無変更。
- `dsl/validator.py`: `_VALID_UNITS`, `_NUMERIC_BOUNDS`,
  `_REQUIRED_KWARGS` を削除。`_check_unit_args` /
  `_check_numeric_args` / `_check_required_kwargs` は
  `get_spec(fname)` 経由で同じデータを参照するよう書き換えた
  （エラーメッセージ文言・制御フローは完全に同一に保ち、
  `dsl/compiler.py::_classify_legacy_message()` の部分文字列
  テーブルと既存のメッセージベースのテストは無改修で通る）。
- `llm/prompt_builder.py`: `_format_function_spec()` が
  `CommandSpec`（`signature`/`doc` を直接保持）から組み立てる形に
  変更し、`from ..dsl import api as dsl_api` と
  `getattr(dsl_api, name)` を削除した。生成される prompt テキストは
  変更前後で完全に一致することをバイト単位で確認済み（19,133 文字、
  diff なし）。

### 16.2 テストの変更

- `tests/test_exp_scheduler_dsl_contract.py`: `SequenceBuilder._BUILDERS`
  / `dsl.parser._LOOP_VAR_ARGS` / `dsl.validator._VALID_UNITS` への
  直接参照を、`get_registry()` / `CommandSpec.argument_rules` を
  使った同等のアサーションに置き換えた（`test_namespace_builders_and_
  registry_agree`、`test_inventory_loop_var_kwargs_matches_the_binder`、
  `test_unit_and_enum_kwargs_reject_invalid_values`、
  `test_normal_stop_is_allowed_and_round_trips`）。アサーションの意図
  ・検証対象は変更していない。他のテストファイル（roundtrip, compiler,
  validator, phase2_review_fixes, keithley_removed）はこれら private
  名を参照していなかったため無改修。

Phase 3 完了時点の `tests/test_exp_scheduler*.py` 総数: 132 test
（1 skip、他 green。skip は Phase 0 以前からの既知の Ch8/Ch11
collision rule 未復旧によるもので Phase 3 と無関係）。件数は
Phase 2 外部レビュー対応後（§15.2）から変わっていない — Phase 3 は
新規テストを追加せず、既存テストが同じ挙動を新しいデータ源
（registry）に対して検証するよう書き換えただけである。

### 16.3 完了条件チェック（§7 Phase 3「完了条件」対応）

- command を追加・削除するとき、command 名の一覧を複数ファイルへ手作業で
  追加しなくてよい: `ALLOWED_FUNCTIONS`、parser の call binding、
  validator の unit/bound/required チェック、LLM prompt はすべて
  `dsl/_registry.py` の registry から導出される。`dsl/api.py` 内の
  `DSL_NAMESPACE` タプルへの追加のみ同一ファイル内で必要（exec()
  context 用の name→function map であり、§2.3 が指摘した「複数ファイル
  にまたがる仕様の多重化」の対象ではない）。
- registry に builder がない command は import/test 時に失敗する:
  `dsl_command()` の `factory` はキーワード専用必須引数のため、
  渡し忘れは `@dsl_command(...)` 適用時（＝`dsl/api.py` import 時）に
  `TypeError` になる。
- metadata-only entry と完全な `CommandSpec` entry が無期限に混在せず、
  移行完了を自動検査できる: 段階的移行ではなく24コマンド全てを一度に
  完全な `CommandSpec` へ移行したため、混在状態そのものが存在しない。
- API / prompt / parser / validator の command 集合が常に一致する:
  `test_namespace_builders_and_registry_agree` /
  `test_allowed_functions_matches_dsl_namespace` で確認。
- `Action.to_dsl()` が出力する引数を compiler がすべて保持する:
  `TakeXrdPerStepOverrideRoundTripTests` を含む roundtrip test 一式が
  無改修のまま green。
- DSL公開引数とLLM prompt表示引数を分ける場合、その差がCommandSpec
  metadataとtestで明示される: §16 冒頭のとおりユーザー判断により
  分離自体を導入しないことを確定した（該当なし）。

Phase 3 はこれで完了条件を満たす。Phase 4（Runner 依存モデルと純粋
安全ルールの抽出）では、`GlobalLimits` 等の settings dataclass 集約と、
`PM16CController` / `PM16CControllerSim` / `PreValidator` に重複している
MOVE_CONSTRAINTS 判定ループの `utils/stage` への一元化を扱う。

---

## 17. Phase 3 外部レビュー対応（2026-07-18）

`d815dd4`（Phase 3 実装直後、clean worktree）に対して外部レビューを実施し、
3件の指摘（High 1件、Medium 1件、Low 1件）を受けた。すべて実際に
再現確認した上で対応した。

### 17.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 (High) | `take_xrd()` の `osc_pos_a_deg`/`osc_pos_b_deg`/`osc_dwell_ms`/`osc_speed` が、`oscillate=True` を同じ呼び出しで指定しなくても compile を通り、`_factories.py::take_xrd()`（旧 `_build_take_xrd`）の `if oscillate else None` 条件によって黙って `None` に落ちる | 確認済み（`take_xrd(osc_pos_a_deg=1.0, osc_pos_b_deg=2.0, osc_speed="L")` が `ok=True` で `osc_pos_a_deg=None` になることを実測） | 修正済み。`ui/step_editor.py`（Visual editor）を調査し、`oscillate` と4つの sub-field が単一の "has oscillation" checkbox で常に一体として設定されること、`TakeXrdAction.to_dsl()` も `self.oscillate` が truthy のときしか sub-field を出力しないことを確認 — つまり「`oscillate=True` を伴わない sub-field 指定」は Visual → Script 変換では絶対に生成されない組み合わせであり、それを compile error にしても既存の round-trip を壊さない。`dsl/validator.py` に `_check_take_xrd_oscillation_group()` を新設し、`osc_pos_a_deg` 等が指定されているのに `oscillate=True`（リテラル）が同じ呼び出しに無い場合を `dsl.oscillation_subfield_without_oscillate` として拒否する（`oscillate=False` と sub-field の同時指定も、resolved oscillate が False になり sub-field が無意味な値になるため同様に拒否）。この narrowing は Phase 2 の `wait(foo=123)` 等と同種の「意図した2.0.0契約のバグ修正」として扱い、`DSL_VERSION` は bump しない（§14 で `_REQUIRED_KWARGS` 等の narrowing 修正時に確立した precedent と同じ扱い）。 |
| 2 (Medium) | `take_xrd()` の `osc_speed` に `ArgumentRule(valid_values=...)` が無く、docstring が要求する `"H"`/`"M"`/`"L"` 以外の文字列が compile を通る | 確認済み（`take_xrd(oscillate=True, osc_speed="X")` が `ok=True` になることを実測） | 修正済み。`dsl/api.py::take_xrd()` の `@dsl_command` に `argument_rules={"osc_speed": ArgumentRule(valid_values=frozenset({"H", "M", "L"}))}` を追加。 |
| 3 (Low) | `api_context()` / `DSL_NAMESPACE` の docstring が、あたかも DSL 実行経路であるかのように読める（実際は production の `DslCompiler` を経由しない legacy/test-only 経路で、`ASTValidator` の検証を一切通らない） | 確認済み（`exec("wait(1)", DSL_NAMESPACE)` は `DslCompiler` が拒否する positional argument を素通しすることを実測） | 修正済み。`dsl/api.py` のモジュール docstring・`api_context()`・`DSL_NAMESPACE` のコメントに、production パイプラインは常に `DslCompiler`（normalize → ASTValidator → SequenceBuilder）を通ることと、`api_context()`/`DSL_NAMESPACE` はテストが直接参照するための legacy/test-only 経路であることを明記した。 |

### 17.2 追加したテスト

`tests/test_exp_scheduler_dsl_phase3_review_fixes.py`（新規、7 test）:
上表 #1・#2 の再現ケースと修正後の期待動作（`oscillate=True` を伴えば
sub-field が正しく保持されること、`osc_speed` が H/M/L のときは受理される
ことを含む）を regression test として固定。#3 は docstring のみの変更の
ため専用テストは追加していない。

Phase 3 外部レビュー対応後の `tests/test_exp_scheduler*.py` 総数: 139 test
（1 skip、他 green）。

### 17.3 未対応事項

なし。3件とも対応済み。

---

## 18. Phase 4 実施記録（2026-07-18）

§7 Phase 4（Runner 依存モデルと純粋安全ルールの抽出）を実施した。目的は
`validator/pre_validator.py` が `runner.py` / `utils.stage.control_stage` の
private名（`_validate_ch11_oscillation_settings`、特に `_OPS`）に依存する状態を
解消し、MOVE_CONSTRAINTS の matching loop を実装間で共有することであり、
検証層そのものは削らない（§2.3 の「仕様定義の多重化」解消であり「検証の
多層化」解消ではない）。

### 18.1 追加・変更したファイル

- 新規 `utils/stage/move_constraints.py`: `MOVE_CONSTRAINTS`、
  `CH9_CH8_SAFE_BOUNDARY`、`CH8_CH11_CONFLICT_BOUNDARY`、
  `CH11_SAFE_RANGE_PULSES`、`_OPS` を `control_stage.py` から移設し、
  MOVE_CONSTRAINTS の matching loop を一つの private helper
  `_rule_violations()` に集約した。公開 API は3種類:
  - `check_move(ch, target_pos, read_pos) -> (bool, str)` — 最初の違反で
    停止する版（real controller・simulator が使う、旧
    `_check_move_constraints_using()`/`_check_move_constraints_locked()`
    と同じ短絡順序）。
  - `list_move_violations(positions, ch, target_pos) -> list[str]` —
    与えられたスナップショットに対する、ある1手の prospective move の
    全違反（PreValidator の step-by-step シミュレーションが使う、旧
    `_violates_move_constraints_for_move()` 相当）。
  - `list_snapshot_violations(positions) -> list[str]` — スナップショット
    自体の自己無矛盾性チェック（旧 `_violates_move_constraints()` 相当）。

  **意図的な仕様統一（挙動変更）**: 旧実装は必須チャンネルが読み取れない
  場合、real controller / simulator は fail-closed（違反として拒否）だが、
  PreValidator 側の2関数は `req_pos is None: continue`（違反なしとして
  スキップ）と非対称だった。これは `validator/pre_validator.py`
  `_check_stage_move_constraints()` が事前に Ch1–11 全チャンネルを読み、
  1つでも読み取り失敗があればその時点で return する実装だったため、
  実運用では到達不能なコードパスだった（到達可能なら unittest で
  再現できるはずだが、該当する既存テストは無かった — §18.2 参照）。
  Phase 4 では四実装のうち安全側（fail-closed）に統一し、
  `list_move_violations`/`list_snapshot_violations` も必須チャンネル
  読み取り不能を violation として報告するようにした
  (`tests/test_move_constraints.py::PureEvaluatorTests::
  test_snapshot_violations_is_fail_closed_on_unreadable_companion_channel`
  等で新しい挙動として固定)。実運用の到達可能パスでは出力が変わらない
  ことを `tests/test_exp_scheduler_pre_validator.py` の既存22テストが
  無改修 green のまま保証している。
- 変更 `utils/stage/control_stage.py`: `MOVE_CONSTRAINTS`/`_OPS`/境界定数の
  定義を削除し、`move_constraints.py` からのインポート
  （try/except の相対・絶対 import 両方に追加）に置き換えた。
  既存の `from utils.stage.control_stage import CH9_CH8_SAFE_BOUNDARY`
  等の外部呼び出し元（`apps/stage_fpd_scope/`、`apps/seq_move/`、
  `apps/dac_oscillation/`、既存テスト群）は無改修で動作する
  （re-export のため）。`_check_move_constraints_using()` は
  `move_constraints.check_move()` を呼ぶ1行の compatibility wrapper に
  縮小。未使用になった `from operator import ge, le, gt, lt, eq` を削除。
- 変更 `utils/stage/control_stage_sim.py`: 同様に `MOVE_CONSTRAINTS`/`_OPS`
  の import を削除し、`move_constraints` モジュールを import。
  `_check_move_constraints_locked()` は `move_constraints.check_move(ch,
  target_pos, self._get_ch_pos_locked)` を呼ぶ1行の wrapper に縮小
  （`self._get_ch_pos_locked` は既存どおり `self._state_lock` 保持前提）。
- 新規 `apps/exp_scheduler/scheduler_settings.py`: `GlobalXrdSettings`、
  `GlobalLimits`、`GlobalFollowSettings`、`GlobalCameraSettings` を
  `runner.py` から移設（フィールド・docstring は無変更）。
  §5.5 の Phase 8 certificate fingerprint に備え、
  `canonical_settings_dict()`/`canonical_settings_json()` を追加 —
  `dataclasses.asdict()`（`repr()`/object identity 不使用）+
  `json.dumps(..., sort_keys=True)` で、field 値が等しい別インスタンスから
  常に同じ文字列を再現できることを
  `tests/test_exp_scheduler_scheduler_settings.py::
  CanonicalSettingsTests::test_json_is_deterministic_for_equal_values`
  で固定した。`SETTINGS_SCHEMA_VERSION = "1"` を導入（Phase 8 で
  fingerprint 対象 field が変わった場合に上げる）。
- 新規 `apps/exp_scheduler/safety_rules.py`: `runner._validate_ch11_oscillation_settings`
  （private）を `validate_ch11_oscillation_settings`（public）として移設
  （ロジックは無変更）。
- 変更 `apps/exp_scheduler/runner.py`: 上記4クラスと
  `_validate_ch11_oscillation_settings` の定義を削除し、
  `scheduler_settings.py`/`safety_rules.py` から import して
  re-export（`# noqa: F401` 付き、既存 import 元への互換性のため
  — `tests/test_exp_scheduler_pre_validator.py` などが
  `from apps.exp_scheduler.runner import GlobalLimits, GlobalXrdSettings`
  を無改修で使い続けられることを
  `tests/test_exp_scheduler_scheduler_settings.py::
  ReExportIdentityTests::test_runner_reexports_are_identical_objects`
  で確認 — `is` 比較で同一オブジェクトであることを検証）。
  内部呼び出し1箇所を `validate_ch11_oscillation_settings(...)` に更新。
  未使用になった `import math` を削除。
- 変更 `apps/exp_scheduler/validator/pre_validator.py`: import を
  `from ..runner import (...)` から
  `from ..safety_rules import validate_ch11_oscillation_settings` +
  `from ..scheduler_settings import (GlobalFollowSettings, GlobalLimits,
  GlobalXrdSettings)` に変更し、`from utils.stage.control_stage import
  MOVE_CONSTRAINTS, PULSE_SCALE, _OPS` から `MOVE_CONSTRAINTS, _OPS` を除去
  （`from utils.stage import move_constraints` を追加、`PULSE_SCALE` は
  維持）。ローカル関数 `_violates_move_constraints()` /
  `_violates_move_constraints_for_move()`（計約50行）を削除し、3箇所の
  呼び出しを `move_constraints.list_snapshot_violations(positions)` /
  `move_constraints.list_move_violations(positions, ch, target)` に置換。
  `_validate_ch11_oscillation_settings` の呼び出し2箇所を
  `validate_ch11_oscillation_settings` にリネーム。
- 変更 `utils/stage/IMPLEMENTATION_DETAILS.md`: 「Inter-channel move
  constraints」節を、4実装が独立に matching loop を持っていた旧説明から、
  `move_constraints.py` が正本でありその他3箇所が compatibility wrapper
  である現状の説明に更新。冒頭の対象ファイル一覧にも
  `move_constraints.py` を追記。

### 18.2 追加したテスト

- 新規 `tests/test_move_constraints.py`（19 test）:
  - `PureEvaluatorTests`（13 test）: fake device 抜きで
    `move_constraints.check_move()`/`list_move_violations()`/
    `list_snapshot_violations()` を直接検証（§7 Phase 4 完了条件
    「抽出した pure rule は fake device なしで unit test できる」）。
    fail-closed 統一（§18.1）の新規挙動もここで固定。
  - `RealVsSimParityTests`（3 test）: `PM16CController`
    （`tests/fake_transport.FakeTransport` 使用、`tests/test_controller_arbiter.py`
    と同じパターン）と `PM16CControllerSim` に同一の Ch8/Ch9 シナリオを
    与え、allow/block 結果と主要メッセージが一致することを確認
    （§7 Phase 4 完了条件「現行4実装に対する parity test」のうち
    controller 側2実装をカバー。PreValidator 側は既存の
    `tests/test_exp_scheduler_pre_validator.py`
    `test_detects_move_constraint_violation_inside_for_loop` が
    `"Move blocked: Ch9"` という同じメッセージ文言のまま green で
    残っており、3実装目のカバレッジを兼ねる）。
  - `Phase4ContractTests`（2 test）: `pre_validator` モジュールが
    `_OPS`/`MOVE_CONSTRAINTS`/`_validate_ch11_oscillation_settings`/
    `_violates_move_constraints`/`_violates_move_constraints_for_move`
    のいずれも定義・importしていないこと、`from ..runner import` /
    `from apps.exp_scheduler.runner import` がソース中に無いことを
    直接検査する — §7 Phase 4 完了条件の該当項目をそのままテスト化した。
- 新規 `tests/test_exp_scheduler_scheduler_settings.py`（16 test）:
  `ReExportIdentityTests`（runner.py の re-export が同一オブジェクトである
  こと、`_validate_ch11_oscillation_settings` が runner.py にもう存在
  しないこと）、`GlobalLimitsTests`、`CanonicalSettingsTests`
  （schema/version、sorted-key 決定性、field 変更時の文字列変化）、
  `Ch11OscillationValidatorTests`（正常系1件 + 異常系7件 — 非数値、非有限、
  負の dwell、非整数 dwell、bool dwell、不正 speed、endpoint 一致）。
- 既存 `tests/test_control_stage_sim.py` / `tests/test_sim_parity.py` /
  `tests/test_controller_arbiter.py` は無改修であり、Phase 4 由来の新規失敗
  はない（既存 skip 2件・fail 1件・error 5件は baseline（`git stash` で
  Phase 4 変更前コードに戻して再実行し確認済み）から存在する既知の問題で
  あり、本 Phase 由来ではない）。

Phase 4 時点の `tests/test_exp_scheduler*.py` 総数: 155 test
（139 + 本 Phase 追加16、1 skip、他 green）。
`utils/stage` 側の MOVE_CONSTRAINTS 関連テスト
（`test_move_constraints.py` + 既存3ファイル）は計63 test
（新規19 green、既存44のうち38 green・2 skip・1 fail・5 error —
fail/error は前述のとおり Phase 4 と無関係な pre-existing）。

### 18.3 完了条件チェック(§7 Phase 4「完了条件」対応)

- `validator/pre_validator.py` が `runner.py` または
  `utils.stage.control_stage` のprivate名（特に`_OPS`）をimportしていない:
  `tests/test_move_constraints.py::Phase4ContractTests` で自動検査。
- real controller、simulator、PreValidatorにMOVE_CONSTRAINTSのmatching loop
  のコピーが残らず、一つのpublic pure evaluatorとcompatibility wrapperを
  使う: `_rule_violations()` が唯一の matching loop 実装であり、他3箇所は
  いずれも `move_constraints` の公開関数を呼ぶだけの1行 wrapper。
- 現行4実装に対するcharacterization/parity testが、同じallow/block結果と
  安全上重要なerror情報を保つ: §18.2 のとおり。唯一の意図的な挙動差
  （fail-closed 統一）は §18.1 に理由とともに明記し、実運用の到達可能
  パスでの出力は不変であることを既存 PreValidator テストの無改修 green
  で確認した。
- 抽出した pure rule は fake device なしで unit test できる:
  `PureEvaluatorTests` はコントローラ・シミュレータ・fake のいずれも
  構築せず、`move_constraints` モジュール関数を直接呼ぶのみ。
  `validate_ch11_oscillation_settings` も同様（`Ch11OscillationValidatorTests`）。
- Runner の実行順、QThread、cleanup、motion lease に差分がない:
  `runner.py` の変更は import 経路の付け替えと1箇所の呼び出し名変更のみ
  （`_validate_ch11_oscillation_settings` → `validate_ch11_oscillation_settings`、
  ロジックは無変更）。`SequenceRunner` のスレッドモデル・cleanup・
  motion lease 関連コードは一切変更していない。

初回実装時点では、§7 Phase 4 item 7 の例示（Global limits の target 判定、
PACE unit/rate 変換と source pressure 判定の共有関数化）と item 8
（Runner と PreValidator 双方をその共有関数へ切り替える）をスコープ外とし、
Global limits の重複（`runner._check_global_limits_before_move`/
`_check_global_limits` と `pre_validator._violates_global_limits`）を
意図的に見送っていた。外部レビューで、この2項目が §7 Phase 4 の作業項目に
明記されているにもかかわらず完了宣言に含めていた点を指摘され、§19 で対応した
— 本節末尾の完了条件は §19 対応後の最終状態を記す。

Phase 4 はこれで完了条件を満たす。Phase 5（`ExecutionTrace` と静的 Action
validation の共通化）は本 Phase の変更対象外。

---

## 19. Phase 4 外部レビュー対応（2026-07-18）

`tests.test_move_constraints` / `tests.test_exp_scheduler_scheduler_settings` /
`tests.test_exp_scheduler_pre_validator` が57 test green（1 skip）の状態、
すなわち §18 の初回実装直後に対して外部レビューを実施し、2件の指摘を受けた。
いずれも実際に再現確認した上で対応した。

### 19.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 | §7 Phase 4 item 7/8 が未完了。Global limits の判定が `runner._limits_for_ch()`/`_check_global_limits_before_move()`/`_check_global_limits()` と `pre_validator._violates_global_limits()` に重複したまま残っている。PACE の単位・rate・source-pressure 判定も共有化されていない。 | Global limits側は確認済み（§18で意図的に見送ったとおり）。PACE側を改めて調査した結果、単位変換テーブル自体（`PRESSURE_UNIT_TO_MPA`/`RATE_UNIT_TO_MPA_PER_MIN`/`rate_to_mpa_per_sec`/`MIN_SLEW_RATE_MPA_PER_SEC`）は元々 `apps/PACE5000/pace5000_backend.py` を単一の正本として runner.py・pre_validator.py の双方が import しており（runner.py は `PRESSURE_UNIT_TO_MPA`/`RATE_UNIT_TO_MPA_PER_MIN` を、pre_validator.py は `MIN_SLEW_RATE_MPA_PER_SEC`/`rate_to_mpa_per_sec` を）、runner↔pre_validator 間の重複ではなかった。一方 `pre_validator.py` 自身が `_PACE_TO_MPA: dict = {"MPa": 1.0, "Bar": 0.1}` という**手書きの複製**を別途保持しており、これは `pace5000_backend.PRESSURE_UNIT_TO_MPA` と値が完全一致する重複だった（`_PACE_VALID_UNITS`/`_PACE_VALID_RATE_UNITS` も同様、`RATE_UNIT_TO_MPA_PER_MIN` のキー集合と同一）。source pressure 判定（`_check_pace5000_source_pressure`）は PreValidator 内にのみ存在し runner.py 側に対応する実装がないため、重複ではない（extract 対象なしとして扱う）。 | **両方修正済み**。(a) Global limits: `safety_rules.py` に `global_limits_for_channel()` / `global_limit_delta_mm()` / `exceeded_global_limit()` の3純粋関数を追加。前者2つは値の参照・変換のみ、`exceeded_global_limit()` は `plus`/`minus`/`None` を返す判定のみで、runner 側の stop event・ASSTP 送信・ログ出力・Qt signal emit といった副作用は `runner.py` 側に残したまま（§3.6 の「Runner の QThread モデル等を変更しない」を維持）、判定部分だけを共有した。`runner._limits_for_ch()`/`_check_global_limits_before_move()`/`_check_global_limits()` と `pre_validator._violates_global_limits()` をこの3関数を呼ぶ実装に書き換えた。(b) PACE: `pre_validator.py` の `_PACE_TO_MPA`/`_PACE_VALID_UNITS` の手書き定義を削除し、`from apps.PACE5000.pace5000_backend import PRESSURE_UNIT_TO_MPA as _PACE_TO_MPA` のエイリアス import に置き換えた（`apps/PACE5000/` は別リポジトリの git submodule のため、そちら自体は変更していない — CLAUDE.md のとおり）。`_PACE_VALID_RATE_UNITS` は `tuple(RATE_UNIT_TO_MPA_PER_MIN)`（import した辞書のキー）から導出する形に変更し、エラーメッセージに表示される tuple の内容・順序は変更前と完全に一致する。 |
| 2 | canonical settings の「全 fingerprint 対象 field をテストで固定」が不足。serializer 自体は `asdict()` で全 field を含むため現状は問題ないが、テストは一部 field しか確認しておらず、version もモジュール定数との自己比較（`d["version"] == settings.SETTINGS_SCHEMA_VERSION`）になっていて、将来 field 追加・削除時の `SETTINGS_SCHEMA_VERSION` 更新漏れを検出できない。 | 確認済み。`test_includes_schema_and_version` は `settings.SETTINGS_SCHEMA_VERSION` という同じモジュール定数を両辺で比較しており、値が何であっても常に true になるトートロジーだった。 | 修正済み。`tests/test_exp_scheduler_scheduler_settings.py::CanonicalSettingsTests` に、`GlobalLimits`/`GlobalXrdSettings`/`GlobalFollowSettings`/`GlobalCameraSettings` 4クラスそれぞれについて `canonical_settings_dict()` 出力の **key 集合をハードコードした literal set** と突き合わせる4テスト（`test_global_limits_field_set_is_pinned` 等）を追加し、version も `self.assertEqual(d["version"], "1")` という **literal 文字列**での比較に変更した（`test_includes_schema_and_pinned_version_literal`）。`dataclasses.fields()` 等からの自動導出ではなく手書きの literal を使うのが意図的な点で — 自動導出だと dataclass 側の変更に追従してテストも自動的に変わってしまい、drift を検出できない。 |

### 19.2 追加したテスト

- `tests/test_exp_scheduler_scheduler_settings.py`:
  - `CanonicalSettingsTests` に5テスト追加（4クラスの field 集合 pin + version
    literal pin。旧 `test_includes_schema_and_version` は
    `test_includes_schema_and_pinned_version_literal` に改名）。
  - 新規 `GlobalLimitPureRuleTests`（9 test）: `global_limits_for_channel`/
    `global_limit_delta_mm`/`exceeded_global_limit` を fake device なしで
    直接検証。`test_runner_and_pre_validator_no_longer_inline_the_delta_mm_formula`
    は `inspect.getsource()` で両モジュールのソースを直接調べ、旧
    `PULSE_SCALE[ch] / 1000` のインライン式が残っていないこと・
    `global_limit_delta_mm`/`exceeded_global_limit` の呼び出しが実在する
    ことを確認する contract test。
- `tests/test_exp_scheduler_pre_validator.py`: 新規
  `Phase4PaceUnitDedupTests`（2 test）: `pre_validator._PACE_TO_MPA` が
  `apps.PACE5000.pace5000_backend.PRESSURE_UNIT_TO_MPA` と同一オブジェクト
  であること（`assertIs`）、`_PACE_VALID_RATE_UNITS` が
  `RATE_UNIT_TO_MPA_PER_MIN` のキー集合と一致することを確認する。

Phase 4 外部レビュー対応後の `tests/test_exp_scheduler*.py` 総数: 170 test
（155 + 本節追加15、1 skip、他 green）。`tests/test_move_constraints.py`
（19 test、無改修）と合わせた stage/exp_scheduler 関連の Phase 4 テスト
総数は 189 test。リポジトリ全体では 298 test（1 fail・5 error は §18 から
変わらず Phase 4 と無関係な pre-existing、3 skip）。

### 19.3 完了条件チェック（再確認）

- 現行4実装に対するcharacterization/parity testが、同じallow/block結果と
  安全上重要なerror情報を保つ: Global limits・PACE unit dedup のいずれも
  リファクタ前後でメッセージ文言・比較順序（`plus` 判定を先に評価してから
  `minus`）を変えていない。`tests/test_exp_scheduler_pre_validator.py` /
  `tests/test_exp_scheduler_dsl_roundtrip.py` 等の既存テストは無改修のまま
  green。
- Runner の実行順、QThread、cleanup、motion lease に差分がない:
  Global limits 抽出は `_trigger_global_limit_error()` 呼び出しの引数・
  タイミング・`moving=`引数を一切変更していない（`exceeded_global_limit()`
  が返す `"plus"`/`"minus"`/`None` を旧来の2つの独立した `if` 文と同じ順序で
  分岐に使っているだけ）。

### 19.4 未対応事項

なし。2件とも対応済み。

---

## 20. Phase 5 実施記録（2026-07-18）

§7 Phase 5（`ExecutionTrace` と静的 Action validation の共通化）を実施した。
目的は `validator/pre_validator.py`（着手前 2208 行）内の3つの独立した
ForLoopAction ウォーカー（`_collect_all_actions` / `_expand_execution_order` /
`_walk_pace_actions`）と、その上に乗る20個超の `_check_*` を、共通の
`ExecutionTrace` と2つの新しい checks モジュール
（`validator/checks/action_params.py` / `sequence_structure.py`）に整理する
こと。検証を減らすことではなく §2.3 の「仕様定義の多重化」解消が目的である
点は他 Phase と同じ。

着手前に `python -m unittest discover -s tests -p 'test_exp_scheduler*.py'`
で 170 test green（1 skip）を確認済み。

### 20.1 設計レビューの往復（3回却下・4回目で承認）

実装計画を Plan mode で提示したところ、3回にわたり技術的に踏み込んだ指摘を
受けて設計を修正した。並行して進んでいる他 Phase と異なりコード変更前の
段階で発見されたため、実装自体には影響していないが、今後同種の作業を行う
際の参考として要点を記録する。

| # | 指摘の要旨 | 対応 |
|---|---|---|
| 1 | 初回計画は「`_collect_all_actions`（現 `flat`）は反復値ぶん実体化するので危険」という前提だったが、実際は `ForLoopAction.body` を反復値と無関係に1回だけ再帰する既存実装であり、この前提は誤りだった。一方 `SetAndWaitPressureAction` の分割要否が `flat`（分割する）と `ordered`（非分割）で食い違っており、`trace.has()` のような新しい「型の有無」判定を導入すると `SetAndWaitPressureAction` 単独のシーケンスで PACE5000 接続確認が抜け落ちる。 | `flat` は「静的 leaf projection」として明文化し、新しい判定 API は導入しない（既存の `_check_pace5000` 等がそのまま `flat` を使う）。`ordered`（真の反復展開、`SequenceRunner._flat_index` と同じ非分割の数え方）と `pace_primitives()`（`SetAndWaitPressureAction` を明示的に分割する API）を分離する設計に確定。 |
| 2 | `flat` 以外にも生の action tree を再帰する関数（`check_stage_schema` 相当、構造チェック群）が残り、深いネストで `RecursionError` になりうる。`flat` 自身も、フレームごとにフル path 文字列を複製する実装だと深さについて二次関数的にメモリを消費する。`compute_loop_stats` が深さ超過を打ち切る際、`total_steps`/`max_nesting_depth` を水増しなく・かつ「少なくとも」の下限として正確に報告する必要がある。 | `flat` の構築を非再帰・スタックベースの `_collect_flat` に変更し、path は親を指すだけの軽量な連結ノード（`_PathNode`、1階層あたり定数メモリ）で表現、文字列化は leaf 出力時のみ行う。`compute_loop_stats` は深さガード付き再帰（最大 `_MAX_LOOP_NESTING_DEPTH+1` フレームで確実に打ち切る）のまま、打ち切ったノードの未探索 body は `total_steps` へ 0 を加算（空 body として扱っても水増しにならない、常に妥当な下限）。ゲートを `depth_safe`（深さのみ）/`candidates_safe`（+ 個々のループの反復数）/`within_limits`（+ 総展開ステップ数）の3段階に分離し、`check_stage_schema`/`check_lakeshore_params`（ループ変数候補を全走査するため単一ループの幅にも依存）だけ `candidates_safe`、他の構造チェックは `depth_safe` でゲートする。 |
| 3 | `Diagnostic` に loop context の格納先がなく、`action_path`/`loop_context` を実際に設定する経路（`require_finite_number` の引数、TraceEntry/ループ候補値/非ループ値の3ケースでの運用契約）が未定義だった。`_require_finite_number` 等の共有ヘルパーは Global limits や `stage_settings.json` 検証（Action 由来ではない）からも呼ばれており、戻り値契約を素朴に Diagnostic 専用へ変えると壊れる。LakeShore の値検証を `_check_lakeshore_sequence` から分離する際、ループ変数解決込みの数値取得が必要。静的 Action 値検証の抽出範囲が LakeShore（ramp_rate/value_k/tol_k/range_index）と `TakeDarkAction.exposure_ms` を含んでおらず不完全だった。 | `Diagnostic.loop_context: str | None` を追加。数値判定ロジックを pure 関数 `parse_finite_number`/`parse_stage_position`（`(value, error) | (value, None)` を返す、Diagnostic も PreCheckResult も知らない）として分離し、`action_params.py` は Diagnostic へ、`pre_validator.py` の Global limits/`stage_settings.json` はそのまま文字列へ変換する薄いラッパーに分ける。`require_finite_number(..., code, action_path, loop_context=None)` で3ケースの運用契約を明記。新設 `check_lakeshore_params`（`action_params.py`）が該当4種を移設、`TakeDarkAction.exposure_ms` は `check_xrd_params`（旧 `check_xrd_settings` を改名・拡張）に統合。`_check_lakeshore_sequence` は検証を重複させず、非エラーの `_try_resolve_float()` で解決済み数値だけを得る。 |

### 20.2 追加・変更したファイル

- 新規 `apps/exp_scheduler/validator/execution_trace.py`（365行）:
  `LoopIteration`, `format_loop_context`, `LoopExpansionStats`
  （`depth_safe`/`candidates_safe`/`within_limits`/`depth_truncated` の
  4プロパティ・フィールド）, 深さガード付き `compute_loop_stats`
  （旧 `_loop_expansion_stats` を移設 — `_depth+1` が
  `_MAX_LOOP_NESTING_DEPTH` を超えたらそれ以上再帰しない）,
  `loop_limit_messages`（旧 `_check_loop_expansion_limits` のメッセージ
  部分を移設 — `depth_truncated` 時は「少なくとも」の言い回しに変える）,
  `child_path`/`_PathNode`/`_path_str`（構造 path のフォーマットと、深さに
  比例した O(depth) メモリの非再帰実装）, `StaticTraceEntry`/`TraceEntry`
  （`action`/`action_path` に加え、`TraceEntry` は `step`/`variables`/
  `loop_context` を保持）, `ExecutionTrace`（`.flat`——非再帰・常に完全、
  `.ordered`——`within_limits` の時だけ実体化、`.pace_primitives()`——
  `SetAndWaitPressureAction` を分割し親の action_path/step/variables/
  loop_context を継承）。旧 `_collect_all_actions` は `_collect_flat` として
  非再帰化、旧 `_expand_execution_order` は `_collect_ordered` として
  ほぼそのまま移設（`within_limits` の時のみ呼ばれ実深さ<=4が保証される
  ため単純な再帰のまま）、旧 `_walk_pace_actions` は
  `ExecutionTrace.pace_primitives()` に統合。
- 変更 `apps/exp_scheduler/validator/models.py`: `Diagnostic` に
  `loop_context: str | None = None` を追加（末尾に追加、既存生成箇所は
  無改修）。新規 `emit_static(sink, code, message, *, action_path=None,
  loop_context=None, severity=Severity.ERROR)` — `Diagnostic` を構築して
  `sink.diagnostics` へ append しつつ `sink.errors`/`sink.warnings` へも
  message を複製する、Phase 1 で導入した `PreCheckResult`/
  `ValidationReport` 両方が満たせる構造的型 `_DiagnosticSink`（`Protocol`）
  を介したブリッジ。`action_params.py`/`sequence_structure.py` が
  `pre_validator.py` を import せず（循環回避）にこの橋渡しを使える。
- 新規 `apps/exp_scheduler/validator/checks/__init__.py`（空）。
- 新規 `apps/exp_scheduler/validator/checks/action_params.py`（612行）:
  `parse_finite_number`/`parse_stage_position`（pure）、
  `require_finite_number`（STATIC 用ラッパー）、`check_stage_schema`
  （旧 `_check_stage_schema` を移設、`_run_candidates` 対象）、
  `check_pace5000_params`（旧 `_check_pace5000_params` を移設、
  `trace.pace_primitives()` を受け取る）、`check_lakeshore_params`
  （新設、`_check_lakeshore_sequence` から ramp_rate/value_k/tol_k/
  range_index の値検証を移設、`_run_candidates` 対象）、`check_xrd_params`
  （旧 `check_xrd_settings` を改名・拡張、`TakeDarkAction.exposure_ms`
  検証を `_check_radicon` から統合）、`check_durations`/
  `check_follow_params`/`check_autofocus`（各旧関数をそのまま移設）。
  PACE5000 の unit/rate テーブル（`PACE_TO_MPA`/`PACE_VALID_RATE_UNITS`/
  `pace_rate_to_mpa_per_sec`）もここへ移設し、`pre_validator.py` から
  `_PACE_TO_MPA`/`_PACE_VALID_RATE_UNITS` として再エクスポート
  （`Phase4PaceUnitDedupTests` 互換）。
- 新規 `apps/exp_scheduler/validator/checks/sequence_structure.py`
  （239行）: `check_empty_sequence`, `check_unused_loop_vars`
  （+ `_loop_body_uses_var` 等ヘルパー）, `check_undefined_loop_vars`,
  `check_empty_loop_body`, `check_empty_loop_values`,
  `check_duplicate_consecutive_actions`（いずれも旧関数を移設、
  `_run_structural` 対象）, `check_follow_pairing`（旧
  `_check_follow_pairing` を移設、`trace.ordered` を受け取る）,
  `check_loop_expansion_limits`（`execution_trace.loop_limit_messages` を
  呼ぶ薄いラッパー）。
- 変更 `apps/exp_scheduler/validator/pre_validator.py`
  （2208行 → 1457行）: 上記3ウォーカー・関連定数・移設した10関数
  （`_check_stage_schema`, `_check_pace5000_params`, `_check_durations`,
  `_check_follow_params`, `_check_autofocus`, `_check_xrd_settings`,
  `_check_empty_sequence`, `_check_unused_loop_vars`,
  `_check_undefined_loop_vars`, `_check_empty_loop_body`,
  `_check_empty_loop_values`, `_check_duplicate_consecutive_actions`,
  `_check_follow_pairing`, `_check_loop_expansion_limits`, および
  `_require_finite_number`/`_validate_stage_position_value`/
  `_validate_stage_position`/`_validate_ls_temp_value` — 実質17関数）を
  削除。`validate()` の先頭で `ExecutionTrace.build(sequence.actions)` を
  1回構築し（`ExecutionTrace.build()` 自体は `_detect_stage_mode` と同様
  try/except で保護 — 失敗時は全ゲートが False になるフォールバック
  `trace` を使う fail-closed 設計）、新設 `_run_structural`/
  `_run_candidates`/`_run_expanded`（共通 `_run_gated` に委譲）で
  `trace.stats.depth_safe`/`.candidates_safe`/`.within_limits` に応じて
  各チェックをゲートする。残存する装置通信チェック
  （`_check_stage`/`_check_pace5000`/`_check_lakeshore`/`_check_radicon`/
  `_check_camera`/`_check_xrd_oscillation_stage`/`_check_stage_compound`）
  は `trace.flat` ベースに、`_check_stage_move_constraints`/
  `_check_pace5000_control_mode`/`_check_pace5000_ordering`/
  `_check_pace5000_wait_duration`/`_check_lakeshore_sequence`/
  `_check_stage_mode_ordering`/`_check_emergency_stop_confirmation`/
  `_find_max_set_pressure_mpa`（`_check_pace5000_source_pressure`）の
  計8箇所は `trace.ordered`/`.pace_primitives()` ベースに書き換えた
  （内部の独自 ForLoopAction 展開・step_counter を削除）。
  `_check_lakeshore_sequence` は新設の非エラー `_try_resolve_float()` で
  ramp_rate/value_k を解決するだけになり、`_check_radicon` は Rad-icon
  接続確認のみに縮小。Global limits と `_check_stage_settings` は
  `action_params.parse_finite_number`/`parse_stage_position` を直接呼ぶ
  形に変更（STATIC phase の Diagnostic は使わない — Action 由来ではない
  設定検証のため）。`PreCheckResult` に非破壊的フィールド
  `diagnostics: list[Diagnostic]` を追加（`.errors`/`.warnings`/`.ok`/
  `.baseline_positions` は無変更、`ValidationReport` への統合は Phase 7
  のまま）。
- 変更 `apps/exp_scheduler/validator/VALIDATOR.md`: 項目25, 51, 70, 77, 95
  （最も大きい変更 — 3段階ゲートの表と `ExecutionTrace` の設計を追記）,
  項目37（`_check_undefined_loop_vars` の参照先更新）, および
  「PreValidator internal error safety net」節を更新。

### 20.3 テストの追加

- 新規 `tests/test_exp_scheduler_execution_trace.py`（29 test）:
  path フォーマット、loop context フォーマット、`compute_loop_stats` の
  深さガード（深さ100の chain で `RecursionError` を出さないこと、
  空 body の打ち切りノードで `total_steps` が水増しされないこと、
  打ち切りノード自身の `len(values)` は正確に `max_loop_iterations` へ
  含まれること）、`loop_limit_messages` の「少なくとも」言い回し、
  `ExecutionTrace.flat` の非再帰性・width 非依存性（深さ100でも
  `RecursionError` なく完走、幅3000の単一ループでも body を1回しか
  訪問しない）、`ExecutionTrace.ordered` の step 番号
  （`SetAndWaitPressureAction` を1ステップとして扱う）・loop_context・
  action_path、`pace_primitives()` の分割と継承。
- 既存 `tests/test_exp_scheduler_pre_validator.py` に4クラス追加
  （13 test）:
  - `SetAndWaitPressureActionAloneTests`（3 test）: `SetAndWaitPressureAction`
    単独のシーケンスで PACE5000 未接続/Control Mode/`check_pace5000_params`
    の非数値検出が `SetPressureAction`/`WaitPressureAction` を直接使った
    場合と同じメッセージで出ることを確認（§20.1 #1 の regression）。
  - `LoopCrossIterationStateTests`（6 test）: `trace.ordered`/
    `pace_primitives()` の8消費者のうち、着手前は `ForLoopAction` を使う
    テストが存在しなかった6項目（stage move constraints は既存テストが
    あったため対象外）について、「1周目の状態が2周目に持ち越される」
    シナリオを1本ずつ追加（pace5000 ordering の wait_pressure 抜け、
    pace5000 wait_duration の ramp 見積もりに使う current_pressure_mpa の
    引き継ぎ、lakeshore sequence の diff==0 警告に使う current_setpoint の
    引き継ぎ、stage mode ordering の状態がループ境界を越えて後続の
    camera action まで持ち越されること、emergency_stop_confirmation が
    周回ごとに再アームされること、source pressure の最大値探索が
    ループ変数の全候補を見ること）。
  - `LoopLimitSafetyRegressionTests`（3 test）: 深さ超過・単一ループ幅
    超過・総ステップ数超過の3ケースそれぞれで、`RecursionError`/ハング
    無く validate() が完了し、Stage/PACE5000 の接続確認が引き続き
    報告されることを確認（完了条件3の直接的な regression test）。
  - `DiagnosticCodeConsistencyTests`（1 test）: DSL compile は通過しつつ
    `check_pace5000_params`（PACE5000 hardware 最小 slew rate 未満）だけが
    拒否する境界例で、DSL 経由の Sequence と直接構築した Action の両方が
    同じ `Diagnostic.code`（`static.pace5000.rate_below_min_slew`）を
    生成することを確認（完了条件2の直接的な regression test）。

Phase 5 完了時点の `tests/test_exp_scheduler*.py` 総数: 212 test
（170 + 新規42、1 skip、他 green）。リポジトリ全体では 340 test
（1 fail・5 error は §18/§19 から変わらず Phase 4 と無関係な pre-existing
`utils/stage` 関連 baseline 問題、3 skip）。

### 20.4 完了条件チェック（§7 Phase 5「完了条件」対応）

- 同じ Sequence に対する loop 展開順が全 checker で共通になる:
  `trace.ordered`/`trace.pace_primitives()` を8箇所の checker が
  共有するようになったことをコードで確認済み。
  `LoopCrossIterationStateTests` で実際に複数の checker が同じ
  `ExecutionTrace` の反復展開結果を正しく参照することを固定した。
- DSL / Visual / JSON の同じ不正 Action が同じ Diagnostic code で
  拒否される: `action_params.py`/`sequence_structure.py` が `code`+
  `loop_context` 付き `Diagnostic` を生成するようになり、
  `DiagnosticCodeConsistencyTests` で固定した。既存の
  `ExpSchedulerPreValidatorDirectActionInjectionTests`（DSL を経由しない
  直接構築 Action の検出）も無改修のまま green。
- loop 上限を超える Sequence でも、巨大な list を生成せず validation が
  終了する: `flat` は非再帰・O(深さ) メモリの `_collect_flat` で常に安全、
  `compute_loop_stats` は深さガード付き再帰＋非水増しの下限値で
  `RecursionError` を防止、raw-tree 構造チェックは `depth_safe`/
  `candidates_safe` の2段階ゲートで適切にスキップされ、
  `ordered`/`pace_primitives()` は `within_limits` でゲートされる。
  `LoopLimitSafetyRegressionTests` と
  `tests/test_exp_scheduler_execution_trace.py` の該当テストで
  深さ・単一ループ幅・総ステップ数それぞれ単独超過のケースを固定した。

§7 Phase 5 の作業項目8点（execution_trace.py の追加、旧3ウォーカーの整理、
loop 上限チェックを実体展開より先に実行、`SetAndWaitPressureAction` の
明示的 API 化、`action_params.py`/`sequence_structure.py` の新設、
PreValidator helper の委譲・二重実行の排除、action path/loop context の
Diagnostic への付与）はすべて満たされた。Phase 5 はこれで完了条件を
満たす。Phase 6（read-only device snapshot と PreValidator の装置別分割）
では、`validator/snapshots.py` の追加、Stage → PACE5000 → LakeShore →
XRD/Rad-icon → Camera/Follow → cross-device の順で `_check_*` を
`validator/checks/stage.py` 等の装置別ファイルへ移す。§15.1 #7
（`_check_pace5000_wait_duration` が Validate 中に PACE5000 へ
`:UNIT:PRES MPA` を write している件）は Phase 6 で実機アクセス可能な
状態で対応することが Phase 2 外部レビュー対応時点から推奨されたままで
あり、Phase 5 でも変更していない。

---

## 21. Phase 5 外部レビュー対応（2026-07-18）

§20 の実装（212 test green）に対して外部レビューを実施し、4件の指摘
（Critical 1件、High 1件、Medium 1件、Low 1件）を受けた。すべて実際に
再現確認した上で対応した。

### 21.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 (Critical) | `check_autofocus`（action_params.py）が `range_um <= 0`/`steps < 2` を直接比較しており、`math.nan` はどちらの比較でも `False` になるため NaN/Inf を静的検証がすり抜ける | 確認済み（`StartFollowingAction(autofocus_range_um=math.nan, autofocus_steps=math.nan)` で Diagnostic が一切生成されないことを実測。実行時は `_do_follow_autofocus()` の整数変換で例外になる） | 修正済み。`range_um`/`steps` とも `require_finite_number()` 経由に変更（`code="static.follow.invalid_autofocus_range"`/`"static.follow.invalid_autofocus_steps"`、下限を `minimum=0.0, min_inclusive=False`/`minimum=2.0` として既存の意味論を維持）。この直接比較は Phase 5 以前から存在した既存バグであり、Phase 5 の共通化作業で初めて一箇所に集約されたことで修正が容易になった。 |
| 2 (High) | `check_xrd_params` の dark/defect file・`save_dir` 警告（旧 `_check_xrd_settings` から移設した部分）と `check_follow_pairing` の「対応する stop_following がない」警告が、Diagnostic 化されず `r.warnings`/`r.errors` への直接 append のまま残っていた | 確認済み。該当箇所の `result.diagnostics` に該当エントリが1件も無いことを実測 | 修正済み。4箇所すべて `models.emit_static()` 経由に変更（`static.xrd.dark_file_not_found`, `static.xrd.defect_file_not_found`, `static.xrd.save_dir_will_be_created`, `static.xrd.save_dir_not_a_directory`, `static.sequence.follow_not_closed`）。Global XRD 設定（`GlobalXrdSettings.dark_file`/`defect_file`）の警告は Action 由来でないため Global limits/`stage_settings.json` と同じ理由で対象外のまま据え置いた。 |
| 3 (Medium) | `parse_finite_number`/`parse_stage_position`（action_params.py）と `_try_resolve_float`（pre_validator.py）の `float()` 呼び出しが `TypeError`/`ValueError` しか捕捉しておらず、`OverflowError`（巨大な int を float に変換できない場合。手編集/破損した Sequence JSON 由来で到達しうる）を捕捉していない | 確認済み。`StageAction(value=10**500)` で `check_stage_schema: internal validation error (OverflowError(...))` という内部エラーになることを実測（本来期待される `static.stage.invalid_position` Diagnostic ではなく `_run_candidates` の防御的 except 節で拾われた汎用エラーになっていた） | 修正済み。3箇所とも `except (TypeError, ValueError, OverflowError):` に変更。これも Phase 5 以前の `_require_finite_number`/`_validate_stage_position_value` に元々あった既存バグで、pure parser への集約によって1箇所の修正で3箇所すべてに効く。 |
| 4 (Low) | `LoopExpansionStats.max_loop_iterations` の docstring が「常に正確」と説明していたが、深さ超過で打ち切られた枝の**内部**にさらに深い（打ち切りノードより深い段の）幅広ループがある場合、そのループの `len(values)` は走査されず反映されない | 確認済み。6段ネストの6段目（打ち切り境界より下）に3001反復のループを置くと `max_loop_iterations=1` になり、反復回数超過メッセージが単独では出ないことを実測 | 修正済み（指摘が提示した2案のうち軽量な案を採用 — 完全非再帰の幅正確探索へは変更しない）。docstring を「打ち切りノード自身の `len(values)` は正確だが、その内部に隠れたより深いループの幅は反映されない」と正確な記述に修正。`loop_limit_messages()` の反復回数メッセージも `depth_truncated` 時は「少なくとも」表記にした。安全性への実害がないことも明記: `depth_truncated` は常に `depth_safe=False` を意味し、`candidates_safe`/`within_limits` は `depth_safe` に依存するため `max_loop_iterations` の不正確さの影響を受けない。深さ超過メッセージは `max_loop_iterations` の値に関係なく単独で発火する。 |

### 21.2 追加したテスト

`tests/test_exp_scheduler_pre_validator.py::StaticCheckRobustnessTests`
（5 test）: 上表 #1〜#3 の再現ケースと修正後の期待動作（autofocus の
NaN/Inf 拒否×2、巨大 int position が internal error でなく
`static.stage.invalid_position` になること、xrd save_dir と
follow_not_closed が `result.diagnostics` に正しい `code` で入ること）を
regression test として固定。
`tests/test_exp_scheduler_execution_trace.py::
ComputeLoopStatsTests::test_a_wide_loop_hidden_below_the_cutoff_is_not_seen_but_stays_safe`
（1 test）: 上表 #4 の再現ケースと、`depth_safe`/`candidates_safe`/
`within_limits` がすべて安全側に倒れることを固定。

Phase 5 外部レビュー対応後の `tests/test_exp_scheduler*.py` 総数: 218 test
（212 + 本節追加6、1 skip、他 green）。リポジトリ全体では 346 test
（1 fail・5 error は §18/§19/§20 から変わらず Phase 4 と無関係な
pre-existing `utils/stage` 関連 baseline 問題、3 skip）。

### 21.3 未対応事項

なし。4件とも対応済み。

---

## 22. Phase 5 外部レビュー対応（2回目、2026-07-18）

§21 の対応後（218 test green）に対してさらに外部レビューを実施し、2件の
指摘（High 1件、Medium 1件）を受けた。いずれも実際に再現確認した上で
対応した。

### 22.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 (High) | `parse_finite_number()` は `"1.5"` のような数値に見える文字列も `float()` で受理してしまうが、`check_follow_params()`（および他の呼び出し元）は変換後の値を Action フィールドへ書き戻さない。さらに `StartFollowingAction.from_dict()` は `interval_s`/`similarity_threshold`/`max_correction_per_step_um` を型変換せずそのまま保持する（`autofocus_range_um`/`autofocus_steps` は `float()`/`int()` 変換済みだが、この3フィールドは未変換）。そのため手編集 JSON でこれらに文字列を指定すると静的 Diagnostic は出ず、`runner.py` の `time.monotonic() + interval_s` や `max_ch4_um / PULSE_SCALE[4]` で `TypeError` になる。同様に `autofocus_steps=2.5` のような非整数 float も現在の `minimum=2.0` 判定だけでは通過してしまう | 確認済み（`parse_finite_number('1.5', ...)` が `(1.5, None)` を返すこと、`StartFollowingAction.from_dict()` の該当3フィールドに型変換が無いこと、`runner.py` の該当2箇所で素の算術に使われていることをコードで確認。`StartFollowingAction(autofocus_steps=2.5)` を直接構築して validate すると Diagnostic が出ないことを実測） | 修正済み。`parse_finite_number`/`parse_stage_position` に、`float()` を試す前の型ガード `_is_strict_number()`（`bool` を除く `int`/`float` のみを受理 — `bool` は `int` のサブクラスで `float(True)==1.0` になるため、JSON の `true`/`false` の誤配置も同時に弾く）を追加した。現在の全呼び出し箇所はループ変数の解決を呼び出し前に済ませており、この時点で `str` が残っているのは常に型の取り違えであり正当な未解決参照ではないことを確認した上での変更。`parse_finite_number`/`require_finite_number` に `integer: bool = False` を追加し、`check_autofocus` の `autofocus_steps` 検証に `integer=True` を指定した（`autofocus_range_um`/`autofocus_steps` 自体は Action 生成時の型変換の妥当性を変えるものではなく、Phase 5 のスコープである静的検証側の型要求を厳格化したもの — Action 生成時の正規化 (`actions.py::StartFollowingAction.from_dict()`) 自体は変更していない）。 |
| 2 (Medium) | `check_follow_pairing`（sequence_structure.py）の「対応する `stop_following` が無い」警告（`static.sequence.follow_not_closed`）が Diagnostic 化はされたが `action_path`/`loop_context` を渡していなかった | 確認済み（ループ内の `start_following` で再現し、`action_path=None`/`loop_context=None` であることを実測） | 修正済み。`depth: int` を `open_stack: list[TraceEntry]`（`len(open_stack)` が旧 `depth` と同値）に置き換え、最後まで閉じられなかった最も新しい `start_following` の `TraceEntry` を保持して、その `action_path`/`loop_context` を最終警告へ付与するようにした。 |

### 22.2 追加したテスト

`tests/test_exp_scheduler_pre_validator.py::StaticCheckRobustnessTests` に
3 test 追加: `follow_not_closed` Diagnostic の `action_path`/`loop_context`
が非 None であること、`interval_s="1.5"`（数値文字列）が拒否されること、
`autofocus_steps=2.5`（非整数 float）が拒否されること。

Phase 5 外部レビュー2回目対応後の `tests/test_exp_scheduler*.py` 総数:
221 test（218 + 本節追加3、1 skip、他 green）。リポジトリ全体では 349
test（1 fail・5 error は §18/§19/§20/§21 から変わらず Phase 4 と無関係な
pre-existing `utils/stage` 関連 baseline 問題、3 skip）。

### 22.3 未対応事項

なし。2件とも対応済み。

---

## 23. Phase 6 実施記録（2026-07-19）

§7 Phase 6（read-only device snapshot と PreValidator の装置別分割）を
実施した。目的は Phase 5 までと同じく「検証を減らす」ことではなく、
`validator/pre_validator.py`（着手前 1457行）内で装置ごとに散らばっていた
実際の物理値読み取りを `validate()` 1回につき1回に共有し、read-only
不変条件（§3.2）を守ること。着手前に
`python -m unittest discover -s tests -p 'test_exp_scheduler*.py'` で 221
test green（1 skip）を確認済み。

実装計画は Plan mode で提示し、6回のレビュー往復（Stage baseline 契約の
保持、PACE5000 各フィールドの個別 gate、`is_moving` の非対称ゲート、
LakeShore `has_data` の tri-state、PACE5000 の NaN/Inf fail-open リスク、
Diagnostic 所有権の原則、output_state と unit の独立性など）を経て
確定した。詳細はセッション内のレビュー記録を参照 — 最終計画は
`/Users/hiroki/.claude/plans/resilient-plotting-pike.md` にあったものを
そのまま実装した。

### 23.1 追加・変更したファイル

- 新規 `apps/exp_scheduler/validator/snapshots.py`（506行）: 読み取り要件
  `SnapshotRequirements`（`stage_moving`/`pace_used`/`pace_output_state`/
  `pace_target`/`pace_max_set_pressure_mpa`/`pace_unit`/`lakeshore_used`/
  `lakeshore_heater_range`/`lakeshore_data`/`radicon_used`。`pace_source`
  は `pace_max_set_pressure_mpa is not None` から導出する `@property`）と
  `determine_requirements(trace, global_xrd)`（既存の各 `_check_*` が
  実際に使っていた gate 条件をそのまま抽出）、スナップショット
  `StageSnapshot`/`PaceSnapshot`/`LakeShoreSnapshot`/`RadiconSnapshot`/
  `ValidationSnapshot`（すべて frozen dataclass）と各
  `collect_*_snapshot()`、`collect_snapshot()`（トップレベル
  オーケストレータ）。`_find_max_set_pressure_mpa`（旧
  `pre_validator._find_max_set_pressure_mpa`）と `load_stage_settings_dict`
  （旧 `pre_validator._load_stage_settings_dict`）もここへ集約し、
  `determine_requirements`/`collect_stage_snapshot` と
  `validator/checks/stage.py` の双方から共有する（前者は
  `pace_source`/最終安全比較の二重計算を避けるため、後者はステージ
  モード判定と compound action 展開の両方に必要なため）。
  PACE5000 は `Pace5000Backend.write()` を一切呼ばない —
  `query(":UNIT:PRES?")`（非破壊の既存汎用クエリ）で圧力単位を読み、
  `"MPA"`/`"BAR"`（大小文字・前後空白を許容）以外の応答は fail-closed で
  `unit=None` として扱う（§23.3 参照）。
- 変更 `apps/exp_scheduler/validator/models.py`: `emit_preflight(sink,
  code, message, *, device, action_path=None, loop_context=None,
  severity=Severity.ERROR)` を追加（`emit_static` と同形だが
  `ValidationPhase.PREFLIGHT` を付与し、`device` を必須キーワードにして
  既存未使用だった `Diagnostic.device` を埋める）。
- 新規 `apps/exp_scheduler/validator/checks/stage.py`（483行）: 旧
  `_check_stage`/`_check_xrd_oscillation_stage`/`_check_stage_compound`/
  `_check_stage_move_constraints`/`_check_stage_mode_ordering`/
  `_check_emergency_stop_confirmation`/`_violates_global_limits`/
  `_check_stage_settings` を移設（`_detect_stage_mode` は独立関数として
  消滅し、`collect_stage_snapshot` に統合）。すべて `emit_preflight`
  経由で Diagnostic を出す。
- 新規 `apps/exp_scheduler/validator/checks/pace5000.py`（300行）: 旧
  `_check_pace5000`/`_check_pace5000_control_mode`/
  `_check_pace5000_adjacency`/`_check_pace5000_ordering`/
  `_check_pace5000_wait_duration`/`_check_pace5000_source_pressure` を
  移設。`write()` 呼び出しは全廃、`snapshot.pace.*`（既に MPa 換算済み）
  を使う。
- 新規 `apps/exp_scheduler/validator/checks/lakeshore.py`（327行）: 旧
  `_check_lakeshore`/`_check_lakeshore_sequence`/`_try_resolve_float` を
  移設。`get_setpoint()` の重複呼び出しを解消。
- 新規 `apps/exp_scheduler/validator/checks/xrd.py`（40行）: 旧
  `_check_radicon`（Rad-icon 接続確認のみ）を移設。
- 新規 `apps/exp_scheduler/validator/checks/camera_follow.py`（127行）:
  旧 `_check_camera`/`_check_calibration` を移設。camera は
  スナップショット化していない（各カメラアクションがその場で
  `cv2.VideoCapture` を開閉する既存方式のまま — §23.3）。
- 変更 `apps/exp_scheduler/validator/pre_validator.py`
  （1457行 → 342行）: 上記で移設した全関数・ヘルパーを削除。
  `validate()` は trace 構築 → `snapshots.determine_requirements()` →
  `snapshots.collect_snapshot()`（両方とも `ExecutionTrace.build()` と
  同様 try/except で保護 — 失敗時は全フィールド `False`/`None` の
  fail-closed フォールバックを使う）→ 各 checker 実行（既存の
  `_run`/`_run_structural`/`_run_candidates`/`_run_expanded` はそのまま
  維持）→ ログ出力、の facade に縮小した。`_check_global_limits` は
  クロージャからトップレベル関数へ変更。
- 変更 `tests/exp_scheduler_fakes.py`: 全 Fake の `call_count(name,
  *args)` が完全一致カウントにも対応（省略時は従来どおりメソッド名だけで
  カウント、後方互換）。`FakeStageController.fail_on` が
  `(method_name, *args)` タプル（例: `{("get_ch_pos", 5)}`）にも対応し、
  チャンネル単位で読み取り失敗を注入できるようにした。`FakePace5000` に
  `query()`/`unit`（既定 `"MPA"`）を追加。
- 新規 `tests/test_exp_scheduler_snapshots.py`（59 test）:
  `determine_requirements()` の全フィールド別ゲート条件、各
  `collect_*_snapshot()` の正常系/未接続/例外/NaN・Inf/非文字列応答、
  重複読み取り解消の end-to-end 回帰（`PreValidator().validate()` 経由で
  `call_count` を検証）、`stage_mode` 二重メッセージの characterization
  test、`emit_preflight()` 単体テストを含む。
- 変更 `tests/test_exp_scheduler_scheduler_settings.py`:
  `test_runner_and_pre_validator_no_longer_inline_the_delta_mm_formula`
  が `validator.pre_validator` ではなく `validator.checks.stage` の
  ソースを検査するよう更新（`_violates_global_limits` の移設先が
  変わったため — 意図的な追随であり回帰ではない）。
- 変更 `apps/exp_scheduler/validator/VALIDATOR.md`: 項目51
  （`_check_lakeshore_sequence` の参照先更新）、「PreValidator internal
  error safety net」節（`_detect_stage_mode` の消滅と
  `snapshots.determine_requirements`/`collect_snapshot` の
  fail-closed フォールバックを追記）。
- 変更 `apps/exp_scheduler/SPEC.md`: `PreValidator._check_stage_move_
  constraints`/`_check_unused_loop_vars`/`_check_undefined_loop_vars` の
  古い参照に現在の移設先を注記（歴史的な Phase 2 計画記述自体は保持）。

### 23.2 意図的な挙動変更

- **PACE5000 の `write(":UNIT:PRES MPA")` 廃止**: §15.1 #7 で未解決の
  まま持ち越されていた read-only 不変条件違反を解消した。
  `query(":UNIT:PRES?")` の応答が `"MPA"`/`"BAR"`（大小文字・前後空白を
  許容）のいずれとも一致しない場合は fail-closed で `unit=None` とし、
  それに依存する `target_pressure_mpa`/`positive_source_pressure_mpa` の
  読み取り自体を行わない（Diagnostic は unit 読み取り失敗の1回のみ —
  依存側で二重に報告しない）。**実機での `:UNIT:PRES?` の実応答形式
  （本当に大文字 `"MPA"`/`"BAR"` を返すか）はこのセッションでは確認
  できていない** — fail-closed 設計のため不一致でも安全側に倒れるが、
  実際に単位変換が正しく機能するかはユーザー側で実機確認が必要
  （§23.4）。
- **Stage の position/mode と `is_moving` の非対称収集**: position
  （Ch1-11）と、そこから導出する `stage_mode`（Ch8/Ch9 由来）は
  `ctx.controller is not None` でありさえすれば常に収集する — UI
  （`ui/scheduler_window.py`）の Validate–Run 間ステージ移動検出が
  この baseline に依存しているため。一方 `is_moving`
  （`get_is_moving()`）は Stage 系 action、または実効 `oscillate=True`
  の `take_xrd` があるときだけ収集する — 無関係な pressure-only
  sequence の validation が、無関係な Stage の通信不調で失敗しないように
  するため。
- **PACE5000 の各フィールドが個別の gate を持つ**: `output_state` は
  `SetPressureAction`/`WaitPressureAction` のいずれかが存在すれば
  読む（`set_control_mode()` だけの sequence では読まない）。
  `target_pressure`（"current pressure" として使う "soft" 値）は
  `SetPressureAction`（`SetAndWaitPressureAction` は含まない）の型が
  存在するだけで読む — pressure 値自体の妥当性は問わない。
  `positive_source_pressure`（"hard" safety gate）は
  `_find_max_set_pressure_mpa()` が非 None を返す場合のみ読む —
  未解決の loop 変数や非数値な pressure は無視されるため、有効な
  設定圧力が一件も無ければ読まない。圧力単位はこの2つのどちらかが
  必要なときだけ読む共有前提条件で、`output_state` の読み取り可否とは
  完全に独立（unit 読み取りが失敗しても Control Mode 検証は失われない）。
  これらはすべて Phase 6 着手前の実コードから抽出した既存の gate
  条件であり、新設したものではない — `SnapshotRequirements` に一元化
  しただけである。
- **stage_mode の二重メッセージを意図的に維持**: Ch8/Ch9 の読み取り
  失敗と「読み取れたが既知プリセットに一致しない」は同じ
  `stage_mode="unknown"` に収斂する既存設計であり、`take_xrd`/
  `take_dark` が後続する場合、collector 相当のエラーと
  `check_stage_mode_ordering` の警告が両方出ることがある。これは
  Phase 6 が新たに作る問題ではなく、`stage_mode` という単一の文字列に
  2つの異なる原因を収斂させている Phase 5 以前からの設計であるため、
  そのまま維持し characterization test
  （`EndToEndDedupRegressionTests::test_ch8_ch9_unreadable_gives_
  communication_error_and_xrd_mode_unknown_warning`）で固定した。
- **Ch1-11 の読み取りを「最初の失敗で打ち切り」から「全チャンネル
  試行」に変更**: 旧 `_check_stage_move_constraints` は最初の読み取り
  失敗チャンネルで即座に return していたため、2つ目以降の壊れた
  チャンネルは一切報告されなかった。`collect_stage_snapshot` は
  全11チャンネルを必ず試行し、失敗したチャンネルはそれぞれ個別に
  Diagnostic を出す（`CollectStageSnapshotTests::
  test_every_channel_attempted_even_after_earlier_failures` で固定）。
  ただし、この後段の MOVE_CONSTRAINTS 反復シミュレーションと
  baseline 記録は、全11チャンネルが揃った場合のみ実行する（一部
  欠けた position スナップショットの上でシミュレーションを行うと
  誤解を招くため — 旧来の「一つでも失敗したら以降はスキップ」という
  安全側の判断自体は維持し、「どのチャンネルが失敗したか全部報告する」
  部分だけを改善した）。
- **LakeShore `setpoint`/`heater_range` への NaN/Inf 追加ガードなし**:
  これらは現行実装でも生の getter 戻り値をそのまま比較・演算に使って
  おり（`diff = val - current_setpoint` 等）、結果は warning レベルの
  ヒューリスティックにしか影響しない（MOVE_CONSTRAINTS のような
  hard safety gate ではない）。Phase 6 で新たにガードを追加するのは
  既存挙動からの逸脱になるため、無ガードのままを明示的な決定として
  維持した。対照的に PACE5000 の `positive_source_pressure_mpa` は
  hard safety gate（設定圧力の上限チェック）に使われるため
  `action_params.parse_finite_number()` で NaN/Inf/非数値を fail-closed
  で弾く。

### 23.3 Diagnostic 所有権の原則

ある物理量の読み取りに対する Diagnostic は snapshot collector が最大1回
だけ発行し、その値に依存する checker はフィールドが `None`（読み取り
失敗、または未収集）である場合は追加の error/warning を出さずに黙って
skip する、という原則で全 checker を書き直した。§23.2 の stage_mode の
二重メッセージのみ、この原則に対する既知の例外として維持している。

### 23.4 追加したテスト

新規 `tests/test_exp_scheduler_snapshots.py`（59 test）。既存
`tests/test_exp_scheduler_pre_validator.py` は無改修のまま全 green を
確認した。Phase 6 完了後の `tests/test_exp_scheduler*.py` 総数: 280
test（221 + 本節追加59、1 skip、他 green）。リポジトリ全体では 349 test
（1 fail・5 error は §18/§19/§20/§21/§22 から変わらず Phase 4 と無関係な
pre-existing `utils/stage`/`test_controller_arbiter` 関連 baseline
問題、3 skip）。

### 23.5 実機で最終確認が必要な残存事項（このセッションでは確認不可）

- `:UNIT:PRES?` の実機レスポンス形式が想定（大文字 `"MPA"`/`"BAR"`）と
  一致するか。fail-closed 設計のため不一致でも安全側に倒れるが、実際に
  単位変換が正しく働くかは実機でのみ確認可能。
- §8.4 に列挙されている実機確認項目は本 Phase 実装後もユーザー側での
  実機確認が必要。

### 23.6 未対応事項

なし。

---

## 24. Phase 6 外部レビュー対応（2026-07-19）

§23 完了後（280 test green）に対して外部レビューを実施し、2件の指摘
（High 1件、Medium 1件）を受けた。いずれも実際に再現確認した上で
対応した。レビュー環境は Python 3.10（PyQt6/opencv-python 未導入）で
実行されたため4モジュールが import error となり新規テストは完走できて
いなかったが、指摘自体はコード読解のみで正確だった。プロジェクトの
実行環境である `.venv`（Python 3.11.5、依存関係導入済み）で該当スイート
を完走させて確認した。

### 24.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 (High) | `snapshots.py::_find_max_set_pressure_mpa()` の `except (TypeError, ValueError):` が `OverflowError` を捕捉しない。手編集/破損 JSON 由来の巨大整数（例: `10**500`）を pressure に持つ `SetPressureAction` があると `float(10**500)` が `OverflowError` を送出し、`determine_requirements()` 経由で呼ばれるこの関数の例外が `pre_validator.py` の snapshot 収集 try/except まで伝播し、`snapshot collection: internal validation error` になって Stage baseline を含む全装置 snapshot が空になる | 確認済み（`float(10**500)` が実際に `OverflowError` を送出すること、`checks/pace5000.py::check_pace5000_wait_duration()` の `float(pressure)`/`float(a.rate)` 変換にも同じ狭い `except (TypeError, ValueError):` があることをコードで確認） | 修正済み。両ファイルの計3箇所すべてで `except (TypeError, ValueError):` → `except (TypeError, ValueError, OverflowError):` に変更。他の `float()` 変換箇所（`action_params.py::parse_finite_number`/`parse_stage_position` は既に `OverflowError` 個別捕捉済み、`checks/lakeshore.py::_try_resolve_float` も同様、`snapshots.py::collect_pace_snapshot` の target/source 圧力変換は元々 `except Exception:` で包括済み）は既に安全だったため変更不要と確認した。 |
| 2 (Medium) | snapshot 収集（`snapshots.determine_requirements`/`collect_snapshot`）が `_run()` の外で実行されるため、collector が発行する Diagnostic（Stage position/is_moving 読み取り失敗、PACE unit/output/source 読み取り失敗、LakeShore setpoint/heater-range 読み取り失敗）は `result.errors`/`result.warnings`（ひいては UI とエラー件数）には反映されるが、`_run()` が出力する `✗`/`⚠` 付きログ行には現れず、保存される validation log から物理読み取り失敗の詳細が欠落する | 確認済み（snapshot 収集ブロックが `_run()` を経由しない直接の try/except であることをコードで確認し、`FakeStageController` の `get_ch_pos` を失敗させて `PreValidator().validate()` の標準出力をキャプチャしたところ `collect_snapshot` のログ行が出力されず `✗ Cannot read Ch5 position` 行も無いことを実測） | 修正済み。`_run()` 内部の「差分ログ出力」ロジックを `_log_diff(label, e0, w0)` として独立させ、`_run()` からはこれを呼ぶよう変更した上で、snapshot 収集ブロックの前後で `e0`/`w0` を記録し `_log_diff("collect_snapshot", e0, w0)` を呼ぶよう変更（collector 側の try/except 構造自体は変更していない — あくまでログ出力を追加しただけ）。 |

### 24.2 追加したテスト

`tests/test_exp_scheduler_snapshots.py` に2クラス・6 test 追加:

- `HugeIntegerPressureOverflowRegressionTests`（3 test）: `_find_max_set_pressure_mpa` が `pressure=10**500` で例外を出さず `None` を返すこと、`PreValidator().validate()` 全体が `internal validation error` を出さず static PACE parameter error（`pressure is not numeric`）のみ出すこと、Stage baseline（全11チャンネル）が維持されること、`get_positive_source_pressure` が0回であること、`checks/pace5000.py` 側の rate 変換（`rate=10**500`）も同様に例外を出さないこと。
- `SnapshotDiagnosticLoggingTests`（3 test）: `PreValidator().validate()` の標準出力をキャプチャし、Stage position 読み取り失敗時／PACE unit 読み取り失敗時にそれぞれ `collect_snapshot` ラベル行が `ERROR` ステータスで出力され、直後の行に該当する `✗` 診断メッセージが現れること、診断が無い場合は `collect_snapshot` が `OK` と出力されること。

Phase 6 外部レビュー対応後の `tests/test_exp_scheduler*.py` 総数: 286
test（280 + 本節追加6、1 skip、他 green）。リポジトリ全体では 414 test
（1 fail・5 error は §18〜§22 から変わらず Phase 4 と無関係な
pre-existing `utils/stage`/`test_controller_arbiter` 関連 baseline
問題、3 skip）。`.venv`（Python 3.11.5）で実行して確認した。

### 24.3 未対応事項

なし。2件とも対応済み。

---

## 25. Phase 7 実施記録（2026-07-19）

§7 Phase 7（ValidationService と一つの Validate UX）を実施した。目的は
`ui/scheduler_window.py` の `_on_run` / `_on_validate_visual` /
`_validate_sequence_from_dsl`（旧名）がそれぞれ個別に持っていた「UI から
Global\*Settings を組み立てる → `PreValidator().validate()` を呼ぶ →
結果表示・Run 有効化状態更新」という手順の重複を、新規
`apps/exp_scheduler/validation_service.py` へ集約すること。着手前に
`python3 -m unittest discover -s tests -p 'test_exp_scheduler*.py'` で 286
test green（1 skip）を確認済み。

実装計画は Plan mode で提示し、レビューで3点の必須修正
（コンパイル失敗/空スクリプト時に validated 状態がリセットされない配線上の欠陥、
internal-error Diagnostic を一律 STATIC にしていた phase 分類の誤り、
UI 配線を検証する自動テストの不足）を指摘され、対応した版を承認された。

### 25.1 追加・変更したファイル

- 変更 `apps/exp_scheduler/validator/models.py`: 汎用 `emit_diagnostic(sink,
  code, message, *, phase, device=None, ...)` を追加し、`emit_static`/
  `emit_preflight` をその薄いラッパーへリファクタリングした（挙動は不変）。
  `ValidationReport` に `sequence: Sequence | None`（循環 import を避けるため
  `TYPE_CHECKING` 経由の文字列 annotation）と
  `baseline_positions: dict[int, int]` を追加した。`errors`/`warnings`/`ok`
  は引き続き `diagnostics` からの computed property のままである。
- 変更 `apps/exp_scheduler/validator/pre_validator.py`: `PreCheckResult.
  diagnostics` を完全にするため、`.errors.append()`/`.warnings.append()`
  を直接書いていた5箇所（`ExecutionTrace.build()` 失敗時の fallback、
  snapshot collection 失敗時の fallback、`_check_global_limits` の2箇所、
  `_run()` 自身の例外セーフティネット）を `emit_static`/`emit_diagnostic`
  経由に置換した。特に `_run`/`_run_gated`/`_run_structural`/
  `_run_candidates`/`_run_expanded` すべてに必須 keyword-only
  `phase: ValidationPhase`（と該当する場合は `device: str | None`）を追加し、
  `validate()` 内の呼び出し30箇所超すべてに、そのチェッカーが所属する
  モジュール（`action_params.py`/`sequence_structure.py` およびローカル
  `_check_global_limits` は `STATIC`；`validator/checks/{stage,pace5000,
  lakeshore,xrd,camera_follow}.py` は各モジュール既存の `_DEVICE` 定数どおり
  `PREFLIGHT`）に対応する phase/device を明示的に指定した。デフォルト値なしの
  keyword-only にしたのは、将来 checker を追加したとき分類を書き忘れると
  `TypeError` になるようにするため。snapshot collection 失敗は装置読み取り
  経路の失敗のため `PREFLIGHT`（`device=None` — 単一の装置に絞れないため)、
  `ExecutionTrace.build()` 失敗は装置に依存しないため `STATIC` とした。
- 変更 `apps/exp_scheduler/validator/checks/action_params.py`: 3箇所の
  `r.warnings.append(...)`（XRD dark file 不在・defect file 不在・
  autofocus の Ch3 limits 未設定）を `emit_static(..., severity=
  Severity.WARNING)` へ置換した。
- 新規 `apps/exp_scheduler/validation_service.py`: `validate_sequence()`/
  `validate_dsl()`/`revalidate_for_run()` の3公開関数（モジュール関数、
  `safety_rules.py`/`scheduler_settings.py` と同じくクラス化しない）。
  `validate_dsl()` は `DslCompiler().compile()` して失敗なら即座に compile
  diagnostics だけの `ValidationReport(sequence=None)` を返し（device
  preflight を開始しない）、成功したら `validate_sequence()` に委譲する。
  `validate_sequence()` は `PreValidator().validate()` を呼び、
  `PreCheckResult` を `ValidationReport` に詰め替える。`source_map`
  （`dsl/compiler.py::ActionSourceMap`）が渡されていれば、内部の
  `_with_source_lines()` が `action_path` の先頭トップレベル index から
  DSL 行番号を引いて `Diagnostic.source_line` をバックフィルする
  （`Diagnostic` は frozen なので `dataclasses.replace()` で新しい
  Diagnostic を作る）。ループ本体内の action は、その `for` 文自体の行に
  帰属する（`ActionSourceMap` はトップレベル文の行しか持たないため）。
  `revalidate_for_run()` は Phase 7 時点では `validate_sequence()` の薄い
  エイリアスであり、§7 Phase 7 item 2 の擬似シグネチャにある `certificate`
  引数を意図的に持たない（§25.2 参照）。
- 変更 `apps/exp_scheduler/ui/dsl_editor.py`: `set_full_validator(fn:
  Sequence -> Report)` を `set_validator(fn: str -> ValidationReport)` に
  変更し、`DslEditor` は host が接続されている通常経路では `DslCompiler`
  を一切呼ばなくなった。`_on_validate()`/`_on_convert()` は、空文字列・
  構文エラー・compile 成功のいずれであっても例外なく `self._validator(
  self.get_text())` を呼ぶ（§25.2 参照）。`DslCompiler` は host が無い
  standalone fallback（`_standalone_compile_feedback()`、本番では未使用）
  だけで使う。
- 変更 `apps/exp_scheduler/ui/scheduler_window.py`: `from ..validator.
  pre_validator import PreValidator` を削除し `from .. import
  validation_service` に変更。`_on_run()`/`_on_validate_visual()` は
  `validation_service.revalidate_for_run()`/`validate_sequence()` を呼ぶ
  よう変更（`_check_stage_unchanged_since_validation()`/
  `_validated_positions` は一切変更していない）。
  `_validate_sequence_from_dsl(seq)` を `_validate_dsl_text(text)` に改名し
  `validation_service.validate_dsl()` を呼ぶよう変更、
  `set_validator(self._validate_dsl_text)` で接続した。`_on_dsl_converted`
  は中身を `self._tabs.setCurrentIndex(0)` だけに縮小した（DslEditor 側の
  コールバックで既に1回 full validate 済みのため、二重呼び出しを解消）。
  `_show_validation_result()` は `result.errors`/`.warnings`（文字列）では
  なく `result.diagnostics` を直接 severity で振り分けて描画するよう
  書き換え、新規 `_line_prefix()` ヘルパーで `Diagnostic.source_line` を
  `"Line N: "` として前置する。ただし `ValidationPhase.COMPILE` の
  Diagnostic（`dsl/compiler.py`/`dsl/parser.py` 由来）は既に自分の
  `message` に `"Line N: "` を埋め込んでいるため、二重表示にならないよう
  `_line_prefix()` は COMPILE phase を除外する（実装中に実機画面
  スクリーンショットで "Line 1: Line 1: SyntaxError: ..." という二重表示を
  発見し修正——§25.3 参照）。

### 25.2 意図的な挙動変更

- **Script tab で構文エラー・空スクリプトを Validate/Convert した場合も
  Run の validated 状態がリセットされるようになった**: 旧実装は
  `DslEditor` が自前で `DslCompiler().compile()` して、成功したときだけ
  host のコールバックを呼んでいたため、compile 失敗や空スクリプトは
  host の `_reset_validation()` に一度も到達しなかった（Visual tab で
  既に validated 済みのシーケンスがある状態で Script tab に壊れた
  テキストを書いて Validate を押しても Run ボタンが有効なままになる、
  という構造的な欠陥があった）。新実装では `DslEditor` が生テキストを
  必ず host の `_validate_dsl_text()` に渡し、host 側が表示と
  validated 状態更新を必ず両方行うため、この非対称が解消された
  （空スクリプトは既存の `sequence_structure.check_empty_sequence` の
  "シーケンスにアクションが一つもありません" エラーにそのまま帰着する）。
- **`revalidate_for_run()` に `certificate` 引数を付けなかった**:
  §7 Phase 7 item 2 の擬似シグネチャには書かれているが、
  `ValidationCertificate` 型自体が Phase 8 で初めて作られるため、
  存在しない型のための未使用引数を持たせなかった。Phase 8 は
  `revalidate_for_run()` 1関数・`_on_run()` 1呼び出し元だけを拡張すれば
  よい。
- **Validation Results panel と Run 前のダイアログが `Diagnostic.
  source_line` を "Line N: " として表示するようになった**: DSL 由来の
  静的/preflight エラーに DSL 行番号が付くようになった（§5.2 が意図
  していた挙動）。Visual/JSON 由来（`source_map` 無し）は従来どおり
  行番号なしで表示される。

### 25.3 追加したテスト

- `tests/test_exp_scheduler_pre_validator.py`（+10 test,
  `Phase7DiagnosticCoverageRegressionTests`）: §25.1 で移設した8箇所すべてに
  ついて `code`/`severity`/`phase`（該当する場合は `device`）を確認する
  回帰テスト、および複数チェッカーが同時発火する fixture で
  `Counter(d.message for d in result.diagnostics)` と
  `Counter(result.errors + result.warnings)` が一致することを確認する
  orphan-message 回帰テスト（同一メッセージが複数 action から出ることが
  あるため、set ではなく multiset 比較にした）。
- 新規 `tests/test_exp_scheduler_validation_service.py`（8 test）:
  `validate_dsl()` の構文エラー/正常系（`PreValidator().validate()` を
  直接呼んだ場合と diagnostics/baseline_positions が一致することを含む）、
  トップレベル action とループ内 action（enclosing `for` 文の行に帰属する
  こと）の `source_line` backfill、`source_map=None` でクラッシュしない
  こと、`revalidate_for_run()` が `validate_sequence()` と一致すること。
- 新規 `tests/test_exp_scheduler_scheduler_window.py`（12 test、
  このリポジトリ初の PyQt widget テスト。`QT_QPA_PLATFORM=offscreen` で
  ヘッドレス実行）: `DslEditor` を `unittest.mock.MagicMock` の validator と
  組み合わせ、Validate/Convert が生テキストで exactly once 呼ばれること
  （構文エラー・空文字列でも呼ばれること）、Convert が `report.ok` の
  ときだけ `sequence_changed` を emit することを確認。実際の
  `ExperimentalSchedulerWindow`（`DeviceContext()` で全装置未接続、モックで
  なく実オブジェクトを構築）に対しては、構文エラー/空スクリプトで Run が
  無効化されること、一度 validated 済みの状態が後続の構文エラーで
  リセットされること、preflight 失敗時に Visual sequence/timeline が
  変化しないこと、成功時に `timeline.set_sequence()` が exactly once
  呼ばれること、warning-only な DSL で Run が有効化されること、
  Convert-to-Visual 経由で `PreValidator.validate` が exactly once
  （`wraps=` 付き mock で計測）呼ばれることを確認した。
- Phase 7 完了後の `tests/test_exp_scheduler*.py` 総数: 316 test
  （286 + 本節追加30、1 skip、他 green）。リポジトリ全体では 444 test
  （1 fail・5 error は §18〜§24 から変わらず Phase 4 と無関係な
  pre-existing `utils/stage`/`test_controller_arbiter` 関連 baseline
  問題、3 skip）。`.venv` 相当の環境（Python 3.13、PyQt6 導入済み）で
  実行して確認した。

### 25.4 手動確認

`QT_QPA_PLATFORM=offscreen` + `PM16CControllerSim` を使い、実際の
`ExperimentalSchedulerWindow` に対して本物の `QPushButton.click()` で
Validate / Convert to Visual を操作し、`QWidget.grab()` でスクリーン
ショットを取得して目視確認した。有効な DSL の Validate → 緑の
"Validation passed" 表示と Run 有効化、Convert to Visual → Visual tab へ
切り替わりシーケンスが反映、構文エラーの DSL の Validate →
赤いエラー表示と Run 無効化（validated 済み状態からのリセットを含む）、
複数行 DSL の2行目のエラーが "Line 2: ..." として正しく表示されることを
確認した（この過程で §25.1 末尾の二重 "Line N:" 表示バグを発見・修正した）。
`_check_stage_unchanged_since_validation()` は本 Phase で変更していない
ことを diff で確認した（Phase 8 の担当のまま）。

### 25.5 未対応事項

なし。

---

## 26. Phase 7 外部レビュー対応（2026-07-19）

§25 完了後（317 test green）に対して外部レビューを実施し、1件（High）の
指摘を受けた。実際に再現確認した上で対応した。

### 26.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 (High) | Convert-to-Visual で full validation が2回実行される。Script tab から Convert すると `dsl_editor._on_convert()` が host validator（`_validate_dsl_text`）を1回呼んで `sequence_changed` を emit し、`scheduler_window._on_dsl_converted()` が `self._tabs.setCurrentIndex(0)` で Visual tab へ切り替える。ここで `QTabWidget.setCurrentIndex()` は `currentChanged` を **同期的に** emit するため、`setCurrentIndex()` が返る前に `_on_tab_changed(0)` が呼ばれる。その時点で `self._last_tab_index` はまだ更新前の `1` のままであり、「Script から Visual へ離脱した」という auto-convert の条件（`index==0 and self._last_tab_index==1 and auto_convert_enabled()`）に一致してしまい、`convert_to_visual()` がもう一度呼ばれ、`PreValidator.validate()` が同じクリックで2回実行される。 | 確認済み。`window._tabs.setCurrentIndex(1)`（Script tab へ）→ `window._dsl_editor._btn_convert.click()` を `PreValidator.validate` の `wraps=` 付き mock で計測したところ `call_count == 2` を実測した。§25 の元テスト（`test_convert_to_visual_calls_pre_validator_exactly_once`）は Visual tab（既定の index 0、`_last_tab_index` が一度も `1` にならない）から `_on_convert()` を直接呼んでいたため、この再入経路を通らず検出できていなかった。 | 修正済み。`ExperimentalSchedulerWindow.__init__` に `self._switching_tabs_after_convert = False` を追加し、`_on_dsl_converted()` が `self._tabs.setCurrentIndex(0)` を呼ぶ間だけ `True` にする（`try`/`finally`）。`_on_tab_changed()` の auto-convert 分岐に `and not self._switching_tabs_after_convert` を追加し、この programmatic な tab 切替中だけ再入を抑止する。手動でタブを切り替える（Convert ボタンを押さない）既存の auto-convert 経路は `_switching_tabs_after_convert` が `False` のままなので影響を受けず、引き続き1回だけ動作する。 |


### 26.2 追加したテスト

`tests/test_exp_scheduler_scheduler_window.py`:

- `test_convert_to_visual_calls_pre_validator_exactly_once` を、Visual tab
  から直接 `_on_convert()` を呼ぶのではなく、先に Script tab
  （`window._tabs.setCurrentIndex(1)`）へ切り替えてから実ボタン
  （`_btn_convert.click()`）を押す形に修正し、`_last_tab_index == 1` の
  状態から Convert する現実の操作を再現するようにした。
- 新規 `test_leaving_script_tab_without_convert_button_still_auto_converts_once`:
  Convert ボタンを押さず手動でタブを切り替える経路が、再入防止ガードに
  よって抑止されず引き続き1回だけ `PreValidator.validate()` を呼ぶことを
  確認する回帰テスト（修正が正当な auto-convert 経路を壊していないことの
  確認）。

Phase 7 外部レビュー対応後の `tests/test_exp_scheduler*.py` 総数: 317 test
（316 + 本節で1件差し替え＋1件追加、1 skip、他 green）。

### 26.3 その他の確認事項（対応不要と判断）

- `git diff --check` が `REORGANISATION_PLAN.md` 末尾の余分な空行を1件
  検出したため、末尾の trailing blank line を削除した（内容面の変更ではない）。
- 全体テスト実行中に出る `AVCaptureDeviceTypeExternal is deprecated for
  Continuity Cameras` 警告は、`validator/checks/camera_follow.py` の
  `check_camera` が接続確認のため `cv2.VideoCapture` を開く際に macOS の
  AVFoundation フレームワークが出す OS レベルの非推奨通知であり、
  Phase 7 とは無関係の環境依存の既存挙動と判断し、対応しなかった。
- 実機での最終確認は本セッションでは実施していない（§8.4 の既存注記どおり）。

### 26.4 未対応事項

なし。1件とも対応済み。

## 27. Phase 8 実施記録（2026-07-19）

実装前に3回の外部レビューを経て設計を固めた（§7 Phase 8 記載の作業計画に対する
指摘・対応）。実装自体は計画どおりに進んだため、レビューで固まった設計判断を
そのままここに記録する。

### 27.1 実装内容

- 変更 `apps/exp_scheduler/validator/models.py`: `ValidationPhase` に
  `RUN_GATE = "run_gate"` を追加。新規 `ValidationCertificate`（frozen
  dataclass）を追加 — §5.5 が想定していた4フィールド
  （`sequence_fingerprint`/`settings_fingerprint`/`snapshot`/
  `validated_at`）に `device_identity` を加えた5フィールドとした
  （§27.2 参照、意図的な逸脱）。`ValidationReport` に `snapshot`（常に
  設定）と `certificate`（clean pass のときだけ設定）を追加。
  `.snapshots` の `ValidationSnapshot` は `TYPE_CHECKING` 経由でのみ
  import する（`snapshots.py` が `.models` を import しているため、
  実行時 import は循環になる — `from __future__ import annotations` が
  あるので型注釈は文字列化され安全）。
- 変更 `apps/exp_scheduler/validator/pre_validator.py`: `PreCheckResult`
  に `snapshot` フィールドを追加。`collect_snapshot()` の成功・内部
  例外フォールバックの両方が合流した直後に `result.snapshot = snapshot`
  を無条件で設定する（レビュー指摘1の直接対応 — baseline diff は
  fresh report が他の理由で error を含んでいても動作する必要がある）。
- 変更 `apps/exp_scheduler/validation_service.py`: `_sequence_fingerprint()`
  （`json.dumps(sequence.to_dict(), sort_keys=True)`）、`_device_identity()`
  （`ctx.controller/pace5000/lakeshore/radicon` の実オブジェクト参照
  タプル）、`_same_device_identity()`（`is` ベースの要素比較 — レビュー
  指摘（2回目）の直接対応）、`make_certificate()`、`_check_stage_baseline()`
  を追加。`validate_sequence()`/`validate_dsl()` に `global_camera` 引数を
  追加し、`report.snapshot` を常に、`report.certificate` は
  `report.ok and result.snapshot is not None` のときだけ設定するよう
  変更。`revalidate_for_run()` を書き換え、`certificate` 引数を追加した:
  fresh な `validate_sequence()` を必ず最後まで実行したうえで、
  certificate が無ければ `run_gate.not_validated`、あれば
  sequence/settings fingerprint 照合・device identity 照合・Ch1-11 stage
  baseline 照合（値の不一致は `run_gate.stage_moved_since_validate`、
  片方でも Ch1-11 が揃っていなければ `run_gate.stage_baseline_incomplete`
  — レビュー指摘（3回目）の直接対応）を行い、診断を蓄積する。戻り値は
  `dataclasses.replace(fresh_report, certificate=None)` で必ず
  certificate を落とす（レビュー指摘2の直接対応 — fresh 単体が clean
  だったことと Run gate 全体の合否は無関係なため）。
- 変更 `apps/exp_scheduler/ui/scheduler_window.py`: `self._validated_positions`
  を `self._certificate: ValidationCertificate | None` に置き換えた。
  `_check_stage_unchanged_since_validation()` を削除し、役割を
  `revalidate_for_run()` の stage baseline diff に完全移管した。
  `_set_validated(report)` は `report.certificate` を保存するよう変更
  （引数を `baseline_positions` から `report: ValidationReport` に変更）。
  `_on_validate_visual()`/`_validate_dsl_text()`/`_on_run()` は
  `global_camera = self._build_global_camera()` を組み立てて
  `validation_service` へ渡すよう変更した（Camera 設定を fingerprint に
  含めるため）。新規 `_wire_settings_invalidation(panel)` を追加し、
  `_build_ui()` 内で Limit/XRD/Camera/Follow の各パネルに適用した
  （Logging パネルは fingerprint 対象外なので除外）。`QSpinBox`/
  `QDoubleSpinBox`/`QLineEdit`/`QCheckBox`/`QComboBox`/`QButtonGroup` を
  型ごとに個別に `findChildren()` して対応する変更シグナルを
  `self._reset_validation` へ接続する（`QButtonGroup` は `QObject` で
  あり `buttonToggled` を使う必要があるため、他の型と一括にできない）。
  `_on_capture_now()`/`_on_load_ref_file()` の成功パス末尾に
  `self._reset_validation()` を追加した（`reference_path` は
  ウィジェットシグナルを持たないフィールドのため）。

### 27.2 意図的な設計判断・逸脱

- **`ValidationCertificate` を4フィールドではなく5フィールドにした**:
  `device_identity` を追加した。DeviceContext のバックエンド差し替えを
  検出するための情報が §5.5 の元設計に無かったため、Phase 7 が
  `revalidate_for_run()` のシグネチャで行った逸脱明記の前例に倣い、
  ここでも明示的に追加した。
- **DeviceContext のバックエンド差し替え検知は Run 押下時
  （`revalidate_for_run()`）でのみ行い、差し替えの瞬間に certificate を
  即座に破棄・Run を無効化する仕組みは実装しなかった**: `DeviceContext`
  の `controller`/`pace5000`/`lakeshore`/`radicon` フィールドを
  `ExperimentalSchedulerWindow` 構築後に再代入する呼び出し箇所は、
  `main.py`・`apps/exp_scheduler/` 全体を検索しても現状ゼロである
  （`main.py::open_exp_scheduler()` が一度だけ `DeviceContext(...)` を
  組み立てて渡すのみ）。即時 UI 無効化には DeviceContext 側の変更通知
  機構（signal やジェネレーション番号）が別途必要になるが、実際に
  backend を差し替える呼び出し元が存在しない現時点でそれを作る根拠が
  ないため、実装しなかった。将来 backend 再接続機能を追加する場合は、
  その変更点自身が `_reset_validation()` を呼ぶ形で対応する
  （このコードパスが増えたときに、あわせて即時無効化の要否を再検討する）。
  この decision は外部レビュー2回目で指摘され、ユーザーに確認のうえ
  「Run押下時の検出のみに縮小」を採用した。`device_identity` の比較は
  `==`/`!=` ではなく明示的に `is` を使う（`_same_device_identity()`）—
  4クラスとも現状 `__eq__` を独自定義していないことは確認済みだが、
  将来 dataclass 化等で値比較が追加された場合に「別 backend を同一と
  誤判定する」リスクを未然に防ぐため。
- **`report.snapshot` は `report.ok` に関わらず常に設定し、
  `report.certificate` だけを clean pass 条件にした**: baseline diff は
  fresh report が（無関係な理由で）error を含んでいても動作する必要が
  あるため。
- **`revalidate_for_run()` の live preflight は Run gate の判定結果に
  関わらず必ず最後まで実行する**（早期 return しない）: PreValidator
  自体の「全チェックを蓄積する」方針と一致させるため、また stale な
  certificate と genuine な fresh preflight error が同時に存在する場合、
  両方を一度に提示するため。

### 27.3 追加したテスト

- `tests/test_exp_scheduler_validation_service.py`（+13 test、
  `CertificateTests`・`RunGateTests`）: 成功時に `report.snapshot`/
  `.certificate` が両方設定されること、preflight error があっても
  `report.snapshot` は設定され `report.certificate` は `None` のままで
  あること、`revalidate_for_run(certificate=None)` →
  `run_gate.not_validated`、sequence/settings 変更 →
  `run_gate.sequence_changed`/`run_gate.settings_changed`、
  `ctx.controller` を同一状態の別インスタンスに差し替え →
  `run_gate.device_context_changed`（`__eq__` が常に `True` を返す
  ダミー backend を使い、`_same_device_identity()` が値比較ではなく
  `is` で判定していることを直接確認するテストを含む）、stage 位置変化
  → `run_gate.stage_moved_since_validate`、certificate 側・fresh 側の
  どちらかで Ch1-11 が揃わない → `run_gate.stage_baseline_incomplete`
  （`stage_moved_since_validate`误判定にならないことを明示的に確認）、
  何も変えなければ certificate があっても新規 run_gate diagnostic が
  出ないこと、`revalidate_for_run()` が返す report の `certificate` は
  常に `None` であること（clean pass でも確認）。既存の
  `RevalidateForRunTests.test_matches_validate_sequence_for_the_same_inputs`
  は Phase 7 時点で certificate 引数が存在しない前提のテストだったため
  Phase 8 の契約に合わせて更新した
  （`revalidate_for_run(..., certificate=a.certificate)` を渡す形に変更）。
- `tests/test_exp_scheduler_scheduler_window.py`（+16 test、
  `Phase8CertificateInvalidationTests`・`Phase8RunGateOrderingTests`）:
  Global Limits の spinbox・XRD oscillation speed・Follow autofocus
  speed/peak（`QButtonGroup` のラジオボタン）・Global Camera の
  `QLineEdit`・Reference 画像の Capture Now/Load from…・タイムライン
  編集のいずれも、Validate 後に certificate を破棄し Run を無効化する
  ことを確認。副作用順序テスト（`main_window`/`SequenceRunner` を
  `unittest.mock` でパッチ）で、certificate なし・sequence 変更後・
  settings 変更後・stage 移動後・`ctx.controller` 差し替え後・fresh
  preflight error・warning ダイアログで No のいずれについても
  `close_all_sub_windows()`/`SequenceRunner` が呼ばれないことを確認し、
  正常系（何も変えず Validate→Run）では逆に `unittest.mock.Mock()` に
  `attach_mock()` した共通の親 mock の `mock_calls` 順序で
  `close_all_sub_windows()` が `SequenceRunner` 構築より先に呼ばれる
  ことを確認した。実装過程で2つのテスト専用の問題を発見・修正した:
  (1) `SequenceRunner` を `unittest.mock.patch` で丸ごと差し替えると
  そのインスタンスの `isRunning()` が既定でtruthyな `MagicMock` を返し、
  テスト cleanup の `window.close()` が「シーケンス実行中」と誤認して
  本物の（headless環境では応答がなく永遠にブロックする）確認ダイアログ
  を出してしまう問題 — `runner_cls.return_value.isRunning.return_value
  = False` を明示的に設定して解消。(2) `ExperimentalSchedulerWindow` が
  Global Limits/XRD/Camera/Follow の設定値を実ファイル
  （`__localdata/scheduler_window_settings.json`）へ永続化・復元する
  既存の仕組み（Phase 8 以前から存在）が、値を変更してから
  `window.close()` するテストを複数実行すると前のテスト（や前回の
  `python -m unittest` 実行）の変更値を次のテストの初期状態として
  読み込んでしまい、「デフォルト値から変更した」つもりの `setText()`/
  `setValue()` が実際には無変化になってシグナルが発火しない、という
  テスト間汚染を引き起こしていた — 新規 `_IsolatedSettingsTestCase`
  （`setUp()` で `_SETTINGS_PATH` を都度の一時ファイルへ差し替える）を
  追加して解消した。
- Phase 8 完了後の `tests/test_exp_scheduler*.py` 総数: 345 test
  （316 + 本節追加29、1 skip、他 green）。リポジトリ全体では 473 test
  （1 fail・5 error は §18〜§26 から変わらず Phase 4 と無関係な
  pre-existing `utils/stage`/`test_controller_arbiter` 関連 baseline
  問題、3 skip）。

### 27.4 手動確認

`QT_QPA_PLATFORM=offscreen` + `PM16CControllerSim` を使い、実際の
`ExperimentalSchedulerWindow` に対してスクリプトで手順を再現した:
Validate（DSL `wait(duration=1, unit="s")`）→ `report.ok=True`・Run
有効化・certificate 生成を確認 → Global Limits の Ch3 −mm を変更 → Run
が即座に無効化され certificate が `None` になることを確認 → 再度
Validate → Run が再度有効化されることを確認 → `SequenceRunner` を
パッチした状態で Run を押下 → `close_all_sub_windows()` 相当の経路を
通り `SequenceRunner` が構築されることを確認した。

### 27.5 未対応事項

なし。

## 28. Phase 8 外部レビュー対応（2026-07-19）

§27 完了後（346 test green）に対して外部レビューを実施し、Medium 1件・Low 2件の
指摘を受けた。実際に再現確認した上で対応した。

### 28.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 (Medium) | `_on_save()`（`ui/scheduler_window.py`）が保存前に現在の Global settings を生きている `self._sequence` へ書き戻していた（`self._sequence.global_xrd = ...` 等の4フィールド）。`sequence_fingerprint` は `Sequence.to_dict()` 全体から作られる（`validation_service._sequence_fingerprint()`）ため、この4フィールドも fingerprint の対象であり、Validate → Save の操作だけで `self._sequence` の内容（ひいては fingerprint）が変わってしまい、次の Run が `run_gate.sequence_changed` で拒否される — Sequence を一切編集していないのに「シーケンスが変更された」と表示される、通常操作を壊す欠陥だった。 | 確認済み。Validate → `_on_save()` → `window._certificate` を診断したところ、certificate 自体は破棄されないが（Save は certificate 無効化配線の対象外）、certificate が保持している古い `sequence_fingerprint` と、Save 後に変化した `self._sequence` から再計算した fingerprint が不一致になることを確認した。安全側（実行の誤許可ではなく誤拒否）に倒れるため重大な安全性欠陥ではないが、UX 上の regression である。 | 修正済み。`_on_save()` は `copy.deepcopy(self._sequence)` で保存用コピーを作り、Global settings はそのコピーにだけ設定して `save()` する（`self._sequence` 自体は一切変更しない）。fingerprint 側（sequence fingerprint から global_* を除外する案）ではなく保存側の修正を選んだ — 変更が `_on_save()` 一箇所に閉じるため。 |
| 2 (Low) | `validator/models.py` の `ValidationCertificate` docstring が「`revalidate_for_run()` で一度だけ消費される（consumed exactly once）」と説明していたが、実装は certificate を消費・削除しない。warning 確認ダイアログで No を選んだ後の再 Run や、ステージを動かさない Sequence の再 Run では同じ certificate がそのまま再利用される。 | 確認済み。`_on_run()` は `revalidate_for_run()` の呼び出しでも `self._certificate` を書き換えない（`_reset_validation()` が呼ばれない限り保持され続ける）ことをコードで確認した。現在の設計として問題はなく、docstring の記述が実装と不一致だっただけ。 | 修正済み。docstring を「Run gate に参照される。`_reset_validation()`（Sequence/settings 編集、Sequence load、Run gate 拒否など）が起きるまで、複数回の Run 試行にわたって同じ certificate が使われ続ける」という記述に修正した。 |
| 3 (Low) | `ui/scheduler_window.py` の `_set_validated(report)` が、渡された `report.certificate` が `None` であっても無条件に `self._validated = True` / `self._btn_run.setEnabled(True)` としていた。現在の呼び出し経路（`_on_validate_visual()`/`_validate_dsl_text()` はどちらも `report.errors` が無いときだけ呼ぶ）では `report.certificate` は必ず設定されるため直ちに問題は起きないが、状態不変条件（Run 有効 ⟺ certificate 保有）をこのメソッド自身が保証していなかった。 | 確認済み。`validate_sequence()` の `report.certificate` は `report.ok and result.snapshot is not None` の場合のみ設定される（§27.2）。`result.snapshot` は現状の実装では常に non-None になるため、実際には `report.ok` かつ `certificate is None` という組み合わせは到達不能だが、これは複数モジュールをまたいだ保証であり、`_set_validated()` 自身が壊れた不変条件を検出できないのは局所的な堅牢性に欠ける。 | 修正済み。`_set_validated()` の先頭で `report.certificate is None` なら `self._reset_validation()` を呼んで即座に return するガードを追加した（`assert` ではなく、既存の「不明な内部エラーは fail-closed で Run 無効化」という本ファイル全体の方針に合わせた）。 |

### 28.2 追加したテスト

- `tests/test_exp_scheduler_scheduler_window.py`
  （`Phase8RunGateOrderingTests.test_save_after_validate_preserves_certificate_and_allows_run`）:
  Validate → `_on_save()`（`QFileDialog.getSaveFileName` をモックして
  実ファイルへ保存）→ `window._certificate` が Save 前と同一オブジェクトの
  ままであること・`_btn_run` が有効なままであることを確認したうえで、
  実際に Run を押して `close_all_sub_windows()`/`SequenceRunner` が
  正常に呼ばれる（`run_gate.sequence_changed` で拒否されない）ことまで
  確認する回帰テスト。

### 28.3 未対応事項

なし。

## 29. Phase 9 実施記録（2026-07-20）

実装前に4回の外部レビューを経て設計を固めた（§7 Phase 9 記載の作業計画に対する
指摘・対応 — MOVE_CONSTRAINTS pre-check の欠落、統一 Diagnostic モデルへの
準拠、二重記録の回避、oscillation thread の例外握り潰し、Global limits の
fail-open 経路の5点が対象）。実装自体は最終計画どおりに進んだため、レビューで
固まった設計をそのままここに記録する。

### 29.1 実装内容

- 変更 `apps/exp_scheduler/runner.py`:
  - 新規 `RunnerError(RuntimeError)`（`code` 属性つき）を追加。MOVE_CONSTRAINTS
    違反・Ch11 oscillation の分類済み失敗はすべてこれで送出する。
  - `_do_stage()`: `target_pos` の計算を Ch3/4/5 限定のブロックから外し、
    `move_absolute`/`move_relative` の全チャンネルで行うよう変更。取得した
    `target_pos` で `ctrl.check_move_constraints(action.ch, target_pos)` を
    移動送信前に呼び、違反時は `RunnerError("runtime.move_constraint_violation", ...)`
    を送出する。これまで通常の stage move は runtime 層で MOVE_CONSTRAINTS を
    一度も確認しておらず、controller 層内部の enforcement
    （`PM16CController.move_ch_absolute()`/`move_ch_relative()` 内の
    `_check_move_constraints_using()`）だけに依存していた
    （実際の衝突は防止されていたが、「multi-layer defense」の runtime 層が
    通常移動に関しては no-op だった）。relative move で現在位置取得に
    失敗した場合、以前は `target_pos = None` にして pre-check を静かに
    スキップしていたが、`RunnerError("runtime.position_unreadable", ...)`
    を送出し move 自体を fail-closed にブロックするよう変更した。
  - `_execute_actions()` の唯一の terminal except 節で
    `code = getattr(exc, "code", "runtime.unexpected_error")` を使って
    `Diagnostic` を組み立て `self._last_diagnostic` に記録するよう変更。
    ops.log の該当行も `[STEP #NNNN ERROR] [code] ...` 形式に変更した。
  - `_trigger_global_limit_error()` を `_abort_for_global_limit(code, message,
    *, moving)` + 薄い wrapper `_trigger_global_limit_exceeded()`/
    `_trigger_global_limit_position_unreadable()` に分割した。
    `_abort_for_global_limit()` は `self._stop_event.set()` を追加で呼ぶ
    ように変更した — 修正前は `_follow_stop_event` だけを set しており、
    follow thread（`_follow_loop()`）由来の Global limit 違反は follow
    thread 自身を止めるだけで main の `_execute_actions()` ループ
    （`_check_stop()` は `_stop_event` しか見ない）を止められていなかった
    （外部レビューで検出された High 相当の欠陥）。`request_stop()`/
    `request_emergency_stop()` が既に両方の event を set する前例に
    倣った。あわせて `error_occurred.emit()` の index を（off-by-one の
    あった）`self._flat_index` から `self._current_step_idx` に修正した。
  - `_check_global_limits()`（post-move 安全網）の位置読み取り失敗を、
    `continue` で握り潰す代わりに `_trigger_global_limit_position_unreadable(
    ch, moving=True)` を呼ぶよう変更（fail-closed 化）。
  - `run()` 起動時の Ch3/4/5 baseline 読み取り失敗
    （`self._global_limits is not None` の場合のみ）を、motion lease
    取得失敗と同じ独立した早期 abort 地点として fail-closed に変更
    （`code="controller.global_limit_baseline_unavailable"`）。以前は
    `except Exception: pass` で無視しており、読み取れなかった channel は
    実行中ずっと無防備なまま誰にも通知されなかった。
  - motion lease 取得失敗の catch サイトに
    `code="controller.motion_lease_acquire_failed"` を追加。
  - `_do_take_xrd()`/`_osc_loop()` の Ch11 oscillation 回復ロジックを
    全面的に再設計した。`_osc_loop()` は例外発生時に `progress_updated`
    を出すだけで握り潰していたため、oscillation が実際に失敗していても
    sequence 全体が正常終了しうる欠陥があった。単一スロットの
    `osc_exception` リストで thread 間の例外を受け渡すよう変更し、
    `_do_take_xrd()` 側で `osc_stop_timed_out` フラグ（30秒 join
    timeout 自体を、その後 thread が生存し続けたか・grace period 内に
    停止したかに関わらず不可逆に失敗として記録）・`capture_exc`・
    `recovery_exc` を個別に捕捉したうえで、5段階の優先順位
    （生存中 stop_failed > stop_timeout > recovery 失敗 > capture 失敗 >
    oscillation 実行失敗）で単一の例外を選んで再送出する。選ばれなかった
    側も `raise ... from ...` の `__cause__` が1つしか保持できないことを
    前提に、優先順位判定の前にすべて ops.log へ個別記録する。強制
    `normal_stop()` 自体が失敗した場合もそれを ops.log へ記録する
    （以前は `except Exception: pass` で握り潰していた）。
  - `_last_diagnostic: Diagnostic | None` を `__init__`/`run()` 冒頭の
    per-run リセット群の両方に追加した。
  - （§30 外部レビュー対応）`validate_ch11_oscillation_settings()` の
    呼び出しを `try/except (TypeError, ValueError)` で囲み、
    `RunnerError("runtime.ch11_oscillation_invalid", str(exc))` に
    変換して送出するよう変更。以前は無防備に呼んでおり、設定不正が
    分類されず generic fallback code に落ちていた。
  - （§30 外部レビュー対応）新規 `self._terminal_error_reported` フラグを
    追加し、`_abort_for_global_limit()` が報告した直後に True へ設定する
    ようにした。`_execute_actions()` の terminal handler と `run()` の
    outer handler の両方で、このフラグが立っている場合は
    `_last_diagnostic`/`error_occurred` を上書き・再送しない
    （ops.log には記録する）よう変更 — follow thread 由来の abort が
    `normal_stop()` で motion lease を revoke した副作用として、main
    thread が独立に別の例外（実機では `MotionRevokedError` 等）を
    送出しうるという競合状態を防ぐ。
  - `_abort_for_global_limit()` の `moving=True` 分岐で、強制
    `normal_stop()` 自体が失敗した場合もその例外を ops.log へ記録する
    よう変更（以前は `except Exception: pass` で握り潰していた —
    oscillation timeout 経路には既にあった対応と揃えた）。
  - `run()` outer handler（`_start_camera_session_if_needed()` 含む、
    `_execute_actions()` の外側で発生した例外の受け皿）にも
    `code = getattr(exc, "code", "runtime.unexpected_error")` を使った
    `Diagnostic` 構築・ops.log への code 記載を追加した。
  - Phase 4 由来の settings re-export（コメント + `# noqa: F401` つきの
    by-name import）を実際に削除した。`from .scheduler_settings import
    GlobalXrdSettings, GlobalLimits, ...` を `from . import
    scheduler_settings` に変更し、内部参照をすべて
    `scheduler_settings.GlobalXrdSettings` 等に置き換えた（§29.2・§30
    参照 — 初版では import 文自体は残していたが、外部レビューで
    「re-export 削除という原文の完了条件を満たしていない」と指摘され、
    module import への切り替えで対応した）。これにより
    `runner.GlobalXrdSettings` 等の属性はもう存在しない。
- 変更 `apps/exp_scheduler/validator/models.py`: `build_runtime_diagnostic()`・
  `build_controller_diagnostic()`（`emit_static`/`emit_preflight` と同様、
  phase を固定した2つの独立関数）を追加。新しい dataclass は追加していない
  — `Diagnostic`/`ValidationPhase.RUNTIME`/`CONTROLLER` は Phase 1 から
  既に存在していたが未使用のまま放置されていた。
- 変更 `apps/exp_scheduler/dsl/_registry.py`: `dsl_command()` デコレータを、
  元の関数から `inspect.signature(fn, eval_str=True)`/`fn.__doc__` を
  capture した直後に、常に `NotImplementedError` を送出する
  `(*args, **kwargs)` 受けの wrapper（`functools.wraps` で `__name__`/
  `__doc__`/`__wrapped__` を保持）に差し替えるよう変更した。
- 変更 `apps/exp_scheduler/dsl/api.py`: 24個の `@dsl_command` 関数の本体を
  `_ctx().append(...)` から「docstring + コメント1行 + `pass`」に置き換えた。
  `_local`/`_ctx()`/`api_context()`/`DSL_NAMESPACE` を削除した
  （実行不能であることがデコレータ1箇所で保証されるようになったため、
  24関数それぞれに手書きの `NotImplementedError` を書く必要がなくなった）。
- 変更 `apps/exp_scheduler/ui/scheduler_window.py` /
  `tests/test_exp_scheduler_pre_validator.py`: settings model の import 元を
  `runner` から `scheduler_settings` に変更した。
- 変更 `tests/exp_scheduler_fakes.py::FakeStageController`: runner.py が
  実際に呼ぶ `move_ch_absolute`/`move_ch_relative`/`check_move_constraints`/
  `acquire_motion`/`release_motion`/`switch_to_loc`/`wait_until_stop`/
  `set_ch_speed`/`normal_stop`/`emergency_stop`/`request_normal_stop`/
  `request_emergency_stop` を追加した（fault-injection 可能な
  `fail_on`/`constraint_violations` 付き）。motion lease は本物の
  `MotionCoordinator` ではなく、`coordinator.is_valid(lease)` だけを
  実装した最小限のスタンドインを新設した。

### 29.2 意図的な設計判断・逸脱

- **Global limits の abort は `_execute_actions()` の terminal handler を
  経由しない**: `_abort_for_global_limit()` は main run thread
  （`_do_stage`/`_check_global_limits`）と follow 背景スレッド
  （`_follow_loop`）の両方から呼ばれる自己完結した唯一の報告点として、
  意図的にこの構造を維持した。follow thread は
  `except _StopRequested: pass` で「既に `_abort_for_global_limit` 内で
  報告済みだから再報告しない」という設計であり、`_execute_actions()`
  の terminal handler へ一本化すると follow thread 発生時に報告が
  失われる（そちらは `_execute_actions` を経由しないため）。
- **follow thread 由来の Global limit 違反で報告される step index は、
  違反の発生元 action を指すとは限らない**: `_abort_for_global_limit()`
  は `self._current_step_idx` を使うが、follow thread からの呼び出しでは
  これは「その瞬間に main thread が実行している別の step」を指すことが
  あり、`start_following()`/`follow_sample_position()` を発生させた step
  とは限らない。発生元 step index を follow thread の closure へ渡す
  仕組みは実装していない（cleanup 主体の Phase 9 の範囲を超えるため）。
- **runner.py の settings 利用は re-export ではなく実利用だが、それでも
  re-export（module import への切り替えで解消済み）**:
  `SequenceRunner.__init__` は `GlobalXrdSettings()`/
  `GlobalFollowSettings()`/`GlobalCameraSettings()` をコンストラクタ
  デフォルトとして実際にインスタンス化しており、これらのクラスへの
  依存自体は runner.py に必要 — ここは正しかった。ただし当初は
  by-name import（`from .scheduler_settings import GlobalXrdSettings,
  ...`）のままだったため、`runner.GlobalXrdSettings` 等が引き続き
  runner.py の公開属性として re-export され続けており、§7 Phase 9
  原文の「settings model 一時 re-export の削除」という完了条件を
  満たしていなかった（外部レビューで指摘、§30 参照）。`from . import
  scheduler_settings` に切り替え、内部では
  `scheduler_settings.GlobalXrdSettings` 等の形で参照するよう修正した
  — クラスへの依存はそのままに、by-name の re-export だけをなくせる。
  `tests/test_exp_scheduler_scheduler_settings.py::ReExportIdentityTests`
  は `runner.GlobalXrdSettings` 等が存在しないことを確認する形に
  更新した。
- **follow thread の任意の例外握り潰しは Phase 9 に含めない**:
  `_follow_loop()`（旧 `_follow_task`）の
  `except Exception as exc: self.progress_updated.emit(f"[follow] Error: {exc}")`
  は、oscillation thread と同型の欠陥を持つ（follow 中の
  `ctrl.move_ch_relative()` が MOVE_CONSTRAINTS 違反等で失敗しても
  progress メッセージだけが出て sequence は継続する）。これは Phase 9
  が直接対応する Global limits の abort semantics（`_abort_for_global_limit`
  が `_stop_event` も set するかどうか）とは別物の、**既知の未対応の
  残課題**として記録する（§29.5）。oscillation thread のように単一の
  `_do_*` 呼び出し内で `join()` して即座に再送出する単純な形にならず
  （`start_following()`/`stop_following()` が別ステップに分かれた
  fire-and-forget モデルのため）、例外をいつ・どのステップで再送出
  するかの設計判断が新たに必要になるため、cleanup 主体の本 Phase の
  スコープを超えると判断した。
- **§7 Phase 9 の作業リストのうち、手書き `ALLOWED_FUNCTIONS`・parser の旧
  `_BUILDERS`・UI/LLM の直接 `ASTValidator`/`SequenceBuilder` 呼び出しは
  既に解消済みだった**: 実装前に確認したところ、`ALLOWED_FUNCTIONS` は
  Phase 3 の時点で既に `frozenset(get_registry().keys())`
  （`dsl/__init__.py`）として導出されており手書きではない。`_BUILDERS`
  という名前は `dsl/parser.py` に存在しない。`ASTValidator`/
  `SequenceBuilder` を直接インスタンス化しているのは `dsl/` パッケージ
  内部（`dsl/compiler.py` 等）のみで、`ui/llm_panel.py`・`llm/session.py`・
  `validator/checks/sequence_structure.py` にある同名の言及はいずれも
  コメント/docstring 内の言及であり、実際のコード呼び出しではないことを
  grep で確認した。したがって Phase 9 でこれらに対する追加の変更は不要
  だった。

### 29.3 追加したテスト

- `tests/test_exp_scheduler_dsl_legacy_cleanup.py`（新規、4 test）:
  `dsl.api` に `DSL_NAMESPACE`/`api_context`/`_ctx`/`_local` が存在しない
  ことの確認。registry の全エントリに対し、`dsl.api` の対応する属性の
  signature（`eval_str=True` で比較 — `from __future__ import annotations`
  によりそのままでは文字列注釈と実型注釈が食い違うため）・docstring が
  一致し、引数の有無に関わらず呼び出すと必ず `NotImplementedError` に
  なることを一括確認。デコレータが signature を wrapping 前の関数から
  capture していること（`(*args, **kwargs)` に潰れていないこと）を
  代表関数で直接確認する回帰テストも追加した。
- `tests/test_exp_scheduler_runner_runtime_diagnostics.py`（新規、17 test
  — うち4件は §30 外部レビュー対応で追加）:
  `SequenceRunner.run()` を（`QThread.start()` を呼ばず）直接同期的に
  実行し、fakes ベースで以下を確認する — 通常 move の MOVE_CONSTRAINTS
  pre-check（絶対値・相対値・無関係チャンネルでの誤検知なし）、Global
  limits の pre-move 違反（違反した step の index が正しいこと含む）・
  post-move 位置読み取り失敗の fail-closed 化・baseline 読み取り失敗に
  よる起動前 abort、motion lease 取得失敗、分類されない例外への generic
  fallback code、Ch11 oscillation の実行時例外・設定不正（不正な速度・
  同一パルスに丸められる endpoint の2パターン）・stop timeout（後で
  thread が生存確認から外れても無条件に失敗扱いになること含む）・
  強制 stop 失敗・θ=0° 復帰失敗のそれぞれが異なる code で報告されること。
  Global limit の abort が `_stop_event`/`_follow_stop_event` を両方 set
  することの直接確認（`_trigger_global_limit_exceeded()` を直接呼ぶ
  ユニットテスト）に加え、実スレッド2本を使い follow thread 由来の abort
  と main thread の in-flight hardware call を実際に競合させる回帰テスト
  （`error_occurred` を `Qt.ConnectionType.DirectConnection` で接続 —
  デフォルトの Auto 接続は emit 元スレッドと receiver のアフィニティが
  異なるとキュー接続になり、イベントループを回さない同期テストでは
  スロットが呼ばれないため）、および Global limit abort 自体の強制
  `normal_stop()` 失敗が ops.log に記録されプライマリ diagnostic を
  上書きしないことを確認するテストを追加した。oscillation の stop
  timeout/stop failure シナリオは実際に30秒/35秒待つ代わりに
  `threading.Thread` を一時的にモンキーパッチしたフェイク thread
  （`join()`/`is_alive()` の挙動だけを模した最小実装）に差し替えている。
- `tests/test_exp_scheduler_dsl_contract.py`/
  `tests/test_exp_scheduler_keithley_removed.py`: `DSL_NAMESPACE` への
  依存を `get_registry()`/`hasattr(dsl.api, name)` ベースの契約へ置き換えた。
- `tests/test_exp_scheduler_scheduler_settings.py::ReExportIdentityTests`:
  `runner.GlobalXrdSettings` 等が存在しないこと、`runner.scheduler_settings`
  が `scheduler_settings` モジュールそのものと同一であることを確認する
  形に更新した（§29.2・§30 参照 — 初版では逆に「同一オブジェクトとして
  存在する」ことを確認する誤った契約になっていた）。
- Phase 9 完了後の `tests/test_exp_scheduler*.py` 総数: 368 test
  （345 + 本節・§30 追加23、1 skip、他 green — 実行結果
  `python -m unittest discover -s tests -p "test_exp_scheduler*.py"` の
  `Ran 368 tests ... OK (skipped=1)` から直接確認）。リポジトリ全体では
  496 test（1 fail・5 error は §18〜§28 から変わらず本 Phase と無関係な
  pre-existing `utils/stage`/`test_controller_arbiter` 関連 baseline
  問題、3 skip）。

### 29.4 手動確認

`QT_QPA_PLATFORM=offscreen` + `PM16CControllerSim` を使い、以下を確認した:

- Ch9 が軸上にある状態で通常の `move_absolute(ch=8, ...)` を Run し、
  controller enforcement だけでなく runtime pre-check でも止まること、
  ops.log に `[runtime.move_constraint_violation]` が出ることを確認した
  （修正前は controller の `ValueError` 経由でしか止まらなかった経路）。
- Global limits 設定下で baseline 読み取りを故意に失敗させ、run 自体が
  開始前に中止されることを確認した。
- Ch11 oscillation 中に MOVE_CONSTRAINTS 違反を注入し、sequence が
  `runtime.ch11_oscillation_execution_failed` で失敗扱いになる
  （黙って成功しない）ことを確認した。
- `grep -R -n --include='*.py' 'DSL_NAMESPACE\|api_context' apps/exp_scheduler tests`
  が0件であることを確認した。

### 29.5 未対応事項

- **follow thread（`_follow_loop()`）の任意の例外握り潰し**:
  `except Exception as exc: self.progress_updated.emit(f"[follow] Error: {exc}")`
  は oscillation thread と同型の欠陥（MOVE_CONSTRAINTS 違反等の失敗が
  progress メッセージだけになり sequence が継続する）を持つが、Phase 9
  では対応していない（§29.2 参照）。別 task 化を推奨する。
- **Global limits 違反が follow thread から報告された場合の step index**:
  発生元 action の index に正確に紐付ける仕組みは実装していない（§29.2）。
- **`run()` の `finally` 節内、cleanup 呼び出し
  （`_cleanup_follow_thread()`/`_cleanup_camera_session()`）自身が例外を
  送出した場合、それは捕捉されず `run()` の外へそのまま伝播する**
  （外部レビューで指摘、§30 参照）。現状どちらの実装も
  `Event.set()`/`Thread.join()`/`VideoCapture.release()` のみで実際に
  例外を送出する可能性は低いと考えられるが、try/except による保証は
  していない。今回は修正せず、既知の未対応事項として記録する。

## 30. Phase 9 外部レビュー対応（2026-07-20）

§29 完了報告後（363 test green と報告）に対して外部レビューを実施し、High
2件・Medium 3件・Low 1件の指摘を受けた。実際に再現・確認した上ですべて
対応した。

### 30.1 検証結果と対応

| # | 指摘 | 再現確認 | 対応 |
|---|---|---|---|
| 1 (High) | `_abort_for_global_limit()` が follow thread から呼ばれた際、`moving=True` なら `ctrl.normal_stop()` で motion lease を revoke する。この瞬間に main thread が stage API 呼び出し中（実機では `MotionRevokedError` 等）だと、その例外は無条件に `_execute_actions()` の terminal handler へ入り、2回目の `error_occurred` を emit し、既に設定済みの Global limit Diagnostic を `runtime.unexpected_error` で上書きしてしまう。指摘時点のテストは `_trigger_global_limit_exceeded()` を直接呼んで `_check_stop()` を確認するだけで、この main thread 側の競合を再現していなかった。 | 確認済み。実スレッド2本（follow thread 役・main thread 役）を使い、main thread が `ctrl.move_ch_absolute()` 内でブロックしている間に follow thread 役が `_trigger_global_limit_exceeded()` を呼んで motion lease 側の副作用相当（ブロック中の呼び出しが例外を送出）を発生させたところ、修正前のコードでは `error_occurred` が2回 emit され `_last_diagnostic.code` が `runtime.unexpected_error` に上書きされることを確認した。 | 修正済み。新規 `self._terminal_error_reported` フラグを追加し、`_abort_for_global_limit()` が報告を完了した時点で True に設定する。`_execute_actions()`/`run()` 双方の terminal except 節はこのフラグを最初に確認し、立っていれば `_last_diagnostic`/`error_occurred` を変更せず、ops.log にのみ「Exception after external abort (not re-reported)」を記録して `_StopRequested` に変換する。回帰テスト
`tests/test_exp_scheduler_runner_runtime_diagnostics.py::GlobalLimitDiagnosticTests::test_follow_thread_abort_racing_a_main_thread_hardware_call_is_not_double_reported` を追加した（`error_occurred` は `Qt.ConnectionType.DirectConnection` で接続 — デフォルトの Auto 接続は emit 元スレッドと receiver のアフィニティが異なるとキュー接続になり、テストがイベントループを回さないためスロットが同期的に呼ばれず検証できないことが判明したため）。 |
| 2 (High) | `_do_take_xrd()` が `validate_ch11_oscillation_settings()` を無防備に呼んでおり、不正な position/dwell/speed で送出される `ValueError` が `RunnerError` でラップされていなかった。実施記録は「Ch11 oscillation の分類済み失敗はすべて `RunnerError`」と説明していたが、この呼び出しだけ対象外だった。 | 確認済み。`osc_speed="NOT_A_SPEED"` を指定した `TakeXrdAction` を実行し、`runner._last_diagnostic.code` が `runtime.unexpected_error`（generic fallback）になることを修正前のコードで確認した。 | 修正済み。`validate_ch11_oscillation_settings()` の呼び出しを `try/except (TypeError, ValueError)` で囲み、`RunnerError("runtime.ch11_oscillation_invalid", str(exc))` に変換して送出するよう変更した。回帰テスト2件（不正な速度・同一パルスに丸められる endpoint）を追加し、いずれも hardware 呼び出しが一切行われないことも確認した。 |
| 3 (Medium) | oscillation timeout 経路では強制 `normal_stop()` 失敗を ops.log へ記録するようになっていたが、`_abort_for_global_limit()` 自身の `moving=True` 分岐（post-move safety net・follow thread 由来）では、同じ `ctrl.normal_stop()` 失敗が `except Exception: pass` で無記録のままだった。 | 確認済み。`ctrl.fail_on = {"normal_stop"}` の状態で `_trigger_global_limit_exceeded(..., moving=True)` を呼び、修正前は ops.log に何も残らないことを確認した。 | 修正済み。例外を捕捉して `f"[STAGE] normal_stop() failed during global-limit abort: {exc}"` を ops.log に記録するよう変更した（プライマリ Diagnostic は Global limit のまま変更しない）。回帰テスト `test_global_limit_abort_logs_normal_stop_failure_without_overriding_the_code` を追加した。 |
| 4 (Medium) | `run()` の outer except 節（`_start_camera_session_if_needed()` 等、`_execute_actions()` の外側で発生した例外の受け皿）には `_last_diagnostic` も code 付き ops.log 行も存在しなかった。Phase 9 の「Runner の実行時エラーを Diagnostic code へ対応付ける」という目的に照らすと未完だった。また `finally` 節内の cleanup 呼び出し自身の例外は元から捕捉されていない。 | 確認済み。該当箇所のコードを再読し、`_last_diagnostic` が設定されないパスが実在することを確認した。 | 修正済み。outer except 節にも `code = getattr(exc, "code", "runtime.unexpected_error")` を使った `Diagnostic` 構築・ops.log への `[code]` 記載を追加した（`_terminal_error_reported` のガードも同様に適用）。`finally` 節内の cleanup 呼び出し自身の例外は今回も修正せず、§29.5 に既知の未対応事項として明記した。 |
| 5 (Medium) | `runner.py` は settings クラスを by-name import しており、`runner.GlobalXrdSettings` 等の属性が引き続き公開されていた。今回は古いコメント（re-export の説明・`# noqa: F401`）だけを削除し、既存テストを「re-export を維持する契約」に変更していたが、§7 Phase 9 原文の作業項目は「settings model 一時 re-export の削除」であり、この対応では満たされていなかった。 | 確認済み。`hasattr(runner, "GlobalXrdSettings")` が True のままであることを確認した。 | 修正済み。`from .scheduler_settings import GlobalXrdSettings, GlobalLimits, GlobalFollowSettings, GlobalCameraSettings` を `from . import scheduler_settings` に変更し、`__init__` のデフォルト値・型注釈・モジュール関数 `_global_limits_to_dict()` の型注釈をすべて `scheduler_settings.GlobalXrdSettings` 等の形に書き換えた。`SequenceRunner.__init__` がこれらのクラスを実際にインスタンス化する必要があるという事実（§29.2）自体は変わらないため import 文自体は残るが、by-name の re-export は解消され `hasattr(runner, "GlobalXrdSettings")` は False になる。`tests/test_exp_scheduler_scheduler_settings.py::ReExportIdentityTests` を非存在確認へ書き換えた。 |
| 6 (Low) | §29.3 に記載した追加テスト数が実ファイルと不一致だった（`test_exp_scheduler_dsl_legacy_cleanup.py` を「8 test」、追加合計を「18」と記載していたが、実際は前者が4件、この2ファイルの合計は17件）。 | 確認済み。`grep -c "    def test_" <file>` で実カウントし、記載が誤りであることを確認した。 | 修正済み。§29.3 の該当記述を実カウント（`dsl_legacy_cleanup.py` 4 test、`runner_runtime_diagnostics.py` 17 test、うち4件は本節で追加）に修正し、Phase 9 完了後の総数も実行結果（`Ran 368 tests ... OK (skipped=1)`）から直接転記する形に改めた。 |

### 30.2 追加したテスト

上表の対応列に記載した4件（`test_follow_thread_abort_racing_a_main_thread_hardware_call_is_not_double_reported`・
`test_invalid_oscillation_speed_is_classified_not_generic`・
`test_oscillation_endpoints_resolving_to_the_same_pulse_is_classified`・
`test_global_limit_abort_logs_normal_stop_failure_without_overriding_the_code`）
はすべて `tests/test_exp_scheduler_runner_runtime_diagnostics.py` に追加した
（§29.3 の総数に反映済み）。並行動作テストは同一プロセス内で
`threading.Event` 2個により厳密に手順を同期させており、実時間待機や
sleep ベースのタイミング調整には依存しない（`for i in 1..5` の反復実行で
安定することを確認済み）。

### 30.3 再検証結果

- `python -m unittest discover -s tests -p "test_exp_scheduler*.py"`:
  368 test, `OK (skipped=1)`。
- `python -m unittest discover -s tests -p "test_*.py"`（リポジトリ全体）:
  496 test, `FAILED (failures=1, errors=5, skipped=3)` — §18〜§29 から
  変わらない pre-existing `utils/stage`/`test_controller_arbiter` 関連の
  baseline で、本 Phase と無関係。
- `python -m pyflakes apps/exp_scheduler/runner.py
  apps/exp_scheduler/dsl/api.py apps/exp_scheduler/dsl/_registry.py
  apps/exp_scheduler/ui/scheduler_window.py
  apps/exp_scheduler/validator/models.py tests/exp_scheduler_fakes.py
  tests/test_exp_scheduler_dsl_legacy_cleanup.py
  tests/test_exp_scheduler_runner_runtime_diagnostics.py
  tests/test_exp_scheduler_scheduler_settings.py`: 新規の unused-import
  等の指摘なし（`serial` の unused-import 警告のみで、他の Phase の
  同パターンのテストファイルすべてに共通する pre-existing のもの）。

今回のレビューはこのプロジェクトの `.venv`（PyQt6 インストール済み）で
実行して再確認した。レビュー環境（別 Python、PyQt6 未インストール）では
`tests.test_exp_scheduler_runner_runtime_diagnostics` の収集自体が
`ModuleNotFoundError` になっていたとの報告を受けており、この差異が
「363 test green」の初回報告と独立に再現できなかった直接の原因と考えられる。

### 30.4 未対応事項

§29.5 に記載の3項目（follow thread の任意の例外握り潰し、follow thread
由来の step index の不正確さ、`run()` の `finally` 節内 cleanup 例外）は
本レビュー対応後も未対応のまま残る。

## 31. Phase 9 後の独立レビューと対応（2026-07-20）

§30 までの外部レビューとは別に、ユーザーの依頼で独立した多角度レビュー
（runner.py+move_constraints・dsl パイプライン・validator 群それぞれの
correctness scan、removed-behavior 監査、cross-file signature tracer、
reuse/simplification/efficiency、altitude/CLAUDE.md conventions の計7回の
finder 実行 → 候補ごとに個別に再現・検証）を実施した。検出された6件は
すべて対応済みである。

### 31.1 §29.5/§30.4 の記載が実装より古くなっていた点（重要）

このレビューで最初に確認したのは、§29.5/§30.4 が「未対応」として挙げる
3項目のうち2つが、レビュー着手時点の作業ツリーで**既に修正済みだった**
ことである。

- `_follow_loop()` の任意の例外握り潰し（§29.5 1項目目）:
  実際のコードでは既に `except Exception as exc: ... self._abort_follow_thread(
  "runtime.follow_thread_failed", ...)` に変わっており（旧
  `self.progress_updated.emit(f"[follow] Error: {exc}")` のみの版ではない）、
  該当箇所のコメントには「round-2 external review finding」という記載が
  ある。
- `run()` の `finally` 節内 cleanup 例外（§29.5 3項目目）:
  実際のコードでは既に `_safe_cleanup()` で各 cleanup 呼び出し
  （follow thread cleanup / camera session cleanup / motion lease
  release）を個別に隔離し、失敗を `self._cleanup_failures` に記録した上で
  `run()` 終了時に `_had_error` へ反映するようになっていた。

しかし、この2件を実装した際の変更記録は本文書のどこにも存在しない
（`grep -rn "_abort_follow_thread\|round-2\|_safe_cleanup" apps/exp_scheduler/
REORGANISATION_PLAN.md` で §29 以前の記載を確認したが該当なし）。コード側の
コメントが引用する「round-2 external review」の実施記録そのものが本文書に
欠落しており、§29.5/§30.4 は実装より古い状態のまま放置されていた。

「仕様定義の多重化を解消する」ことを目的とするこの再編において、計画文書
自身が実装からドリフトしていたことになる。§29.5/§30.4 の本文はそのまま
歴史的記録として残すが、実際に未対応のまま残るのは次の1項目のみである
（真の残課題は §31.4 参照）。

- follow thread 由来の Global limit 違反で報告される step index の不正確さ
  （`runner.py` の `_abort_for_global_limit()` が `self._current_step_idx`
  を使うが、follow thread からの呼び出しでは発生元 step を指すとは
  限らない）。

### 31.2 検出内容と対応

| # | 検出内容 | 対応 |
|---|---|---|
| 1 | `runner.py::_do_take_xrd()` の `except Exception as exc: recovery_exc = exc`（`_return_ch11_to_zero()` 呼び出しを囲む節）が `_StopRequested` も捕捉していた。オシレーション撮影後の θ=0° 復帰中にユーザーが Stop を押すと、`_check_stop()` が送出する `_StopRequested` がここで握り潰され、`RunnerError("runtime.ch11_return_to_zero_failed", ...)` というハードウェア故障風のエラーとして再送出されていた。 | `except _StopRequested: raise` を `except Exception` より前に追加し、Stop はそのまま `_StopRequested` として伝播させ、通常のクリーンな停止（`sequence_stopped`）として扱われるようにした。回帰テスト `tests/test_exp_scheduler_runner_runtime_diagnostics.py::OscillationThreadDiagnosticTests::test_stop_during_return_to_zero_is_a_clean_stop_not_an_error` を追加。 |
| 2 | メインスレッドと `_follow_loop()`（背景の追従スレッド）が単一の `self._motion_lease` を共有している。オシレーション撮影後の `_return_ch11_to_zero()` や DSL の `normal_stop()`/`emergency_stop()` ステップが自己トリガーで lease を revoke → 再取得する際、ちょうど follow thread がその lease で移動中だと `MotionRevokedError` が発生する。§30 で追加された `_abort_follow_thread()` 呼び出し（§31.1 参照）は、この良性の自己誘発 revocation もシーケンス全体の中断として扱ってしまっていた。`start_following()` と oscillate=True の `take_xrd()` の組み合わせを警告・禁止する validation も存在しない。 | `_follow_loop()` の1周期分の本体を `try/except MotionRevokedError` で囲み、`self._stop_event`/`self._follow_stop_event` のいずれも set されていなければ「良性の自己誘発 revocation」とみなして次周期へ `continue`（中断しない）。いずれかが set 済みなら既に別経路で中断処理済みとして黙って `return` する。回帰テスト2件（`FollowThreadMotionRevokedTests`）を追加。 |
| 3 | `dsl/parser.py::_eval_fstring()` は任意のコマンドの任意の str 引数で `{loopvar}` プレースホルダーを許可していたが、`runner.py` が実行時に `{var}` を解決するのは `LogAction.message` のみ（`_execute_one()` の `.format(**var_context)`）。`take_xrd(prefix=f"scan_{p}")` はコンパイルを通り、実行すると全フレームのファイル名に文字通り `"scan_{p}"` が入る（値が展開されない）。 | f-string に実際のプレースホルダー（`ast.FormattedValue`）が含まれる場合、バインドされた bare name 参照と同様に `loop_var_keywords` へ加え、既存の `allowed_loop_var_args`（`ArgumentRule.loop_var_allowed`）ゲートを通すようにした（`dsl/parser.py`）。`log_message` の `message` 引数に明示的に `loop_var_allowed=True` を付与（`dsl/api.py`）。回帰テスト2件を `FStringValidationTests` に追加。プレースホルダーを含まない f-string（例: `f"scan_fixed"`）は従来通りただのリテラルとして許可される。 |
| 4 | `dsl/normalizer.py::_eval_int_args()` は `ast.Constant` のみを整数リテラルとして受理していたが、`-5` のような負のリテラルは `ast.UnaryOp(USub(), Constant(5))` としてパースされるため、`range(-5, 5)` のような `SPEC.md` 記載の通常の DSL 構文がコンパイルエラーになっていた。 | `_literal_number()` を新設し、単項 `+`/`-` を再帰的に unwrap してから `ast.Constant` 判定するようにした。`range()` の start/stop/step いずれの位置でも負値を受理する。回帰テスト2件を `NormalizerRangeSafetyTests` に追加。 |
| 5 | （本節 §31.1 として記載） | （本節 §31.1 として記載） |
| 6 | `validator/checks/sequence_structure.py`（5関数）、`action_params.py`（`check_stage_schema`/`check_lakeshore_params`）、`pace5000.py`（`check_pace5000_adjacency`）が、それぞれ独立した `_walk`/`_scan` ローカル関数で同じ「raw ForLoopAction 木を再帰する」形を再実装していた — Phase 5 の `execution_trace.py` がまさにこの重複を解消するために作られたにもかかわらず。`check_pace5000_adjacency` は自前の再帰に深さ保護がないという理由だけで `_run_structural`（depth_safe）ゲートを必要としていた。 | `execution_trace.py` に共有ジェネレータ `walk_raw(actions, path_prefix="", _loop_values=...)` を新設。`(action, path, siblings, index, loop_values)` を yield し、ForLoopAction ノード自身とその子孫の両方を訪問する。`loop_values` は祖先の ForLoopAction すべての `{var: values}` を蓄積する。7関数すべてをこの共有ジェネレータを使う形に書き換えた。`check_duplicate_consecutive_actions` は `siblings[i-1]` で直前要素を得られるため、手動の `prev` 変数追跡が不要になった。既存の `depth_safe` ゲートはそのまま維持（`walk_raw` も `_collect_flat` のような非再帰ではなく、他の構造チェックと同じ有界深度前提の平叙再帰のため）。 |

### 31.3 意図的に採用しなかった代替案

`check_pace5000_adjacency` を `trace.flat`（非再帰・無制限深度）へ単純に
置き換える案も検討したが採用しなかった。`trace.flat` はループ境界をまたいで
1本のリストへ平坦化するため、「同じブロック内の直後のアクション」という
このチェックの意味論（ループ本体の最後のアクションには "次" が無い）を
壊してしまう。`walk_raw` による統一の方が、深さゲートも含め既存の意味論を
保ったまま重複だけを解消できる。

### 31.4 現時点で残る既知の未対応事項

- follow thread 由来の Global limit 違反で報告される step index の不正確さ
  （§29.2/§31.1 参照、対応していない）。
- `run()` の `finally` 節内 cleanup 呼び出し自身が例外を送出した場合の扱い
  は §29.1 の記載通り修正済み（§31.1 参照）。
- `_follow_loop()` の一般例外処理（`MotionRevokedError` 以外の Exception）は
  引き続き `_abort_follow_thread()` でシーケンス全体を中断する。これは
  意図した挙動である（追従スレッドで発生した分類不能な失敗は安全側に倒して
  停止する）。

### 31.5 再検証結果

- `python -m unittest discover -s tests -p "test_exp_scheduler*.py"`:
  修正前 392 test → 本節で追加した7 test（回帰テスト、§31.2 各行に記載）を
  加えて399 test、いずれも `OK (skipped=1)`。§30 記載の368という数値は
  §30 完了時点のものであり、その後の別セッションでの作業により本レビュー
  着手時点で既に392まで増えていた（§31.1 で触れた記録漏れと同種の
  ドキュメント遅れであり、本節の数値は実測値のみを記載する）。
- `python -m pyflakes` を変更した全ファイルに対して実行し、新規の
  unused-import 等の指摘がないことを確認した
  （`tests/test_exp_scheduler_dsl_phase2_review_fixes.py` の `serial`
  unused-import と `SequenceBuildError` unused-import は本レビュー以前から
  存在する pre-existing のもの）。
