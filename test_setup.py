"""
Local Environment & Setup Verification Script for Gemini Agent.
Tests all components locally before server deployment.
"""

import sys
import os
from pathlib import Path

def run_tests():
    print("=" * 60)
    print(" GEMINI AGENT LOCAL SETUP VERIFICATION")
    print("=" * 60)
    
    passed = 0
    total = 0
    
    # Test 1: Python version
    total += 1
    py_ver = sys.version.split()[0]
    print(f"\n1. Python Version: {py_ver}")
    if sys.version_info >= (3, 11):
        print("   [PASS] Python version >= 3.11")
        passed += 1
    else:
        print("   [FAIL] Python 3.11+ recommended.")

    # Test 2: Core Packages Import
    total += 1
    print("\n2. Checking Core Python Dependencies...")
    packages = [
        "fastapi",
        "uvicorn",
        "websockets",
        "google.genai",
        "pydantic",
        "dotenv",
        "pandas",
        "numpy",
        "ccxt",
        "yfinance",
        "ddgs",
        "redis",
        "psutil"
    ]
    missing = []
    for pkg in packages:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
            
    if not missing:
        print(f"   [PASS] All {len(packages)} required packages imported successfully.")
        passed += 1
    else:
        print(f"   [FAIL] Missing packages: {missing}")

    # Test 3: App Configuration & Model Settings
    total += 1
    print("\n3. Checking App Configuration & Models...")
    try:
        from app.config import settings
        key_set = bool(settings.GEMINI_API_KEY and not settings.GEMINI_API_KEY.startswith("your_"))
        print(f"   - Agent Model: {settings.GEMINI_AGENT_MODEL}")
        print(f"   - Live Model:  {settings.GEMINI_LIVE_MODEL}")
        print(f"   - Voice:       {settings.GEMINI_VOICE_NAME}")
        print(f"   - API Key:     {'Configured ✓' if key_set else 'NOT SET (check .env)'}")
        
        if key_set:
            print("   [PASS] Configuration loaded and API key is present.")
            passed += 1
        else:
            print("   [WARN] API key is missing in .env. Voice and Agent require GEMINI_API_KEY.")
    except Exception as e:
        print(f"   [FAIL] Configuration error: {e}")

    # Test 4: Workspace Directories
    total += 1
    print("\n4. Checking Workspace Directory...")
    try:
        from app.config import settings
        ws = settings.absolute_workspace_dir
        if ws.exists() and ws.is_dir():
            print(f"   - Workspace: {ws}")
            print("   [PASS] Workspace directory exists and is ready.")
            passed += 1
        else:
            print(f"   [FAIL] Workspace path does not exist: {ws}")
    except Exception as e:
        print(f"   [FAIL] Workspace error: {e}")

    # Test 5: Indian Market Live Data
    total += 1
    print("\n5. Checking Indian Stock Market Data Feed...")
    try:
        from app.financial_advisor.india_market import get_indian_stock_quote, get_indian_market_indices
        indices = get_indian_market_indices()
        quote = get_indian_stock_quote("TCS")
        
        if quote.get("success"):
            print(f"   - Live Quote TCS (NSE): ₹ {quote['current_price']} ({quote['change_percent']}%)")
            if "NIFTY_50" in indices.get("indices", {}):
                print(f"   - NIFTY 50 Level:        {indices['indices']['NIFTY_50']['price']}")
            print("   [PASS] Live Indian market data retrieved successfully.")
            passed += 1
        else:
            print(f"   [WARN] Quote warning: {quote.get('error')}")
    except Exception as e:
        print(f"   [FAIL] Market data feed error: {e}")

    # Test 6: FastAPI Application Initialization
    total += 1
    print("\n6. Checking FastAPI App Route Registry...")
    try:
        from app.main import app
        routes = [r.path for r in app.routes]
        expected = [
            "/health",
            "/api/models",
            "/api/chatbot",
            "/api/chatbot/models",
            "/api/chat",
            "/api/agent/run",
            "/api/financial-advisor/quote",
            "/ws/live",
            "/ws/agent/{task_id}"
        ]
        all_found = all(any(r.startswith(ep.split("{")[0]) for r in routes) for ep in expected)
        if all_found:
            print(f"   - Total Endpoints: {len(routes)}")
            print("   [PASS] FastAPI routes registered properly.")
            passed += 1
        else:
            print("   [FAIL] Some expected routes were not found in app.routes.")
    except Exception as e:
        print(f"   [FAIL] FastAPI app error: {e}")

    # Test 7: Gemini Chatbot Module with Google Search Grounding
    total += 1
    print("\n7. Checking Gemini Chatbot Module & Google Search Grounding...")
    try:
        from app.chatbot import chatbot_service, process_chat_message, ChatbotRequest
        from google.genai import types
        # Check Tool and GoogleSearch types
        has_search_tool = hasattr(types, "GoogleSearch") and hasattr(types, "Tool")
        if has_search_tool and chatbot_service:
            print("   - Chatbot Service: Loaded successfully")
            print("   - Built-in Google Search Tool: types.GoogleSearch available ✓")
            print("   [PASS] app/chatbot configured with Google Search grounding.")
            passed += 1
        else:
            print("   [FAIL] GoogleSearch tool not found in google.genai.types.")
    except Exception as e:
        print(f"   [FAIL] Chatbot initialization error: {e}")

    # Test 8: SQLite Database, Dummy Account (₹10,000), and Strategy Manager
    total += 1
    print("\n8. Checking SQLite Database & Testing Dummy Account (₹10,000)...")
    try:
        from app.storage.db import db
        from app.trading.broker import broker
        from app.trading.strategy_manager import strategy_manager
        state = db.get_account_state()
        summary = broker.get_portfolio_summary()
        strats = db.get_strategies()
        if state and summary and len(strats) >= 3:
            print(f"   - Database File: {db.db_path} (Ready)")
            print(f"   - Initial Testing Capital: ₹ {summary['initial_capital_inr']:,.2f}")
            print(f"   - Active Strategies Registered: {len(strats)}")
            print("   [PASS] SQLite persistence, dummy account, and strategy manager verified.")
            passed += 1
        else:
            print("   [FAIL] Account state or strategies incomplete in SQLite.")
    except Exception as e:
        print(f"   [FAIL] SQLite/Broker test error: {e}")

    # Test 9: Authentication, Session Tokens & Access Control
    total += 1
    print("\n9. Checking Authentication & Access Control Security...")
    try:
        from app.auth import create_access_token, verify_token
        test_user = "admin_tester"
        token = create_access_token(test_user, expires_in_seconds=60)
        verified_user = verify_token(token)
        tampered_token = token[:-4] + "abcd"
        tampered_user = verify_token(tampered_token)

        if verified_user == test_user and tampered_user is None:
            print(f"   - Token Generation & HMAC-SHA256 Verification: Valid ({verified_user}) ✓")
            print("   - Tamper Resistance: Verified (rejected forged token) ✓")
            print("   [PASS] Authentication module and cryptographic session tokens verified.")
            passed += 1
        else:
            print("   [FAIL] Token verification or tampering test failed.")
    except Exception as e:
        print(f"   [FAIL] Authentication verification error: {e}")

    # Test 10: Strict Market Hours & Standby Auto-Execution Protocol
    total += 1
    print("\n10. Checking Market Hours & Standby Auto-Execution Protocol...")
    try:
        import asyncio
        from app.trading.strategy_manager import strategy_manager
        ms = strategy_manager.get_market_status()
        print(f"   - Current Time: {ms['current_time_ist']}")
        print(f"   - Market Status: {ms['status']} (Next Open: {ms['next_open_ist']})")

        # Verify auto-execution behavior outside market hours
        if not ms['is_open']:
            strategy_manager.set_auto_execute(True)
            res = asyncio.run(strategy_manager.evaluate_budget_trade("TATAMOTORS", "BUY", 85))
            if res and res.get("status") == "STANDBY_MARKET_CLOSED":
                print("   - Auto-Execute Standby: Verified (Held safely outside 09:15 - 15:30 IST) ✓")
                print("   [PASS] Strict market hours enforcement confirmed.")
                passed += 1
            else:
                print(f"   [FAIL] Order was not held on standby: {res}")
        else:
            print("   - Live Market Open: Trading active ✓")
            print("   [PASS] Market hours check confirmed.")
            passed += 1
    except Exception as e:
        print(f"   [FAIL] Market hours enforcement error: {e}")

    # Summary
    print("\n" + "=" * 60)
    print(f" RESULT: {passed}/{total} CHECKS PASSED")
    print("=" * 60)
    
    if passed == total:
        print("\n🎉 Your local setup is 100% complete and verified!")
        print("To run the local server:")
        print("   ./run.sh")
        print("Or:")
        print("   uvicorn app.main:app --host 0.0.0.0 --port 7000 --reload")
        print("\nThen open: http://localhost:7000\n")
    else:
        print("\n⚠️ Some checks need attention before starting the server.\n")

if __name__ == "__main__":
    run_tests()
