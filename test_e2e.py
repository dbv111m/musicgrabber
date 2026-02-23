"""
E2E Tests for MusicGrabber Telegram Bot

Tests the full flow from search to file sending.
Requires bot token and test chat ID.
"""

import asyncio
import os
import sys
import httpx
from pathlib import Path

# Test configuration
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8618035807:AAHYJEJUg-qAQvH5ZFjSvOw7CzGO8wTVFAw")
TEST_CHAT_ID = os.getenv("TEST_CHAT_ID")  # Your chat ID for testing
API_URL = "http://localhost:8080"


class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def add_pass(self, test_name):
        self.passed += 1
        print(f"✅ {test_name}")

    def add_fail(self, test_name, error):
        self.failed += 1
        self.errors.append((test_name, error))
        print(f"❌ {test_name}: {error}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*50}")
        print(f"Tests: {self.passed}/{total} passed")
        if self.failed > 0:
            print(f"\nFailed tests:")
            for name, error in self.errors:
                print(f"  - {name}: {error}")
        print(f"{'='*50}")
        return self.failed == 0


results = TestResults()


async def test_api_health():
    """Test that the API is running"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{API_URL}/api/config")
            if response.status_code == 200:
                results.add_pass("API Health Check")
                return True
            else:
                results.add_fail("API Health Check", f"Status {response.status_code}")
                return False
    except Exception as e:
        results.add_fail("API Health Check", str(e))
        return False


async def test_search_music():
    """Test music search functionality"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{API_URL}/api/search",
                json={"query": "Arctic Monkeys", "source": "all", "limit": 5}
            )
            if response.status_code != 200:
                results.add_fail("Search Music", f"Status {response.status_code}")
                return False

            data = response.json()
            search_results = data.get("results", [])
            if len(search_results) > 0:
                results.add_pass("Search Music")
                return True
            else:
                results.add_fail("Search Music", "No results found")
                return False
    except Exception as e:
        results.add_fail("Search Music", str(e))
        return False


async def test_check_existing_file():
    """Test checking if a file exists in library"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Check for a file that should exist (if you've downloaded DJ Snark before)
            response = await client.get(
                f"{API_URL}/api/check-file",
                params={"artist": "DJ Snark", "title": "Это вайб пати, детка"}
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("exists"):
                    results.add_pass("Check Existing File (found)")
                    return True
                else:
                    results.add_pass("Check Existing File (not found)")
                    return True
            else:
                results.add_fail("Check Existing File", f"Status {response.status_code}")
                return False
    except Exception as e:
        results.add_fail("Check Existing File", str(e))
        return False


async def test_download_queue():
    """Test that download endpoint works (doesn't actually download)"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Try to queue a download
            response = await client.post(
                f"{API_URL}/api/download",
                json={
                    "video_id": "test_video_id",
                    "title": "Test Track",
                    "artist": "Test Artist",
                    "source": "youtube",
                    "convert_to_flac": False
                }
            )

            # We expect either 200 (queued) or 400 (invalid video_id)
            # Both are acceptable for this test
            if response.status_code in [200, 400]:
                results.add_pass("Download Queue Endpoint")
                return True
            else:
                results.add_fail("Download Queue Endpoint", f"Status {response.status_code}")
                return False
    except Exception as e:
        results.add_fail("Download Queue Endpoint", str(e))
        return False


async def test_telegram_bot_running():
    """Test that Telegram bot is running"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    bot_name = data.get("result", {}).get("username", "unknown")
                    results.add_pass(f"Telegram Bot Running (@{bot_name})")
                    return True
                else:
                    results.add_fail("Telegram Bot Running", "Bot not ok")
                    return False
            else:
                results.add_fail("Telegram Bot Running", f"Status {response.status_code}")
                return False
    except Exception as e:
        results.add_fail("Telegram Bot Running", str(e))
        return False


async def send_test_message_to_chat():
    """Send a test message to verify bot can send messages"""
    if not TEST_CHAT_ID:
        print("⚠️ Skipping send message test (TEST_CHAT_ID not set)")
        return True

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TEST_CHAT_ID,
                    "text": "🧪 MusicGrabber E2E Test\n\nTesting bot functionality..."
                }
            )

            if response.status_code == 200:
                results.add_pass("Send Test Message")
                return True
            else:
                results.add_fail("Send Test Message", f"Status {response.status_code}")
                return False
    except Exception as e:
        results.add_fail("Send Test Message", str(e))
        return False


async def run_all_tests():
    """Run all e2e tests"""
    print("🧪 MusicGrabber E2E Tests")
    print(f"{'='*50}\n")

    # API tests
    print("Testing API...")
    await test_api_health()
    await test_search_music()
    await test_check_existing_file()
    await test_download_queue()

    print()

    # Telegram bot tests
    print("Testing Telegram Bot...")
    await test_telegram_bot_running()
    await send_test_message_to_chat()

    print()

    # Summary
    success = results.summary()
    return success


if __name__ == "__main__":
    print("\n⚠️ Make sure MusicGrabber is running: docker compose up -d")
    print("⚠️ Set TEST_CHAT_ID env var to test message sending")
    print()

    try:
        success = asyncio.run(run_all_tests())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️ Tests interrupted")
        sys.exit(1)
