# Autonomy engineering evaluation contract v1

## Capability / Product intent

開発者が、Autoware等の実装バグ・統合バグ・機能追加をLLM agentへ任せられる範囲を測る。
コードを生成できたかに加え、観測から原因を絞り、既存契約を保って変更し、再現可能な検証を残せたかを評価する。
初期成果物は3系統のdraft台帳と静的validator。実行済みのベンチマークではない。

## Actors / Surfaces

管理者はsource・課題・環境・予算・採点器を事前に固定する。agentは許可したworkspaceを読み書きし、同じツールを使う。
採点controllerはcandidate環境の外に置く。解析内容はモデル名を伏せたreviewで評価し、自己申告やキーワード一致を点数にしない。
catalog CLIはmetadataだけを検査する。artifactの取得、hashの実ファイル照合、build、ROS通信、モデル推論は行わない。

## States / Constraints

`draft -> ready`は管理者の再現確認とreview後に行う。タスク内容・入力・採点が変わる場合は別version/IDとし、過去の結果を書き換えない。
draftは選定中。readyはsource commit・ライセンス・image digest・ROS distro・RMW・platform・通信隔離profile・予算・編集範囲が固定されたもの。
readyにはbundleとverification記録のSHA256、未修正fail・参照修正pass・誤修正fail・回帰passを3回以上再現した証拠を要求する。
validatorはその宣言の形式と整合だけを確認する。記録の存在・真正性・再現実行を保証する機能はまだない。
schema v1は全階層で未定義fieldを拒否する。fieldの追加はschemaとvalidatorを明示的に更新する。
`historical`は上流履歴の不具合、`injected`は注入障害、`feature`は追加/マスクした機能。`candidate`のままreadyにはできない。

## Interface contract

入力: 固定source snapshot、課題文、必要な設定/launch、許可されたログ・topic/TF/clock観測、既存テスト、編集許可範囲。
出力: patch、原因/実装判断と証拠への参照、再現コマンドと期待観測、追加テスト、実行記録。
agentは採点用データ・参照解・修正済みcommit/PR・他モデルの解答を受け取らない。
候補bundleに含める情報はallowlistで組み立てる。repo全体や親ディレクトリをそのまま渡さない。

実行runnerではモデルID・量子化・推論基盤・思考・context/出力上限・時間・tool回数・ツール版・hardware・seed・cache条件をrun manifestへ残す。
同じケースの比較では入力、ツール、予算、環境、採点を揃える。API制限やcontext不足も記録する。
再実行は新しいrun IDにし、理由と全attemptを保持する。途中runを完了扱いにしない。

## Scoring / Failure and recovery

| 軸 | 判定方法 |
|---|---|
| 原因解析 | 0:誤り/根拠なし、1:症状のみ、2:因果は妥当だが証拠不足、3:根拠・切分け実験・予測が一致。盲検reviewと根拠を保存 |
| 修正・機能実装 | 外部controllerの受入テストと回帰テスト。実行できるpatchと編集範囲の遵守を要求 |
| コスト | 所要時間、入力/出力tokens、tool呼出し、失敗/再試行回数を別記録 |

総合点へ勝手に加重合算しない。各trackの成功数/選択したreadyケース数と、解析・回帰・コストを並記する。
`pass`は全必須機能/回帰を通過。`task_fail`・`invalid_submission`・`capability_unsupported`は完遂率の0件として分母に残す。
`infra_error`は環境/採点器の故障。runをincompleteとして扱い、除外して高いスコアを報告しない。
モデルtimeoutは固定予算内ならtask_fail、controller故障や環境準備失敗はinfra_errorとし、観測根拠を記録する。

## Data / Trust boundaries

metadataと小さな集計をgitで管理し、第三者source・bag・地図・raw・候補workspace・参照解は外部cacheに分離する。
verification記録には入力/採点器hash、実コマンド、環境、各controlの結果、反復、時刻を含める。
runnerはhashを実データと照合し、candidateから参照解・grader・採点出力を読取/改変できない境界を実装してから使用する。
隔離containerに資源上限を設け、Docker socket・秘密情報・host network・実車interfaceを与えない。
通信ケースは閉じた環境で実通信を行う。文字列のQoS設定比較だけで合格にしない。
公開履歴由来の課題には学習データ混入の可能性がある。sourceと公開時期を記録し、モデルの未見性を断定しない。

## Non-goals / Open questions / Handoff

この初期版はagent runner、ROS/Autoware実行環境、case bundle、実採点器、モデル比較表を含まない。実車の妥当性認定にも使わない。
未決: 対象Autoware revision、採用する既知不具合、初期ROS distro/RMW、総tokens/時間/tool予算、最初の比較モデル、artifact保管先。
個別ケースは[#2](https://github.com/1222-takeshi/autonomy-agent-bench/issues/2)・[#3](https://github.com/1222-takeshi/autonomy-agent-bench/issues/3)・[#4](https://github.com/1222-takeshi/autonomy-agent-bench/issues/4)で具体化する。
次の実装はケース選定と参照/誤修正controlの再現から開始し、その証拠を使ってrunnerの採点境界を検証する。
