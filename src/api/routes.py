from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .models import (
    ClientDevice,
    CloudDeviceInfo,
    DashboardSummary,
    DecoNode,
    DeviceMode,
    FirmwareStatus,
    MacClone,
    Performance,
    RawRequest,
    RebootRequest,
    TimeSettings,
    WirelessConfigUpdate,
    WirelessPower,
    WlanBandToggle,
)
from .service import DecoService

router = APIRouter(prefix="/api")


def _get_service(request: Request) -> DecoService:
    return request.app.state.deco


Service = Annotated[DecoService, Depends(_get_service)]


@router.get("/health", tags=["system"])
async def health(service: Service) -> dict[str, Any]:
    """ルーターに触れずに、サーバー状態とログイン状況を返す。"""
    return {
        "status": "ok",
        "host": service.client.host,
        "logged_in": service.client.logged_in,
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
    wan = (await service.run(service.client.get_wan)).get("wan") or {}
    wan_ip = (wan.get("ip_info") or {}).get("ip")
    return DashboardSummary(
        internet_online=bool(wan_ip),
        connection_type=wan.get("dial_type"),
        wan_ipv4=wan_ip,
        cpu_usage=performance.get("cpu_usage"),
        mem_usage=performance.get("mem_usage"),
        deco_count=len(decos),
        online_clients=sum(1 for c in clients if c.get("online")),
        decos=[DecoNode.model_validate(d) for d in decos],
    )


@router.get("/devices", tags=["devices"])
async def devices(service: Service) -> list[DecoNode]:
    """Deco ユニット (メッシュノード) 一覧。"""
    raw = await service.run(service.client.get_device_list)
    return [DecoNode.model_validate(d) for d in raw]


@router.get("/clients", tags=["clients"])
async def clients(service: Service, online_only: bool = False) -> list[ClientDevice]:
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


@router.get("/wireless", tags=["wireless"])
async def wireless_get(service: Service) -> dict[str, Any]:
    """Wi-Fi 設定 (band ごとに host / guest)。ssid / password は base64 のまま返す。"""
    return await service.run(service.client.get_wlan)


@router.post("/wireless", tags=["wireless"])
async def wireless_toggle(body: WlanBandToggle, service: Service) -> dict[str, Any]:
    """band 単位で Wi-Fi を ON / OFF する。接続中のクライアントは一時的に切断される。"""
    settings = {"enable": body.enable}
    await service.run(service.client.set_wlan, body.band, body.network, settings)
    return {"updated": {body.band: {body.network: settings}}}


@router.post("/wireless/config", tags=["wireless"])
async def wireless_config(
    body: WirelessConfigUpdate, service: Service
) -> dict[str, Any]:
    """band × network の Wi-Fi 設定 (SSID / パスワード / channel など) を変更する。

    ssid / password は平文で受け取り、サーバー側で base64 化してルーターへ送る。
    接続中のクライアントは一時的に切断される。
    """
    settings = body.settings.model_dump(exclude_none=True)
    result = await service.run(
        service.client.set_wlan, body.band, body.network, settings
    )
    if "password" in settings:
        settings["password"] = "***"
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


@router.get("/cloud/device-info", tags=["cloud"])
async def cloud_device_info(service: Service) -> CloudDeviceInfo:
    """クラウド連携情報 (model / role など)。"""
    raw = await service.run(service.client.get_cloud_device_info)
    return CloudDeviceInfo.model_validate(raw)


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
    service: Service,
    level: Annotated[int, Query(ge=1, le=8)] = 8,
    index: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1)] = 100,
) -> dict[str, Any]:
    """システムログを 1 ページ分取得する。

    level は /system/log-types の value (8 = ALL)。index は 0 始まりのページ番号、limit は
    1 ページの件数で、応答の totalNum はその limit での総ページ数。
    """
    return await service.run(service.client.get_system_log, level, index, limit)


@router.get("/system/firmware", tags=["system"])
async def system_firmware(service: Service) -> list[FirmwareStatus]:
    """各ノードにファームウェア更新があるかを TP-Link cloud に問い合わせる (数秒かかる)。"""
    raw = await service.run(service.client.check_firmware)
    return [FirmwareStatus.model_validate(f) for f in raw]


@router.post("/reboot", tags=["system"])
async def reboot(body: RebootRequest, service: Service) -> dict[str, Any]:
    """Deco を再起動する。macs 省略時は全ノード。"""
    if not body.confirm:
        raise HTTPException(
            status_code=400, detail="Set confirm=true to reboot the Deco units"
        )
    macs = body.macs
    if not macs:
        decos = await service.run(service.client.get_device_list)
        macs = [d["mac"] for d in decos if d.get("mac")]
    if not macs:
        raise HTTPException(status_code=404, detail="No Deco units found to reboot")
    await service.run(service.client.reboot, macs)
    return {"rebooting": macs}


@router.post("/raw", tags=["raw"])
async def raw(body: RawRequest, service: Service) -> dict[str, Any]:
    """任意の Deco エンドポイントを呼び、復号した envelope をそのまま返す。

    write 系の operation はルーター設定を変更できるので注意。
    """
    return await service.run(service.client.raw, body.path, body.operation, body.params)
