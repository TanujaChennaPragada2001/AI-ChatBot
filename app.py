from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import subprocess
import boto3
from datetime import datetime

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# ---------------- AWS Setup ----------------
# DynamoDB table for chat history
dynamodb = boto3.resource('dynamodb', region_name="ap-south-1")
table_name = "ChatHistory"
table = dynamodb.Table(table_name)

# CloudWatch logs setup
logs_client = boto3.client('logs', region_name="ap-south-1")
log_group = "/ai-chatbot/logs"
log_stream = "chatbot-stream"

# Ensure CloudWatch log group and stream exist
def setup_cloudwatch():
    try:
        logs_client.create_log_group(logGroupName=log_group)
    except logs_client.exceptions.ResourceAlreadyExistsException:
        pass
    try:
        logs_client.create_log_stream(logGroupName=log_group, logStreamName=log_stream)
    except logs_client.exceptions.ResourceAlreadyExistsException:
        pass

setup_cloudwatch()
sequence_token = None

def log_to_cloudwatch(message):
    global sequence_token
    try:
        event = {
            'logGroupName': log_group,
            'logStreamName': log_stream,
            'logEvents': [{
                'timestamp': int(datetime.utcnow().timestamp() * 1000),
                'message': message
            }]
        }
        if sequence_token:
            event['sequenceToken'] = sequence_token
        response = logs_client.put_log_events(**event)
        sequence_token = response['nextSequenceToken']
    except Exception as e:
        print(f"CloudWatch logging error: {e}")

# ---------------- Routes ----------------

@app.route('/')
def home():
    return render_template('index.html')


@app.route('/chat', methods=['POST'])
def chat():
    try:
        user_message = request.json.get("message", "").strip()
        user_id = request.json.get("user_id", "Tanuja")  # default user

        if not user_message:
            return jsonify({"response": "Please enter a message."}), 400

        # Load last 5 messages from DynamoDB
        history = []
        try:
            response = table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key('user_id').eq(user_id),
                Limit=10,
                ScanIndexForward=True
            )
            items = response.get("Items", [])
            for item in items[-5:]:
                history.append({"role": "user", "content": item["user_message"]})
                history.append({"role": "bot", "content": item["bot_reply"]})
        except Exception as e:
            print(f"DynamoDB query error: {e}")

        # ---------- PROMPT FOR BULLET POINTS ----------
        context = "\n".join([f"{m['role']}: {m['content']}" for m in history])
        full_prompt = f"{context}\nuser: {user_message}\nbot (respond in English using bullet points, start each point with '-'):"
        # ------------------------------------------------

        # Call Ollama
        try:
            result = subprocess.run(
                ["ollama", "run", "llama3.2:1b", full_prompt],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                return jsonify({"response": f"Error in Ollama: {result.stderr}"}), 500
            bot_reply = result.stdout.strip()
        except Exception as e:
            return jsonify({"response": f"Subprocess error: {str(e)}"}), 500

        # Save message to DynamoDB
        try:
            table.put_item(Item={
                "user_id": user_id,
                "timestamp": int(datetime.utcnow().timestamp() * 1000),
                "user_message": user_message,
                "bot_reply": bot_reply
            })
        except Exception as e:
            print(f"DynamoDB put_item error: {e}")

        # Log to CloudWatch
        log_to_cloudwatch(f"USER: {user_message} | BOT: {bot_reply}")

        return jsonify({"response": bot_reply})

    except Exception as e:
        print(f"Unhandled error: {e}")
        return jsonify({"response": f"Error: {str(e)}"}), 500


@app.route('/history', methods=['GET'])
def history():
    try:
        user_id = request.args.get("user_id", "Tanuja")
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('user_id').eq(user_id),
            Limit=10,
            ScanIndexForward=True
        )
        items = response.get("Items", [])
        return jsonify(items)
    except Exception as e:
        print(f"History fetch error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)

