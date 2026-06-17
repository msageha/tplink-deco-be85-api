from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .schemas import (
    ClientDevice,
    CloudDeviceInfo,
    DashboardSummary,
    DecoNode,
    DeviceMode,
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


def get_service(request: Request) -> DecoService:
    return request.app.state.deco


def _wan_ipv4(wan: dict[str, Any]) -> str | None:
    info = (wan.get("wan") or {}).get("ip_info") or {}
    return info.get("ip")


@router.get("/health", tags=["system"])
async def health(service: DecoService = Depends(get_service)) -> dict[str, Any]:
    return {
        "status": "ok",
        "host": service.client.host,
        "logged_in": service.client.logged_in,
    }


@router.post("/login", tags=["system"])
async def login(service: DecoService = Depends(get_service)) -> dict[str, Any]:
    await service.run(service.client.login)
    return {"logged_in": True, "stok": "***"}


@router.post("/logout", tags=["system"])
async def logout(service: DecoService = Depends(get_service)) -> dict[str, Any]:
    await service.run(service.client.logout)
    return {"logged_in": False}


@router.get("/dashboard", response_model=DashboardSummary, tags=["status"])
async def dashboard(service: DecoService = Depends(get_service)) -> DashboardSummary:
    decos_raw = await service.run(service.client.get_device_list)
    clients_raw = await service.run(service.client.get_client_list)
    perf = await service.run(service.client.get_performance)
    wan = await service.run(service.client.get_wan)

    decos = [DecoNode.from_raw(d) for d in decos_raw]
    wan_ip = _wan_ipv4(wan)
    return DashboardSummary(
        internet_online=bool(wan_ip),
        connection_type=(wan.get("wan") or {}).get("dial_type"),
        wan_ipv4=wan_ip,
        cpu_usage=perf.get("cpu_usage"),
        mem_usage=perf.get("mem_usage"),
        deco_count=len(decos),
        online_clients=sum(1 for c in clients_raw if c.get("online")),
        decos=decos,
    )


@router.get("/devices", response_model=list[DecoNode], tags=["devices"])
async def devices(service: DecoService = Depends(get_service)) -> list[DecoNode]:
    raw = await service.run(service.client.get_device_list)
    return [DecoNode.from_raw(d) for d in raw]


@router.get("/clients", response_model=list[ClientDevice], tags=["clients"])
async def clients(
    online_only: bool = False,
    service: DecoService = Depends(get_service),
) -> list[ClientDevice]:
    raw = await service.run(service.client.get_client_list)
    items = [ClientDevice.from_raw(c) for c in raw]
    if online_only:
        items = [c for c in items if c.online]
    return items


@router.get("/network/wan", tags=["network"])
async def network_wan(service: DecoService = Depends(get_service)) -> dict[str, Any]:
    return await service.run(service.client.get_wan)


@router.get("/network/internet", tags=["network"])
async def network_internet(
    service: DecoService = Depends(get_service),
) -> dict[str, Any]:
    return await service.run(service.client.get_internet)


@router.get("/network/lan", tags=["network"])
async def network_lan(service: DecoService = Depends(get_service)) -> dict[str, Any]:
    return await service.run(service.client.get_lan)


@router.get("/network/ipv6", tags=["network"])
async def network_ipv6(service: DecoService = Depends(get_service)) -> dict[str, Any]:
    return await service.run(service.client.get_ipv6)


@router.get("/network/performance", response_model=Performance, tags=["network"])
async def network_performance(
    service: DecoService = Depends(get_service),
) -> Performance:
    raw = await service.run(service.client.get_performance)
    return Performance.model_validate(raw)


@router.get("/wireless", tags=["wireless"])
async def wireless_get(service: DecoService = Depends(get_service)) -> dict[str, Any]:
    return await service.run(service.client.get_wlan)


@router.post("/wireless", tags=["wireless"])
async def wireless_set(
    toggle: WlanBandToggle,
    service: DecoService = Depends(get_service),
) -> dict[str, Any]:
    # band/network are validated by the WifiBand/WifiNetwork enums.
    params = {toggle.band.value: {toggle.network.value: {"enable": toggle.enable}}}
    await service.run(service.client.set_wlan, params)
    return {"updated": params}


@router.post("/wireless/config", tags=["wireless"])
async def wireless_config(
    body: WirelessConfigUpdate,
    service: DecoService = Depends(get_service),
) -> dict[str, Any]:
    """Update Wi-Fi settings for one band+network (host/guest).

    Writes to ``admin/wireless?form=wlan``. ``ssid``/``password`` are accepted in
    plain text and base64-encoded before sending. Changing Wi-Fi briefly affects
    connected clients.
    """
    params = body.to_params()
    result = await service.run(service.client.set_wlan, params)
    return {"updated": body.summary(), "result": result}


@router.get("/wireless/power", response_model=WirelessPower, tags=["wireless"])
async def wireless_power(service: DecoService = Depends(get_service)) -> WirelessPower:
    raw = await service.run(service.client.get_wireless_power)
    return WirelessPower.model_validate(raw)


@router.get("/device/mode", response_model=DeviceMode, tags=["device"])
async def device_mode(service: DecoService = Depends(get_service)) -> DeviceMode:
    raw = await service.run(service.client.get_mode)
    return DeviceMode.model_validate(raw)


@router.get("/device/time", response_model=TimeSettings, tags=["device"])
async def device_time(service: DecoService = Depends(get_service)) -> TimeSettings:
    raw = await service.run(service.client.get_time_settings)
    return TimeSettings.model_validate(raw)


@router.get("/network/mac-clone", response_model=MacClone, tags=["network"])
async def network_mac_clone(service: DecoService = Depends(get_service)) -> MacClone:
    raw = await service.run(service.client.get_mac_clone)
    return MacClone.model_validate(raw)


@router.get("/clients/blocked", response_model=list[ClientDevice], tags=["clients"])
async def clients_blocked(
    service: DecoService = Depends(get_service),
) -> list[ClientDevice]:
    raw = await service.run(service.client.get_blocked_clients)
    return [ClientDevice.from_raw(c) for c in raw]


@router.get("/cloud/device-info", response_model=CloudDeviceInfo, tags=["cloud"])
async def cloud_device_info(
    service: DecoService = Depends(get_service),
) -> CloudDeviceInfo:
    raw = await service.run(service.client.get_cloud_device_info)
    return CloudDeviceInfo.model_validate(raw)


@router.get("/system/component-info", tags=["system"])
async def system_component_info(
    service: DecoService = Depends(get_service),
) -> dict[str, Any]:
    return await service.run(service.client.get_extra_component_info)


@router.get("/system/switch-list", tags=["system"])
async def system_switch_list(
    service: DecoService = Depends(get_service),
) -> dict[str, Any]:
    return await service.run(service.client.get_switch_list)


@router.get("/system/log-types", tags=["system"])
async def system_log_types(service: DecoService = Depends(get_service)) -> Any:
    return await service.run(service.client.get_log_types)


@router.post("/raw", tags=["raw"])
async def raw_passthrough(
    body: RawRequest,
    service: DecoService = Depends(get_service),
) -> dict[str, Any]:
    """Call any Deco endpoint and return the full decrypted response envelope.

    Powerful escape hatch: with ``operation: write`` (and the right ``params``)
    this can change router settings, so use with care.
    """
    return await service.run(service.client.raw, body.path, body.to_payload())


@router.post("/reboot", tags=["system"])
async def reboot(
    body: RebootRequest,
    service: DecoService = Depends(get_service),
) -> dict[str, Any]:
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
