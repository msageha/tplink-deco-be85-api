from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.models import (
    ClientDevice,
    CloudDeviceInfo,
    DashboardSummary,
    DecoNode,
    DestructiveOperationRequest,
    DeviceMode,
    FirmwareStatus,
    MacClone,
    OperationRequest,
    Performance,
    RawRequest,
    RebootRequest,
    SpeedTest,
    TimeSettings,
    WirelessConfigUpdate,
    WirelessPower,
    WlanBandToggle,
)
from api.service import DecoService
from deco import (
    LUCI_OPERATIONS,
    TMP_OPERATIONS,
    DecoClient,
    DecoError,
    LuciOperation,
    TmpClient,
    TmpOperation,
)

router = APIRouter(prefix="/api")


def _get_service(request: Request) -> DecoService[DecoClient]:
    return request.app.state.deco


def _get_tmp_service(request: Request) -> DecoService[TmpClient]:
    return request.app.state.tmp


type Service = Annotated[DecoService[DecoClient], Depends(_get_service)]
type TmpService = Annotated[DecoService[TmpClient], Depends(_get_tmp_service)]


def _object(value: Any) -> dict[str, Any]:
    """
    ルーターの応答の入れ子の値を object として読む。無ければ空として扱う。

    Raises:
        DecoError: object でない (ルーターの不正な応答)。
    """
    if value is None or value == []:  # 空の Lua テーブルは [] でも返る
        return {}
    if not isinstance(value, dict):
        raise DecoError("Deco returned an unexpected response: not a JSON object")
    return cast("dict[str, Any]", value)


@router.get("/health", tags=["system"])
async def health(*, service: Service, tmp: TmpService) -> dict[str, Any]:
    """ルーターに触れずに、サーバー状態とログイン状況 (luci・TMP) を返す。"""
    return {
        "status": "ok",
        "host": service.client.host,
        "logged_in": service.client.logged_in,
        # TMP は、閉じられたセッションをソケットで見分けるため DecoService の worker thread で確かめる。
        "tmp_connected": await tmp.run(tmp.client.check_connected),
    }


@router.post("/login", tags=["system"])
async def login(service: Service) -> dict[str, Any]:
    await service.run(service.client.login)
    return {"logged_in": True}


@router.post("/logout", tags=["system"])
async def logout(service: Service) -> dict[str, Any]:
    await service.run(service.client.logout)
    return {"logged_in": False}


@router.get("/dashboard", tags=["status"])
async def dashboard(service: Service) -> DashboardSummary:
    """回線 / CPU / メモリ / Deco 台数 / 接続数の概況。"""
    decos = await service.run(service.client.get_device_list)
    clients = await service.run(service.client.get_client_list)
    performance = await service.run(service.client.get_performance)
    wan = _object((await service.run(service.client.get_wan)).get("wan"))
    wan_ip = _object(wan.get("ip_info")).get("ip")
    return DashboardSummary(
        internet_online=bool(wan_ip),
        connection_type=wan.get("dial_type"),
        wan_ipv4=wan_ip,
        cpu_usage=performance.get("cpu_usage"),
        mem_usage=performance.get("mem_usage"),
        deco_count=len(decos),
        online_clients=sum(1 for c in clients if ClientDevice.model_validate(c).online),
        decos=[DecoNode.model_validate(d) for d in decos],
    )


@router.get("/devices", tags=["devices"])
async def devices(service: Service) -> list[DecoNode]:
    """Deco ユニット (メッシュノード) 一覧。"""
    raw = await service.run(service.client.get_device_list)
    return [DecoNode.model_validate(d) for d in raw]


@router.get("/clients", tags=["clients"])
async def clients(*, service: Service, online_only: bool = False) -> list[ClientDevice]:
    """接続クライアント一覧。"""
    raw = await service.run(service.client.get_client_list)
    items = [ClientDevice.model_validate(c) for c in raw]
    if online_only:
        items = [c for c in items if c.online]
    return items


@router.get("/clients/blocked", tags=["clients"])
async def clients_blocked(service: Service) -> list[ClientDevice]:
    """ブロック中のクライアント一覧。"""
    raw = await service.run(service.client.get_blocked_clients)
    return [ClientDevice.model_validate(c) for c in raw]


@router.get("/network/wan", tags=["network"])
async def network_wan(service: Service) -> dict[str, Any]:
    """WAN IPv4 ステータス。"""
    return await service.run(service.client.get_wan)


