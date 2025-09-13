import os
import time
import logging
from datetime import datetime
from flask import Flask, jsonify, request, Blueprint
from flask_cors import CORS
import requests
import json
import threading
from typing import Optional


# Application factory (simple instantiation for this project)
app = Flask(__name__)
CORS(app)
app.url_map.strict_slashes = False


# Basic logging to stdout
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("POSPOS_API_SALE")


# Log every outgoing response (status and body) for debugging
@app.after_request
def log_response(response):
    try:
        content_type = response.headers.get("Content-Type", "")
        try:
            body_text = response.get_data(as_text=True)
        except Exception:
            body_text = "<unreadable>"
        if body_text and len(body_text) > 4000:
            body_text = body_text[:4000] + "...(truncated)"
        app.logger.info(
            "Response %s %s -> %s | Content-Type=%s | Body=%s",
            request.method,
            request.full_path if request.query_string else request.path,
            response.status,
            content_type,
            body_text,
        )
    except Exception as exc:  # pragma: no cover
        app.logger.warning("Failed to log response: %s", exc)
    return response


# Simulated processing delay for all routes (1 second)
@app.before_request
def add_processing_delay():
    time.sleep(1)


# In-memory state to emulate order lifecycle and passthrough
order_amount = 0.0
is_cancelled = False
last_order_created_at = 0.0
cashin_ack_received = False
last_cashin_received_baht = 0.0

# Upstream base API (configurable)
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://192.168.1.33:5000")

# Global HTTP timeout (seconds) applied to all outbound API calls.
# Default is 300 seconds; configurable via HTTP_TIMEOUT_SECONDS environment variable.
# Infinite/None timeouts are not allowed.
def _resolve_global_http_timeout():
    try:
        raw = os.getenv("HTTP_TIMEOUT_SECONDS", "300")
        val = float(str(raw).strip())
        # Coerce invalid/zero/negative to default 300
        return 300.0 if val <= 0 else val
    except Exception:
        return 300.0

HTTP_TIMEOUT_SECONDS = _resolve_global_http_timeout()


