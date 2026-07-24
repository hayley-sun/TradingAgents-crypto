#!/usr/bin/env python3
"""
TradingAgents Web Application Launcher
"""

import sys
import os
from pathlib import Path

# Add current directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from web_app import app, socketio
    print("✅ Successfully imported TradingAgents web application")
except ImportError as e:
    print(f"❌ Failed to import required modules: {e}")
    print("Please make sure all dependencies are installed:")
    print("pip install -r requirements_web.txt")
    sys.exit(1)

if __name__ == '__main__':
    print("🚀 Starting TradingAgents Web Application...")
    print("📊 Dashboard will be available at: http://localhost:5000")
    print("🔄 Real-time analysis updates via WebSocket")
    print("📱 Responsive design for desktop and mobile")
    print("=" * 50)
    
    try:
        socketio.run(app, debug=False, host='0.0.0.0', port=5000)
    except KeyboardInterrupt:
        print("\n👋 TradingAgents Web Application stopped by user")
    except Exception as e:
        print(f"❌ Error starting web application: {e}")
        sys.exit(1) 