"""按显式批次运行真实 AKShare-family 上游探针，整个矩阵不连接或写入数据库。

默认不访问外部网络。运维人员必须设置 `DATA_SYNC_AKSHARE_LIVE_BATCH` 为一个批次名、
逗号分隔的批次列表或 `all`，才会运行相应只读探针。每个探针直接调用 adapter 的
`fetch`，检查其返回的标准化批次；这证明 SDK/HTTP 调用与 adapter 映射可用，但不把
空数据集、历史可见性或 publication 误报成已完成入库。

P0 `market.stock_connect.active_security.snapshot`、SSE 两融资格以及北交所余额/证券
明细没有可验证真源时，会作为 `CURRENTLY_UNSUPPORTED` 显式断言，不计入成功矩阵。
北交所资格名单则实测 `stock_margin_underlying_info_bse` 并校验其标准载荷。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.infrastructure.providers.akshare import (
    cnindex_index_snapshot,
    eastmoney_http,
)
from service_data_sync.infrastructure.providers.akshare.cnindex_index_snapshot import (
    AkshareCnindexIndexSnapshotAdapter,
)
from service_data_sync.infrastructure.providers.akshare.cninfo_company_profile import (
    AkshareCninfoCompanyProfileAdapter,
)
from service_data_sync.infrastructure.providers.akshare.csindex_index_snapshot import (
    AkshareCsindexIndexSnapshotAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_corporate_actions import (
    AkshareEastmoneyCorporateActionsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_equity_catalog import (
    AkshareEastmoneyEquityCatalogAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_equity_period_bars import (
    AkshareEastmoneyEquityPeriodBarsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_financial import (
    AkshareEastmoneyFinancialAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_bars import (
    AkshareEastmoneySectorBarsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_eod import (
    AkshareEastmoneySectorEodAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_membership import (
    AkshareEastmoneySectorMembershipAdapter,
)
from service_data_sync.infrastructure.providers.akshare.exchange_equity_lifecycle import (
    AkshareExchangeEquityLifecycleAdapter,
)
from service_data_sync.infrastructure.providers.akshare.money_flow import (
    AkshareEastmoneyMoneyFlowAdapter,
    AkshareThsMoneyFlowAdapter,
)
from service_data_sync.infrastructure.providers.akshare.p0_market_data import (
    AkshareP0MarketDataAdapter,
)
from service_data_sync.infrastructure.providers.akshare.sina_adjustment_factors import (
    AkshareSinaAdjustmentFactorsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.sw_industry_snapshot import (
    AkshareSwIndustrySnapshotAdapter,
)
from service_data_sync.infrastructure.providers.akshare.tencent_daily_bars import (
    AkshareTencentDailyBarsAdapter,
)

pytestmark = pytest.mark.integration

_BATCH_ENV = "DATA_SYNC_AKSHARE_LIVE_BATCH"
_BATCHES = frozenset(
    {
        "equity",
        "p0-etf-margin",
        "p0-events",
        "financial",
        "money-flow",
        "index",
        "sector",
    }
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REQUEST_TIMEOUT_SECONDS = 90
_PAUSE_SECONDS = 1.0
_CORPORATE_ACTION_FIELDS = frozenset(
    {
        "sourceEventKey",
        "reportPeriod",
        "status",
        "announcementDate",
        "recordDate",
        "exDate",
        "cashDividendPer10",
        "bonusSharesPer10",
        "transferSharesPer10",
    }
)
_COMPANY_PROFILE_FIELDS = frozenset(
    {
        "companyName",
        "englishName",
        "industry",
        "legalRepresentative",
        "establishedOn",
        "website",
        "email",
        "phone",
        "registeredAddress",
        "officeAddress",
        "mainBusiness",
        "businessScope",
        "summary",
    }
)


class _BatchFetcher(Protocol):
    """描述本矩阵使用的最小 adapter 获取能力，避免耦合具体 provider 类型。"""

    provider_id: str

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按 provider-neutral 请求读取一批真实上游响应。"""
        ...


def _requested_batches() -> frozenset[str]:
    """解析显式环境变量，空值表示默认完全跳过外部网络探针。"""
    raw_value = os.environ.get(_BATCH_ENV, "").strip()
    if not raw_value:
        return frozenset()
    requested = frozenset(value.strip().lower() for value in raw_value.split(",") if value.strip())
    unknown = requested.difference(_BATCHES | {"all"})
    if unknown:
        pytest.fail(f"{_BATCH_ENV} contains unknown batches: {', '.join(sorted(unknown))}")
    return requested


def _require_batch(batch: str) -> None:
    """只允许获显式授权的批次发起外网调用，未选择时给出可复制运行方式。"""
    requested = _requested_batches()
    if "all" not in requested and batch not in requested:
        pytest.skip(f"set {_BATCH_ENV}={batch} or all to run this live probe batch")


@pytest.fixture(autouse=True)
def _install_production_eastmoney_compatibility() -> None:
    """为已选择的实测批次安装与组合根相同的东财受限传输兼容层。"""
    if _requested_batches():
        eastmoney_http.install_eastmoney_request_compatibility()


def test_selected_batch_installs_production_eastmoney_compatibility() -> None:
    """验证已选择批次会先走组合根同款兼容层，不访问任何外部数据源。"""
    _require_batch("equity")
    assert eastmoney_http._installed


