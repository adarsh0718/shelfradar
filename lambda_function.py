"""
ShelfRadarBackend - AWS Serverless Lambda Handler
Integrates Amazon DynamoDB, Amazon Bedrock (Anthropic Claude 3 Haiku), and API Gateway WebSockets.

Runtime: Python 3.12
Environment Variables:
  - DYNAMODB_TABLE: Table name (default: 'ShelfRadar')
  - BEDROCK_MODEL_ID: Model ID (default: 'us.anthropic.claude-3-haiku-20240307-v1:0')
  - API_GATEWAY_ENDPOINT: Callback URL (https://...) for post_to_connection
"""

import os
import json
import time
import uuid
import boto3
from botocore.exceptions import ClientError

# Initialize AWS SDK Clients
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "ShelfRadar")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-3-haiku-20240307-v1:0")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
bedrock_runtime = boto3.client("bedrock-runtime")


def handler(event, context):
    """Main routing handler for API Gateway WebSocket events."""
    request_context = event.get("requestContext", {})
    route_key = request_context.get("routeKey")
    connection_id = request_context.get("connectionId")
    domain_name = request_context.get("domainName")
    stage = request_context.get("stage", "dev")

    # Construct API Gateway Management endpoint for sending WebSocket messages
    callback_url = os.environ.get(
        "API_GATEWAY_ENDPOINT",
        f"https://{domain_name}/{stage}" if domain_name else ""
    )

    print(f"[ShelfRadar] Route: {route_key} | ConnectionId: {connection_id}")

    try:
        # Route 1: $connect
        if route_key == "$connect":
            return handle_connect(connection_id)

        # Route 2: $disconnect
        elif route_key == "$disconnect":
            return handle_disconnect(connection_id)

        # Route 3: Custom routes (broadcast, claim_lead, submit_quote, create_order, verify_otp)
        body = {}
        if "body" in event and event["body"]:
            try:
                body = json.loads(event["body"])
            except Exception:
                body = {"raw": event["body"]}

        action = body.get("action", route_key)

        if action in ("broadcast", "sendping"):
            return handle_broadcast(body, connection_id, callback_url)
        elif action in ("claim_lead", "claimlead"):
            return handle_claim_lead(body, connection_id, callback_url)
        elif action in ("submit_quote", "quote"):
            return handle_submit_quote(body, connection_id, callback_url)
        elif action in ("create_order", "createorder"):
            return handle_create_order(body, connection_id, callback_url)
        elif action in ("verify_otp", "verifyotp"):
            return handle_verify_otp(body, connection_id, callback_url)
        else:
            # Fallback: echo / generic handler
            return {"statusCode": 200, "body": json.dumps({"status": "received", "action": action})}

    except Exception as e:
        print(f"[ShelfRadar ERROR] {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def handle_connect(connection_id):
    """Registers active WebSocket connection in DynamoDB."""
    table.put_item(
        Item={
            "PK": f"CONN#{connection_id}",
            "SK": "META",
            "connectionId": connection_id,
            "connectedAt": int(time.time()),
            "ttl": int(time.time()) + (24 * 3600)  # Auto-expire after 24h
        }
    )
    return {"statusCode": 200, "body": "Connected"}


def handle_disconnect(connection_id):
    """Removes disconnected WebSocket client from DynamoDB."""
    table.delete_item(
        Key={
            "PK": f"CONN#{connection_id}",
            "SK": "META"
        }
    )
    return {"statusCode": 200, "body": "Disconnected"}


from decimal import Decimal

def convert_floats(obj):
    if isinstance(obj, list):
        return [convert_floats(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: convert_floats(v) for k, v in obj.items()}
    elif isinstance(obj, float):
        return Decimal(str(obj))
    return obj

def handle_broadcast(body, connection_id, callback_url):
    """
    Categorizes unstructured distress ping using Amazon Bedrock Claude 3 Haiku,
    saves the ping to DynamoDB, and broadcasts to all active connected shopkeepers.
    """
    ping_data = body.get("ping", body.get("data", body))
    query_text = ping_data.get("query", ping_data.get("text", ping_data.get("item", "")))
    ping_id = ping_data.get("pingId", str(uuid.uuid4())[:8])

    # 1. AI Categorization via Bedrock (Claude 3 Haiku)
    ai_analysis = analyze_with_bedrock(query_text)

    # 2. Persist Ping to DynamoDB
    item_record = {
        "PK": f"PING#{ping_id}",
        "SK": "DATA",
        "pingId": ping_id,
        "query": query_text,
        "category": ai_analysis.get("category", "General Essentials"),
        "urgency": ai_analysis.get("urgency", "MEDIUM"),
        "detectedItems": ai_analysis.get("detected_items", [query_text]),
        "suggestedPrice": ai_analysis.get("suggested_price", "₹50 - ₹150"),
        "customerLocation": ping_data.get("location", {"lat": "17.6868", "lng": "83.2185", "address": "Dwaraka Nagar, Vizag"}),
        "radiusKm": ping_data.get("radiusKm", 1),
        "senderConnectionId": connection_id,
        "createdAt": int(time.time()),
        "status": "ACTIVE"
    }
    table.put_item(Item=convert_floats(item_record))

    # 3. Fan-out broadcast to all active connections
    broadcast_payload = {
        "type": "NEW_PING",
        "ping": item_record,
        "aiAnalysis": ai_analysis
    }
    fan_out(broadcast_payload, callback_url, exclude_conn=connection_id)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "broadcasted",
            "pingId": ping_id,
            "aiAnalysis": ai_analysis
        })
    }


