# CI運用

GitHub-hosted Actionsの課金／利用上限による起動停止に対応し、当面はprivate repo専用self-hosted runnerを使う。
これはBilling設定の復旧ではない。検査内容は従来と同じで、モデルやROSは起動しない。

## Runner

- repo単位で登録し、`self-hosted` / `Linux` / `ARM64` / `autonomy-agent-bench`の全labelで選択する。
- Ubuntu系container内の非root userで実行する。CPU 1、memory 2 GiB、pids 256を上限とする。
- host mount・Docker socket・GPU・追加capabilityを与えず、`no-new-privileges`を有効にする。
- containerにはPython 3.10以上、git、Node 24対応の公式Actions runnerを用意する。
- 登録tokenは短期tokenをstdin経由で一度だけ渡す。PAT・SSH鍵・hostのhomeをcontainerへ渡さない。
- runner資格情報はcontainer内に保持する。継続利用するため、信頼された同repoのコードだけを実行する。
- containerは自動再起動を有効にする。削除・再作成時は古いrunner登録を解除し、資格情報をimageへ含めない。

workflowはprivateと同repo PRを条件にし、jobを10分で打ち切る。
`contents: read`、checkoutのSHA固定、credential非保持は継続する。`workflow_dispatch`で手動検証も可能。
失敗時はActionsのjobが実際に起動したかと各stepを確認し、runner待機とテスト失敗を区別する。

## Public化

public化は可能。現在privateなのは初期の運用方針であり、GitHubの技術的な制約ではない。
公開するとcommit履歴・Issue・PR・Actionsログ等も公開対象になるため、秘密情報と第三者データの有無、コードのライセンスを確認する。

公開前にrepoのself-hosted runner登録を解除し、containerを停止する。その後workflowをGitHub-hostedへ切り替える。
workflowのif条件は誤操作を防ぐもので、公開repoの不信PRに対する隔離境界ではない。PRからworkflow自体を変更できるため、条件だけで安全とは扱わない。
問題用に公開できる資料と参照解・隠し採点データの公開範囲も分ける。

- [GitHub公式: self-hosted runnerの利用](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow)
- [GitHub公式: self-hosted runnerのアクセス管理](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access)
- [GitHub公式: repository visibilityの変更](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)