def _request(capability: str, **parameters: str) -> SourceRequest:
    """构造只含冻结中立参数的来源请求，不接受供应商 SDK 参数。"""
    return SourceRequest(capability=capability, parameters=tuple(parameters.items()))


def _shanghai_today() -> date:
    """返回当前上海业务日，供仅支持 current snapshot 的来源使用。"""
    return datetime.now(_SHANGHAI).date()


def _latest_weekday(today: date) -> date:
    """返回不晚于当前日的最近工作日，降低周末请求空窗口的概率。"""
    candidate = today
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _probe(
    adapter: _BatchFetcher,
    request: SourceRequest,
    *,
    label: str,
    batch_validator: Callable[[ProviderBatch], None] | None = None,
) -> dict[str, object]:
    """串行调用一个真实 adapter，并校验返回的是可审计标准批次而非 fixture。"""
    batch = asyncio.run(adapter.fetch(request))
    assert batch.provider_id == adapter.provider_id, label
    assert batch.capability == request.capability, label
    assert batch.raw_payload is not None, label
    assert batch.observed_at.tzinfo is not None, label
    if batch_validator is not None:
        batch_validator(batch)
    payload = json.loads(batch.payload)
    assert isinstance(payload, dict), label
    assert isinstance(payload.get("schema"), str), label
    # 限速在每次完整 adapter 调用后执行；不并发、也不绕开各 adapter 自己的总预算。
    time.sleep(_PAUSE_SECONDS)
    return payload


def _non_empty_normalized_array_validator(
    request: SourceRequest,
    *,
    schema: str,
    array_key: str,
) -> Callable[[ProviderBatch], None]:
    """返回要求已规范化行情或因子数组非空的真实批次校验器。"""
    parameters = dict(request.parameters)
    expected_instrument = parameters["instrument"]

    def validate(batch: ProviderBatch) -> None:
        """验证来源返回目标证券的非空标准数组，避免空窗口误报为代表分区成功。"""
        payload = json.loads(batch.payload)
        assert isinstance(payload, dict)
        assert payload.get("schema") == schema
        assert payload.get("instrument") == expected_instrument
        values = payload.get(array_key)
        assert isinstance(values, list) and values
        assert all(isinstance(value, dict) for value in values)

    return validate


def _corporate_action_batch_validator(
    request: SourceRequest,
) -> Callable[[ProviderBatch], None]:
    """返回公司行动结构校验器；明确允许已证明的合法空事件窗口。"""
    expected_instrument = dict(request.parameters)["instrument"]

    def validate(batch: ProviderBatch) -> None:
        """验证事件数组与每项标准字段，不把空数组升级为非空行情成功。"""
        payload = json.loads(batch.payload)
        assert isinstance(payload, dict)
        assert payload.get("schema") == "quant-v2.equity-corporate-action.v1"
        assert payload.get("instrument") == expected_instrument
        actions = payload.get("actions")
        assert isinstance(actions, list)
        for action in actions:
            assert isinstance(action, dict)
            assert frozenset(action) == _CORPORATE_ACTION_FIELDS
            assert isinstance(action.get("sourceEventKey"), str) and action["sourceEventKey"]
            assert isinstance(action.get("reportPeriod"), str) and action["reportPeriod"]
            assert isinstance(action.get("status"), str) and action["status"]

    return validate


def _company_profile_batch_validator(
    request: SourceRequest,
) -> Callable[[ProviderBatch], None]:
    """返回公司概况结构校验器，要求当前概况含非空公司名称。"""
    expected_instrument = dict(request.parameters)["instrument"]

    def validate(batch: ProviderBatch) -> None:
        """验证概况只含冻结标准字段，防止供应商字段或空对象穿过探针。"""
        payload = json.loads(batch.payload)
        assert isinstance(payload, dict)
        assert payload.get("schema") == "quant-v2.equity-profile.v1"
        assert payload.get("instrument") == expected_instrument
        profile = payload.get("profile")
        assert isinstance(profile, dict)
        assert frozenset(profile) == _COMPANY_PROFILE_FIELDS
        assert isinstance(profile.get("companyName"), str) and profile["companyName"].strip()
        assert all(value is None or isinstance(value, str) for value in profile.values())

    return validate


def _validator_batch(*, capability: str, payload: dict[str, object]) -> ProviderBatch:
    """构造不访问网络的标准批次，供 live probe 校验器回归测试使用。"""
    return ProviderBatch(
        provider_id="validator-fixture",
        capability=capability,
        payload=json.dumps(payload, ensure_ascii=False).encode(),
        observed_at=datetime(2026, 8, 1, tzinfo=_SHANGHAI),
    )


@pytest.mark.parametrize(
    ("capability", "schema", "array_key"),
    (
        ("equity.bar.1d.raw", "quant-v2.equity-daily-bar.v1", "bars"),
        ("equity.bar.1w.raw", "quant-v2.equity-period-bar.v1", "bars"),
        ("equity.bar.1mo.raw", "quant-v2.equity-period-bar.v1", "bars"),
        ("equity.adjustment_factor", "quant-v2.equity-adjustment-factor.v1", "factors"),
    ),
)
def test_live_equity_nonempty_validators_reject_empty_normalized_arrays(
    capability: str,
    schema: str,
    array_key: str,
) -> None:
    """日周月和因子探针必须拒绝合法空窗，不能将其算作有效代表分区。"""
    request = _request(
        capability,
        instrument="SSE.600519",
        start="2026-07-01",
        end="2026-07-31",
    )
    validator = _non_empty_normalized_array_validator(
        request,
        schema=schema,
        array_key=array_key,
    )

    with pytest.raises(AssertionError):
        validator(
            _validator_batch(
                capability=capability,
                payload={
                    "schema": schema,
                    "instrument": "SSE.600519",
                    array_key: [],
                },
            )
        )


