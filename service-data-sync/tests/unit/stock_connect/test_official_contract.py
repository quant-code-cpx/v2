"""互联互通官方交付 profile、时间语义和预检边界测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    ProviderPreflightRequest,
    SourceRequest,
)
from service_data_sync.application.stock_connect.bundle_sync import (
    _source_ref,
    _status_quality_issues,
    decode_stock_connect_channel_status_batch,
    decode_stock_connect_instrument_master_batch,
)
from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.entrypoints.stock_connect_manifests import main as manifest_cli_main
from service_data_sync.infrastructure.providers.official.stock_connect import (
    CHANNEL_STATUS_CAPABILITY,
    OfficialStockConnectAdapter,
    OfficialStockConnectConfig,
    _available_readiness_evidence,
    _daily_statistics_regime,
    _FetchedObject,
    _load_calendar_manifest,
    _load_master_fixed_length_profile,
    _load_status_coverage_manifest,
    _market_record,
    _preflight_scope,
    _StockConnectProbeDates,
    _unavailable_readiness_evidence,
    calculate_stock_connect_sftp_manifest_root,
    parse_hkex_securities_master,
    parse_stock_connect_status,
)


def test_status_coverage_manifest_rejects_future_required_from(tmp_path: Path) -> None:
    """状态 coverage 不能以未来日期首次启用，从而把当前缺件伪装成历史缺源。"""
    manifest_path = tmp_path / "future-status-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifestSchema": "quant-v2.stock-connect-status-coverage-manifest.v1",
                "requiredFrom": "2100-01-01",
                "observedAt": "2026-07-30T00:00:00Z",
                "entries": [],
            }
        )
    )

    with pytest.raises(ValueError, match="cannot be in the future"):
        _load_status_coverage_manifest(
            manifest_path,
            expected_required_from=date(2100, 1, 1),
            max_bytes=16_384,
        )


def _profile_manifest() -> dict[str, object]:
    """构造可被摘要钉住的最小 fixed-length licensed profile。"""
    return {
        "manifestSchema": ("quant-v2.hkex-securities-master-fixed-length-profile-set.v2"),
        "profiles": [
            {
                "effectiveFrom": "2014-11-17",
                "effectiveTo": None,
                "profileId": "licensed-test-v1",
                "specificationReference": "licensed-specification-test",
                "recordLengthBytes": 40,
                "lineEnding": "LF",
                "fileName": {
                    "pattern": r"MS_(?P<issued>\d{8})\.dat",
                    "issuedDateGroup": "issued",
                    "issuedDateFormat": "%Y%m%d",
                    "dateRole": "ISSUED_DATE",
                },
                "dataRecord": {
                    "selector": None,
                    "securityId": {
                        "offsetBytes": 0,
                        "lengthBytes": 8,
                        "encoding": "ascii",
                        "trim": "RIGHT",
                    },
                    "instrumentCode": {
                        "offsetBytes": 8,
                        "lengthBytes": 5,
                        "encoding": "ascii",
                        "trim": "RIGHT",
                    },
                    "displayName": {
                        "offsetBytes": 13,
                        "lengthBytes": 12,
                        "encoding": "ascii",
                        "trim": "RIGHT",
                    },
                    "effectiveTradeDate": {
                        "offsetBytes": 25,
                        "lengthBytes": 8,
                        "encoding": "ascii",
                        "trim": "NONE",
                        "dateFormat": "%Y%m%d",
                    },
                },
            },
        ],
    }


def _fixed_record() -> bytes:
    """构造一条长度精确且同时携带 T+1 生效日的主档记录。"""
    record = bytearray(b" " * 40)
    record[0:8] = b"SID00700"
    record[8:13] = b"00700"
    record[13:25] = b"TENCENT     "
    record[25:33] = b"20260729"
    return bytes(record) + b"\n"


def _omdc_frame(
    channel: str = "SH",
    trade_date: date | None = None,
) -> bytes:
    """构造连续 sequence 的单条 OMD-C MMDH Msg80 日终额度帧。"""
    business_date = trade_date or date(2026, 7, 29)
    observed = datetime.combine(
        business_date,
        datetime.min.time(),
        tzinfo=UTC,
    ) + timedelta(hours=8)
    observed_ns = int(observed.timestamp() * 1_000_000_000)
    send_ns = observed_ns + 1_000
    message = struct.pack(
        "<HH2s2sqQ",
        24,
        80,
        channel.encode("ascii"),
        b"NB",
        123_000,
        observed_ns,
    )
    return struct.pack("<H2sIIQ", 44, b"\x00\x00", 1, 1, send_ns) + message


def _calendar_payload(
    year: int,
    *,
    closed_dates: frozenset[date] = frozenset(),
) -> bytes:
    """构造覆盖完整年度、并可明确指定休市日的官方日历 CSV 测试交付。"""
    rows = ["Date,Northbound Trading,Southbound Trading,Hong Kong,Shanghai & Shenzhen"]
    current = date(year, 1, 1)
    while current.year == year:
        closed = current in closed_dates
        trading_state = "Closed" if closed else ""
        market_state = "CLOSED" if closed else "OPEN"
        rows.append(
            f"{current.isoformat()},{trading_state},{trading_state},{market_state},{market_state}"
        )
        current += timedelta(days=1)
    return ("\n".join(rows) + "\n").encode()


def _official_adapter(
    tmp_path: Path,
    *,
    calendar_years: tuple[int, ...] = (2025, 2026),
    local_calendar_years: frozenset[int] = frozenset(),
    closed_calendar_dates: frozenset[date] = frozenset(),
    sftp_entitlements: tuple[dict[str, str], ...] = (),
) -> OfficialStockConnectAdapter:
    """构造使用真实本地 landing 和受控 HTTPS reader 的官方 adapter。"""
    key_path = tmp_path / "hkex.key"
    known_hosts_path = tmp_path / "known_hosts"
    status_root = tmp_path / "status"
    status_root.mkdir()
    key_path.write_text("test-key")
    known_hosts_path.write_text("sftp.data.hkex.com.hk ssh-ed25519 AAAATEST")
    manifest_bytes = json.dumps(
        _profile_manifest(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_path = tmp_path / "profile.json"
    manifest_path.write_bytes(manifest_bytes)
    calendar_entries = []
    for year in calendar_years:
        relative_path: str | None = None
        source_kind = "HTTPS_TEMPLATE" if year == datetime.now(UTC).year else "HTTPS_OBJECT"
        url: str | None = (
            None
            if source_kind == "HTTPS_TEMPLATE"
            else f"https://www.hkex.com.hk/official-test/{year}.csv"
        )
        if year in local_calendar_years:
            source_kind = "LOCAL_ARCHIVE"
            url = None
            relative_path = f"calendars/{year}.csv"
            local_path = tmp_path / relative_path
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(_calendar_payload(year, closed_dates=closed_calendar_dates))
        calendar_entries.append(
            {
                "year": year,
                "sourceKind": source_kind,
                "url": url,
                "relativePath": relative_path,
                "sha256": hashlib.sha256(
                    _calendar_payload(year, closed_dates=closed_calendar_dates)
                ).hexdigest(),
                "sourcePublicationAt": "2026-01-01T00:00:00Z",
                "observedAt": "2026-01-02T00:00:00Z",
            }
        )
    calendar_manifest_path = tmp_path / "calendar-manifest.json"
    calendar_manifest_path.write_text(
        json.dumps(
            {
                "manifestSchema": ("quant-v2.hkex-stock-connect-calendar-manifest.v1"),
                "entries": calendar_entries,
            }
        )
    )
    sftp_page = {
        "pageSchema": "quant-v2.hkex-sftp-delivery-manifest-page.v1",
        "pageNo": 0,
        "entries": list(sftp_entitlements),
    }
    sftp_page_bytes = json.dumps(
        sftp_page,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    sftp_page_path = tmp_path / "sftp-deliveries-0000.json"
    sftp_page_path.write_bytes(sftp_page_bytes)
    sftp_pages = [
        {
            "pageNo": 0,
            "relativePath": sftp_page_path.name,
            "sha256": hashlib.sha256(sftp_page_bytes).hexdigest(),
        }
    ]
    sftp_root_hash = hashlib.sha256(
        json.dumps(
            {"pages": sftp_pages},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    sftp_manifest_path = tmp_path / "sftp-manifest.json"
    sftp_manifest_path.write_text(
        json.dumps(
            {
                "manifestSchema": "quant-v2.hkex-sftp-delivery-manifest.v1",
                "rootHash": sftp_root_hash,
                "pages": sftp_pages,
            }
        )
    )
    status_entries = []
    for business_date in (
        date(2026, 7, 27),
        date(2026, 7, 28),
        date(2026, 7, 29),
    ):
        for channel in ("SH", "SZ"):
            payload = _omdc_frame(channel, business_date)
            sidecar = _omdc_sidecar(channel, business_date, payload=payload)
            status_entries.append(
                {
                    "tradeDate": business_date.isoformat(),
                    "channel": channel,
                    "direction": "NORTHBOUND",
                    "relativePath": (
                        f"hkex-omdc/{channel}/{business_date.year}/{business_date:%Y%m%d}.bin"
                    ),
                    "profileId": "hkex-omdc-mmdh-msg80-v2.1",
                    "payloadSha256": hashlib.sha256(payload).hexdigest(),
                    "sidecarSha256": hashlib.sha256(sidecar).hexdigest(),
                }
            )
    status_manifest_path = tmp_path / "status-manifest.json"
    status_manifest_path.write_text(
        json.dumps(
            {
                "manifestSchema": ("quant-v2.stock-connect-status-coverage-manifest.v1"),
                "requiredFrom": "2026-07-01",
                "observedAt": "2026-07-29T09:00:00Z",
                "entries": status_entries,
            }
        )
    )

    def read_calendar(url: str) -> _FetchedObject:
        """按 URL 年份返回带官方 publication 元数据的年度日历。"""
        year = int(url.rsplit("/", maxsplit=1)[-1].split(".", maxsplit=1)[0])
        return _FetchedObject(
            payload=_calendar_payload(year, closed_dates=closed_calendar_dates),
            content_type="text/csv",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            product_name=f"{year}.csv",
            upstream_source="HKEX_CALENDAR",
        )

    def fixed_now() -> datetime:
        """返回与测试业务日一致的来源观察时间。"""
        return datetime(2026, 7, 29, 9, tzinfo=UTC)

    return OfficialStockConnectAdapter(
        OfficialStockConnectConfig(
            sftp_host="sftp.data.hkex.com.hk",
            sftp_port=22,
            sftp_username="licensed@example.com",
            sftp_private_key_path=key_path,
            sftp_private_key_passphrase=None,
            sftp_known_hosts_path=known_hosts_path,
            sh_daily_path_template="/daily/sh/{trade_date}.csv",
            sz_daily_path_template="/daily/sz/{trade_date}.csv",
            securities_master_path_template="/master/MS_{issued_date}.dat",
            securities_master_profile_manifest_path=manifest_path,
            securities_master_profile_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            calendar_url_template=("https://www.hkex.com.hk/official-test/{year}.csv"),
            calendar_manifest_path=calendar_manifest_path,
            sftp_delivery_manifest_path=sftp_manifest_path,
            status_delivery_root=status_root,
            status_manifest_path=status_manifest_path,
            status_required_from=date(2026, 7, 1),
            omdc_status_path_template=("hkex-omdc/{channel}/{year}/{trade_date}.bin"),
            sse_status_path_template=("sse-mdgw/{year}/{trade_date}/trdses04.csv"),
            szse_status_path_template=("szse-step/{year}/{trade_date}/390019.csv"),
            request_timeout_seconds=5,
            preflight_timeout_seconds=10,
            min_partitions_per_minute=20,
            delivery_expiry_safety_seconds=3_600,
            max_delivery_bytes=1_048_576,
            max_manifest_bytes=16_384,
            max_zip_compression_ratio=100,
        ),
        now=fixed_now,
        https_reader=read_calendar,
    )


def test_manifest_cli_validates_all_four_local_contracts_without_runtime_dependencies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """统一 CLI 必须离线完成 exact schema、摘要、分页 root 与边界校验。"""
    _official_adapter(tmp_path)
    profile_path = tmp_path / "profile.json"
    exit_code = manifest_cli_main(
        [
            "validate-all",
            "--calendar",
            str(tmp_path / "calendar-manifest.json"),
            "--sftp",
            str(tmp_path / "sftp-manifest.json"),
            "--status",
            str(tmp_path / "status-manifest.json"),
            "--status-required-from",
            "2026-07-01",
            "--master",
            str(profile_path),
            "--master-sha256",
            hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert result["ok"] is True
    assert [item["kind"] for item in result["manifests"]] == [
        "calendar",
        "sftp-delivery",
        "status-coverage",
        "securities-master-profile",
    ]


def test_manifest_cli_calculates_sftp_root_and_redacts_validation_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """root 计算只返回 canonical 摘要，失败输出不得泄露页面路径或订单引用。"""
    _official_adapter(tmp_path)
    manifest_path = tmp_path / "sftp-manifest.json"
    calculated = calculate_stock_connect_sftp_manifest_root(
        manifest_path,
        max_bytes=256 * 1024,
    )
    header = json.loads(manifest_path.read_text())
    assert calculated == header["rootHash"]

    page_path = tmp_path / "sftp-deliveries-0000.json"
    page_path.write_text('{"secretOrderReference":"must-not-leak"}')
    exit_code = manifest_cli_main(
        [
            "validate-sftp",
            "--path",
            str(manifest_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "ok": False,
        "command": "validate-sftp",
        "errorCode": "MANIFEST_INVALID",
    }


def _write_omdc_landing(
    root: Path,
    channel: str,
    trade_date: date | None = None,
) -> None:
    """写入一个通道独占的 Msg80 文件和 END_OF_DAY_FINAL sidecar。"""
    business_date = trade_date or date(2026, 7, 29)
    path = root / f"hkex-omdc/{channel}/{business_date.year}/{business_date:%Y%m%d}.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _omdc_frame(channel, business_date)
    path.write_bytes(payload)
    path.with_suffix(".bin.manifest.json").write_bytes(
        _omdc_sidecar(channel, business_date, payload=payload)
    )


def _omdc_sidecar(channel: str, business_date: date, *, payload: bytes) -> bytes:
    """构造与状态覆盖清单摘要一致的最终性 sidecar。"""
    return json.dumps(
        {
            "producer": "HKEX_OMDC_CAPTURE",
            "sourceProfile": "HKEX_OMDC_MMDH_MSG80_V2.1",
            "projectionProfile": ("quant-v2.stock-connect-status-raw-final.v1"),
            "messageType": "80",
            "marketId": "HKEX",
            "businessDate": business_date.isoformat(),
            "channel": channel,
            "direction": "NORTHBOUND",
            "finality": "END_OF_DAY_FINAL",
            "payloadSha256": hashlib.sha256(payload).hexdigest(),
            "sequence": 1,
        }
    ).encode()


def test_fixed_length_master_separates_issued_and_effective_dates(
    tmp_path: Path,
) -> None:
    """主档文件名 issued date 与记录 effective date 必须分别精确匹配。"""
    manifest_bytes = json.dumps(
        _profile_manifest(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_path = tmp_path / "profile.json"
    manifest_path.write_bytes(manifest_bytes)
    profile_set = _load_master_fixed_length_profile(
        manifest_path,
        expected_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        max_bytes=64 * 1024,
    )
    profile = profile_set.select(date(2026, 7, 29))

    records = parse_hkex_securities_master(
        _fixed_record(),
        effective_trade_date=date(2026, 7, 29),
        expected_issued_date=date(2026, 7, 28),
        product_name="MS_20260728.dat",
        profile="hkex-securities-master-fixed-length-manifest-v2",
        layout=profile,
    )

    assert records == [
        {
            "securityId": "SID00700",
            "instrumentCode": "00700",
            "displayName": "TENCENT",
            "effectiveFrom": "2026-07-29",
        }
    ]
    with pytest.raises(ValueError, match="issued date"):
        parse_hkex_securities_master(
            _fixed_record(),
            effective_trade_date=date(2026, 7, 29),
            expected_issued_date=date(2026, 7, 27),
            product_name="MS_20260728.dat",
            profile="hkex-securities-master-fixed-length-manifest-v2",
            layout=profile,
        )


def test_master_profile_requires_stable_security_id_field(tmp_path: Path) -> None:
    """profile 未证明稳定证券 ID 字节位置时 adapter 必须在联网前失败。"""
    manifest = _profile_manifest()
    profiles = manifest["profiles"]
    assert isinstance(profiles, list)
    profile = profiles[0]
    assert isinstance(profile, dict)
    data_record = profile["dataRecord"]
    assert isinstance(data_record, dict)
    del data_record["securityId"]
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_path = tmp_path / "profile-without-security-id.json"
    manifest_path.write_bytes(manifest_bytes)

    with pytest.raises(ValueError, match="data-record profile"):
        _load_master_fixed_length_profile(
            manifest_path,
            expected_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            max_bytes=64 * 1024,
        )


def test_fixed_length_master_preserves_missing_stable_id_for_degradation(
    tmp_path: Path,
) -> None:
    """已证明字段位置但单行缺稳定 ID 时保留空值，禁止退回代码加名称建根身份。"""
    manifest_bytes = json.dumps(
        _profile_manifest(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_path = tmp_path / "profile.json"
    manifest_path.write_bytes(manifest_bytes)
    profile = _load_master_fixed_length_profile(
        manifest_path,
        expected_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        max_bytes=64 * 1024,
    ).select(date(2026, 7, 29))
    record = bytearray(_fixed_record())
    record[0:8] = b" " * 8

    values = parse_hkex_securities_master(
        bytes(record),
        effective_trade_date=date(2026, 7, 29),
        expected_issued_date=date(2026, 7, 28),
        product_name="MS_20260728.dat",
        profile="hkex-securities-master-fixed-length-manifest-v2",
        layout=profile,
    )

    assert values[0]["securityId"] is None


def test_master_decoder_keeps_missing_stable_id_out_of_entity_key() -> None:
    """标准主档载荷缺稳定 ID 时返回显式空身份，不能用代码或名称补键。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.stock-connect-instrument-master.v2",
            "records": [
                {
                    "securityId": None,
                    "instrumentCode": "00700",
                    "displayName": "TENCENT",
                    "effectiveFrom": "2026-07-29",
                }
            ],
        }
    ).encode()

    records = decode_stock_connect_instrument_master_batch(payload)

    assert records[0].source_security_id is None
    assert records[0].source_instrument_code == "00700"


