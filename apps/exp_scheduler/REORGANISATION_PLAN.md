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