def test_live_equity_structure_validators_keep_action_empty_window_distinct() -> None:
    """公司行动可接受合法空数组；公司概况仍必须满足完整标准结构。"""
    action_request = _request(
        "equity.corporate_action",
        instrument="SSE.600519",
        start="2026-07-01",
        end="2026-07-31",
    )
    _corporate_action_batch_validator(action_request)(
        _validator_batch(
            capability=action_request.capability,
            payload={
                "schema": "quant-v2.equity-corporate-action.v1",
                "instrument": "SSE.600519",
                "actions": [],
            },
        )
    )

    profile_request = _request("equity.profile", instrument="SSE.600519")
    profile = {field: cast(object, None) for field in _COMPANY_PROFILE_FIELDS}
    profile["companyName"] = "探针样本公司"
    _company_profile_batch_validator(profile_request)(
        _validator_batch(
            capability=profile_request.capability,
            payload={
                "schema": "quant-v2.equity-profile.v1",
                "instrument": "SSE.600519",
                "profile": profile,
            },
        )
    )


def test_live_equity_structure_validators_reject_malformed_payloads() -> None:
    """公司行动和公司概况探针必须拒绝缺字段或关键身份为空的伪标准载荷。"""
    action_request = _request(
        "equity.corporate_action",
        instrument="SSE.600519",
        start="2026-07-01",
        end="2026-07-31",
    )
    malformed_action = {field: cast(object, None) for field in _CORPORATE_ACTION_FIELDS}
    malformed_action.update(
        {
            "sourceEventKey": "eastmoney:sample",
            "reportPeriod": "2026-07-31",
            "status": "",
        }
    )
    with pytest.raises(AssertionError):
        _corporate_action_batch_validator(action_request)(
            _validator_batch(
                capability=action_request.capability,
                payload={
                    "schema": "quant-v2.equity-corporate-action.v1",
                    "instrument": "SSE.600519",
                    "actions": [malformed_action],
                },
            )
        )

    profile_request = _request("equity.profile", instrument="SSE.600519")
    with pytest.raises(AssertionError):
        _company_profile_batch_validator(profile_request)(
            _validator_batch(
                capability=profile_request.capability,
                payload={
                    "schema": "quant-v2.equity-profile.v1",
                    "instrument": "SSE.600519",
                    "profile": {"companyName": "不完整概况"},
                },
            )
        )


def _record_probe_status(
    statuses: list[str],
    *,
    adapter: _BatchFetcher,
    request: SourceRequest,
    label: str,
    batch_validator: Callable[[ProviderBatch], None] | None = None,
) -> None:
    """运行一个真实探针并记录成功、失败或可重试分类，不让首个失败遮蔽同批其余接口。"""
    try:
        _probe(adapter, request, label=label, batch_validator=batch_validator)
    except ProviderError as error:
        statuses.append(
            f"FAILED {label}: code={error.code.value}, retryable={error.retryable}, detail={error}"
        )
    except Exception as error:
        statuses.append(f"FAILED {label}: type={type(error).__name__}, detail={error}")
    else:
        statuses.append(f"SUCCEEDED {label}")


def _fail_if_probe_batch_incomplete(*, batch: str, statuses: list[str]) -> None:
    """把一个显式 live 批次的全部状态一起报告，未执行项也不能被显示成成功。"""
    incomplete = [status for status in statuses if not status.startswith("SUCCEEDED ")]
    if incomplete:
        pytest.fail(f"AKShare {batch} live probe matrix incomplete:\n" + "\n".join(statuses))


def _money_flow_batch_validator(request: SourceRequest) -> Callable[[ProviderBatch], None]:
    """返回校验资金流 schema、来源身份和请求目标一一对应关系的真实批次断言。"""
    parameters = dict(request.parameters)

    def validate(batch: ProviderBatch) -> None:
        """验证一种资金流 capability 的来源、范围和非空标准载荷。"""
        payload = json.loads(batch.payload)
        assert isinstance(payload, dict)
        is_eastmoney = request.capability.startswith("money_flow.order_size.")
        assert batch.upstream_source == (
            "eastmoney.money-flow" if is_eastmoney else "10jqka.money-flow"
        )
        if request.capability in {
            "money_flow.order_size.daily.equity.raw",
            "money_flow.order_size.daily.sector.raw",
            "money_flow.order_size.daily.market.raw",
        }:
            assert payload.get("schema") == "quant-v2.money-flow-daily.v1"
            scope = payload.get("scope")
            observations = payload.get("observations")
            assert isinstance(scope, dict)
            assert isinstance(observations, list) and observations
            if request.capability.endswith("equity.raw"):
                assert scope == {
                    "scopeType": "equity",
                    "exchange": parameters["exchange"],
                    "symbol": parameters["symbol"],
                }
            elif request.capability.endswith("sector.raw"):
                assert scope == {
                    "scopeType": "sector",
                    "scheme": parameters["scheme"],
                    "sectorCode": parameters["sectorCode"],
                    "name": parameters["sectorName"],
                }
            else:
                assert scope == {"scopeType": "market", "marketCode": parameters["marketCode"]}
            return
        assert payload.get("schema") == "quant-v2.money-flow-ranking.v1"
        assert payload.get("targetTradeDate") == parameters["targetDate"]
        assert payload.get("isComplete") is False
        items = payload.get("items")
        assert isinstance(items, list) and items
        expected_scope = "equity" if request.capability.endswith("equity.raw") else "sector"
        assert payload.get("scopeType") == expected_scope
        first_item = items[0]
        assert isinstance(first_item, dict)
        scope = first_item.get("scope")
        assert isinstance(scope, dict) and scope.get("scopeType") == expected_scope

    return validate


