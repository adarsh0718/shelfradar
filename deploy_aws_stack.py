"""
deploy_aws_stack.py - Automated AWS Cloud Provisioning for ShelfRadar
Provisions DynamoDB, IAM Role, Bedrock-enabled Lambda, and API Gateway WebSockets.

Usage:
  python deploy_aws_stack.py --region us-east-1
"""

import os
import sys
import time
import json
import zipfile
import io
import argparse

def install_boto3():
    try:
        import boto3
    except ImportError:
        print("[Setup] Installing boto3...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "boto3"])
        import boto3
    return boto3

def main():
    parser = argparse.ArgumentParser(description="Provision ShelfRadar AWS Serverless Stack")
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"), help="AWS Region (default: us-east-1)")
    parser.add_argument("--access-key", default=os.environ.get("AWS_ACCESS_KEY_ID"), help="AWS Access Key ID")
    parser.add_argument("--secret-key", default=os.environ.get("AWS_SECRET_ACCESS_KEY"), help="AWS Secret Access Key")
    parser.add_argument("--session-token", default=os.environ.get("AWS_SESSION_TOKEN"), help="AWS Session Token (optional)")
    args = parser.parse_args()

    boto3 = install_boto3()

    session_kwargs = {"region_name": args.region}
    if args.access_key and args.secret_key:
        session_kwargs["aws_access_key_id"] = args.access_key
        session_kwargs["aws_secret_access_key"] = args.secret_key
        if args.session_token:
            session_kwargs["aws_session_token"] = args.session_token

    session = boto3.Session(**session_kwargs)

    # 0. Test credentials
    sts = session.client("sts")
    try:
        caller = sts.get_caller_identity()
        account_id = caller["Account"]
        print(f"[AWS Auth] Connected as Account: {account_id} | ARN: {caller['Arn']}", flush=True)
    except Exception as e:
        print(f"\n[ERROR] Could not authenticate with AWS: {e}", flush=True)
        print("Please ensure AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_DEFAULT_REGION are set.", flush=True)
        sys.exit(1)

    region = args.region

    # =========================================================================
    # STEP 1: DynamoDB Table (ShelfRadar)
    # =========================================================================
    print("\n--- STEP 1: Creating DynamoDB Table (ShelfRadar) ---", flush=True)
    dynamodb = session.client("dynamodb")
    table_name = "ShelfRadar"
    try:
        dynamodb.describe_table(TableName=table_name)
        print(f"  [OK] Table '{table_name}' already exists.")
    except dynamodb.exceptions.ResourceNotFoundException:
        print(f"  Creating table '{table_name}' (PK: String, SK: String, On-Demand)...")
        dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"}
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST"
        )
        print("  Waiting for DynamoDB table to become ACTIVE...")
        waiter = dynamodb.get_waiter("table_exists")
        waiter.wait(TableName=table_name)
        print("  [OK] DynamoDB table is ACTIVE.")

    # =========================================================================
    # STEP 2: IAM Role (ShelfRadarExecutionRole)
    # =========================================================================
    print("\n--- STEP 2: Creating IAM Role (ShelfRadarExecutionRole) ---")
    iam = session.client("iam")
    role_name = "ShelfRadarExecutionRole"
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }

    try:
        role_resp = iam.get_role(RoleName=role_name)
        role_arn = role_resp["Role"]["Arn"]
        print(f"  [OK] IAM Role '{role_name}' already exists: {role_arn}")
    except iam.exceptions.NoSuchEntityException:
        print(f"  Creating IAM Role '{role_name}'...")
        role_resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Execution role for ShelfRadar serverless backend"
        )
        role_arn = role_resp["Role"]["Arn"]
        print(f"  [OK] Created IAM Role: {role_arn}")

    # Attach required policies
    policies = [
        "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
        "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
        "arn:aws:iam::aws:policy/AmazonAPIGatewayInvokeFullAccess",
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    ]
    for pol in policies:
        try:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=pol)
        except Exception as e:
            print(f"  Policy attach note ({pol.split('/')[-1]}): {e}")

    print("  Waiting 10 seconds for IAM role propagation...")
    time.sleep(10)

    # =========================================================================
    # STEP 3: Lambda Function (ShelfRadarBackend)
    # =========================================================================
    print("\n--- STEP 3: Creating Lambda Function (ShelfRadarBackend) ---")
    lambda_client = session.client("lambda")
    fn_name = "ShelfRadarBackend"

    # Read lambda_function.py and package into a zip in memory
    src_path = os.path.join(os.path.dirname(__file__), "lambda_function.py")
    if not os.path.exists(src_path):
        print(f"  [ERROR] {src_path} not found!")
        sys.exit(1)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src_path, arcname="lambda_function.py")
    zip_bytes = zip_buffer.getvalue()

    lambda_env = {
        "DYNAMODB_TABLE": table_name,
        "BEDROCK_MODEL_ID": "us.anthropic.claude-3-haiku-20240307-v1:0"
    }

    try:
        lambda_client.get_function(FunctionName=fn_name)
        print(f"  Updating code for existing function '{fn_name}'...")
        lambda_client.update_function_code(FunctionName=fn_name, ZipFile=zip_bytes)
        time.sleep(3)
        lambda_client.update_function_configuration(
            FunctionName=fn_name,
            Environment={"Variables": lambda_env},
            Timeout=15,
            MemorySize=256
        )
        fn_arn = f"arn:aws:lambda:{region}:{account_id}:function:{fn_name}"
        print(f"  [OK] Lambda function updated: {fn_arn}")
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"  Creating function '{fn_name}' (Python 3.12)...")
        fn_resp = lambda_client.create_function(
            FunctionName=fn_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="lambda_function.handler",
            Code={"ZipFile": zip_bytes},
            Description="ShelfRadar Bedrock AI router & WebSocket fan-out backend",
            Timeout=15,
            MemorySize=256,
            Environment={"Variables": lambda_env}
        )
        fn_arn = fn_resp["FunctionArn"]
        print(f"  [OK] Created Lambda function: {fn_arn}")

    # =========================================================================
    # STEP 4: Amazon API Gateway (WebSocket API: ShelfRadarWebSockets)
    # =========================================================================
    print("\n--- STEP 4: Creating API Gateway WebSocket (ShelfRadarWebSockets) ---")
    apigw = session.client("apigatewayv2")
    api_name = "ShelfRadarWebSockets"

    # Check if API exists
    apis = apigw.get_apis().get("Items", [])
    target_api = next((a for a in apis if a["Name"] == api_name), None)

    if target_api:
        api_id = target_api["ApiId"]
        print(f"  [OK] Found existing API Gateway: {api_name} (ID: {api_id})")
    else:
        print(f"  Creating WebSocket API '{api_name}'...")
        api_resp = apigw.create_api(
            Name=api_name,
            ProtocolType="WEBSOCKET",
            RouteSelectionExpression="$request.body.action",
            Description="ShelfRadar real-time distress signals and shopkeeper pings"
        )
        api_id = api_resp["ApiId"]
        print(f"  [OK] Created WebSocket API: {api_id}")

    # Integration URI for Lambda
    integ_uri = f"arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/{fn_arn}/invocations"

    # Create integration
    integs = apigw.get_integrations(ApiId=api_id).get("Items", [])
    if integs:
        integ_id = integs[0]["IntegrationId"]
    else:
        integ_resp = apigw.create_integration(
            ApiId=api_id,
            IntegrationType="AWS_PROXY",
            IntegrationUri=integ_uri
        )
        integ_id = integ_resp["IntegrationId"]
        print(f"  [OK] Created Lambda Proxy Integration: {integ_id}")

    # Create routes: $connect, $disconnect, broadcast
    target_str = f"integrations/{integ_id}"
    existing_routes = {r["RouteKey"]: r["RouteId"] for r in apigw.get_routes(ApiId=api_id).get("Items", [])}

    for rkey in ["$connect", "$disconnect", "broadcast"]:
        if rkey not in existing_routes:
            apigw.create_route(
                ApiId=api_id,
                RouteKey=rkey,
                Target=target_str
            )
            print(f"  [OK] Created route: {rkey}")

    # Grant API Gateway permission to invoke Lambda
    try:
        lambda_client.add_permission(
            FunctionName=fn_name,
            StatementId=f"apigw-invoke-{api_id}",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{region}:{account_id}:{api_id}/*"
        )
        print("  [OK] Added Lambda invocation permission for API Gateway.")
    except lambda_client.exceptions.ResourceConflictException:
        pass

    # Create Deployment and 'dev' stage
    dep_resp = apigw.create_deployment(ApiId=api_id, Description="Automated deployment")
    dep_id = dep_resp["DeploymentId"]

    try:
        apigw.create_stage(
            ApiId=api_id,
            StageName="dev",
            DeploymentId=dep_id,
            AutoDeploy=True,
            Description="Development stage"
        )
        print("  [OK] Created stage 'dev'.")
    except apigw.exceptions.ConflictException:
        apigw.update_stage(ApiId=api_id, StageName="dev", DeploymentId=dep_id)
        print("  [OK] Updated stage 'dev'.")

    # Final URLs
    ws_url = f"wss://{api_id}.execute-api.{region}.amazonaws.com/dev"
    conn_url = f"https://{api_id}.execute-api.{region}.amazonaws.com/dev"

    print("\n" + "=" * 65)
    print("  >>> SHELFRADAR AWS SERVERLESS STACK DEPLOYED! <<<")
    print(f"  WebSocket URL  (wss://)  : {ws_url}")
    print(f"  Connection URL (https://): {conn_url}")
    print("=" * 65 + "\n")

    # Update Lambda with the callback URL
    lambda_env["API_GATEWAY_ENDPOINT"] = conn_url
    lambda_client.update_function_configuration(
        FunctionName=fn_name,
        Environment={"Variables": lambda_env}
    )
    print("  [OK] Configured Lambda with API Gateway Connection URL.")

    # Update frontend awsBackend.js
    aws_backend_path = os.path.join(os.path.dirname(__file__), "src", "services", "awsBackend.js")
    if os.path.exists(aws_backend_path):
        with open(aws_backend_path, "r", encoding="utf-8") as f:
            content = f.read()
        import re
        content = re.sub(r"WEB_SOCKET_URL:\s*'[^']*'", f"WEB_SOCKET_URL: '{ws_url}'", content)
        content = re.sub(r"AWS_REGION:\s*'[^']*'", f"AWS_REGION: '{region}'", content)
        content = re.sub(r"IS_SIMULATION_MODE:\s*true", "IS_SIMULATION_MODE: false", content)
        with open(aws_backend_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("  [OK] Injected live WebSocket URL into src/services/awsBackend.js!")

if __name__ == "__main__":
    main()
