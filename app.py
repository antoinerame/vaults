from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, render_template, request

from vaults import (
    MORPHO_SITE_URL,
    compute_pnl_from_prices,
    fetch_curator_profile,
    fetch_share_price_usd_series,
    fetch_vault_history_timeseries,
    fetch_vaults_for_curator,
    fetch_vault_details,
    get_network_by_id,
    iso_date_to_unix_timestamp,
    networks,
    pick_start_end_points,
)

app = Flask(__name__)

DEFAULT_RANGE_DAYS = 30
CURATOR_VAULT_WINDOW_DAYS = 30
CURATOR_VAULT_CACHE_TTL = 1800  # 30 minutes
CURATOR_VAULT_CACHE: Dict[
    Tuple[str, int, int, int], Dict[str, Any]
] = {}


def _format_usd_short(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    abs_val = abs(value)
    for suffix, threshold in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs_val >= threshold:
            return f"{sign}{abs_val / threshold:.2f} {suffix} $"
    if abs_val >= 1:
        return f"{sign}{abs_val:,.2f} $"
    return f"{sign}{abs_val:.4f} $"


def _format_timestamp_label(ts: Optional[int]) -> str:
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _summarize_vault(
    raw: Dict[str, Any],
    network_slug: Optional[str],
) -> Dict[str, Any]:
    state = raw.get("state") or {}
    metadata = raw.get("metadata") or {}
    asset = raw.get("asset") or {}
    liquidity = raw.get("liquidity") or {}
    total_assets = state.get("totalAssetsUsd")
    share_price = state.get("sharePriceUsd")
    liquidity_usd = liquidity.get("usd")
    cash_ratio = (
        liquidity_usd / total_assets if liquidity_usd and total_assets else None
    )
    last_update = state.get("timestamp")
    return {
        "name": raw.get("name"),
        "symbol": raw.get("symbol"),
        "address": raw.get("address"),
        "network": network_slug,
        "asset_symbol": asset.get("symbol"),
        "asset_name": asset.get("name"),
        "description": metadata.get("description"),
        "whitelisted": raw.get("whitelisted"),
        "promoted": raw.get("promoted"),
        "tvl": _format_usd_short(total_assets),
        "raw_tvl": total_assets,
        "total_assets": total_assets,
        "liquidity_usd": liquidity_usd,
        "liquidity_label": _format_usd_short(liquidity_usd),
        "cash_ratio": cash_ratio,
        "lent_ratio": (1 - cash_ratio) if cash_ratio is not None else None,
        "apy": state.get("apy"),
        "net_apy": state.get("netApy"),
        "fee": state.get("fee"),
        "share_price": share_price,
        "share_price_label": f"{share_price:.6f} $" if share_price else "N/A",
        "curator": state.get("curator"),
        "guardian": state.get("guardian"),
        "owner": state.get("owner"),
        "last_update": last_update,
        "last_update_label": _format_timestamp_label(last_update),
    }


def _build_composition_rows(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    total = state.get("totalAssetsUsd") or 0.0
    rows: List[Dict[str, Any]] = []
    allocations = state.get("allocation") or []
    for allocation in allocations:
        supply = allocation.get("supplyAssetsUsd")
        if supply is None:
            continue
        percent = (supply / total * 100) if total else None
        market = allocation.get("market") or {}
        loan_obj = market.get("loanAsset") or {}
        collateral_obj = market.get("collateralAsset") or {}
        oracle_obj = market.get("oracle") or {}
        market_state = market.get("state") or {}
        loan = loan_obj.get("symbol")
        collateral = collateral_obj.get("symbol")
        title = market.get("uniqueKey")
        assets = " / ".join([val for val in (loan, collateral) if val])
        lltv_value = market.get("lltv")
        if isinstance(lltv_value, str):
            try:
                lltv_value = float(lltv_value)
            except ValueError:
                lltv_value = None
        market_lltv_pct = (lltv_value / 1e16) if lltv_value else None
        oracle_type = oracle_obj.get("type")
        rows.append(
            {
                "title": title or assets or "Allocation",
                "assets": assets or "N/A",
                "loan_symbol": loan,
                "collateral_symbol": collateral,
                "market_oracle": oracle_type,
                "market_lltv": market_lltv_pct,
                "market_utilization": market_state.get("utilization"),
                "market_liquidity_usd": market_state.get("liquidityAssetsUsd"),
                "tvl": _format_usd_short(supply),
                "tvl_raw": supply,
                "percent": f"{percent:.2f} %" if percent is not None else "N/A",
                "percent_value": percent or 0.0,
                "enabled": allocation.get("enabled"),
            }
        )
    rows.sort(key=lambda row: row.get("percent_value", 0.0), reverse=True)
    return rows


def _compute_performance_metrics(
    history_points: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    usable = [
        p
        for p in sorted(history_points, key=lambda x: x["timestamp"])
        if p.get("share_price") is not None and p.get("total_assets_usd") is not None
    ]
    if len(usable) < 2:
        return None
    start = usable[0]
    end = usable[-1]
    pnl_pct = (end["share_price"] / start["share_price"]) - 1
    pnl_abs = (
        start["total_assets_usd"] * pnl_pct
        if start["total_assets_usd"] is not None
        else None
    )
    duration_days = (end["timestamp"] - start["timestamp"]) / 86400
    annualized = None
    if duration_days > 0:
        annualized = (1 + pnl_pct) ** (365 / duration_days) - 1

    peak = usable[0]["share_price"]
    max_drawdown = 0.0
    for point in usable:
        price = point["share_price"]
        if price > peak:
            peak = price
        drawdown = (price - peak) / peak if peak else 0.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown

    flow_total = 0.0
    pnl_component = 0.0
    prev = usable[0]
    for current in usable[1:]:
        prev_assets = prev.get("total_assets_usd")
        if prev_assets is None:
            prev = current
            continue
        price_ratio = (
            (current["share_price"] / prev["share_price"]) - 1
            if prev["share_price"]
            else 0.0
        )
        pnl_delta = prev_assets * price_ratio
        pnl_component += pnl_delta
        flow_delta = (current["total_assets_usd"] - prev_assets) - pnl_delta
        flow_total += flow_delta
        prev = current

    return {
        "pnl_pct": pnl_pct,
        "pnl_abs": pnl_abs,
        "annualized_pct": annualized,
        "drawdown_pct": abs(max_drawdown),
        "flow_usd": flow_total,
        "pnl_component_usd": pnl_component,
        "tvl_change_usd": end["total_assets_usd"] - start["total_assets_usd"],
        "tvl_start": start["total_assets_usd"],
        "tvl_end": end["total_assets_usd"],
        "period_days": duration_days,
    }


def _build_risk_summary(
    current_vault: Optional[Dict[str, Any]],
    composition: List[Dict[str, Any]],
) -> List[str]:
    warnings: List[str] = []
    if not current_vault:
        return warnings

    cash_ratio = current_vault.get("cash_ratio")
    if cash_ratio is not None:
        if cash_ratio < 0.1:
            warnings.append(
                f"Liquidity risk: seulement {cash_ratio*100:.1f}% des actifs sont en cash."
            )
        elif cash_ratio > 0.4:
            warnings.append(
                f"{cash_ratio*100:.1f}% des actifs sont disponibles en cash."
            )

    high_util_pct = sum(
        row.get("percent_value", 0.0)
        for row in composition
        if (row.get("market_utilization") or 0) >= 0.95
    )
    if high_util_pct >= 30:
        warnings.append(
            f"{high_util_pct:.1f}% du vault est expose a des marches utilises a plus de 95%."
        )

    top3_pct = sum(row.get("percent_value", 0.0) for row in composition[:3])
    if top3_pct >= 60:
        warnings.append(
            f"Concentration elevee: top 3 marches = {top3_pct:.1f}% du vault."
        )

    exotic_tokens = {"XUSD", "DEUSD", "USD0", "USDE"}
    exotic_pct = sum(
        row.get("percent_value", 0.0)
        for row in composition
        if (row.get("loan_symbol") or "").upper() in exotic_tokens
    )
    if exotic_pct > 0:
        warnings.append(
            f"Stablecoins exotiques: {exotic_pct:.1f}% du vault en {', '.join(exotic_tokens)}."
        )

    return warnings


def _get_curator_vault_metrics_cached(
    address: str,
    chain_id: int,
    start_ts: int,
    end_ts: int,
) -> Dict[str, str]:
    key = (address.lower(), chain_id, start_ts // 3600, end_ts // 3600)
    now = int(time.time())
    cached = CURATOR_VAULT_CACHE.get(key)
    if cached and (now - cached["timestamp"] < CURATOR_VAULT_CACHE_TTL):
        return cached["data"]

    pnl_label = "N/A"
    tvl_change_label = "N/A"
    tvl_pct_label = "N/A"
    try:
        history_points = fetch_vault_history_timeseries(
            vault_address=address,
            chain_id=chain_id,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        if history_points:
            metrics = _compute_performance_metrics(history_points)
            if metrics and metrics.get("pnl_pct") is not None:
                pnl_label = f"{metrics['pnl_pct'] * 100:.2f} %"
            window_summary = _summarize_tvl_window(
                history_points, days=CURATOR_VAULT_WINDOW_DAYS
            )
            if window_summary:
                tvl_change_label = window_summary.get("change", "N/A")
                tvl_pct_label = window_summary.get("pct", "N/A")
    except Exception:
        pass

    data = {
        "pnl_30d_label": pnl_label,
        "tvl_change_30d": tvl_change_label,
        "tvl_pct_30d": tvl_pct_label,
    }
    CURATOR_VAULT_CACHE[key] = {"timestamp": now, "data": data}
    return data


def _summarize_tvl_window(
    history_points: List[Dict[str, Any]],
    days: Optional[int] = 30,
) -> Optional[Dict[str, Any]]:
    usable = sorted(
        [p for p in history_points if p.get("total_assets_usd") is not None],
        key=lambda x: x["timestamp"],
    )
    if len(usable) < 2:
        return None
    latest_ts = usable[-1]["timestamp"]
    window_days = days
    if window_days is None or window_days <= 0:
        window_days = max(1, int((latest_ts - usable[0]["timestamp"]) / 86400))
    cutoff = latest_ts - window_days * 86400
    window_points = [p for p in usable if p["timestamp"] >= cutoff]
    if len(window_points) < 2:
        window_points = usable
    start = window_points[0]
    end = window_points[-1]
    change = end["total_assets_usd"] - start["total_assets_usd"]
    start_label = _format_usd_short(start["total_assets_usd"])
    end_label = _format_usd_short(end["total_assets_usd"])
    change_label = _format_usd_short(change)
    pct = (change / start["total_assets_usd"]) if start["total_assets_usd"] else None
    pct_label = f"{pct * 100:.2f} %" if pct is not None else "N/A"
    return {
        "start": start_label,
        "end": end_label,
        "change": change_label,
        "pct": pct_label,
        "days": window_days,
    }


def _default_dates() -> tuple[str, str]:
    """Return ISO strings for the default start/end date inputs."""
    today = datetime.utcnow().date()
    start = today - timedelta(days=DEFAULT_RANGE_DAYS)
    return start.isoformat(), today.isoformat()


def _format_timestamp(ts: int) -> str:
    """Human friendly UTC label for tooltips / summary cards."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _get_network_slug(chain_id: int) -> Optional[str]:
    for net in networks:
        if net.get("id") == chain_id:
            return net.get("network")
    return None


@app.route("/", methods=["GET"])
def index():
    start_default, end_default = _default_dates()

    start_date = request.args.get("start_date") or start_default
    end_date = request.args.get("end_date") or end_default
    vault_address = (request.args.get("vault_address") or "").strip()
    selected_network_id = request.args.get("network_id", "")

    full_history = request.args.get("full_history") == "1"

    chart_points: List[Dict[str, float]] = []
    summary: Optional[Dict[str, float]] = None
    error: Optional[str] = None
    morpho_url: Optional[str] = None
    network_slug: Optional[str] = None
    current_vault: Optional[Dict[str, Any]] = None
    current_vault_error: Optional[str] = None
    vault_composition: List[Dict[str, Any]] = []
    history_points: List[Dict[str, Any]] = []
    performance_summary: Optional[Dict[str, Any]] = None
    risk_summary: List[str] = []
    tvl_window_summary: Optional[Dict[str, Any]] = None
    tvl_window_days: Optional[int] = None

    curator_query = (request.args.get("curator") or "").strip()
    curator_info: Optional[Dict[str, Any]] = None
    curator_vaults: List[Dict[str, Any]] = []
    curator_error: Optional[str] = None
    curator_open_param = request.args.get("curator_open")
    curator_open = (
        curator_open_param == "1" if curator_open_param is not None else False
    )

    if vault_address and selected_network_id:
        try:
            chain_id = int(selected_network_id)
        except ValueError:
            error = "Le reseau selectionne est invalide."
        else:
            try:
                start_ts = iso_date_to_unix_timestamp(start_date)
                end_ts = iso_date_to_unix_timestamp(end_date)
                if not full_history and end_ts <= start_ts:
                    raise ValueError(
                        "La date de fin doit etre strictement posterieure a la date de debut."
                    )

                raw_series = fetch_share_price_usd_series(
                    vault_address=vault_address,
                    chain_id=chain_id,
                    start_ts=None if full_history else start_ts,
                    end_ts=None if full_history else end_ts,
                )
                if not raw_series:
                    raise ValueError(
                        "Il n'y a pas de points sharePriceUsd pour cette periode."
                    )

                if full_history:
                    start_point = raw_series[0]
                    end_point = raw_series[-1]
                else:
                    start_point, end_point = pick_start_end_points(
                        raw_series, start_ts=start_ts, end_ts=end_ts
                    )
                pnl_decimal = compute_pnl_from_prices(start_point[1], end_point[1])

                chart_points = [
                    {"timestamp": ts * 1000, "value": value} for ts, value in raw_series
                ]
                summary = {
                    "start_ts": start_point[0],
                    "end_ts": end_point[0],
                    "start_label": _format_timestamp(start_point[0]),
                    "end_label": _format_timestamp(end_point[0]),
                    "start_price": start_point[1],
                    "end_price": end_point[1],
                    "pnl_decimal": pnl_decimal,
                    "is_full_history": full_history,
                }
                if not full_history:
                    tvl_window_days = max(1, int((end_ts - start_ts) / 86400))
                else:
                    tvl_window_days = None
                history_start = None if full_history else start_ts
                history_end = None if full_history else end_ts
                try:
                    history_points = fetch_vault_history_timeseries(
                        vault_address=vault_address,
                        chain_id=chain_id,
                        start_ts=history_start,
                        end_ts=history_end,
                    )
                except Exception:
                    history_points = []
                performance_summary = _compute_performance_metrics(history_points)
                if performance_summary:
                    performance_summary["pnl_abs_label"] = _format_usd_short(
                        performance_summary.get("pnl_abs")
                    )
                    performance_summary["pnl_pct_label"] = (
                        f"{performance_summary['pnl_pct'] * 100:.2f} %"
                        if performance_summary.get("pnl_pct") is not None
                        else "N/A"
                    )
                    performance_summary["annualized_pct_label"] = (
                        f"{performance_summary['annualized_pct'] * 100:.2f} %"
                        if performance_summary.get("annualized_pct") is not None
                        else "N/A"
                    )
                    performance_summary["drawdown_pct_label"] = (
                        f"{performance_summary['drawdown_pct'] * 100:.2f} %"
                        if performance_summary.get("drawdown_pct") is not None
                        else "N/A"
                    )
                    performance_summary["flow_label"] = _format_usd_short(
                        performance_summary.get("flow_usd")
                    )
                    performance_summary["pnl_component_label"] = _format_usd_short(
                        performance_summary.get("pnl_component_usd")
                    )
                    performance_summary["tvl_start_label"] = _format_usd_short(
                        performance_summary.get("tvl_start")
                    )
                    performance_summary["tvl_end_label"] = _format_usd_short(
                        performance_summary.get("tvl_end")
                    )
                    performance_summary["period_days_label"] = (
                        f"{performance_summary['period_days']:.1f} j"
                        if performance_summary.get("period_days") is not None
                        else ""
                    )
                tvl_window_summary = (
                    _summarize_tvl_window(history_points, days=tvl_window_days)
                    if history_points
                    else None
                )
                network_slug = _get_network_slug(chain_id)
                if network_slug:
                    morpho_url = (
                        f"{MORPHO_SITE_URL}{network_slug}/vault/{vault_address}"
                    )
                try:
                    current_vault_data = fetch_vault_details(
                        vault_address=vault_address,
                        chain_id=chain_id,
                    )
                except Exception as exc:  # pragma: no cover
                    current_vault_error = str(exc)
                    current_vault_data = None
                if current_vault_data:
                    current_vault = _summarize_vault(
                        current_vault_data,
                        network_slug=network_slug,
                    )
                    vault_composition = _build_composition_rows(
                        current_vault_data.get("state") or {}
                    )
                    risk_summary = _build_risk_summary(
                        current_vault,
                        vault_composition,
                    )
            except Exception as exc:  # pragma: no cover - surface message to UI
                error = str(exc)

    if curator_query:
        try:
            curator_info = fetch_curator_profile(curator_query)
            if curator_info is None:
                curator_error = "Aucun curator ne correspond a cette valeur."
            else:
                raw_curator_vaults = fetch_vaults_for_curator(curator_info["id"])
                curator_vaults = []
                now_ts = int(datetime.utcnow().timestamp())
                start_window_ts = now_ts - CURATOR_VAULT_WINDOW_DAYS * 86400
                for item in raw_curator_vaults:
                    chain_id = (item.get("chain") or {}).get("id")
                    total_assets = (item.get("state") or {}).get("totalAssetsUsd")
                    metrics_cached = _get_curator_vault_metrics_cached(
                        item.get("address"),
                        chain_id,
                        start_window_ts,
                        now_ts,
                    )
                    curator_vaults.append(
                        {
                            "id": item.get("id"),
                            "name": item.get("name"),
                            "address": item.get("address"),
                            "whitelisted": item.get("whitelisted"),
                            "chain_id": chain_id,
                            "network": get_network_by_id(chain_id) or chain_id,
                            "asset_symbol": (item.get("asset") or {}).get("symbol"),
                            "total_assets_usd": total_assets,
                            "display_tvl": _format_usd_short(total_assets),
                            **metrics_cached,
                        }
                    )
                curator_vaults.sort(
                    key=lambda vault: vault.get("total_assets_usd") or 0.0,
                    reverse=True,
                )
        except Exception as exc:  # pragma: no cover
            curator_error = str(exc)

    if curator_open_param is None and (curator_vaults or curator_error):
        curator_open = True

    return render_template(
        "index.html",
        networks=networks,
        selected_network_id=(
            int(selected_network_id) if selected_network_id.isdigit() else None
        ),
        vault_address=vault_address,
        start_date=start_date,
        end_date=end_date,
        full_history=full_history,
        chart_points=chart_points,
        summary=summary,
        error=error,
        morpho_url=morpho_url,
        network_slug=network_slug,
        curator_query=curator_query,
        curator_info=curator_info,
        curator_vaults=curator_vaults,
        curator_error=curator_error,
        curator_open=curator_open,
        current_vault=current_vault,
        current_vault_error=current_vault_error,
        vault_composition=vault_composition,
        history_points=history_points,
        performance_summary=performance_summary,
        risk_summary=False,
        tvl_window_summary=tvl_window_summary,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
