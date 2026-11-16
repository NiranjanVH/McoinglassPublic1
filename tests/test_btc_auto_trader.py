import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import btc_auto_trader


class DryRunTests(unittest.TestCase):
    @patch.object(btc_auto_trader, "place_btc_limit_order")
    @patch.object(btc_auto_trader, "cancel_order_by_id")
    @patch.object(btc_auto_trader, "save_last_processed_id")
    @patch.object(
        btc_auto_trader,
        "evaluate_btc_exposure",
        return_value=("MODIFY", "updated limit", {"id": "existing-order"}),
    )
    @patch.object(btc_auto_trader, "verify_exit_ip", return_value="127.0.0.1")
    @patch.object(btc_auto_trader, "get_last_processed_id", return_value=None)
    @patch.object(btc_auto_trader, "validate_config")
    @patch.object(
        btc_auto_trader,
        "fetch_latest_btc_signal",
        new_callable=AsyncMock,
    )
    def test_dry_run_does_not_place_or_save_order(
        self,
        fetch_signal,
        validate_config,
        get_last_processed_id,
        verify_exit_ip,
        evaluate_exposure,
        save_last_processed_id,
        cancel_order,
        place_order,
    ):
        signal = {
            "message_id": 42,
            "direction": "LONG",
            "range_low": 64000.0,
            "range_high": 64500.0,
            "entry": 64500.0,
            "tp1": 64800.0,
            "probability": "N/A",
            "confidence": "N/A",
        }

        fetch_signal.return_value = signal
        tunnel = patch.object(btc_auto_trader, "OracleSshTunnel")
        tunnel_mock = tunnel.start()
        tunnel_mock.return_value.__enter__.return_value = tunnel_mock.return_value
        tunnel_mock.return_value.__exit__.return_value = False
        self.addCleanup(tunnel.stop)

        with patch.object(btc_auto_trader, "DRY_RUN", True):
            btc_auto_trader.run_auto_trader()

        evaluate_exposure.assert_not_called()
        cancel_order.assert_not_called()
        place_order.assert_not_called()
        save_last_processed_id.assert_not_called()


class TelegramSessionTests(unittest.TestCase):
    def test_invalid_session_string_has_actionable_error(self):
        with patch.object(btc_auto_trader, "TG_SESSION_STRING", "not-a-session"):
            with self.assertRaisesRegex(ValueError, "TG_SESSION_STRING is invalid"):
                btc_auto_trader.asyncio.run(btc_auto_trader.fetch_latest_btc_signal())


class SignalConfirmationTests(unittest.TestCase):
    def test_returns_latest_signal_when_two_directions_match(self):
        latest = {"direction": "SHORT", "message_id": 12}
        previous = {"direction": "SHORT", "message_id": 10}

        self.assertIs(
            btc_auto_trader.confirm_signal_direction([latest, previous]),
            latest,
        )

    def test_rejects_signals_when_directions_disagree(self):
        latest = {"direction": "LONG", "message_id": 12}
        previous = {"direction": "SHORT", "message_id": 10}

        self.assertIsNone(
            btc_auto_trader.confirm_signal_direction([latest, previous])
        )

    def test_rejects_a_single_signal_without_confirmation(self):
        self.assertIsNone(
            btc_auto_trader.confirm_signal_direction(
                [{"direction": "LONG", "message_id": 12}]
            )
        )

    def test_fetch_compares_latest_two_btc_signals_and_uses_newest(self):
        messages = [
            SimpleNamespace(
                id=12,
                date="today",
                text="BITCOIN\nDirection: SHORT\nEntry Range: $64,450-$64,600\nLimit: $64,500",
            ),
            SimpleNamespace(id=11, date="today", text="ETHEREUM signal"),
            SimpleNamespace(
                id=10,
                date="yesterday",
                text="BTC\nDirection: SHORT\nEntry Range: $64,400-$64,550\nLimit: $64,450",
            ),
        ]

        class FakeTelegramClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def iter_messages(self, chat):
                for message in messages:
                    yield message

        with patch.object(
            btc_auto_trader,
            "TelegramClient",
            return_value=FakeTelegramClient(),
        ):
            signal = btc_auto_trader.asyncio.run(
                btc_auto_trader.fetch_latest_btc_signal()
            )

        self.assertEqual(signal["message_id"], 12)
        self.assertEqual(signal["direction"], "SHORT")


class ConfigurationTests(unittest.TestCase):
    def test_live_trading_rejects_disabled_host_verification(self):
        with (
            patch.object(btc_auto_trader, "DRY_RUN", False),
            patch.object(btc_auto_trader, "SSH_STRICT_HOST_KEY_CHECKING", False),
        ):
            with self.assertRaisesRegex(RuntimeError, "Live trading requires SSH host-key verification"):
                btc_auto_trader.validate_config()


if __name__ == "__main__":
    unittest.main()