def handle_claim_lead(body, connection_id, callback_url):
    """Shopkeeper claims a ping / sends an instant acceptance."""
    claim = body.get("claim", {})
    ping_id = claim.get("pingId")

    fan_out({
        "type": "CLAIM_PING",
        "claim": claim
    }, callback_url)

    return {"statusCode": 200, "body": json.dumps({"status": "claimed"})}


def handle_submit_quote(body, connection_id, callback_url):
    """Shopkeeper submits a price and delivery estimate quote."""
    quote = body.get("quote", {})
    ping_id = quote.get("pingId")
    shop_id = quote.get("shopId", "shop_1")

    # Persist quote to DynamoDB
    table.put_item(
        Item=convert_floats({
            "PK": f"PING#{ping_id}",
            "SK": f"QUOTE#{shop_id}",
            "quote": quote,
            "submittedAt": int(time.time())
        })
    )

    fan_out({
        "type": "NEW_QUOTE",
        "quote": quote
    }, callback_url)

    return {"statusCode": 200, "body": json.dumps({"status": "quote_submitted"})}


def handle_create_order(body, connection_id, callback_url):
    """Customer confirms order and escrow payment."""
    order = body.get("order", {})
    order_id = order.get("orderId", str(uuid.uuid4())[:8])

    table.put_item(
        Item=convert_floats({
            "PK": f"ORDER#{order_id}",
            "SK": "DETAILS",
            "order": order,
            "orderId": order_id,
            "createdAt": int(time.time())
        })
    )

    fan_out({
        "type": "ORDER_CREATED",
        "order": order
    }, callback_url)

    return {"statusCode": 200, "body": json.dumps({"status": "order_created", "orderId": order_id})}


def handle_verify_otp(body, connection_id, callback_url):
    """Delivery partner verifies customer OTP and releases escrow payout."""
    order_id = body.get("orderId")
    otp = str(body.get("otp", "")).strip()

    resp = table.get_item(Key={"PK": f"ORDER#{order_id}", "SK": "DETAILS"})
    item = resp.get("Item")

    if item and str(item.get("order", {}).get("deliveryOtp", "")).strip() == otp:
        item["order"]["deliveryStatus"] = "DELIVERED"
        table.put_item(Item=item)

        fan_out({
            "type": "ORDER_STATUS_UPDATE",
            "orderId": order_id,
            "deliveryStatus": "DELIVERED"
        }, callback_url)

        return {"statusCode": 200, "body": json.dumps({"status": "verified", "orderId": order_id})}

    return {"statusCode": 400, "body": json.dumps({"error": "Invalid delivery OTP"})}


def analyze_with_bedrock(text):
    """Invokes Amazon Bedrock Anthropic Claude 3 Haiku for request classification."""
    if not text:
        return {
            "category": "General Essentials",
            "urgency": "MEDIUM",
            "detected_items": [],
            "suggested_price": "₹50 - ₹100"
        }

    prompt = f"""Human: You are the AI Router for ShelfRadar, an instant hyperlocal distress and essentials network in Visakhapatnam, India.
Analyze this distress request from a customer:
"{text}"

Categorize it strictly into one of these 4 departments:
1. "Stationery & Office"
2. "Snacks & Munchies"
3. "Juices & Beverages"
4. "Electronics & Hardware"

Respond ONLY with valid JSON matching this exact structure:
{{
  "category": "Stationery & Office",
  "urgency": "HIGH",
  "detected_items": ["item 1"],
  "suggested_price": "₹100"
}}

Assistant:"""

    try:
        response = bedrock_runtime.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 200,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1
            })
        )
        response_body = json.loads(response["body"].read())
        content = response_body.get("content", [{}])[0].get("text", "{}")
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)
    except Exception as e:
        print(f"[Bedrock Fallback] Failed: {e}")
        t_lower = text.lower()
        if any(w in t_lower for w in ["pen", "paper", "a4", "notebook", "stapler", "marker", "pencil"]):
            cat = "Stationery & Office"
        elif any(w in t_lower for w in ["chips", "snack", "kurkure", "maggi", "lays", "biscuit", "cookie"]):
            cat = "Snacks & Munchies"
        elif any(w in t_lower for w in ["juice", "red bull", "water", "drink", "coffee", "milk", "beverage"]):
            cat = "Juices & Beverages"
        else:
            cat = "Electronics & Hardware"

        return {
            "category": cat,
            "urgency": "HIGH" if any(w in t_lower for w in ["urgent", "emergency", "fast", "asap"]) else "MEDIUM",
            "detected_items": [text],
            "suggested_price": "₹100 - ₹200"
        }


def fan_out(payload, callback_url, exclude_conn=None):
    """Sends payload to all active WebSocket connections."""
    if not callback_url:
        print("[Fan-out] No callback URL provided.")
        return

    try:
        resp = table.scan(
            ProjectionExpression="connectionId",
            FilterExpression="begins_with(PK, :prefix) AND SK = :meta",
            ExpressionAttributeValues={
                ":prefix": "CONN#",
                ":meta": "META"
            }
        )
        connections = resp.get("Items", [])
    except Exception as e:
        print(f"[Scan Error] {e}")
        return

    apigw = boto3.client("apigatewaymanagementapi", endpoint_url=callback_url)
    data_bytes = json.dumps(payload).encode("utf-8")

    stale = []
    for conn in connections:
        cid = conn.get("connectionId")
        if not cid or cid == exclude_conn:
            continue
        try:
            apigw.post_to_connection(ConnectionId=cid, Data=data_bytes)
        except ClientError as ce:
            if ce.response.get("Error", {}).get("Code") == "GoneException":
                stale.append(cid)
        except Exception as ex:
            print(f"[Post Error] {cid}: {ex}")

    for dead_cid in stale:
        try:
            table.delete_item(Key={"PK": f"CONN#{dead_cid}", "SK": "META"})
        except Exception:
            pass