def _sector_bar_batch_validator(request: SourceRequest) -> Callable[[ProviderBatch], None]:
    """返回校验板块原生周期、分类体系和目录冻结身份的真实批次断言。"""
    parameters = dict(request.parameters)

    def validate(batch: ProviderBatch) -> None:
        """验证返回栏位来自所请求的板块及上游原生周期，而不是日线派生。"""
        payload = json.loads(batch.payload)
        assert isinstance(payload, dict)
        assert payload.get("schema") == "quant-v2.sector-bar.v1"
        assert payload.get("sectorScheme") == parameters["sectorScheme"]
        assert payload.get("sector") == parameters["sector"]
        assert payload.get("period") == parameters["period"]
        bars = payload.get("bars")
        assert isinstance(bars, list) and bars

    return validate


def _probe_currently_unsupported(
    adapter: _BatchFetcher,
    request: SourceRequest,
    *,
    reason_code: str,
) -> None:
    """验证无真源的请求失败关闭，并保留脱敏且可定位的失败证据。"""
    with pytest.raises(ProviderError) as captured:
        asyncio.run(adapter.fetch(request))
    assert captured.value.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED
    assert captured.value.retryable is False
    assert captured.value.failure_evidence is not None
    evidence = json.loads(captured.value.failure_evidence)
    assert evidence["reasonCode"] == reason_code
    assert len(evidence["request"]["requestFingerprint"]) == 64


def _assert_live_cni_catalog_raw_contract(batch: ProviderBatch) -> None:
    """验证真实国证目录仍是带冻结字段集的全字典 JSON 行。"""
    raw = json.loads(batch.raw_payload or b"")
    assert isinstance(raw, dict)
    data = raw.get("data")
    assert isinstance(data, dict)
    rows = data.get("rows")
    assert isinstance(rows, list)
    assert rows
    assert all(
        isinstance(record, dict)
        and frozenset(record) == frozenset(cnindex_index_snapshot._CNI_CATALOG_FIELDS)
        for record in rows
    )
    payload = json.loads(batch.payload)
    assert isinstance(payload, dict)
    normalized_records = payload.get("records")
    assert isinstance(normalized_records, list)
    codes = [record.get("indexCode") for record in normalized_records if isinstance(record, dict)]
    assert len(codes) == len(normalized_records) == len(rows)
    assert all(
        isinstance(code, str)
        and 6 <= len(code) <= 8
        and code.isascii()
        and code.isalnum()
        and code == code.upper()
        for code in codes
    )
    # 两种跨六码形状均为当前真实国证目录证据，消失或漂移时必须人工复核。
    assert {"AITCNYG", "39926401"} <= set(codes)


def _assert_live_csi_catalog_alphanumeric_identity(batch: ProviderBatch) -> None:
    """验证中证目录完整保留实证字母数字代码，而非过滤为旧纯数字子集。"""
    payload = json.loads(batch.payload)
    assert isinstance(payload, dict)
    normalized_records = payload.get("records")
    assert isinstance(normalized_records, list)
    raw = json.loads(batch.raw_payload or b"")
    assert isinstance(raw, dict)
    raw_records = raw.get("records")
    assert isinstance(raw_records, list)
    codes = [record.get("indexCode") for record in normalized_records if isinstance(record, dict)]
    assert len(codes) == len(normalized_records) == len(raw_records)
    assert all(
        isinstance(code, str)
        and len(code) == 6
        and code.isascii()
        and code.isalnum()
        and code == code.upper()
        for code in codes
    )
    # `H00999` 是中证当前真实目录的稳定实证样例；消失或改形状应触发人工复核，而非静默过滤。
    assert "H00999" in codes


def _catalog_sector_code(
    adapter: _BatchFetcher,
    *,
    scheme: str,
    observation_date: date,
) -> str:
    """从真实同批目录选择一个板块代码，避免把过时样例代码伪装成当前可用身份。"""
    payload = _probe(
        adapter,
        _request(
            "sector.catalog.raw",
            sectorScheme=scheme,
            observationDate=observation_date.isoformat(),
        ),
        label=f"sector catalog {scheme}",
    )
    sectors = payload.get("sectors")
    assert isinstance(sectors, list) and sectors, f"sector catalog {scheme} is empty"
    first = sectors[0]
    assert isinstance(first, dict) and isinstance(first.get("code"), str)
    return first["code"]