@router.get("/network/internet", tags=["network"])
async def network_internet(service: Service) -> dict[str, Any]:
    """インターネット接続情報 (IPv4 / IPv6)。"""
    return await service.run(service.client.get_internet)


@router.get("/network/lan", tags=["network"])
async def network_lan(service: Service) -> dict[str, Any]:
    """LAN / DHCP DNS / WAN IP。"""
    return await service.run(service.client.get_lan)


@router.get("/network/ipv6", tags=["network"])
async def network_ipv6(service: Service) -> dict[str, Any]:
    """IPv6 の有効状態。"""
    return await service.run(service.client.get_ipv6)


@router.get("/network/performance", tags=["network"])
async def network_performance(service: Service) -> Performance:
    """CPU / メモリ使用率。"""
    raw = await service.run(service.client.get_performance)
    return Performance.model_validate(raw)


@router.get("/network/mac-clone", tags=["network"])
async def network_mac_clone(service: Service) -> MacClone:
    """MAC クローン設定。"""
    raw = await service.run(service.client.get_mac_clone)
    return MacClone.model_validate(raw)


@router.get("/network/wan-mode", tags=["network"])
async def network_wan_mode(service: Service) -> dict[str, Any]:
    """WAN ポートの動作モード。"""
    return await service.run(service.client.get_wan_mode)


@router.get("/network/dhcp-dial", tags=["network"])
async def network_dhcp_dial(service: Service) -> dict[str, Any]:
    """WAN の DHCP 接続設定 (unicast など)。"""
    return await service.run(service.client.get_dhcp_dial)


@router.get("/network/igmp", tags=["network"])
async def network_igmp(service: Service) -> dict[str, Any]:
    """IGMP (マルチキャスト) 設定。"""
    return await service.run(service.client.get_igmp_setting)


@router.get("/network/fast-xmit", tags=["network"])
async def network_fast_xmit(service: Service) -> dict[str, Any]:
    """fast xmit (高速転送) の有効状態。"""
    return await service.run(service.client.get_fast_xmit_setting)


@router.get("/network/vlan", tags=["network"])
async def network_vlan(service: Service) -> dict[str, Any]:
    """VLAN (IPTV) 設定。"""
    return await service.run(service.client.get_vlan)


@router.get("/network/ddns", tags=["network"])
async def network_ddns(service: Service) -> dict[str, Any]:
    """DDNS の有効状態とドメイン。"""
    return await service.run(service.client.get_ddns)


@router.get("/wireless", tags=["wireless"])
async def wireless_get(service: Service) -> dict[str, Any]:
    """Wi-Fi 設定 (band ごとに host / guest)。ssid / password は base64 のまま返す。"""
    return await service.run(service.client.get_wlan)


@router.post(
    "/wireless",
    tags=["wireless"],
    responses={404: {"description": "band の省略時に、まとめて切り替えるゲストが無い"}},
)
async def wireless_toggle(*, body: WlanBandToggle, tmp: TmpService) -> dict[str, Any]:
    """
    band 単位で Wi-Fi を ON / OFF する。接続中のクライアントは一時的に切断される。

    guest で band を省略すると、guest が別扱い (6 GHz など) でない band をまとめて切り替える。
    """
    settings = {"enable": body.enable}
    if body.band is None:
        bands = await tmp.run(tmp.client.set_guest_enable, enable=body.enable)
        if not bands:
            raise HTTPException(
                status_code=404, detail="No guest network found to toggle"
            )
        return {"updated": {band: {body.network: settings} for band in bands}}
    await tmp.run(
        tmp.client.set_wlan, band=body.band, network=body.network, settings=settings
    )
    return {"updated": {body.band: {body.network: settings}}}


@router.post("/wireless/config", tags=["wireless"])
async def wireless_config(
    *, body: WirelessConfigUpdate, tmp: TmpService
) -> dict[str, Any]:
    """
    band の 1 つの network の Wi-Fi 設定 (SSID / パスワード など) と、band の radio の設定
    (channel など。network によらず band 全体に効く) を変更する。

    ssid / password は平文で受け取り、サーバー側で base64 化してルーターへ送る。
    接続中のクライアントは一時的に切断される。
    """
    settings = body.settings.model_dump(exclude_none=True)
    result = await tmp.run(
        tmp.client.set_wlan, band=body.band, network=body.network, settings=settings
    )
    if "password" in settings:
        settings["password"] = "***"  # noqa: S105 - 応答に平文のパスワードを返さないための伏せ字
    return {"updated": {body.band: {body.network: settings}}, "result": result}


