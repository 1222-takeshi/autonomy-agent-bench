# CI運用

public repoのCIはGitHub-hosted `ubuntu-24.04`で実行する。
検査内容は従来と同じで、モデルやROSは起動しない。

## Runner

- mainへのpush、PR、`workflow_dispatch`で検査する。jobは10分で打ち切る。
- `contents: read`、checkoutのSHA固定、credential非保持を継続する。秘密情報を必要とするstepはない。
- forkからのPRもGitHub-hosted上で実行する。`pull_request_target`は使わない。
- private時の専用self-hosted runnerは公開前に登録解除・停止する。public repoへ再登録しない。

失敗時はActionsのjobが実際に起動したかと各stepを確認し、基盤の起動失敗とテスト失敗を区別する。
過去のprivate repo用GitHub-hosted runは課金／利用上限で起動しなかった。これはテストの失敗ではなく、Billing設定も変更していない。
public repoの標準GitHub-hosted runnerの実行時間は無料。追加サービス・storage等の条件は公式料金を参照する。

## 公開データ

commit履歴・Issue・PR・Actionsログ等も公開対象として扱う。
秘密情報・第三者の非公開データ・参照解・隠し採点データを含めない。問題用に公開できる資料と採点用データの公開範囲を分ける。

公開前は全runのログ（未起動runはannotation）、artifact/cache、Git履歴・PR head、Issue/PR本文・コメント・review・差分を確認する。
Git履歴のauthor/committerメールも確認する。秘密情報スキャナだけで個人情報の公開可否を判断しない。
新たな秘密情報が見つかった場合は、資格情報の失効と公開範囲の対処を優先する。検出用のraw出力自体をcommitしない。

- [GitHub公式: Actions料金](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub公式: self-hosted runnerのアクセス管理](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access)
- [GitHub公式: repository visibilityの変更](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)