def test_master_profile_set_rejects_missing_and_overlapping_effective_ranges(
    tmp_path: Path,
) -> None:
    """历史布局缺口在选择时拒绝，重叠区间在 adapter 启动前拒绝。"""
    gap_manifest = _profile_manifest()
    gap_profiles = gap_manifest["profiles"]
    assert isinstance(gap_profiles, list)
    gap_profile = gap_profiles[0]
    assert isinstance(gap_profile, dict)
    gap_profile["effectiveFrom"] = "2020-01-01"
    gap_bytes = json.dumps(gap_manifest, sort_keys=True, separators=(",", ":")).encode()
    gap_path = tmp_path / "gap-profile.json"
    gap_path.write_bytes(gap_bytes)
    gap_set = _load_master_fixed_length_profile(
        gap_path,
        expected_sha256=hashlib.sha256(gap_bytes).hexdigest(),
        max_bytes=64 * 1024,
    )
    with pytest.raises(ValueError, match="no unique profile"):
        gap_set.select(date(2019, 12, 31))

    overlap_manifest = _profile_manifest()
    overlap_profiles = overlap_manifest["profiles"]
    assert isinstance(overlap_profiles, list)
    first = overlap_profiles[0]
    assert isinstance(first, dict)
    first["effectiveTo"] = "2025-12-31"
    second = json.loads(json.dumps(first))
    second["effectiveFrom"] = "2025-01-01"
    second["effectiveTo"] = None
    second["profileId"] = "licensed-test-v2"
    overlap_profiles.append(second)
    overlap_bytes = json.dumps(
        overlap_manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    overlap_path = tmp_path / "overlap-profile.json"
    overlap_path.write_bytes(overlap_bytes)

    with pytest.raises(ValueError, match="ranges overlap"):
        _load_master_fixed_length_profile(
            overlap_path,
            expected_sha256=hashlib.sha256(overlap_bytes).hexdigest(),
            max_bytes=64 * 1024,
        )


def test_daily_statistics_regime_enforces_buy_sell_disclosure_change() -> None:
    """变更日前买卖额必须成对报告，变更日后北向必须为空且不能被解释为零。"""
    before = _market_record(
        {"turnover": "30", "buy": "10", "sell": "20"},
        trade_date=date(2024, 8, 16),
        direction="NORTHBOUND",
    )
    after = _market_record(
        {"turnover": "30"},
        trade_date=date(2024, 8, 19),
        direction="NORTHBOUND",
    )

    assert before["buyAmount"] == "10"
    assert before["sellAmount"] == "20"
    assert after["buyAmount"] is None
    assert after["sellAmount"] is None
    availability = after["fieldAvailability"]
    assert isinstance(availability, dict)
    assert availability["buyAmount"] == "NOT_DISCLOSED_BY_REGIME"
    with pytest.raises(ValueError, match="regime-required"):
        _market_record(
            {"turnover": "30"},
            trade_date=date(2024, 8, 16),
            direction="NORTHBOUND",
        )
    with pytest.raises(ValueError, match="forbidden post-change"):
        _market_record(
            {"turnover": "30", "buy": "10", "sell": "20"},
            trade_date=date(2024, 8, 19),
            direction="NORTHBOUND",
        )
    with pytest.raises(ValueError, match="no unique regime"):
        _daily_statistics_regime(
            trade_date=date(2014, 11, 16),
            direction="NORTHBOUND",
        )


def test_omdc_session_is_derived_only_with_calendar_and_finality() -> None:
    """Msg80 本身没有会话状态，缺任一证据都不能发布派生 CLOSED。"""
    frame = _omdc_frame()
    with pytest.raises(ValueError, match="calendar and end-of-day finality"):
        parse_stock_connect_status(
            frame,
            channel="SH",
            direction="NORTHBOUND",
            trade_date=date(2026, 7, 29),
            profile="hkex-omdc-mmdh-msg80-v2.1",
            source_code="HKEX_OMDC",
            product_name="HKEX OMD-C MMDH Stock Connect DQB",
        )

    status = parse_stock_connect_status(
        frame,
        channel="SH",
        direction="NORTHBOUND",
        trade_date=date(2026, 7, 29),
        profile="hkex-omdc-mmdh-msg80-v2.1",
        source_code="HKEX_OMDC",
        product_name="HKEX OMD-C MMDH Stock Connect DQB",
        calendar_trading_day=True,
        landing_final=True,
    )

    assert status["sessionState"] == "CLOSED"
    assert status["sessionAvailability"] == "DERIVED"
    assert status["buyOrderAccepted"] is None
    assert status["sellOrderAccepted"] is None
    assert status["quotaBalance"] == "123000"


def test_derived_session_emits_the_frozen_quality_issue() -> None:
    """北向派生 CLOSED 必须以冻结 issue 暴露，不能静默伪装成来源直报。"""
    payload = {
        "schema": "quant-v2.stock-connect-channel-status.v1",
        "channel": "SH",
        "direction": "NORTHBOUND",
        "records": [
            {
                "tradeDate": "2026-07-29",
                "tradingDay": True,
                "sessionState": "CLOSED",
                "sessionAvailability": "DERIVED",
                "buyOrderAccepted": None,
                "sellOrderAccepted": None,
                "quotaState": "ACTUAL_REPORTED",
                "quotaBalance": "123000",
                "quotaCurrency": "CNY",
                "observedAt": "2026-07-29T08:00:00Z",
                "sourceCode": "HKEX_OMDC",
                "productName": "HKEX OMD-C MMDH Stock Connect DQB",
                "sourcePublicationAt": "2026-07-29T08:00:00Z",
                "sourceFileSha256": "b" * 64,
            }
        ],
    }
    status = decode_stock_connect_channel_status_batch(
        json.dumps(payload).encode(),
        channel=StockConnectChannel("SH", "NORTHBOUND"),
    )

    assert _status_quality_issues(status) == [
        {
            "code": "SESSION_STATE_DERIVED_FROM_CALENDAR_AND_FINALITY",
            "component": "channel-status",
            "detail": (
                "OMD-C Msg80 does not report session state; CLOSED is derived "
                "from the official open-day calendar and END_OF_DAY_FINAL evidence"
            ),
        }
    ]


def test_all_northbound_preflight_and_fetch_use_distinct_channel_landings(
    tmp_path: Path,
) -> None:
    """ALL 的沪、深北向必须分别校验和读取带 `{channel}` 的最终文件。"""
    adapter = _official_adapter(tmp_path)
    status_root = tmp_path / "status"
    _write_omdc_landing(status_root, "SH")
    _write_omdc_landing(status_root, "SZ")
    probe_dates = _StockConnectProbeDates(
        daily_dates=(),
        status_dates=(
            (
                "status-sh-northbound-2026-07-29",
                "SH",
                "NORTHBOUND",
                date(2026, 7, 29),
            ),
            (
                "status-sz-northbound-2026-07-29",
                "SZ",
                "NORTHBOUND",
                date(2026, 7, 29),
            ),
        ),
        master_dates=(),
    )

    checks, evidence = adapter._preflight_status_landings(
        probe_dates,
        deadline=monotonic() + 10,
    )
    batches = [
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability=CHANNEL_STATUS_CAPABILITY,
                    parameters=(
                        ("channel", channel),
                        ("direction", "NORTHBOUND"),
                        ("start", "2026-07-29"),
                        ("end", "2026-07-29"),
                    ),
                )
            )
        )
        for channel in ("SH", "SZ")
    ]

    assert {item.component for item in checks if item.accepted} == {
        "status-sh-northbound-deliveries",
        "status-sz-northbound-deliveries",
    }
    assert {item["relativePath"] for item in evidence} == {
        "hkex-omdc/SH/2026/20260729.bin",
        "hkex-omdc/SZ/2026/20260729.bin",
    }
    assert [json.loads(batch.payload)["channel"] for batch in batches] == [
        "SH",
        "SZ",
    ]
    assert batches[0].raw_payload != batches[1].raw_payload


