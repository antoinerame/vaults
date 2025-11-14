import requests
from datetime import datetime, timezone
from typing import List, Tuple, Dict, Any, Optional

MORPHO_API_URL = "https://api.morpho.org/graphql"

MORPHO_SITE_URL = "https://app.morpho.org/"

networks = [
    {"id": 1, "network": "ethereum"},
    {"id": 8453, "network": "base"},
    {"id": 57073, "network": "ink"},
    {"id": 137, "network": "polygon"},
    {"id": 130, "network": "unichain"},
    {"id": 10, "network": "optimism"},
    {"id": 747474, "network": "katana"},
    {"id": 42161, "network": "arbitrum"},
    {"id": 239, "network": "tac"},
    {"id": 999, "network": "hyperliquid"},
]


def iso_date_to_unix_timestamp(date_str: str) -> int:
    """
    Convert an ISO-like date string (e.g. '2024-10-01' or '2024-10-01 12:00')
    to a Unix timestamp (seconds since epoch, UTC).
    """
    # Try full datetime with time first
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            # Assume UTC (API works with UTC timestamps)
            dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue

    raise ValueError(f"Unsupported date format: '{date_str}'")


def run_graphql_query(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a GraphQL query against the Morpho API and return the 'data' field.
    Raises an error if HTTP error or GraphQL error occurs.
    """
    response = requests.post(
        MORPHO_API_URL,
        json={"query": query, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")

    return payload.get("data", {})


def fetch_share_price_usd_series(
    vault_address: str,
    chain_id: int,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> List[Tuple[int, float]]:
    """
    Fetch sharePriceUsd time series for a given vault between start_ts and end_ts.

    Returns a list of (timestamp, share_price_usd) tuples sorted by timestamp.
    Uses the 'historicalState.sharePriceUsd(options: TimeseriesOptions)' field.
    """

    query = """
    query VaultSharePriceHistory(
      $address: String!,
      $chainId: Int!,
      $options: TimeseriesOptions
    ) {
      vaultByAddress(address: $address, chainId: $chainId) {
        address
        name
        asset {
          symbol
          decimals
        }
        historicalState {
          sharePriceUsd(options: $options) {
            x
            y
          }
        }
      }
    }
    """

    options: Dict[str, int] = {}
    if start_ts is not None:
        options["startTimestamp"] = start_ts
    if end_ts is not None:
        options["endTimestamp"] = end_ts

    variables: Dict[str, Any] = {
        "address": vault_address,
        "chainId": chain_id,
        "options": options or None,
    }

    data = run_graphql_query(query, variables)

    vault_data = data.get("vaultByAddress")
    if vault_data is None:
        raise ValueError("Vault not found for given address / chainId.")

    hist = vault_data.get("historicalState")
    if hist is None or hist.get("sharePriceUsd") is None:
        raise ValueError("No historical sharePriceUsd data available for this vault.")

    series_raw = hist["sharePriceUsd"]

    # series_raw is expected to be a list of { "x": timestamp, "y": value }
    series: List[Tuple[int, float]] = []
    for point in series_raw:
        ts = int(point["x"])
        value = float(point["y"])
        series.append((ts, value))

    # Sort by timestamp, just in case
    series.sort(key=lambda p: p[0])
    return series


def pick_start_end_points(
    series: List[Tuple[int, float]],
    start_ts: int,
    end_ts: int,
) -> Tuple[Tuple[int, float], Tuple[int, float]]:
    """
    From a time series of (timestamp, value), pick:
      - the first point with timestamp >= start_ts (or earliest if none),
      - the last point with timestamp <= end_ts (or latest if none).

    Returns (start_point, end_point), each a (timestamp, value) tuple.
    """
    if not series:
        raise ValueError("Empty time series, cannot pick start/end points.")

    # Ensure sorted
    series = sorted(series, key=lambda p: p[0])

    # Pick start
    start_point = None
    for ts, val in series:
        if ts >= start_ts:
            start_point = (ts, val)
            break
    if start_point is None:
        # If no point >= start_ts, use earliest
        start_point = series[0]

    # Pick end
    end_point = None
    for ts, val in reversed(series):
        if ts <= end_ts:
            end_point = (ts, val)
            break
    if end_point is None:
        # If no point <= end_ts, use latest
        end_point = series[-1]

    if start_point[0] > end_point[0]:
        raise ValueError("Inconsistent series: start point is after end point.")

    return start_point, end_point


def compute_pnl_from_prices(
    start_price: float,
    end_price: float,
) -> float:
    """
    Compute P&L given start and end prices:
      PnL = end_price / start_price - 1
    Returns a decimal
    """
    if start_price <= 0:
        raise ValueError("Start price must be positive.")
    return (end_price / start_price) - 1.0


def compute_vault_pnl_between_dates(
    vault_address: str,
    chain_id: int,
    start_date_str: str,
    end_date_str: str,
) -> Dict[str, Any]:
    """
      1. Convert date strings to timestamps.
      2. Fetch sharePriceUsd time series for the vault.
      3. Pick start/end points.
      4. Compute P&L between the two dates.

    Returns a dict with useful info:
      {
        "vault_address": str,
        "chain_id": int,
        "start_timestamp": int,
        "end_timestamp": int,
        "start_price_usd": float,
        "end_price_usd": float,
        "pnl_decimal": float,
      }
    """
    start_ts = iso_date_to_unix_timestamp(start_date_str)
    end_ts = iso_date_to_unix_timestamp(end_date_str)

    if end_ts <= start_ts:
        raise ValueError("end_date must be strictly after start_date.")

    series = fetch_share_price_usd_series(
        vault_address=vault_address,
        chain_id=chain_id,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    (ts_start, price_start), (ts_end, price_end) = pick_start_end_points(
        series,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    pnl_decimal = compute_pnl_from_prices(price_start, price_end)

    return {
        "vault_address": vault_address,
        "chain_id": chain_id,
        "start_timestamp": f"{ts_start}, {datetime.fromtimestamp(ts_start):%Y-%m-%d}",
        "end_timestamp": f"{ts_end}, {datetime.fromtimestamp(ts_end):%Y-%m-%d}",
        "start_price_usd": price_start,
        "end_price_usd": price_end,
        "pnl_decimal": pnl_decimal,
    }


def get_network_by_id(id):
    for n in networks:
        if n.get("id") == id:
            return n.get("network")
    return None


def looks_like_address(value: str) -> bool:
    if not value:
        return False
    value = value.lower()
    return value.startswith("0x") and len(value) == 42 and all(
        c in "0123456789abcdef" for c in value[2:]
    )


def fetch_curator_by_id(curator_id: str) -> Optional[Dict[str, Any]]:
    query = """
    query CuratorById($curatorId: String!) {
      curator(id: $curatorId) {
        id
        name
        description
        verified
        addresses {
          chainId
          address
        }
      }
    }
    """
    data = run_graphql_query(query, {"curatorId": curator_id})
    return data.get("curator")


def fetch_curator_by_address(address: str) -> Optional[Dict[str, Any]]:
    query = """
    query CuratorByAddress($address: String!) {
      curators(where: { address_in: [$address] }, first: 1) {
        items {
          id
          name
          description
          verified
          addresses {
            chainId
            address
          }
        }
      }
    }
    """
    data = run_graphql_query(query, {"address": address})
    items = data.get("curators", {}).get("items") or []
    return items[0] if items else None


def fetch_curator_profile(curator_query: str) -> Optional[Dict[str, Any]]:
    """
    Try to resolve a curator either by its slug/id (e.g. '9summits')
    or by one of its known addresses.
    """
    if not curator_query:
        return None

    normalized = curator_query.strip()

    curator = None
    if not looks_like_address(normalized):
        curator = fetch_curator_by_id(normalized)

    if curator is None:
        curator = fetch_curator_by_address(normalized)

    return curator


def fetch_vaults_for_curator(
    curator_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    if not curator_id:
        return []

    query = """
    query CuratorVaults($curatorId: String!, $first: Int!) {
      vaults(first: $first, where: { curator_in: [$curatorId] }) {
        items {
          id
          name
          address
          whitelisted
          chain {
            id
          }
          asset {
            symbol
          }
          state {
            totalAssetsUsd
          }
        }
      }
    }
    """

    data = run_graphql_query(query, {"curatorId": curator_id, "first": limit})
    return data.get("vaults", {}).get("items") or []


def fetch_vault_details(
    vault_address: str,
    chain_id: int,
) -> Optional[Dict[str, Any]]:
    query = """
    query VaultExtended($address: String!, $chainId: Int!) {
      vaultByAddress(address: $address, chainId: $chainId) {
        address
        name
        symbol
        whitelisted
        promoted
        metadata {
          description
          image
        }
        asset {
          symbol
          name
          decimals
        }
        chain {
          id
        }
        state {
          totalAssetsUsd
          totalAssets
          apy
          netApy
          netApyWithoutRewards
          fee
          sharePriceUsd
          curator
          feeRecipient
          guardian
          owner
          allocation {
            supplyAssetsUsd
            supplyCapUsd
            enabled
            market {
              uniqueKey
              loanAsset {
                symbol
              }
              collateralAsset {
                symbol
              }
            }
          }
        }
      }
    }
    """
    variables = {"address": vault_address, "chainId": chain_id}
    data = run_graphql_query(query, variables)
    return data.get("vaultByAddress")


if __name__ == "__main__":
    example_vault_address = "0xd63070114470f685b75B74D60EEc7c1113d33a3D"
    example_chain_id = 1

    example_start_date = "2025-11-06"
    example_end_date = "2025-11-13"

    result = compute_vault_pnl_between_dates(
        vault_address=example_vault_address,
        chain_id=example_chain_id,
        start_date_str=example_start_date,
        end_date_str=example_end_date,
    )

    print("Vault address:", result["vault_address"])
    print(
        "Chain ID:", f"{result['chain_id']} => {get_network_by_id(result['chain_id'])}"
    )
    print("Start timestamp:", result["start_timestamp"])
    print("End timestamp:", result["end_timestamp"])
    print("Start sharePriceUsd:", result["start_price_usd"])
    print("End sharePriceUsd:", result["end_price_usd"])
    print(f"PnL: {result['pnl_decimal'] * 100:.4f}%")