@router.get("/wireless/power", tags=["wireless"])
async def wireless_power(service: Service) -> WirelessPower:
    """電波の情報 (DFS 対応など)。"""
    raw = await service.run(service.client.get_wireless_power)
    return WirelessPower.model_validate(raw)


@router.get("/wireless/beamforming", tags=["wireless"])
async def wireless_beamforming(service: Service) -> dict[str, Any]:
    """beamforming の有効状態。"""
    return await service.run(service.client.get_beamforming)


@router.get("/wireless/operation-mode", tags=["wireless"])
async def wireless_operation_mode(service: Service) -> dict[str, Any]:
    """無線の動作モード (AP / router など)。"""
    return await service.run(service.client.get_operation_mode)


@router.get("/wireless/bridge", tags=["wireless"])
async def wireless_bridge(service: Service) -> dict[str, Any]:
    """ブリッジ / PLC の状態。"""
    return await service.run(service.client.get_bridge)


@router.get("/wireless/roaming", tags=["wireless"])
async def wireless_roaming(service: Service) -> dict[str, Any]:
    """802.11r 高速ローミングの有効状態。"""
    return await service.run(service.client.get_fast_roaming)


@router.get("/wireless/bandwidth", tags=["wireless"])
async def wireless_bandwidth(service: Service) -> dict[str, Any]:
    """160MHz 幅 (HT160) の有効状態。"""
    return await service.run(service.client.get_bandwidth)


@router.get("/device/mode", tags=["device"])
async def device_mode(service: Service) -> DeviceMode:
    """動作モード (region / workmode / sysmode)。"""
    raw = await service.run(service.client.get_mode)
    return DeviceMode.model_validate(raw)


@router.get("/device/time", tags=["device"])
async def device_time(service: Service) -> TimeSettings:
    """時刻・タイムゾーン設定。"""
    raw = await service.run(service.client.get_time_settings)
    return TimeSettings.model_validate(raw)


@router.get("/device/speedtest", tags=["device"])
async def device_speedtest(service: Service) -> SpeedTest:
    """直近のスピードテスト結果 (計測は行わない)。"""
    raw = await service.run(service.client.get_speedtest_result)
    return SpeedTest.model_validate(raw)


@router.get("/cloud/device-info", tags=["cloud"])
async def cloud_device_info(service: Service) -> CloudDeviceInfo:
    """クラウド連携情報 (model / role など)。"""
    raw = await service.run(service.client.get_cloud_device_info)
    return CloudDeviceInfo.model_validate(raw)


@router.get("/cloud/login-status", tags=["cloud"])
async def cloud_login_status(service: Service) -> dict[str, Any]:
    """TP-Link ID (クラウド) のログイン状態。"""
    return await service.run(service.client.get_cloud_login_status)


@router.get("/system/component-info", tags=["system"])
async def system_component_info(service: Service) -> dict[str, Any]:
    """ERP / 省電力などのコンポーネント情報。"""
    return await service.run(service.client.get_extra_component_info)


@router.get("/system/switch-list", tags=["system"])
async def system_switch_list(service: Service) -> dict[str, Any]:
    """UI の機能スイッチ。"""
    return await service.run(service.client.get_switch_list)


@router.get("/system/log-types", tags=["system"])
async def system_log_types(service: Service) -> Any:
    """エクスポート可能なログ種別。"""
    return await service.run(service.client.get_log_types)


@router.get("/system/log", tags=["system"])
async def system_log(
    *,
    service: Service,
    level: Annotated[int, Query(ge=1, le=8)] = 8,
    index: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1)] = 100,
) -> dict[str, Any]:
    """
    システムログを 1 ページ分取得する。

    level は /system/log-types の value (8 = ALL)。index は 0 始まりのページ番号、limit は
    1 ページの件数で、応答の totalNum はその limit での総ページ数。
    """
    return await service.run(
        service.client.get_system_log, level=level, index=index, limit=limit
    )


@router.get("/system/firmware", tags=["system"])
async def system_firmware(service: Service) -> list[FirmwareStatus]:
    """各ノードにファームウェア更新があるかを TP-Link cloud に問い合わせる (数秒かかる)。"""
    raw = await service.run(service.client.check_firmware)
    return [FirmwareStatus.model_validate(f) for f in raw]