def test_full_window_status_preflight_rejects_a_missing_middle_delivery(
    tmp_path: Path,
) -> None:
    """首尾最终文件齐全但中间开放日缺件时，全窗预检仍必须拒绝。"""
    adapter = _official_adapter(tmp_path)
    scope = _preflight_scope(
        ProviderPreflightRequest(
            dataset_code="market.stock_connect.overview.bundle",
            mode="DATE_RANGE",
            selector={
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "SH",
                "direction": "NORTHBOUND",
            },
            date_from="2026-07-27",
            date_to="2026-07-29",
            observation_date=None,
            timeout_seconds=10,
        ),
        anchor_date=date(2026, 7, 29),
    )
    probe_dates = adapter._preflight_calendar_dates(
        scope,
        deadline=monotonic() + 10,
    )
    readiness = _available_readiness_evidence(
        probe_dates=probe_dates,
        calendar_manifest_sha256="d" * 64,
        evidence_observed_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
    )
    readiness_days = readiness["days"]
    assert isinstance(readiness_days, list)
    status_root = tmp_path / "status"
    _write_omdc_landing(status_root, "SH", date(2026, 7, 27))
    _write_omdc_landing(status_root, "SH", date(2026, 7, 29))

    checks, evidence = adapter._preflight_status_landings(
        probe_dates,
        deadline=monotonic() + 10,
    )

    assert [item[3] for item in probe_dates.status_dates] == [
        date(2026, 7, 27),
        date(2026, 7, 28),
        date(2026, 7, 29),
    ]
    assert [
        (
            item["calendarDate"],
            item["channel"],
            item["direction"],
            item["calendarState"],
            item["publicationAvailability"],
        )
        for item in readiness_days
    ] == [
        ("2026-07-27", "SH", "NORTHBOUND", "OPEN", "REPORTED"),
        ("2026-07-28", "SH", "NORTHBOUND", "OPEN", "REPORTED"),
        ("2026-07-29", "SH", "NORTHBOUND", "OPEN", "REPORTED"),
    ]
    assert (
        readiness["calendarDataVersion"]
        == hashlib.sha256(
            json.dumps(
                {
                    "calendarManifestSha256": "d" * 64,
                    "days": readiness_days,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    assert checks[0].accepted is False
    middle = next(item for item in evidence if item["tradeDate"] == "2026-07-28")
    assert middle["available"] is False
    assert middle["failureReason"] == "FINAL_LANDING_VERIFICATION_FAILED"


def test_closed_only_window_preserves_official_not_trading_evidence(
    tmp_path: Path,
) -> None:
    """精确休市日没有交付目标仍须保留 CLOSED，不得误报官方日历来源缺失。"""
    closed_date = date(2026, 7, 29)
    adapter = _official_adapter(
        tmp_path,
        closed_calendar_dates=frozenset({closed_date}),
    )
    scope = _preflight_scope(
        ProviderPreflightRequest(
            dataset_code="market.stock_connect.overview.bundle",
            mode="DATE_RANGE",
            selector={
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "SH",
                "direction": "NORTHBOUND",
            },
            date_from=closed_date.isoformat(),
            date_to=closed_date.isoformat(),
            observation_date=None,
            timeout_seconds=10,
        ),
        anchor_date=closed_date,
    )

    probe_dates = adapter._preflight_calendar_dates(
        scope,
        deadline=monotonic() + 10,
    )
    readiness = _available_readiness_evidence(
        probe_dates=probe_dates,
        calendar_manifest_sha256="f" * 64,
        evidence_observed_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
    )

    assert probe_dates.status_dates == ()
    assert probe_dates.bundle_targets == ()
    assert readiness["days"] == [
        {
            "calendarDate": "2026-07-29",
            "channel": "SH",
            "direction": "NORTHBOUND",
            "calendarState": "CLOSED",
            "sourceFileSha256": hashlib.sha256(
                _calendar_payload(
                    2026,
                    closed_dates=frozenset({closed_date}),
                )
            ).hexdigest(),
            "sourcePublicationAt": "2026-01-01T00:00:00Z",
            "publicationAvailability": "REPORTED",
            "sourceObservedAt": "2026-01-02T00:00:00Z",
        }
    ]


def test_historical_status_not_declared_is_approved_with_explicit_warning(
    tmp_path: Path,
) -> None:
    """运营边界前未归档参与者状态时仍可回补成交事实，但状态必须保持真实缺源。"""
    adapter = _official_adapter(tmp_path)
    business_date = date(2020, 7, 29)
    probe_dates = _StockConnectProbeDates(
        daily_dates=(),
        status_dates=(
            (
                "status-sh-northbound-2020-07-29",
                "SH",
                "NORTHBOUND",
                business_date,
            ),
        ),
        master_dates=(),
    )

    checks, evidence = adapter._preflight_status_landings(
        probe_dates,
        deadline=monotonic() + 10,
    )
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability=CHANNEL_STATUS_CAPABILITY,
                parameters=(
                    ("channel", "SH"),
                    ("direction", "NORTHBOUND"),
                    ("start", business_date.isoformat()),
                    ("end", business_date.isoformat()),
                ),
            )
        )
    )
    status = decode_stock_connect_channel_status_batch(
        batch.payload,
        channel=StockConnectChannel("SH", "NORTHBOUND"),
    )

    assert checks[0].accepted is True
    assert checks[0].reason == "APPROVED_WITH_HISTORICAL_STATUS_GAPS"
    assert evidence[0]["failureReason"] == "STATUS_SOURCE_NOT_AVAILABLE_HISTORICAL"
    assert evidence[0]["required"] is False
    assert status.session_state == "UNKNOWN"
    assert status.session_availability == "SOURCE_MISSING"
    assert status.quota_state == "SOURCE_MISSING"
    assert status.quota_balance is None
    assert status.source_publication_at is None
    assert status.source_file_sha256 is None
    assert _status_quality_issues(status)[0]["code"] == ("STATUS_SOURCE_NOT_AVAILABLE_HISTORICAL")


def test_historical_calendar_uses_digest_pinned_local_official_archive(
    tmp_path: Path,
) -> None:
    """历史年度可从只读配置卷读取，但必须逐年出现在清单且摘要完全一致。"""
    adapter = _official_adapter(
        tmp_path,
        calendar_years=(2020, 2025, 2026),
        local_calendar_years=frozenset({2020}),
    )

    fetched, records = adapter._calendar_delivery(2020)

    assert fetched.upstream_source == "HKEX_CALENDAR"
    assert fetched.published_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert len(records) == 366
    assert records[0]["calendarDate"] == "2020-01-01"


def test_calendar_manifest_missing_middle_year_fails_closed_without_url_guess(
    tmp_path: Path,
) -> None:
    """FULL 所需中间年份未 provision 时不可退回 URL 模板临时猜取。"""
    adapter = _official_adapter(
        tmp_path,
        calendar_years=(2019, 2021, 2025, 2026),
        local_calendar_years=frozenset({2019, 2021}),
    )

    with pytest.raises(ValueError, match="does not cover"):
        adapter._calendar_delivery(2020)


def test_historical_calendar_rejects_template_before_any_provider_probe(
    tmp_path: Path,
) -> None:
    """2014 只能钉住精确官方对象或本地归档，模板配置在网络和 command 前即失败。"""
    manifest_path = tmp_path / "calendar-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifestSchema": "quant-v2.hkex-stock-connect-calendar-manifest.v1",
                "entries": [
                    {
                        "year": 2014,
                        "sourceKind": "HTTPS_TEMPLATE",
                        "url": None,
                        "relativePath": None,
                        "sha256": "a" * 64,
                        "sourcePublicationAt": "2015-01-01T00:00:00Z",
                        "observedAt": "2026-07-29T00:00:00Z",
                    }
                ],
            }
        )
    )

    with pytest.raises(ValueError, match="historical calendar"):
        _load_calendar_manifest(manifest_path, max_bytes=16_384)


