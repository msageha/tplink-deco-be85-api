# CLAUDE.md

TP-Link Deco BE85 のローカル Web API (luci) を FastAPI でラップした REST サーバー。
endpoint 一覧・ログインフロー・実機で確認した luci form の挙動は README.md に集約している。

## セットアップと検証

- `mise install`: ツールのインストール、`uv sync --locked`、git hooks (pre-commit / commit-msg) のセットアップ。旧 pre-commit (`uv run pre-commit install`) の hook が残る clone では `mise exec -- prek install --force` で置き換える。
- `mise run test`: coverage 付き pytest。オフラインで完結し、実機には触れない。
- `mise run lint`: ruff format --check / ruff check / ty check。
- `mise exec -- prek run --all-files`: 全 hook (dprint、actionlint、hadolint、ruff、ty、gitleaks 等) を実行する。CI の `prek` job と同じ検証。
- `mise run dev`: 開発サーバー (http://127.0.0.1:8000)。認証情報と接続先はリポジトリ直下の `.env` (`PASSWORD` / `DECO_HOST`)。
- `mise run docs`: `mise.toml` の tasks を変更したあと README のタスク一覧を同期する。

## 構成

- `src/deco/`: Deco ローカル API クライアント (FastAPI 非依存)。`crypto.py` (AES / RSA 暗号化と署名)、`client.py` (ログイン・再ログイン・endpoint 呼び出し)、`exceptions.py`。
- `src/api/`: FastAPI 層 (`routes.py` / `models.py` / `service.py`)。`service.py` が `DecoClient` を lock + worker thread で直列実行する。`src/main.py` が例外 → HTTP status の対応を持つ。
- `src/settings.py`: 環境変数 (pydantic-settings)。`PASSWORD` / `DECO_HOST` / `ACCOUNT` / `VERIFY_SSL` / `TIMEOUT` の名前は Home Assistant 側の compose が依存しているので変えない。
- `tests/`: `src` をミラーしたオフライン単体テスト。

## 規約

- commit message は Conventional Commits (`type(scope): subject`)。commit-msg hook の commitlint が検査する。
- json / yaml / markdown / toml は dprint、python は ruff で整形する。手で整えず `prek run --all-files` に任せる。
- ruff / ty は `uv run --locked` で `uv.lock` のバージョンを使う。pre-commit 側にバージョンを持たない。
- `.gitignore` はホワイトリスト方式。新しいファイルを追跡するときは対応する `!` の行を追記する。
- ツールは `mise.toml` に exact version で pin し、変更したら `mise lock` で `mise.lock` を追従させる。Python 本体は uv が管理する。
- workflow の `uses:` は commit SHA で固定し、バージョンをコメントで併記する。
- コメント・ドキュメントは日本語で書き、技術用語・識別子は原語のまま使う。
- 実機への write 系 (`POST /api/wireless*` / `/api/reboot` / `/api/raw` の `operation:write`) は指示があるときだけ叩く。read 系は自由に叩いてよい。誤パスワードでのログイン失敗は実機側で回数制限される (`attemptsAllowed: 8`) ので、故意の失敗は繰り返さない。
