from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import re

import requests
from flask import Flask, Response, abort, render_template, request, url_for
from urllib.parse import urljoin

from vaults import (
    MORPHO_SITE_URL,
    compute_pnl_from_prices,
    fetch_curator_profile,
    fetch_share_price_usd_series,
    fetch_vaults_for_curator,
    fetch_vault_details,
    get_network_by_id,
    iso_date_to_unix_timestamp,
    networks,
    pick_start_end_points,
)

app = Flask(__name__)

DEFAULT_RANGE_DAYS = 30


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


def _prepare_morpho_html(html: str) -> str:
    cleaned = re.sub(
        r'<meta[^>]+http-equiv=["\']?Content-Security-Policy["\']?[^>]*>',
        "",
        html,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r'<meta[^>]+http-equiv=["\']?X-Frame-Options["\']?[^>]*>',
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"<script\b[^>]*>.*?</script>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    body_match = re.search(r"(?is)<body[^>]*>(.*?)</body>", cleaned)
    fragment = body_match.group(1) if body_match else cleaned

    def _rewrite(match: re.Match[str]) -> str:
        attr = match.group("attr")
        url = match.group("url")
        if url.startswith(("#", "mailto:", "javascript:", "data:")):
            return match.group(0)
        if url.startswith(("http://", "https://", "//")):
            return match.group(0)
        absolute = urljoin(MORPHO_SITE_URL, url)
        return f'{attr}="{absolute}"'

    fragment = re.sub(
        r'(?P<attr>href|src)="(?P<url>[^"]+)"',
        _rewrite,
        fragment,
        flags=re.IGNORECASE,
    )
    return fragment


def _fetch_morpho_embed_html(network: str, address: str) -> str:
    upstream_url = f"{MORPHO_SITE_URL}{network}/vault/{address}"
    response = requests.get(upstream_url, timeout=30)
    response.raise_for_status()
    return _prepare_morpho_html(response.text)


def _summarize_vault(
    raw: Dict[str, Any],
    network_slug: Optional[str],
) -> Dict[str, Any]:
    state = raw.get("state") or {}
    metadata = raw.get("metadata") or {}
    asset = raw.get("asset") or {}
    total_assets = state.get("totalAssetsUsd")
    share_price = state.get("sharePriceUsd")
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
        "apy": state.get("apy"),
        "net_apy": state.get("netApy"),
        "fee": state.get("fee"),
        "share_price": share_price,
        "share_price_label": f"{share_price:.6f} $" if share_price else "N/A",
        "curator": state.get("curator"),
        "guardian": state.get("guardian"),
        "owner": state.get("owner"),
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
        loan = (market.get("loanAsset") or {}).get("symbol")
        collateral = (market.get("collateralAsset") or {}).get("symbol")
        title = market.get("uniqueKey")
        assets = " / ".join([val for val in (loan, collateral) if val])
        if percent < 0.001:
            continue
        rows.append(
            {
                "title": title or assets or "Allocation",
                "assets": assets or "N/A",
                "tvl": _format_usd_short(supply),
                "percent": f"{percent:.2f} %" if percent is not None else "N/A",
                "percent_raw": percent if percent is not None else -1.0,
                "enabled": allocation.get("enabled"),
            }
        )
    rows.sort(key=lambda row: row.get("percent_raw", -1.0), reverse=True)
    return rows


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
    morpho_embed_html: Optional[str] = None
    morpho_embed_error: Optional[str] = None
    network_slug: Optional[str] = None
    current_vault: Optional[Dict[str, Any]] = None
    current_vault_error: Optional[str] = None
    vault_composition: List[Dict[str, Any]] = []

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
                if network_slug:
                    try:
                        morpho_embed_html = _fetch_morpho_embed_html(
                            network_slug, vault_address
                        )
                    except Exception as exc:  # pragma: no cover
                        morpho_embed_error = str(exc)
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
                for item in raw_curator_vaults:
                    chain_id = (item.get("chain") or {}).get("id")
                    total_assets = (item.get("state") or {}).get("totalAssetsUsd")
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
        morpho_embed_html=morpho_embed_html,
        morpho_embed_error=morpho_embed_error,
        network_slug=network_slug,
        curator_query=curator_query,
        curator_info=curator_info,
        curator_vaults=curator_vaults,
        curator_error=curator_error,
        curator_open=curator_open,
        current_vault=current_vault,
        current_vault_error=current_vault_error,
        vault_composition=vault_composition,
    )


@app.route("/proxy/morpho")
def proxy_morpho_vault():
    network = request.args.get("network")
    address = request.args.get("address")
    if not network or not address:
        abort(400, description="Parametres network et address requis.")

    upstream_url = f"{MORPHO_SITE_URL}{network}/vault/{address}"
    try:
        upstream = requests.get(upstream_url, timeout=30)
        upstream.raise_for_status()
    except requests.RequestException as exc:  # pragma: no cover
        abort(502, description=f"Impossible de charger la page Morpho: {exc}")

    html = _prepare_morpho_html(upstream.text)

    return Response(
        html,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "text/html"),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