def test_calendar_url_404_fails_closed_even_when_year_is_manifested(
    tmp_path: Path,
) -> None:
    """清单中的 HTTPS 年度对象读取失败时不得改用第三方或推导日历。"""
    adapter = _official_adapter(tmp_path)

    def fail_url(_url: str) -> _FetchedObject:
        """模拟 HKEX 历史 URL 不存在，不提供任何替代响应。"""
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "official calendar unavailable",
            retryable=True,
        )

    adapter._custom_https_reader = fail_url
    with pytest.raises(ProviderError):
        adapter._calendar_delivery(2026)


def test_sftp_preflight_requires_expiry_entitlement_for_every_delivery(
    tmp_path: Path,
) -> None:
    """SFTP 无法报告保留期限时，缺部署 availableUntil 的历史对象必须零任务拒绝。"""
    adapter = _official_adapter(tmp_path)
    business_date = date(2026, 7, 29)
    probe_dates = _StockConnectProbeDates(
        daily_dates=(("daily-sh", "SH", business_date),),
        status_dates=(),
        master_dates=(),
        bundle_targets=(("SH", "NORTHBOUND", business_date),),
    )

    checks, evidence = adapter._preflight_sftp(
        probe_dates,
        deadline=monotonic() + 10,
    )

    assert checks[0].accepted is False
    assert checks[0].reason == "DELIVERY_ENTITLEMENT_MISSING"
    assert evidence[0]["available"] is False