@router.post(
    "/reboot",
    tags=["system"],
    responses={
        400: {"description": "confirm が false"},
        404: {"description": "再起動できる Deco が見つからない"},
    },
)
async def reboot(*, body: RebootRequest, service: Service) -> dict[str, Any]:
    """Deco を再起動する。macs 省略時は全ノード。"""
    if not body.confirm:
        raise HTTPException(
            status_code=400, detail="Set confirm=true to reboot the Deco units"
        )
    macs = body.macs
    if not macs:
        decos = await service.run(service.client.get_device_list)
        found = [d.get("mac") for d in decos]
        # 型の違う値 (0 など) を無い MAC と同じに扱って黙って対象から外さないよう、絞り込む前に確かめる。
        if not all(mac is None or isinstance(mac, str) for mac in found):
            raise DecoError(
                "Deco returned a device list with a MAC that is not a string"
            )
        macs = [mac for mac in found if mac]
    if not macs:
        raise HTTPException(status_code=404, detail="No Deco units found to reboot")
    await service.run(service.client.reboot, macs)
    return {"rebooting": macs}


@router.post("/raw", tags=["raw"])
async def raw(*, body: RawRequest, service: Service) -> dict[str, Any]:
    """
    任意の Deco エンドポイントを呼び、復号した envelope をそのまま返す。

    write 系の operation はルーター設定を変更できるので注意。
    """
    return await service.run(
        service.client.raw, body.path, operation=body.operation, params=body.params
    )


def _require_confirm(body: DestructiveOperationRequest) -> None:
    if not body.confirm:
        raise HTTPException(
            status_code=400, detail="Set confirm=true to run this operation"
        )


_CONFIRM_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "confirm が false"}
}
_TMP_RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {"description": "params が TMP の 1 packet に収まらない"}
}


def _add_luci_route(entry: LuciOperation) -> None:
    """luci の 1 操作を `POST /api/luci/<module>/<form>/<operation>` として登録する。"""

    async def call(*, body: OperationRequest | None = None, service: Service) -> Any:
        params = body.params if body is not None else None
        return await service.run(
            service.client.request,
            entry.path,
            operation=entry.operation,
            params=params,
        )

    async def call_destructive(
        *, body: DestructiveOperationRequest, service: Service
    ) -> Any:
        _require_confirm(body)
        return await call(body=body, service=service)

    module = entry.module.removeprefix("admin/")
    router.add_api_route(
        f"/luci/{module}/{entry.form}/{entry.operation}",
        call_destructive if entry.destructive else call,
        methods=["POST"],
        name=f"luci_{module}_{entry.form}_{entry.operation}",
        summary=f"{entry.path} の {entry.operation}",
        tags=[f"luci: {module}"],
        responses=_CONFIRM_RESPONSES if entry.destructive else None,
        openapi_extra={
            "x-deco-luci-path": entry.path,
            "x-deco-operation": entry.operation,
            "x-deco-destructive": entry.destructive,
        },
    )


def _add_tmp_route(entry: TmpOperation) -> None:
    """TMP の 1 操作を `POST /api/app/<controller>/<form>/<operation>` として登録する。"""

    async def call(*, body: OperationRequest | None = None, tmp: TmpService) -> Any:
        params = body.params if body is not None else None
        return await tmp.run(tmp.client.call, entry.opcode, params=params)

    async def call_destructive(
        *, body: DestructiveOperationRequest, tmp: TmpService
    ) -> Any:
        _require_confirm(body)
        return await call(body=body, tmp=tmp)

    router.add_api_route(
        f"/app/{entry.controller}/{entry.form}/{entry.operation}",
        call_destructive if entry.destructive else call,
        methods=["POST"],
        name=f"app_{entry.controller}_{entry.form}_{entry.operation}",
        summary=f"opcode {entry.opcode:#06x} ({entry.form} の {entry.operation})",
        tags=[f"app: {entry.controller}"],
        responses=_TMP_RESPONSES | (_CONFIRM_RESPONSES if entry.destructive else {}),
        openapi_extra={
            "x-deco-tmp-opcode": entry.opcode,
            "x-deco-operation": entry.operation,
            "x-deco-destructive": entry.destructive,
        },
    )


for _luci_entry in LUCI_OPERATIONS:
    _add_luci_route(_luci_entry)
for _tmp_entry in TMP_OPERATIONS:
    _add_tmp_route(_tmp_entry)