def _load_generic_template(filename: str) -> dict:
    template_path = os.path.join(
        os.path.dirname(__file__), "templates", "generic", filename
    )
    with open(template_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_inserted_amount_from_latest(latest: dict) -> float:
    """Extract inserted amount (THB) from REST_API_CI /socket/latest payload."""
    try:
        if not isinstance(latest, dict):
            return 0.0
        value = latest.get("inserted_amount_baht")
        if value is None:
            parsed = latest.get("parsed")
            if isinstance(parsed, dict):
                value = parsed.get("inserted_amount_baht")
        return float(value) if value is not None else 0.0
    except Exception:
        return 0.0


@app.get("/")
def health_check():
    port = int(os.getenv("PORT", "5215"))
    return jsonify(
        {
            "message": "POSPOS_API_SALE is running (Flask)",
            "port": port,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    )


api_v1 = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def _safe_first_value(container, key):
    try:
        v = container.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            v = v.get("value", v)
        return v
    except Exception:
        return None


def _map_inventory_response(raw: dict) -> list:
    """Map REST_API_CI /inventory payload to generic get-inventory-success.json data array.

    Rules:
      - Use entries from Body[0].InventoryResponse[0].Cash where type=="3" as inStacker counts
      - Use entries from Body[0].InventoryResponse[0].Cash where type=="4" as qty counts
      - Denomination.fv is in minor units (satang). denom/value = fv/100
      - type = 1 if fv >= 2000 (>= 20 THB, banknotes), else 2 (coins)
      - Sum counts per denomination across lists
    """
    try:
        if not isinstance(raw, dict):
            return []
        body = raw.get("Body")
        if isinstance(body, list) and body:
            inv_list = body[0].get("InventoryResponse") if isinstance(body[0], dict) else None
        else:
            inv_list = None
        inv = inv_list[0] if isinstance(inv_list, list) and inv_list else None
        cash_list = inv.get("Cash") if isinstance(inv, dict) else None
        if not isinstance(cash_list, list):
            return []

        denom_map = {}

        def ensure_num(x, default=0):
            try:
                if x is None:
                    return default
                if isinstance(x, (int, float)):
                    return x
                s = str(x)
                return float(s) if ("." in s) else int(s)
            except Exception:
                return default

        for cash in cash_list:
            if not isinstance(cash, dict):
                continue
            cash_type = str(cash.get("type")) if cash.get("type") is not None else None
            denoms = cash.get("Denomination")
            denoms = denoms if isinstance(denoms, list) else ([denoms] if isinstance(denoms, dict) else [])
            for dn in denoms:
                if not isinstance(dn, dict):
                    continue
                fv_minor = ensure_num(dn.get("fv"), default=None)
                if fv_minor is None:
                    continue
                key = str(fv_minor)
                piece_val = ensure_num(_safe_first_value(dn, "Piece"), 0)
                # initialize entry
                if key not in denom_map:
                    value_baht = float(fv_minor) / 100.0
                    # cast to int if whole number
                    value_field = int(value_baht) if abs(value_baht - int(value_baht)) < 1e-9 else value_baht
                    denom_map[key] = {
                        "denom": f"{value_baht:.2f}",
                        "value": value_field,
                        "qty": 0,
                        "inStacker": 0,
                        "type": 1 if fv_minor >= 2000 else 2,
                    }
                if cash_type == "3":
                    # type 3 = changeable (dispensable)
                    denom_map[key]["qty"] += int(piece_val)
                elif cash_type == "4":
                    # type 4 = total in stacker
                    denom_map[key]["inStacker"] += int(piece_val)

        # sort by value desc to keep output stable
        items = list(denom_map.values())
        items.sort(key=lambda x: (float(x.get("value", 0))), reverse=True)
        return items
    except Exception:
        return []


def _extract_cashin_amount_baht(raw: dict) -> float:
    """Extract accepted cash amount (THB) from REST_API_CI /cashin response.

    Expected path (from logs):
      response.change_response.Body[0].ChangeResponse[0].Amount[0].value
    The value is satang; divide by 100 to get THB.
    """
    try:
        if not isinstance(raw, dict):
            return 0.0
        response = raw.get("response")
        if not isinstance(response, dict):
            return 0.0
        change_response_root = response.get("change_response")
        if not isinstance(change_response_root, dict):
            return 0.0
        body_list = change_response_root.get("Body")
        if not isinstance(body_list, list) or not body_list:
            return 0.0
        body0 = body_list[0] if isinstance(body_list[0], dict) else None
        if not body0:
            return 0.0
        change_resp_list = body0.get("ChangeResponse")
        if not isinstance(change_resp_list, list) or not change_resp_list:
            return 0.0
        change0 = change_resp_list[0] if isinstance(change_resp_list[0], dict) else None
        if not change0:
            return 0.0

        # Preferred: compute from Cash[].Denomination[]. (fv in satang) * sum(Piece[].value)
        total_satang = 0.0
        cash_list = change0.get("Cash")
        if isinstance(cash_list, list) and cash_list:
            for cash in cash_list:
                if not isinstance(cash, dict):
                    continue
                # Only accept Cash of type "1" (per requirement/example)
                cash_type = cash.get("type")
                if cash_type is not None and str(cash_type) != "1":
                    continue
                denoms = cash.get("Denomination")
                # normalize to list
                if isinstance(denoms, dict):
                    denoms = [denoms]
                if not isinstance(denoms, list):
                    continue
                for dn in denoms:
                    if not isinstance(dn, dict):
                        continue
                    fv_str = dn.get("fv")
                    try:
                        fv_minor = float(fv_str) if fv_str is not None and "." in str(fv_str) else int(str(fv_str))
                    except Exception:
                        continue
                    piece_items = dn.get("Piece")
                    piece_sum = 0
                    if isinstance(piece_items, list):
                        for p in piece_items:
                            if isinstance(p, dict):
                                try:
                                    pv = p.get("value")
                                    piece_sum += int(str(pv)) if pv is not None else 0
                                except Exception:
                                    continue
                    elif isinstance(piece_items, dict):
                        try:
                            pv = piece_items.get("value")
                            piece_sum += int(str(pv)) if pv is not None else 0
                        except Exception:
                            piece_sum += 0
                    # accumulate
                    total_satang += float(fv_minor) * float(piece_sum)

        if total_satang > 0:
            return float(total_satang) / 100.0

        # Fallback: Amount[0].value (satang)
        amount_list = change0.get("Amount")
        if isinstance(amount_list, list) and amount_list:
            amount0 = amount_list[0]
            raw_val = amount0.get("value") if isinstance(amount0, dict) else None
            if raw_val is not None:
                s = str(raw_val)
                try:
                    satang = float(s) if "." in s else int(s)
                    return float(satang) / 100.0
                except Exception:
                    return 0.0
        return 0.0
    except Exception:
        return 0.0


def _call_upstream_cashin(amount_value: float) -> None:
    global cashin_ack_received, last_cashin_received_baht
    try:
        url = f"{UPSTREAM_BASE}/cashin"
        payload = {"amount": amount_value}
        start_ts = time.time()
        logger.info("Calling upstream /cashin url=%s payload=%s", url, payload)
        resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
        duration_ms = (time.time() - start_ts) * 1000.0
        content_type = resp.headers.get("Content-Type", "")
        try:
            body_text = resp.text
            if body_text and len(body_text) > 2000:
                body_text = body_text[:2000] + "...(truncated)"
        except Exception:
            body_text = "<unreadable>"

        logger.info(
            "Upstream /cashin responded status=%s duration_ms=%.1f content_type=%s body=%s",
            resp.status_code,
            duration_ms,
            content_type,
            body_text,
        )

        if resp.ok:
            # Parse accepted amount from response JSON if possible
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = None
            if isinstance(resp_json, dict):
                amount_baht = _extract_cashin_amount_baht(resp_json)
                if amount_baht > 0:
                    last_cashin_received_baht = amount_baht
                    logger.info("Parsed cashin amount from upstream response: %s THB", amount_baht)
            cashin_ack_received = True
        else:
            logger.warning("Upstream /cashin non-OK status: %s", resp.status_code)
    except Exception as exc:
        logger.warning("Upstream /cashin failed (async): %s", exc)


def _submit_cashin_async(amount_value: float) -> None:
    t = threading.Thread(target=_call_upstream_cashin, args=(amount_value,), daemon=True)
    t.start()


@api_v1.get("/balances")
def get_balances():
    try:
        # Call upstream inventory and map to generic shape
        shaped = _load_generic_template("get-inventory-success.json")
        data_items = []
        try:
            resp = requests.get(f"{UPSTREAM_BASE}/inventory", timeout=HTTP_TIMEOUT_SECONDS)
            if resp.ok:
                upstream = resp.json()
                data_items = _map_inventory_response(upstream)
        except Exception as exc:
            logger.warning("Upstream /inventory failed: %s", exc)

        if data_items:
            shaped["data"] = data_items
        return jsonify(shaped)
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to load balances via REST_API_CI")
        return jsonify({"success": False, "error": "Failed to load inventory data", "message": str(exc)}), 500


@api_v1.post("/order")
def create_order():
    global order_amount, is_cancelled, last_order_created_at, cashin_ack_received, last_cashin_received_baht
    try:
        is_cancelled = False
        cashin_ack_received = False
        last_cashin_received_baht = 0.0

        payload = request.get_json(silent=True) or {}
        amount_value = payload.get("amount", 0)
        try:
            order_amount = float(amount_value)
        except (TypeError, ValueError):
            order_amount = 0.0

        # mark order start time
        last_order_created_at = time.time()

        # Submit upstream /cashin asynchronously and return immediately
        _submit_cashin_async(order_amount)

        # Load generic template and respond as processing
        response = _load_generic_template("create-sale-success.json")
        response["data"]["amount"] = int(order_amount)
        response["data"]["status"] = "processing"
        return jsonify(response), 200
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to create sale")
        return jsonify({"success": False, "error": "Failed to create sale", "message": str(exc)}), 500


@api_v1.get("/status")
def get_status():
    global order_amount, is_cancelled, cashin_ack_received, last_cashin_received_baht
    try:
        # Fetch latest snapshot via HTTP only
        latest_payload = None
        try:
            resp = requests.get(f"{UPSTREAM_BASE}/socket/latest", timeout=HTTP_TIMEOUT_SECONDS)
            if resp.ok:
                latest_payload = resp.json()
        except Exception as exc:
            logger.warning("Upstream /socket/latest failed: %s", exc)

        # Load generic status template
        shaped = _load_generic_template("get-by-id-success.json")
        inserted_amount = _get_inserted_amount_from_latest(latest_payload)
        # amount should be the original order amount from /order, not from socket
        shaped["data"]["amount"] = int(order_amount)
        # cashin should reflect accepted amount from /cashin response; fallback to socket/latest
        cashin_value = int(last_cashin_received_baht) if last_cashin_received_baht else int(inserted_amount)
        shaped["data"]["cashin"] = cashin_value

        if is_cancelled:
            shaped["data"]["status"] = "cancelled"
            shaped["data"]["cashin"] = 0
            # clear state after reporting cancelled
            is_cancelled = False
            cashin_ack_received = False
            last_cashin_received_baht = 0.0
        else:
            if cashin_ack_received:
                shaped["data"]["status"] = "succeeded"
                # one-shot success, clear after reporting
                cashin_ack_received = False
            else:
                shaped["data"]["status"] = "processing"

        return jsonify(shaped)
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get status")
        return jsonify({"success": False, "error": "Failed to load status", "message": str(exc)}), 500


@api_v1.patch("/cancel/<string:sale_id>")
@api_v1.patch("/cancel", defaults={"sale_id": None})
def cancel_order(sale_id: Optional[str] = None):
    global is_cancelled
    try:
        # Call upstream cancel (side-effect only)
        try:
            _ = requests.get(f"{UPSTREAM_BASE}/cashin_cancel", timeout=HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("Upstream /cashin_cancel failed: %s", exc)

        is_cancelled = True
        response = _load_generic_template("cancel-sale-success.json")
        return jsonify(response)
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to cancel sale")
        return jsonify({"success": False, "error": "Failed to cancel sale", "message": str(exc)}), 500


# Mount API blueprint (after all routes are defined)
app.register_blueprint(api_v1)