def _catalog_sector_identity(
    adapter: _BatchFetcher,
    *,
    scheme: str,
    observation_date: date,
) -> tuple[str, str]:
    """从真实目录取得同一板块代码和名称，避免资金流请求拼接失配身份。"""
    payload = _probe(
        adapter,
        _request(
            "sector.catalog.raw",
            sectorScheme=scheme,
            observationDate=observation_date.isoformat(),
        ),
        label=f"sector catalog identity {scheme}",
    )
    sectors = payload.get("sectors")
    assert isinstance(sectors, list) and sectors, f"sector catalog {scheme} is empty"
    first = sectors[0]
    assert isinstance(first, dict)
    code = first.get("code")
    name = first.get("name")
    assert isinstance(code, str) and code
    assert isinstance(name, str) and name
    return code, name


def _catalog_equity_instrument(
    adapter: _BatchFetcher,
    *,
    exchange: str,
    observation_date: date,
) -> str:
    """从当前东财目录解析一个有效证券，禁止把历史样例当作仍可用身份。"""
    payload = _probe(
        adapter,
        _request(
            "equity.master.catalog",
            exchange=exchange,
            targetDate=observation_date.isoformat(),
        ),
        label=f"EastMoney equity catalog {exchange}",
    )
    entries = payload.get("entries")
    assert isinstance(entries, list) and entries, f"equity catalog {exchange} is empty"
    first = entries[0]
    assert isinstance(first, dict)
    symbol = first.get("symbol")
    assert isinstance(symbol, str) and len(symbol) == 6 and symbol.isdigit()
    return f"{exchange}.{symbol}"


@pytest.mark.parametrize("exchange", ("SSE", "SZSE", "BSE"))
def test_live_equity_lifecycle_adapter(exchange: str) -> None:
    """逐所独立探测显式生命周期，任一交易所失败不遮蔽其它个股来源。"""
    _require_batch("equity")
    today = _shanghai_today()
    _probe(
        AkshareExchangeEquityLifecycleAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS),
        _request("equity.lifecycle.explicit", exchange=exchange, targetDate=today.isoformat()),
        label=f"equity lifecycle {exchange}",
    )


