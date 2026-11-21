# CLAUDE.md

TP-Link Deco BE85 のローカル Web API (luci) を FastAPI でラップした REST サーバー。
endpoint 一覧・ログインフロー・実機で確認した luci form の挙動は README.md に集約している。

## セットアップと検証

- `mise install`: ツールのインストール、`uv sync --locked`、git hooks (pre-commit / pre-push / commit-msg) のセットアップ。旧 pre-commit (`uv run pre-commit install`) の hook が残る clone では `mise exec -- prek install --force` で置き換える。
- `mise run test`: coverage 付き pytest (xdist で並列)。オフラインで完結し、実機には触れない。
- `mise run format`: リポジトリ全体の import の並べ替えと ruff format。commit 時の `ruff-isort` / `ruff-format` hook は commit するファイルだけを整形する。
- `mise run lint`: ruff check / ruff format --check / lint-imports / ty check / pyright。pre-commit の `lint` hook も同じ task を呼ぶ。
- `mise run pre-commit`: pre-commit stage の hook (dprint、actionlint、zizmor、hadolint、typos、ruff の整形 / lint、gitleaks 等) を全追跡ファイルに実行する。CI の `prek` job の `Run prek` step と同じ検証 (commit-msg stage の commitlint は含まない)。`test` hook は pre-push で `mise run test` を実行するが、push 前に自分でも `mise run test` を流す。
- commit 時の hook (dprint・ruff の整形・README のタスク一覧の同期) がファイルを書き換えると、その commit は失敗する。書き換えた差分を見直し、stage し直してから commit し直す。`.pre-commit-config.yaml` を変えたら `mise exec -- prek install` で hook を登録し直す。
- `mise run zizmor`: zizmor の監査。online audit (impostor-commit 等) は `ZIZMOR_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` のどれも無いと走らず、その環境では prek の zizmor hook も offline の audit だけで成功する。`.github/`・`.pre-commit-config.yaml`・`mise.toml` を変えたら `ZIZMOR_GITHUB_TOKEN=$(gh auth token) mise run zizmor` で CI と同じ範囲を確かめ、出力に `running in offline mode` の warning が無いことを見る (出たら `gh auth status` を確かめる)。
- `mise run dev`: 開発サーバー (http://127.0.0.1:8000)。`PASSWORD` は `fnox.toml` (age で暗号化) を `fnox exec` が復号して渡し、`DECO_HOST` は `mise.toml` の `[env]` が渡す。`fnox.toml` の鍵で復号する環境では 1Password CLI (`op`) で age の秘密鍵を読むので、1Password の承認 (Touch ID 等) を待って止まることがある。
- `mise run check-image`: Docker イメージを `deco-be85-api:latest` として build し、ダミーの設定・read-only・capability なしで起動して `/api/health` の応答を確かめる (CI の `image` job が amd64 / arm64 で同じ task を実行する)。image の build の入力 (`Dockerfile`・`.dockerignore`・`.python-version`・`pyproject.toml` の依存・`uv.lock`) か、この task を変えたら実行する。Docker を使えない環境では、その変更を含む commit で走った PR の `image` job の結果で確かめ、無ければ未検証と報告する。ランナーは distroless で shell が無いので、中は `docker run --rm --entrypoint python deco-be85-api:latest -c '<code>'` で調べる。
- `mise run docs`: `mise.toml` の tasks を変更したあと README のタスク一覧を同期する。

## 構成

- `src/deco/`: Deco ローカル API クライアント (FastAPI 非依存)。`crypto.py` (AES / RSA 暗号化と署名)、`client.py` (ログイン・再ログイン・endpoint 呼び出し)、`exceptions.py`。
- `src/api/`: FastAPI 層 (`routes.py` / `models.py` / `service.py`)。`service.py` が `DecoClient` を lock + worker thread で直列実行する。`src/main.py` が例外 → HTTP status の対応を持つ。
- `src/settings.py`: 環境変数 (pydantic-settings)。`PASSWORD` / `DECO_HOST` / `ACCOUNT` / `VERIFY_SSL` / `TIMEOUT` の名前は Home Assistant 側の compose が依存しているので変えない。
- `tests/`: 機能ごとの e2e テスト。xdist の worker ごとに `uvicorn main:app` を subprocess で起動し、luci のログインフローと暗号化を実装した fake の Deco (`tests/harness.py`) につなぐ。入力の全域で成り立つ契約は Hypothesis の property で書く。src の内部を直接 import・patch しない。
- 層の依存は `pyproject.toml` の `[tool.importlinter]` が検査する。`deco` は `api`・`main`・`settings` に依存しない。`api` は `routes` → `models` / `service`、`deco` は `client` → `crypto` / `exceptions` の向きにだけ依存する。fastapi は `api`、Crypto は `deco.crypto`、requests は `deco.client` だけが直接 import できる。`api`・`deco` の直下にモジュールを足したら、`exhaustive = true` の契約の `layers` にも足す。

## 規約

- commit message は Conventional Commits (`type(scope): subject`)。commit-msg hook の commitlint が検査する。
- json / yaml / markdown / toml / Dockerfile は dprint、python は ruff で整形する。手で整えず、`mise run format` (python) と `mise run pre-commit` (全種類) に任せる。
- ruff / ty / import-linter は `uv run --locked` で `uv.lock` のバージョンを、pyright は `mise.toml` の版を使う。pre-commit 側にバージョンを持たず、hook は mise の task か `mise exec -- uv run --locked` を呼ぶ。
- `.gitignore` はホワイトリスト方式。新しいファイルを追跡するときは対応する `!` の行を追記する。
- ツールは `mise.toml` に exact version で pin し、変更したら `mise lock` で `mise.lock` を追従させる。npm: backend のツール (pyright) は `mise.lock` が `.mise/locks/` の下の依存の lock を digest で参照するので、その差分も同じ commit に含め、手で編集しない。Python 本体は uv が管理する。
- Python は 3.14 以上で、minor は `.python-version` で固定する (uv が手元・CI・Docker の build で読む)。ruff・ty・pyright は strict に設定している (例: 2 引数目以降は keyword-only、相対 import 禁止)。
- lint の指摘は原因を直し、設定 (ruff・ty・pyright の規則、`.hadolint.yaml` の severity・`trustedRegistries`・`label-schema`、hook や `mise run zizmor` の option) を緩めて通さない。原因を直せない誤検知だけを除外し、理由を添える: python は規則名付きの `# noqa: <code>`・`# ty: ignore[<rule>]`・`# pyright: ignore[<rule>]` (`# type: ignore` は ty・pyright とも無効)、hadolint は `.hadolint.yaml` の `ignored` (Dockerfile 内の `# hadolint ignore=` は効かない)、typos は `typos.toml` の `[default.extend-words]` (綴りを変えられない識別子は `[default.extend-identifiers]` で識別子ごと)、zizmor は該当行の `# zizmor: ignore[<audit 名>]`。
- workflow の `uses:` は commit SHA で固定し、バージョンをコメントで併記する。
- コメント・ドキュメントは日本語で書き、技術用語・識別子は原語のまま使う。
- 実機への write 系 (`POST /api/wireless*` / `/api/reboot` / `/api/raw` の `operation:write`) は指示があるときだけ叩く。read 系は自由に叩いてよい。誤パスワードでのログイン失敗は実機側で回数制限される (`attemptsAllowed: 8`) ので、故意の失敗は繰り返さない。
