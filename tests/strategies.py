"""ルーターが返しうる JSON の生成と、JSON としての比較。"""

import json
from collections.abc import Collection
from typing import Any

from hypothesis import strategies as st

json_values: st.SearchStrategy[Any] = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(st.text(), children, max_size=3)
    ),
    max_leaves=8,
)


def unknown_fields(*, known: Collection[str] = ()) -> st.SearchStrategy[dict[str, Any]]:
    """ファームウェア固有の未知のフィールド。API のモデルが宣言したフィールド (known) とは重ならない。"""
    return st.dictionaries(
        st.text().filter(lambda name: name not in known), json_values, max_size=3
    )


# ルーターの成功応答は error_code / result 形式と success / data 形式の 2 通りある。
ok_envelope_styles = st.sampled_from(["error_code", "success"])


def ok_envelope(result: Any, *, style: str) -> dict[str, Any]:
    if style == "success":
        return {"success": True, "data": result}
    return {"error_code": 0, "result": result}


def merged(
    *parts: st.SearchStrategy[dict[str, Any]],
) -> st.SearchStrategy[dict[str, Any]]:
    """生成した dict を 1 つにまとめる (キーが重なれば後ろの値)。"""

    def merge(dicts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for d in dicts:
            result |= d
        return result

    return st.tuples(*parts).map(merge)


def as_json(value: Any) -> str:
    """
    JSON の値として比べるための表現。

    Python の == は True と 1、False と 0 を等しいとみなし、JSON の型の違いを見逃す。
    """
    return json.dumps(value, sort_keys=True)
