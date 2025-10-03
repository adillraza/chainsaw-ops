#!/usr/bin/env python3
"""
Simple webhook server for automated deployments.
Run this on the server to listen for GitHub webhook events.
"""
from flask import Flask, request, jsonify
import hmac
import hashlib
import subprocess
import os

app = Flask(__name__)

# Set this in your .env file or as environment variable
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'your-webhook-secret-change-this')
DEPLOY_SCRIPT = '/opt/chainsaw-ops/deploy.sh'

def verify_signature(payload, signature):
    """Verify GitHub webhook signature"""
    if not signature:
        return False
    
    sha_name, signature = signature.split('=')
    if sha_name != 'sha256':
        return False
    
    mac = hmac.new(
        WEBHOOK_SECRET.encode(),
        msg=payload,
        digestmod=hashlib.sha256
    )
    
    return hmac.compare_digest(mac.hexdigest(), signature)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle GitHub webhook"""
    signature = request.headers.get('X-Hub-Signature-256')
    
    # Verify signature
    if not verify_signature(request.data, signature):
        return jsonify({'error': 'Invalid signature'}), 401
    
    # Get event type
    event = request.headers.get('X-GitHub-Event')
    
    # Only deploy on push events to main branch
    if event == 'push':
        payload = request.json
        if payload.get('ref') == 'refs/heads/main':
            try:
                # Run deployment script
                result = subprocess.run(
                    [DEPLOY_SCRIPT],
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                
                return jsonify({
                    'status': 'success',
                    'message': 'Deployment triggered',
                    'output': result.stdout
                }), 200
            except Exception as e:
                return jsonify({
                    'status': 'error',
                    'message': str(e)
                }), 500
    
    return jsonify({'status': 'ignored', 'event': event}), 200

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy'}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=False)