def test_sftp_preflight_rejects_window_that_expires_before_safe_completion(
    tmp_path: Path,
) -> None:
    """保守吞吐估算加安全窗超过 availableUntil 时，不得先排队再等待对象消失。"""
    adapter = _official_adapter(
        tmp_path,
        sftp_entitlements=(
            {
                "remotePath": "/daily/sh/20260729.csv",
                "orderReference": "licensed-order-test",
                "availableUntil": "2026-07-29T09:30:00Z",
            },
        ),
    )
    business_date = date(2026, 7, 29)
    checks, evidence = adapter._preflight_sftp(
        _StockConnectProbeDates(
            daily_dates=(("daily-sh", "SH", business_date),),
            status_dates=(),
            master_dates=(),
            bundle_targets=(("SH", "NORTHBOUND", business_date),),
        ),
        deadline=monotonic() + 10,
    )

    assert checks[0].accepted is False
    assert checks[0].reason == "DELIVERY_WINDOW_INSUFFICIENT"
    assert evidence[0]["availableUntil"] == "2026-07-29T09:30:00Z"
    estimated_completion = evidence[0]["estimatedCompletionAt"]
    available_until = evidence[0]["availableUntil"]
    assert isinstance(estimated_completion, str)
    assert isinstance(available_until, str)
    assert estimated_completion > available_until


