"""GET /api/system/log: ルーターに level で絞ったログを build させてから、1 ページ分を read する。"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hypothesis import given, strategies as st

from tests.harness import Api, RouterCall, Stack
from tests.strategies import as_json, merged, unknown_fields

_LOG = "admin/log_export?form=feedback_log"


def _respond_log(stack: Stack, *, page: dict[str, Any]) -> None:
    stack.router.respond_result(_LOG, result={}, operation="build")
    stack.router.respond_result(_LOG, result=page)


@given(
    level=st.integers(min_value=1, max_value=8),
    index=st.integers(min_value=0),
    limit=st.integers(min_value=1),
    page=merged(
        st.fixed_dictionaries(
            {
                "totalNum": st.integers(min_value=0),
                "currentIndex": st.integers(min_value=0),
                "logList": st.lists(st.fixed_dictionaries({"content": st.text()})),
            }
        ),
        unknown_fields(known={"totalNum", "currentIndex", "logList"}),
    ),
)
def test_log_page_is_built_for_level_then_read(
    *, stack: Stack, level: int, index: int, limit: int, page: dict[str, Any]
) -> None:
    stack.reset()
    _respond_log(stack, page=page)

    response = stack.api.get(
        "/api/system/log", params={"level": level, "index": index, "limit": limit}
    )

    assert response.status_code == 200
    assert as_json(response.json()) == as_json(page)
    assert stack.router.calls == [
        RouterCall(_LOG, "build", {"level": level}),
        RouterCall(_LOG, "read", {"index": index, "limit": limit}),
    ]


def test_log_defaults_to_all_levels_first_page_of_100(stack: Stack) -> None:
    stack.reset()
    _respond_log(stack, page={})

    assert stack.api.get("/api/system/log").status_code == 200
    assert stack.router.calls == [
        RouterCall(_LOG, "build", {"level": 8}),
        RouterCall(_LOG, "read", {"index": 0, "limit": 100}),
    ]


def test_concurrent_log_requests_each_read_the_level_they_built(stack: Stack) -> None:
    """build と read の間に、別のリクエストの build が割り込まない。"""
    stack.reset()
    # ログインの競合と切り分けるため、先にログインしておく。
    assert stack.api.post("/api/login").status_code == 200
    lock = threading.Lock()
    built: dict[str, int] = {}
    in_progress: list[int] = []
    overlaps: list[int] = []

    def build(params: dict[str, int]) -> dict[str, Any]:
        with lock:
            built["level"] = params["level"]
            in_progress.append(params["level"])
            overlaps.append(len(in_progress))
        # 応答を遅らせ、直列化されていなければ別のリクエストの build がこの間に届くようにする。
        time.sleep(0.05)
        return {"error_code": 0}

    def read(_: object) -> dict[str, Any]:
        with lock:
            level = built["level"]
            in_progress.remove(level)
        return {"error_code": 0, "result": {"level": level}}

    stack.router.respond(_LOG, envelope=build, operation="build")
    stack.router.respond(_LOG, envelope=read)
    levels = list(range(1, 9))

    def fetch(level: int) -> tuple[int, Any]:
        response = Api(stack.api.base_url).get(
            "/api/system/log", params={"level": level}
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=len(levels)) as pool:
        results = list(pool.map(fetch, levels))

    assert results == [(200, {"level": level}) for level in levels]
    assert max(overlaps) == 1


_out_of_range = st.one_of(
    st.fixed_dictionaries(
        {"level": st.integers(max_value=0) | st.integers(min_value=9)}
    ),
    st.fixed_dictionaries({"index": st.integers(max_value=-1)}),
    st.fixed_dictionaries({"limit": st.integers(max_value=0)}),
)


@given(params=_out_of_range)
def test_log_query_out_of_range_is_rejected_without_contacting_router(
    *, stack: Stack, params: dict[str, int]
) -> None:
    stack.reset()

    response = stack.api.get("/api/system/log", params=params)

    assert response.status_code == 422
    assert stack.router.arrivals == []