def test_live_equity_market_adapters() -> None:
    """探测目录、日周月、因子、公司行动和概况；生命周期另行独立报告。"""
    _require_batch("equity")
    today = _shanghai_today()
    end = _latest_weekday(today)
    start = end - timedelta(days=60)
    factor_start = end - timedelta(days=365 * 5)
    statuses: list[str] = []
    catalog = AkshareEastmoneyEquityCatalogAdapter(request_timeout_seconds=600)
    catalog_instruments: dict[str, str] = {}
    # 三所目录各自留痕；SZSE/BSE 失败不能阻止已从 SSE 解析出的个股来源继续验证。
    for exchange in ("SSE", "SZSE", "BSE"):
        try:
            catalog_instruments[exchange] = _catalog_equity_instrument(
                catalog,
                exchange=exchange,
                observation_date=today,
            )
        except ProviderError as error:
            statuses.append(
                f"FAILED EastMoney equity catalog {exchange}: "
                f"code={error.code.value}, retryable={error.retryable}, detail={error}"
            )
        except Exception as error:
            statuses.append(
                f"FAILED EastMoney equity catalog {exchange}: "
                f"type={type(error).__name__}, detail={error}"
            )
        else:
            statuses.append(f"SUCCEEDED EastMoney equity catalog {exchange}")

    equity = catalog_instruments.get("SSE")
    if equity is None:
        # 后续来源只能使用当前目录确认的证券；目录不可用时明确记录未执行而非猜测样本。
        statuses.extend(
            (
                "UNEXECUTED Tencent equity daily bars: current SSE catalog identity unavailable",
                "UNEXECUTED EastMoney equity 1w bars: current SSE catalog identity unavailable",
                "UNEXECUTED EastMoney equity 1mo bars: current SSE catalog identity unavailable",
                "UNEXECUTED Sina adjustment factors: current SSE catalog identity unavailable",
                "UNEXECUTED EastMoney corporate actions: current SSE catalog identity unavailable",
                "UNEXECUTED CNINFO company profile: current SSE catalog identity unavailable",
            )
        )
    else:
        daily_request = _request(
            "equity.bar.1d.raw", instrument=equity, start=start.isoformat(), end=end.isoformat()
        )
        _record_probe_status(
            statuses,
            adapter=AkshareTencentDailyBarsAdapter(
                request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
            ),
            request=daily_request,
            label="Tencent equity daily bars",
            batch_validator=_non_empty_normalized_array_validator(
                daily_request,
                schema="quant-v2.equity-daily-bar.v1",
                array_key="bars",
            ),
        )
        period = AkshareEastmoneyEquityPeriodBarsAdapter(
            request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
        )
        for capability, value in (("equity.bar.1w.raw", "1w"), ("equity.bar.1mo.raw", "1mo")):
            period_request = _request(
                capability,
                instrument=equity,
                period=value,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            _record_probe_status(
                statuses,
                adapter=period,
                request=period_request,
                label=f"EastMoney equity {value} bars",
                batch_validator=_non_empty_normalized_array_validator(
                    period_request,
                    schema="quant-v2.equity-period-bar.v1",
                    array_key="bars",
                ),
            )
        factor_request = _request(
            "equity.adjustment_factor",
            instrument=equity,
            start=factor_start.isoformat(),
            end=end.isoformat(),
        )
        _record_probe_status(
            statuses,
            adapter=AkshareSinaAdjustmentFactorsAdapter(
                request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
            ),
            request=factor_request,
            label="Sina adjustment factors",
            batch_validator=_non_empty_normalized_array_validator(
                factor_request,
                schema="quant-v2.equity-adjustment-factor.v1",
                array_key="factors",
            ),
        )
        action_request = _request(
            "equity.corporate_action",
            instrument=equity,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        _record_probe_status(
            statuses,
            adapter=AkshareEastmoneyCorporateActionsAdapter(
                request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
            ),
            request=action_request,
            label="EastMoney corporate actions",
            batch_validator=_corporate_action_batch_validator(action_request),
        )
        profile_request = _request("equity.profile", instrument=equity)
        _record_probe_status(
            statuses,
            adapter=AkshareCninfoCompanyProfileAdapter(
                request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
            ),
            request=profile_request,
            label="CNINFO company profile",
            batch_validator=_company_profile_batch_validator(profile_request),
        )
    _fail_if_probe_batch_incomplete(batch="equity", statuses=statuses)


def test_live_p0_etf_and_margin_adapters() -> None:
    """探测 P0 ETF、两融真源及显式不支持边界，不把伪空当成成功。"""
    _require_batch("p0-etf-margin")
    today = _shanghai_today()
    end = _latest_weekday(today)
    start = end - timedelta(days=14)
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS)

    for venue in ("SSE", "SZSE"):
        _probe(
            adapter,
            _request("fund.etf.master", venue=venue, observationDate=today.isoformat()),
            label=f"P0 ETF master {venue}",
        )
    etf = "SSE.510300"
    for capability in ("fund.etf.trading_state", "fund.etf.nav.1d.reported"):
        _probe(
            adapter,
            _request(capability, etf=etf, start=start.isoformat(), end=end.isoformat()),
            label=f"P0 {capability}",
        )
    _probe(
        adapter,
        _request(
            "fund.etf.bar.1d.raw",
            etf=etf,
            start=start.isoformat(),
            end=end.isoformat(),
            priceBasis="UNADJUSTED",
        ),
        label="P0 ETF Tencent bars",
    )
    for capability in ("market.margin.market.1d.reported", "market.margin.security.1d.reported"):
        for venue in ("SSE", "SZSE"):
            _probe(
                adapter,
                _request(capability, venue=venue, start=end.isoformat(), end=end.isoformat()),
                label=f"P0 {capability} {venue}",
            )
    _probe(
        adapter,
        _request(
            "market.margin.eligibility.reported",
            venue="SZSE",
            start=end.isoformat(),
            end=end.isoformat(),
        ),
        label="P0 margin eligibility SZSE",
    )
    bse_payload = _probe(
        adapter,
        _request(
            "market.margin.eligibility.reported",
            venue="BSE",
            start=end.isoformat(),
            end=end.isoformat(),
        ),
        label="P0 margin eligibility BSE",
    )
    bse_records = bse_payload.get("records")
    assert isinstance(bse_records, list) and bse_records
    assert all(record.get("evidenceBasis") == "OBSERVED_LIST" for record in bse_records)
    assert all(record.get("effectiveFrom") == end.isoformat() for record in bse_records)
    assert all(
        record.get("status") in {"ELIGIBLE", "FINANCING_ONLY", "LENDING_ONLY", "INELIGIBLE"}
        for record in bse_records
    )
    _probe_currently_unsupported(
        adapter,
        _request(
            "market.margin.eligibility.reported",
            venue="SSE",
            start=end.isoformat(),
            end=end.isoformat(),
        ),
        reason_code="SSE_MARGIN_ELIGIBILITY_NO_UNDERLYING_ENDPOINT",
    )
    _probe_currently_unsupported(
        adapter,
        _request(
            "market.margin.market.1d.reported",
            venue="BSE",
            start=end.isoformat(),
            end=end.isoformat(),
        ),
        reason_code="BSE_MARGIN_MARKET_NOT_MAPPED",
    )
    _probe_currently_unsupported(
        adapter,
        _request(
            "market.margin.security.1d.reported",
            venue="BSE",
            start=end.isoformat(),
            end=end.isoformat(),
        ),
        reason_code="BSE_MARGIN_SECURITY_NOT_MAPPED",
    )
    _probe_currently_unsupported(
        adapter,
        _request(
            "market.stock_connect.active_security.snapshot",
            channel="SH",
            direction="NORTHBOUND",
            start=end.isoformat(),
            end=end.isoformat(),
        ),
        reason_code="NO_VERIFIED_ACTIVE_SECURITY_SOURCE",
    )


def test_live_p0_event_and_market_adapters() -> None:
    """探测 P0 港通、业绩、交易事件、衍生品、停牌、股本及申万成分实际接口。"""
    _require_batch("p0-events")
    today = _shanghai_today()
    end = _latest_weekday(today)
    start = end - timedelta(days=1)
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS)

    for channel, direction in (
        ("SH", "NORTHBOUND"),
        ("SZ", "NORTHBOUND"),
        ("SH", "SOUTHBOUND"),
        ("SZ", "SOUTHBOUND"),
    ):
        _probe(
            adapter,
            _request(
                "market.stock_connect.market_stat.reported",
                channel=channel,
                direction=direction,
                start=start.isoformat(),
                end=end.isoformat(),
            ),
            label=f"P0 stock connect {channel} {direction}",
        )
    for capability in (
        "corporate.disclosure.earnings.p0",
        "market.dragon_tiger.disclosure.1d",
        "market.block_trade.execution.1d",
    ):
        _probe(
            adapter,
            _request(
                capability,
                instrument="SSE.600519",
                start=start.isoformat(),
                end=end.isoformat(),
            ),
            label=f"P0 {capability}",
        )
    year_month = f"{today.year % 100:02d}{today.month:02d}"
    _probe(
        adapter,
        _request(
            "derivative.bar.1d.reported",
            contract=f"CFFEX.IF{year_month}",
            start=(end - timedelta(days=14)).isoformat(),
            end=end.isoformat(),
        ),
        label="P0 EastMoney derivative bars",
    )
    _probe(
        adapter,
        _request("equity.trading_status.1d", observationDate=end.isoformat()),
        label="P0 EastMoney equity trading status",
    )
    _probe(
        adapter,
        _request("equity.share_capital.reported", instrument="SSE.600519"),
        label="P0 EastMoney equity share capital",
    )
    _probe(
        adapter,
        _request(
            "sector.sw2021.membership.snapshot",
            nodeCode="801010",
            observationDate=today.isoformat(),
        ),
        label="P0 Legulegu SW membership",
    )