def test_preflight_scope_checks_actual_full_and_range_boundaries() -> None:
    """FULL 保留沪深不同上线日，范围请求则锁定用户明确首尾日期。"""
    full = _preflight_scope(
        ProviderPreflightRequest(
            dataset_code="market.stock_connect.overview.bundle",
            mode="FULL",
            selector={
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "ALL",
                "direction": None,
            },
            date_from=None,
            date_to=None,
            observation_date=None,
            timeout_seconds=30,
        ),
        anchor_date=date(2026, 7, 29),
    )
    ranged = _preflight_scope(
        ProviderPreflightRequest(
            dataset_code="market.stock_connect.overview.bundle",
            mode="DATE_RANGE",
            selector={
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "SZ",
                "direction": "SOUTHBOUND",
            },
            date_from="2026-07-01",
            date_to="2026-07-29",
            observation_date=None,
            timeout_seconds=30,
        ),
        anchor_date=date(2026, 7, 29),
    )

    assert dict(full.channel_starts) == {
        "SH": date(2014, 11, 17),
        "SZ": date(2016, 12, 5),
    }
    assert ranged.channel_starts == (("SZ", date(2026, 7, 1)),)
    assert ranged.end == date(2026, 7, 29)


def test_calendar_failure_freezes_unknown_full_window_readiness() -> None:
    """官方日历不可取得时逐日证据必须全为 UNKNOWN，不能把缺记录猜成休市。"""
    scope = _preflight_scope(
        ProviderPreflightRequest(
            dataset_code="market.stock_connect.overview.bundle",
            mode="DATE_RANGE",
            selector={
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "SH",
                "direction": "SOUTHBOUND",
            },
            date_from="2026-07-28",
            date_to="2026-07-29",
            observation_date=None,
            timeout_seconds=10,
        ),
        anchor_date=date(2026, 7, 29),
    )

    readiness = _unavailable_readiness_evidence(
        scope=scope,
        calendar_manifest_sha256="e" * 64,
        evidence_observed_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
    )

    assert readiness["days"] == [
        {
            "calendarDate": "2026-07-28",
            "channel": "SH",
            "direction": "SOUTHBOUND",
            "calendarState": "UNKNOWN",
            "sourceFileSha256": None,
            "sourcePublicationAt": None,
            "publicationAvailability": "SOURCE_MISSING",
            "sourceObservedAt": None,
        },
        {
            "calendarDate": "2026-07-29",
            "channel": "SH",
            "direction": "SOUTHBOUND",
            "calendarState": "UNKNOWN",
            "sourceFileSha256": None,
            "sourcePublicationAt": None,
            "publicationAvailability": "SOURCE_MISSING",
            "sourceObservedAt": None,
        },
    ]


