from flask import Flask, render_template, request, jsonify
import datetime
import json
import time
from pathlib import Path
from typing import Dict, List, Optional
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'tradingagents_secret_key'

# Global storage for analysis sessions (simplified for serverless)
analysis_sessions = {}

class SimpleMessageBuffer:
    def __init__(self, session_id):
        self.session_id = session_id
        self.messages = []
        self.agent_status = {
            "Market Analyst": "pending",
            "Social Analyst": "pending", 
            "News Analyst": "pending",
            "Fundamentals Analyst": "pending",
            "Bull Researcher": "pending",
            "Bear Researcher": "pending",
            "Research Manager": "pending",
            "Trader": "pending",
            "Portfolio Manager": "pending",
        }
        self.report_sections = {
            "market_report": None,
            "sentiment_report": None,
            "news_report": None,
            "fundamentals_report": None,
            "investment_plan": None,
            "trader_investment_plan": None,
            "final_trade_decision": None,
        }
        self.current_step = "waiting"
        self.progress = 0

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        message = {"timestamp": timestamp, "type": message_type, "content": content}
        self.messages.append(message)

    def update_agent_status(self, agent, status):
        self.agent_status[agent] = status

    def update_report_section(self, section_name, content):
        if section_name in self.report_sections:
            self.report_sections[section_name] = content

    def update_progress(self, progress, step):
        self.progress = progress
        self.current_step = step

@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception as e:
        return jsonify({
            'error': 'Template not found',
            'details': str(e),
            'message': 'Please ensure templates folder is properly deployed'
        })

@app.route('/analysis')
def analysis_page():
    try:
        return render_template('analysis.html')
    except Exception as e:
        return jsonify({
            'error': 'Template not found', 
            'details': str(e),
            'message': 'Please ensure templates folder is properly deployed'
        })

@app.route('/api/start_analysis', methods=['POST'])
def start_analysis():
    try:
        data = request.json
        session_id = data.get('session_id', str(int(time.time())))
        
        # Create a simplified response for Vercel environment
        buffer = SimpleMessageBuffer(session_id)
        buffer.add_message("System", f"Analysis request received for {data.get('ticker', 'Unknown')}...")
        buffer.add_message("System", "Note: This is a demo version running on Vercel with limited functionality.")
        buffer.add_message("System", "For full analysis capabilities, please run the application locally.")
        
        # Simulate some progress
        buffer.update_progress(10, "Initializing...")
        buffer.update_agent_status("Market Analyst", "in_progress")
        buffer.update_progress(30, "Market analysis...")
        buffer.update_agent_status("Market Analyst", "completed")
        buffer.update_agent_status("Social Analyst", "in_progress")
        buffer.update_progress(50, "Social sentiment analysis...")
        buffer.update_agent_status("Social Analyst", "completed")
        buffer.update_progress(100, "Demo completed")
        
        # Add demo reports
        buffer.update_report_section("market_report", "## Demo Market Analysis\n\nThis is a demonstration version running on Vercel. For real analysis, please deploy locally or use a persistent server environment.")
        buffer.update_report_section("final_trade_decision", "## Demo Decision\n\n**HOLD** - This is a demo response. Real trading analysis requires full system deployment.")
        
        analysis_sessions[session_id] = {
            'config': data,
            'buffer': buffer,
            'status': 'demo_completed'
        }
        
        return jsonify({
            'session_id': session_id, 
            'status': 'demo_completed',
            'message': 'Demo analysis completed. For full functionality, please run locally.'
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e),
            'message': 'Failed to process analysis request'
        }), 500

@app.route('/api/session/<session_id>/status', methods=['GET'])
def get_session_status(session_id):
    """Get the current status of an analysis session"""
    try:
        if session_id in analysis_sessions:
            session = analysis_sessions[session_id]
            return jsonify({
                'session_id': session_id,
                'status': session['status'],
                'messages': session['buffer'].messages,
                'agent_status': session['buffer'].agent_status,
                'report_sections': session['buffer'].report_sections,
                'progress': session['buffer'].progress,
                'current_step': session['buffer'].current_step
            })
        else:
            return jsonify({'error': 'Session not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health_check():
    return jsonify({
        'status': 'healthy', 
        'timestamp': datetime.datetime.now().isoformat(),
        'environment': 'vercel',
        'mode': 'demo'
    })

@app.route('/api/info')
def api_info():
    return jsonify({
        'name': 'Trading Agents Crypto - Vercel Demo',
        'version': '1.0.0-vercel',
        'description': 'Simplified demo version for Vercel deployment',
        'limitations': [
            'No real-time analysis',
            'No persistent storage', 
            'Demo responses only',
            'Limited to 5-minute execution time'
        ],
        'recommendation': 'For full functionality, deploy to a persistent server environment'
    })

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found', 'message': 'The requested resource was not found'}), 404

@app.errorhandler(500)  
def internal_error(error):
    return jsonify({'error': 'Internal server error', 'message': 'An unexpected error occurred'}), 500

if __name__ == '__main__':
    app.run(debug=True) 