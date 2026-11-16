import unittest
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

        evaluate_exposure.assert_called_once_with(signal)
        cancel_order.assert_not_called()
        place_order.assert_not_called()
        save_last_processed_id.assert_not_called()


class TelegramSessionTests(unittest.TestCase):
    def test_invalid_session_string_has_actionable_error(self):
        with patch.object(btc_auto_trader, "TG_SESSION_STRING", "not-a-session"):
            with self.assertRaisesRegex(ValueError, "TG_SESSION_STRING is invalid"):
                btc_auto_trader.asyncio.run(btc_auto_trader.fetch_latest_btc_signal())


if __name__ == "__main__":
    unittest.main()
