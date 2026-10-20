# autonomy-agent-bench

Autoware等の自動運転ソフトウェアを調査・修正・拡張するLLM agentの評価基盤。
単発の競技問題に加えて、コード・設定・ログ・通信をまたぐ開発作業を評価する。

**ready 4件。モデル比較は未公表。**
- ALG-001・ALG-002・INT-001 は、固定 snapshot に意図的に入れた障害で、上流に実在するバグではない。
- FEAT-001 は、上流にある機能を snapshot から外したもの。
- 4件とも autoware_core 1.10.0（Apache-2.0）を使う。

| 系統 | ケース | 状態 |
|---|---|---|
| アルゴリズムのバグ解析 | [ALG-001](https://github.com/1222-takeshi/autonomy-agent-bench/issues/2): 姿勢補間のヨー角±180°境界 | ready（注入障害） |
| アルゴリズムのバグ解析 | ALG-002: Uターンの復路で横偏差がずれる | ready（注入障害） |
| システム統合のバグ解析 | [INT-001](https://github.com/1222-takeshi/autonomy-agent-bench/issues/3): topicは見えるが届かないROS 2通信（実DDS通信で採点） | ready（注入障害） |
| アルゴリズムの機能実装 | [FEAT-001](https://github.com/1222-takeshi/autonomy-agent-bench/issues/4): 停止点を保持する軌跡再サンプリング | ready（上流の機能を外したもの） |

統合系はQoS・DDS・namespace/remapping・TF・stamp/clockを対象に含める。
原因の説明、機能の合否、回帰、作業時間、tokens、ツール操作を別々に記録する。
旧LiveCodeBenchの点数とは別の評価軸とし、初期の少数例で総合順位を出さない。

## 使い方

Python 3.10以上、標準ライブラリのみ。現時点のコマンドは静的な台帳検査であり、モデル・ROS・ケースを実行しない。

```sh
python3 -m bench.catalog cases/catalog.json
python3 -m unittest discover -s tests -v
python3 -m bench.catalog cases/catalog.json --require-ready
```

最後のコマンドは、全ケースが ready なので通る。
`--require-ready`が通っても実行・証拠の真正性を検証したことにはならず、管理者の再現確認が別途必要。

## 構成と次の段階

- [cases/catalog.json](cases/catalog.json): 候補と再現・昇格条件。revision等の未決値はnull。
- [docs/protocol.md](docs/protocol.md): 入出力、採点、状態遷移、データ境界。
- [docs/verify.md](docs/verify.md): 隔離containerで1パッケージを構築・テストする採点controller。
- [docs/runner.md](docs/runner.md): readyケースを1回agentに解かせ、編集範囲・予算・改変を確かめて採点し、実行記録を残すrunner。
- [docs/sandbox.md](docs/sandbox.md): 実モデルのagentを、外へはモデルのendpointにしか出られないcontainerで動かすlauncherと中継。
- `bench/catalog.py` / `tests/`: metadata検査とnormal/edge/invalid/regressionのテスト。
- GitHub-hosted CI: テスト・catalog・Python構文・差分空白を検査。ROSやモデルは起動しない。[CI運用](docs/ci.md)。

各ケースは、固定source・環境・隔離実行・参照修正・誤修正・回帰を用意してからreadyへ進めた。
候補bundle・採点用テスト・参照修正・検証記録はrepo外に保管し、catalogにはSHA256だけを記録する。
採点imageは、ALG-001が `cases/env/ros-humble-cpp/`、ほかのケースが `cases/env/<ケースID小文字>/` のDockerfileから作る。どのimageにも、編集対象のパッケージは入れない。build は apt の版を固定しないため bit 単位では再現しない。catalog が固定する image ID の tar を正本とする。
実モデルはsandboxのcontainerで動かす。比較の前に、隔離の検証（逃げる子プロセス・時間切れ・改ざん）と比較対象モデルのrun設定を固定する。

## データと出典

このrepoはpublic。第三者source・rosbag・地図・大きな生成物・raw結果・参照解はcommitしない。
取得元、revision、ライセンス、artifactのSHA256を記録し、候補側には問題に必要なsnapshotだけを渡す。
registryへ公開しないimageは、`runtime.image`の`@sha256:`にローカルのimage IDを記録する。保管したtarを`docker load`して使い、tarのSHA256は検証記録に残す。
実車・実ネットワークの設定変更や走行試験を評価に含めない。コードと文書は[Apache-2.0](LICENSE)。bundleに含める上流のsourceは各repositoryのライセンス（catalogの`source.license`）に従う。第三者データを追加する場合は出典と利用条件を確認する。

- [Autoware公式インストール手順](https://docs.autoware.org/main/installation/autoware/source-installation/): 対象revisionに合うROS環境を選び、ケースごとに固定する。
- [ROS 2公式QoS仕様](https://github.com/ros2/ros2_documentation/blob/humble/source/Concepts/Intermediate/About-Quality-of-Service-Settings.rst): 提供/要求の互換性と遅延参加の契約を参照する。