def test_preflight_scope_accepts_sh_daily_statistics_back_issue_boundary() -> None:
    """SH DATE_RANGE 接受官方 back issues 覆盖的 2014-11-17，并拒绝更早日期。"""
    with pytest.raises(ValueError, match="product first issue date"):
        _preflight_scope(
            ProviderPreflightRequest(
                dataset_code="market.stock_connect.overview.bundle",
                mode="DATE_RANGE",
                selector={
                    "kind": "STOCK_CONNECT",
                    "operation": "MARKET",
                    "channel": "SH",
                    "direction": "NORTHBOUND",
                },
                date_from="2014-11-16",
                date_to="2014-11-17",
                observation_date=None,
                timeout_seconds=30,
            ),
            anchor_date=date(2026, 7, 29),
        )

    back_issue_boundary = _preflight_scope(
        ProviderPreflightRequest(
            dataset_code="market.stock_connect.overview.bundle",
            mode="DATE_RANGE",
            selector={
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "SH",
                "direction": "NORTHBOUND",
            },
            date_from="2014-11-17",
            date_to="2014-11-17",
            observation_date=None,
            timeout_seconds=30,
        ),
        anchor_date=date(2026, 7, 29),
    )

    assert back_issue_boundary.channel_starts == (("SH", date(2014, 11, 17)),)