def test_live_financial_adapters() -> None:
    """探测东财三表、供应商指标与历史估值三项独立财务能力。"""
    _require_batch("financial")
    adapter = AkshareEastmoneyFinancialAdapter(
        request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
        max_concurrency=1,
        requests_per_minute=20,
    )
    for capability in (
        "financial.statement.raw",
        "financial.metric.raw",
        "financial.valuation.raw",
    ):
        _probe(
            adapter,
            _request(capability, exchange="SSE", symbol="600519"),
            label=f"EastMoney {capability}",
        )


def test_live_money_flow_adapters() -> None:
    """逐项探测八个资金流接口，首项失败也必须留下同批其余接口的真实状态。"""
    _require_batch("money-flow")
    today = _shanghai_today()
    statuses: list[str] = []
    eastmoney = AkshareEastmoneyMoneyFlowAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS)
    sector_catalog = AkshareEastmoneySectorBarsAdapter(
        request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
    )
    try:
        sector_code, sector_name = _catalog_sector_identity(
            sector_catalog,
            scheme="eastmoney.industry",
            observation_date=today,
        )
    except ProviderError as error:
        sector_code = None
        sector_name = None
        statuses.append(
            "FAILED prerequisite EastMoney sector catalog: "
            f"code={error.code.value}, retryable={error.retryable}, detail={error}"
        )
    except Exception as error:
        sector_code = None
        sector_name = None
        statuses.append(
            "FAILED prerequisite EastMoney sector catalog: "
            f"type={type(error).__name__}, detail={error}"
        )
    else:
        statuses.append("SUCCEEDED prerequisite EastMoney sector catalog")

    for capability, parameters in (
        (
            "money_flow.order_size.daily.equity.raw",
            {"exchange": "SSE", "symbol": "600519"},
        ),
        ("money_flow.order_size.daily.market.raw", {"marketCode": "cn-a"}),
        (
            "money_flow.order_size.ranking.equity.raw",
            {"indicator": "今日", "targetDate": today.isoformat()},
        ),
        (
            "money_flow.order_size.ranking.sector.raw",
            {
                "indicator": "今日",
                "sectorType": "行业资金流",
                "targetDate": today.isoformat(),
            },
        ),
    ):
        request = _request(capability, **parameters)
        _record_probe_status(
            statuses,
            adapter=eastmoney,
            request=request,
            label=f"EastMoney {capability}",
            batch_validator=_money_flow_batch_validator(request),
        )
    if sector_code is None or sector_name is None:
        statuses.append(
            "UNEXECUTED EastMoney money_flow.order_size.daily.sector.raw: "
            "current sector catalog identity was unavailable"
        )
    else:
        sector_request = _request(
            "money_flow.order_size.daily.sector.raw",
            scheme="eastmoney.industry",
            sectorCode=sector_code,
            sectorName=sector_name,
        )
        _record_probe_status(
            statuses,
            adapter=eastmoney,
            request=sector_request,
            label="EastMoney money_flow.order_size.daily.sector.raw",
            batch_validator=_money_flow_batch_validator(sector_request),
        )
    # 同花顺个股 SDK 逐页扫描完整排行；稳定验收使用已经实测成功的三日滚动窗口。
    ths = AkshareThsMoneyFlowAdapter(request_timeout_seconds=150)
    ths_target_date = _latest_weekday(today)
    for capability, indicator in (
        ("money_flow.trade_direction.ranking.equity.raw", "3日排行"),
        ("money_flow.trade_direction.ranking.industry.raw", "即时"),
        ("money_flow.trade_direction.ranking.concept.raw", "即时"),
    ):
        request = _request(
            capability,
            indicator=indicator,
            targetDate=ths_target_date.isoformat(),
        )
        _record_probe_status(
            statuses,
            adapter=ths,
            request=request,
            label=f"THS {capability} {indicator}",
            batch_validator=_money_flow_batch_validator(request),
        )
    _fail_if_probe_batch_incomplete(batch="money-flow", statuses=statuses)


