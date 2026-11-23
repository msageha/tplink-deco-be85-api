from collections.abc import Iterator

import pytest
from hypothesis import settings

from tests.fake_tmp import FakeTmp
from tests.harness import ROUTER_PASSWORD, FakeDeco, Stack, run_api

# 1 つの入力が 2 つのプロセスを跨ぐ HTTP の往復で、並列実行の負荷による遅れを入力ごとの失敗にしないため deadline を外す。
settings.register_profile("e2e", deadline=None)
settings.load_profile("e2e")


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """
    fake の Deco (luci・TMP) と、それに接続した API サーバー (xdist の worker ごとに 1 組)。

    状態を共有するので、各テストと property の各入力は最初に `reset` を呼ぶ。
    """
    log_path = tmp_path_factory.mktemp("api") / "uvicorn.log"
    with (
        FakeDeco.serve() as router,
        FakeTmp.serve(password=ROUTER_PASSWORD) as app,
        run_api(deco_host=router.url, tmp_port=app.port, log_path=log_path) as api,
    ):
        yield Stack(router, app, api)