def test_source_reference_requires_publication_availability_and_observed_time() -> None:
    """来源未报告 publication 时必须显式为空，观察时间仍需与批次完全一致。"""
    observed = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    normalized = {
        "schema": "quant-v2.stock-connect-market-daily.v1",
        "productName": "licensed-daily.csv",
        "sourcePublicationAvailability": "NOT_PROVIDED_BY_SOURCE",
        "sourcePublicationAt": None,
        "sourceObservedAt": "2026-07-29T09:00:00Z",
        "sourceFileSha256": "a" * 64,
        "records": [],
    }
    batch = ProviderBatch(
        provider_id="official-stock-connect",
        capability="market.stock_connect.market_stat.reported",
        payload=json.dumps(normalized).encode(),
        observed_at=observed,
        upstream_source="HKEX_DATA_MARKETPLACE",
    )

    source_ref = _source_ref(batch)

    assert source_ref["sourcePublicationAvailability"] == ("NOT_PROVIDED_BY_SOURCE")
    assert source_ref["sourcePublicationAt"] is None
    normalized["sourcePublicationAvailability"] = "REPORTED"
    invalid = ProviderBatch(
        provider_id=batch.provider_id,
        capability=batch.capability,
        payload=json.dumps(normalized).encode(),
        observed_at=observed,
        upstream_source=batch.upstream_source,
    )
    with pytest.raises(ProviderError, match="publication time"):
        _source_ref(invalid)