def test_live_ths_equity_instant_state_boundary() -> None:
    """记录同花顺个股即时排行的时段状态；它不能替代已验证的滚动窗口验收。"""
    _require_batch("money-flow")
    today = _shanghai_today()
    request = _request(
        "money_flow.trade_direction.ranking.equity.raw",
        indicator="即时",
        targetDate=_latest_weekday(today).isoformat(),
    )
    statuses: list[str] = []
    _record_probe_status(
        statuses,
        adapter=AkshareThsMoneyFlowAdapter(request_timeout_seconds=150),
        request=request,
        label="THS money_flow.trade_direction.ranking.equity.raw 即时",
        batch_validator=_money_flow_batch_validator(request),
    )

    assert len(statuses) == 1
    status = statuses[0]
    if status.startswith("SUCCEEDED"):
        return
    assert status.startswith(
        "FAILED THS money_flow.trade_direction.ranking.equity.raw 即时: "
        "code=unavailable, retryable=True"
    )


def test_live_index_adapters() -> None:
    """探测中证与国证各自目录、成分和权重快照接口。"""
    _require_batch("index")
    for administrator, adapter, code in (
        (
            "CSI",
            AkshareCsindexIndexSnapshotAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS),
            "000300",
        ),
        (
            "CNI",
            AkshareCnindexIndexSnapshotAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS),
            "399001",
        ),
    ):
        _probe(
            adapter,
            _request("index.catalog.snapshot", administrator=administrator),
            label=f"{administrator} index catalog",
            batch_validator=(
                _assert_live_csi_catalog_alphanumeric_identity
                if administrator == "CSI"
                else _assert_live_cni_catalog_raw_contract
            ),
        )
        for capability in ("index.constituent.snapshot", "index.weight.snapshot"):
            _probe(
                adapter,
                _request(capability, administrator=administrator, indexCode=code),
                label=f"{administrator} {capability}",
            )


def test_live_sector_adapters() -> None:
    """逐项探测行业、概念的六个原生周期接口，失败时不遮蔽同批其它周期。"""
    _require_batch("sector")
    today = _shanghai_today()
    end = _latest_weekday(today)
    start = end - timedelta(days=60)
    statuses: list[str] = []
    sector_codes: dict[str, str] = {}
    bars = AkshareEastmoneySectorBarsAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS)
    for scheme in ("eastmoney.industry", "eastmoney.concept"):
        try:
            code = _catalog_sector_code(bars, scheme=scheme, observation_date=today)
        except ProviderError as error:
            statuses.append(
                f"FAILED prerequisite EastMoney {scheme} catalog: "
                f"code={error.code.value}, retryable={error.retryable}, detail={error}"
            )
            for _capability, period in (
                ("sector.bar.1d.raw", "1d"),
                ("sector.bar.1w.raw", "1w"),
                ("sector.bar.1mo.raw", "1mo"),
            ):
                statuses.append(
                    f"UNEXECUTED EastMoney {scheme} {period} bars: "
                    "current catalog identity unavailable"
                )
            continue
        except Exception as error:
            statuses.append(
                f"FAILED prerequisite EastMoney {scheme} catalog: "
                f"type={type(error).__name__}, detail={error}"
            )
            for _capability, period in (
                ("sector.bar.1d.raw", "1d"),
                ("sector.bar.1w.raw", "1w"),
                ("sector.bar.1mo.raw", "1mo"),
            ):
                statuses.append(
                    f"UNEXECUTED EastMoney {scheme} {period} bars: "
                    "current catalog identity unavailable"
                )
            continue
        statuses.append(f"SUCCEEDED prerequisite EastMoney {scheme} catalog")
        sector_codes[scheme] = code
        for capability, period in (
            ("sector.bar.1d.raw", "1d"),
            ("sector.bar.1w.raw", "1w"),
            ("sector.bar.1mo.raw", "1mo"),
        ):
            request = _request(
                capability,
                sectorScheme=scheme,
                sector=code,
                period=period,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            _record_probe_status(
                statuses,
                adapter=bars,
                request=request,
                label=f"EastMoney {scheme} {period} bars",
                batch_validator=_sector_bar_batch_validator(request),
            )
    eod = AkshareEastmoneySectorEodAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS)
    membership = AkshareEastmoneySectorMembershipAdapter(
        request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
    )
    for scheme in ("eastmoney.industry", "eastmoney.concept"):
        eod_request = _request(
            "sector.quote.eod.snapshot.raw", sectorScheme=scheme, tradeDate=today.isoformat()
        )
        _record_probe_status(
            statuses,
            adapter=eod,
            request=eod_request,
            label=f"EastMoney {scheme} EOD snapshot",
        )
        code = sector_codes.get(scheme)
        if code is None:
            statuses.append(
                f"UNEXECUTED EastMoney {scheme} membership: current catalog identity unavailable"
            )
            continue
        membership_request = _request(
            "sector.membership.snapshot.raw",
            sectorScheme=scheme,
            sector=code,
            observationDate=today.isoformat(),
        )
        _record_probe_status(
            statuses,
            adapter=membership,
            request=membership_request,
            label=f"EastMoney {scheme} membership",
        )
    sw_adapter = AkshareSwIndustrySnapshotAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS)
    sw_request = _request("sector.sw.snapshot.raw", snapshotDate=today.isoformat())
    _record_probe_status(
        statuses,
        adapter=sw_adapter,
        request=sw_request,
        label="Legulegu SW industry snapshot",
    )
    _fail_if_probe_batch_incomplete(batch="sector", statuses=statuses)
